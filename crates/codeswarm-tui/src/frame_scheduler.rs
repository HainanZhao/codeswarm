//! Latest-state terminal output scheduling.
//!
//! Terminal writes are ordered side effects: dropping an incremental diff can
//! invalidate every later diff. This scheduler therefore drops stale deltas
//! and requires the caller to submit a complete repaint before deltas resume.

use std::time::{Duration, Instant};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Frame {
    pub bytes: Vec<u8>,
    pub complete: bool,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct FrameScheduler {
    in_flight: bool,
    pending: Option<Frame>,
    resync_required: bool,
    repaint_requested: bool,
}

#[derive(Clone, Debug)]
pub struct ResizeCoalescer {
    pending: Option<(u16, u16)>,
    last_event: Option<Instant>,
    settle_after: Duration,
}

impl ResizeCoalescer {
    pub fn new(settle_after: Duration) -> Self {
        Self {
            pending: None,
            last_event: None,
            settle_after,
        }
    }

    pub fn push(&mut self, width: u16, height: u16, now: Instant) {
        self.pending = Some((width, height));
        self.last_event = Some(now);
    }

    pub fn take_settled(&mut self, now: Instant) -> Option<(u16, u16)> {
        let last_event = self.last_event?;
        if now.duration_since(last_event) < self.settle_after {
            return None;
        }
        self.last_event = None;
        self.pending.take()
    }
}

impl FrameScheduler {
    pub fn submit_delta(&mut self, bytes: impl Into<Vec<u8>>) -> bool {
        if self.resync_required || self.in_flight || self.pending.is_some() {
            self.resync_required = true;
            if !self.repaint_requested {
                self.repaint_requested = true;
            }
            return false;
        }
        self.pending = Some(Frame {
            bytes: bytes.into(),
            complete: false,
        });
        true
    }

    pub fn submit_complete(&mut self, bytes: impl Into<Vec<u8>>) -> bool {
        self.pending = Some(Frame {
            bytes: bytes.into(),
            complete: true,
        });
        self.resync_required = false;
        self.repaint_requested = false;
        true
    }

    pub fn take_next(&mut self) -> Option<Frame> {
        if self.in_flight {
            return None;
        }
        let frame = self.pending.take()?;
        self.in_flight = true;
        Some(frame)
    }

    pub fn finish_write(&mut self) {
        self.in_flight = false;
    }

    pub fn needs_repaint(&self) -> bool {
        self.repaint_requested
    }

    pub fn has_in_flight_write(&self) -> bool {
        self.in_flight
    }

    pub fn has_pending_frame(&self) -> bool {
        self.pending.is_some()
    }
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, Instant};

    use super::{FrameScheduler, ResizeCoalescer};

    #[test]
    fn stale_deltas_are_dropped_until_a_complete_repaint() {
        let mut scheduler = FrameScheduler::default();
        assert!(scheduler.submit_delta("first"));
        let first = scheduler.take_next().expect("first frame");
        assert_eq!(first.bytes, b"first");
        assert!(!scheduler.submit_delta("stale-1"));
        assert!(!scheduler.submit_delta("stale-2"));
        assert!(scheduler.needs_repaint());
        scheduler.finish_write();
        assert!(scheduler.take_next().is_none());
        assert!(scheduler.submit_complete("complete"));
        assert!(!scheduler.needs_repaint());
        assert_eq!(scheduler.take_next().expect("repaint").bytes, b"complete");
    }

    #[test]
    fn only_one_frame_can_be_in_flight() {
        let mut scheduler = FrameScheduler::default();
        assert!(scheduler.submit_delta("frame"));
        assert!(scheduler.take_next().is_some());
        assert!(scheduler.take_next().is_none());
        assert!(scheduler.has_in_flight_write());
    }

    #[test]
    fn resize_coalescer_emits_only_final_geometry() {
        let start = Instant::now();
        let mut resize = ResizeCoalescer::new(Duration::from_millis(100));
        resize.push(80, 24, start);
        resize.push(90, 30, start + Duration::from_millis(50));
        assert_eq!(
            resize.take_settled(start + Duration::from_millis(149)),
            None
        );
        assert_eq!(
            resize.take_settled(start + Duration::from_millis(150)),
            Some((90, 30))
        );
    }
}
