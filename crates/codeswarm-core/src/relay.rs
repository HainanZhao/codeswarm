//! Deterministic sequential roster scheduling.
//!
//! This owns turn selection only. Prompt construction and adapter I/O remain
//! outside the scheduler, making the relay safe to replay and test.

use std::collections::VecDeque;

use crate::RosterSlot;
use crate::collaboration::CollaborationContext;

pub const MAX_QUEUED_PROMPTS: usize = 100;
pub const STOP_TOKEN: &str = "[CODESWARM:STOP]";
pub const DEFAULT_STOP_ACKNOWLEDGMENT: &str = "👍";

pub fn strip_stop_token(response: &str) -> (String, bool) {
    let trimmed = response.trim_end();
    let requested = trimmed.ends_with(STOP_TOKEN);
    let visible = if requested {
        trimmed[..trimmed.len() - STOP_TOKEN.len()]
            .trim_end()
            .to_owned()
    } else {
        response.to_owned()
    };
    (visible, requested)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum QueuedKind {
    Steering,
    Direct,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QueuedPrompt {
    pub slot: RosterSlot,
    pub prompt: String,
    pub kind: QueuedKind,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RelayDecision {
    Dispatch {
        slot: RosterSlot,
        prompt: String,
        direct: bool,
        can_stop: bool,
    },
    Paused,
    Collapsed,
    Complete,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Relay {
    active: Vec<bool>,
    max_rounds: usize,
    rounds: usize,
    paused: bool,
    last_active: RosterSlot,
    next: Option<RosterSlot>,
    steering: VecDeque<QueuedPrompt>,
    direct: VecDeque<QueuedPrompt>,
    previous_slot: Option<RosterSlot>,
    context: CollaborationContext,
}

impl Relay {
    pub fn new(roster_size: usize, max_rounds: usize) -> Self {
        assert!(roster_size >= 1);
        assert!(max_rounds >= 1);
        Self {
            active: vec![true; roster_size],
            max_rounds,
            rounds: 0,
            paused: false,
            last_active: 0,
            next: None,
            steering: VecDeque::new(),
            direct: VecDeque::new(),
            previous_slot: None,
            context: CollaborationContext::new(roster_size),
        }
    }

    pub fn active_slots(&self) -> impl Iterator<Item = RosterSlot> + '_ {
        self.active
            .iter()
            .enumerate()
            .filter_map(|(slot, active)| active.then_some(slot))
    }

    pub fn pause(&mut self) {
        self.paused = true;
    }

    pub fn resume(&mut self) {
        self.paused = false;
    }

    pub fn tombstone(&mut self, slot: RosterSlot) -> Result<(), &'static str> {
        let active = self.active.get_mut(slot).ok_or("slot out of range")?;
        *active = false;
        Ok(())
    }

    pub fn drop_agent(&mut self, slot: RosterSlot) -> Result<(), &'static str> {
        if slot == 0 {
            return Err("owner cannot be dropped");
        }
        self.tombstone(slot)?;
        self.direct.retain(|queued| queued.slot != slot);
        self.steering.retain(|queued| queued.slot != slot);
        Ok(())
    }

    pub fn enqueue_human(
        &mut self,
        prompt: impl Into<String>,
        selected: Option<RosterSlot>,
    ) -> bool {
        let prompt = prompt.into();
        if prompt.trim().is_empty() || self.queued_count() >= MAX_QUEUED_PROMPTS {
            return false;
        }
        let slot = selected.unwrap_or(self.last_active);
        if !self.active.get(slot).copied().unwrap_or(false) {
            return false;
        }
        self.steering.push_back(QueuedPrompt {
            slot,
            prompt,
            kind: QueuedKind::Steering,
        });
        true
    }

    pub fn enqueue_direct(
        &mut self,
        slot: RosterSlot,
        prompt: impl Into<String>,
    ) -> Result<bool, &'static str> {
        let prompt = prompt.into();
        if !self.active.get(slot).copied().unwrap_or(false) {
            return Err("direct target is not active");
        }
        if prompt.trim().is_empty() || self.queued_count() >= MAX_QUEUED_PROMPTS {
            return Ok(false);
        }
        self.direct.push_back(QueuedPrompt {
            slot,
            prompt,
            kind: QueuedKind::Direct,
        });
        Ok(true)
    }

    pub fn queued_count(&self) -> usize {
        self.direct.len() + self.steering.len()
    }

    pub fn set_shared_task(&mut self, task: impl Into<String>) {
        self.context.set_shared_task(task);
    }

    pub fn shared_task(&self) -> Option<&str> {
        self.context.shared_task()
    }

    pub fn record_public(&mut self, speaker: impl Into<String>, text: impl Into<String>) {
        self.context.record(speaker, text, &self.active);
    }

    pub fn mark_context_seen(&mut self, slot: RosterSlot) {
        self.context.mark_seen(slot);
    }

    pub fn unseen_context(&mut self, slot: RosterSlot) -> String {
        self.context.unseen(slot)
    }

    pub fn add_agent(&mut self) {
        self.active.push(true);
        self.context.add_agent();
    }

    /// Select the next causal turn. Direct work always precedes steering work.
    pub fn begin(&mut self, initial_prompt: impl Into<String>, first: RosterSlot) -> RelayDecision {
        if self.paused {
            return RelayDecision::Paused;
        }
        if self.active_slots().count() < 2 {
            return RelayDecision::Collapsed;
        }
        if self.rounds >= self.max_rounds {
            return RelayDecision::Complete;
        }
        let queued = Self::pop_active(&self.active, &mut self.direct)
            .or_else(|| Self::pop_active(&self.active, &mut self.steering));
        let (slot, prompt, direct) = match queued {
            Some(queued) => (
                queued.slot,
                queued.prompt,
                queued.kind == QueuedKind::Direct,
            ),
            None => {
                let slot = self
                    .next
                    .filter(|slot| self.active[*slot])
                    .unwrap_or_else(|| self.first_active_from(first));
                (slot, initial_prompt.into(), false)
            }
        };
        let can_stop = !direct && self.previous_slot.is_some_and(|previous| previous != slot);
        self.last_active = slot;
        self.rounds += 1;
        RelayDecision::Dispatch {
            slot,
            prompt,
            direct,
            can_stop,
        }
    }

    /// Finalize a dispatched turn and choose the next ring position. Direct
    /// turns never become shared relay context.
    pub fn finish(&mut self, slot: RosterSlot, direct: bool, accepted_stop: bool) {
        self.next = Some(self.next_active(slot));
        if !direct {
            self.previous_slot = Some(slot);
        }
        if accepted_stop && self.direct.is_empty() && self.steering.is_empty() {
            self.rounds = self.max_rounds;
        }
    }

    fn pop_active(active: &[bool], queue: &mut VecDeque<QueuedPrompt>) -> Option<QueuedPrompt> {
        let position = queue
            .iter()
            .position(|queued| active.get(queued.slot).copied().unwrap_or(false))?;
        queue.remove(position)
    }

    fn first_active_from(&self, start: RosterSlot) -> RosterSlot {
        (0..self.active.len())
            .map(|offset| (start + offset) % self.active.len())
            .find(|slot| self.active[*slot])
            .expect("callers require an active roster")
    }

    fn next_active(&self, slot: RosterSlot) -> RosterSlot {
        (1..=self.active.len())
            .map(|offset| (slot + offset) % self.active.len())
            .find(|candidate| self.active[*candidate])
            .expect("callers require an active roster")
    }
}

#[cfg(test)]
mod tests {
    use super::{Relay, RelayDecision, STOP_TOKEN, strip_stop_token};

    #[test]
    fn relay_moves_around_the_ring_without_self_review() {
        let mut relay = Relay::new(3, 10);
        let first = relay.begin("task", 0);
        assert!(matches!(
            first,
            RelayDecision::Dispatch {
                slot: 0,
                can_stop: false,
                ..
            }
        ));
        relay.finish(0, false, false);
        let second = relay.begin("response", 0);
        assert!(matches!(
            second,
            RelayDecision::Dispatch {
                slot: 1,
                can_stop: true,
                ..
            }
        ));
    }

    #[test]
    fn explicit_human_target_beats_ring_order() {
        let mut relay = Relay::new(3, 10);
        relay.begin("task", 0);
        assert!(relay.enqueue_human("correction", Some(2)));
        relay.finish(0, false, false);
        assert!(matches!(
            relay.begin("response", 0),
            RelayDecision::Dispatch { slot: 2, prompt, direct: false, .. } if prompt == "correction"
        ));
    }

    #[test]
    fn direct_work_has_priority_and_owner_is_not_droppable() {
        let mut relay = Relay::new(3, 10);
        relay.enqueue_human("ordinary", Some(1));
        assert_eq!(relay.enqueue_direct(2, "private"), Ok(true));
        assert!(matches!(
            relay.begin("task", 0),
            RelayDecision::Dispatch {
                slot: 2,
                direct: true,
                ..
            }
        ));
        assert_eq!(relay.drop_agent(0), Err("owner cannot be dropped"));
    }

    #[test]
    fn relay_context_tracks_public_updates_per_slot() {
        let mut relay = Relay::new(2, 10);
        relay.set_shared_task("refactor");
        relay.record_public("Agent 0", "first answer");
        relay.mark_context_seen(0);
        assert_eq!(relay.unseen_context(0), "");
        assert_eq!(relay.unseen_context(1), "Agent 0:\nfirst answer");
        assert_eq!(relay.shared_task(), Some("refactor"));
        relay.add_agent();
        assert_eq!(relay.active_slots().count(), 3);
    }

    #[test]
    fn stop_token_is_stripped_only_from_the_response_suffix() {
        let (visible, requested) = strip_stop_token(&format!("looks good\n{STOP_TOKEN}"));
        assert_eq!(visible, "looks good");
        assert!(requested);
        let (visible, requested) = strip_stop_token("ordinary response");
        assert_eq!(visible, "ordinary response");
        assert!(!requested);
    }
}
