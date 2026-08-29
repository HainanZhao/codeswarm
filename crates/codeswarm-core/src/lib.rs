//! Framework-independent CodeSwarm domain state.
//!
//! The terminal UI and each CLI protocol adapter communicate through this
//! event vocabulary. The reducer remains synchronous and deterministic so
//! recorded sessions can be replayed without a terminal or subprocess.

use std::collections::VecDeque;
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

pub mod relay;

/// Stable roster position; slot zero is always the owner.
pub type RosterSlot = usize;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct Mode {
    pub id: String,
    pub label: String,
}

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub struct AgentCapabilities {
    pub supports_cancel: bool,
    pub supports_modes: bool,
    pub supports_permissions: bool,
    pub supports_terminals: bool,
    pub supports_session_load: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub enum AgentEvent {
    Ready {
        slot: RosterSlot,
        capabilities: AgentCapabilities,
    },
    ModesReplaced {
        slot: RosterSlot,
        modes: Vec<Mode>,
        current_mode: Option<String>,
    },
    Text {
        slot: RosterSlot,
        text: String,
    },
    Thought {
        slot: RosterSlot,
        text: String,
    },
    TurnComplete {
        slot: RosterSlot,
    },
    Failed {
        slot: RosterSlot,
        started: bool,
        detail: String,
    },
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub enum Effect {
    Render,
    DispatchPrompt { slot: RosterSlot, prompt: String },
    OfferReload { slot: RosterSlot, crashed: bool },
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct AgentSlot {
    pub active: bool,
    pub capabilities: AgentCapabilities,
    pub modes: Vec<Mode>,
    pub current_mode: Option<String>,
}

impl Default for AgentSlot {
    fn default() -> Self {
        Self {
            active: true,
            capabilities: AgentCapabilities::default(),
            modes: Vec::new(),
            current_mode: None,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct SessionState {
    pub slots: Vec<AgentSlot>,
    pub active_slot: Option<RosterSlot>,
    pub queued_prompts: VecDeque<(RosterSlot, String)>,
    pub public_text: Vec<(RosterSlot, String)>,
}

impl SessionState {
    pub fn new(roster_size: usize) -> Self {
        Self {
            slots: (0..roster_size).map(|_| AgentSlot::default()).collect(),
            active_slot: None,
            queued_prompts: VecDeque::new(),
            public_text: Vec::new(),
        }
    }
}

/// Apply one normalized event. I/O and rendering are represented by effects,
/// never performed in the reducer.
pub fn reduce(state: &mut SessionState, event: AgentEvent) -> Vec<Effect> {
    match event {
        AgentEvent::Ready { slot, capabilities } => {
            if let Some(agent) = state.slots.get_mut(slot) {
                agent.capabilities = capabilities;
            }
            vec![Effect::Render]
        }
        AgentEvent::ModesReplaced {
            slot,
            modes,
            current_mode,
        } => {
            if let Some(agent) = state.slots.get_mut(slot) {
                agent.modes = modes;
                agent.current_mode =
                    current_mode.filter(|id| agent.modes.iter().any(|mode| mode.id == *id));
            }
            vec![Effect::Render]
        }
        AgentEvent::Text { slot, text } => {
            state.public_text.push((slot, text));
            vec![Effect::Render]
        }
        AgentEvent::Thought { .. } => vec![Effect::Render],
        AgentEvent::TurnComplete { .. } => {
            state.active_slot = None;
            let next =
                state
                    .queued_prompts
                    .pop_front()
                    .map(|(target, prompt)| Effect::DispatchPrompt {
                        slot: target,
                        prompt,
                    });
            let mut effects = vec![Effect::Render];
            if let Some(effect) = next {
                effects.push(effect);
            }
            effects
        }
        AgentEvent::Failed {
            slot,
            started,
            detail: _,
        } => {
            if let Some(agent) = state.slots.get_mut(slot) {
                agent.active = false;
            }
            if state.active_slot == Some(slot) {
                state.active_slot = None;
            }
            vec![
                Effect::Render,
                Effect::OfferReload {
                    slot,
                    crashed: started,
                },
            ]
        }
    }
}

/// A durable newline-delimited event log. It deliberately records normalized
/// events rather than UI operations, so sessions can be replayed by a future
/// renderer or adapter host.
#[derive(Clone, Debug)]
pub struct EventLog {
    path: PathBuf,
}

impl EventLog {
    pub fn open(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn append(&self, event: &AgentEvent) -> std::io::Result<()> {
        let encoded = serde_json::to_string(event)
            .map_err(|error| std::io::Error::other(error.to_string()))?;
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        file.write_all(encoded.as_bytes())?;
        file.write_all(b"\n")?;
        file.sync_data()
    }

    pub fn read(&self) -> std::io::Result<Vec<AgentEvent>> {
        let file = match File::open(&self.path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => return Err(error),
        };
        BufReader::new(file)
            .lines()
            .enumerate()
            .filter_map(|(line_number, result)| match result {
                Ok(line) if line.trim().is_empty() => None,
                Ok(line) => Some(serde_json::from_str(&line).map_err(|error| {
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        format!("event log line {}: {error}", line_number + 1),
                    )
                })),
                Err(error) => Some(Err(error)),
            })
            .collect()
    }

    pub fn replay(&self, roster_size: usize) -> std::io::Result<SessionState> {
        let mut state = SessionState::new(roster_size);
        for event in self.read()? {
            reduce(&mut state, event);
        }
        Ok(state)
    }
}

#[cfg(test)]
mod tests {
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::{AgentCapabilities, AgentEvent, Effect, Mode, SessionState, reduce};

    #[test]
    fn replacement_catalog_invalidates_stale_mode() {
        let mut state = SessionState::new(1);
        reduce(
            &mut state,
            AgentEvent::ModesReplaced {
                slot: 0,
                modes: vec![Mode {
                    id: "read".into(),
                    label: "Read only".into(),
                }],
                current_mode: Some("write".into()),
            },
        );
        assert_eq!(state.slots[0].current_mode, None);
    }

    #[test]
    fn crash_tombstones_slot_and_uses_crash_copy() {
        let mut state = SessionState::new(2);
        reduce(
            &mut state,
            AgentEvent::Ready {
                slot: 1,
                capabilities: AgentCapabilities::default(),
            },
        );
        let effects = reduce(
            &mut state,
            AgentEvent::Failed {
                slot: 1,
                started: true,
                detail: "process exited".into(),
            },
        );
        assert!(!state.slots[1].active);
        assert!(effects.contains(&Effect::OfferReload {
            slot: 1,
            crashed: true,
        }));
    }

    #[test]
    fn event_log_replays_into_the_same_state() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let path = std::env::temp_dir().join(format!("codeswarm-core-{unique}.jsonl"));
        let log = super::EventLog::open(&path);
        let events = [
            AgentEvent::Text {
                slot: 0,
                text: "first".into(),
            },
            AgentEvent::Failed {
                slot: 1,
                started: true,
                detail: "crashed".into(),
            },
        ];
        for event in &events {
            log.append(event).expect("append");
        }
        let replayed = log.replay(2).expect("replay");
        let mut expected = SessionState::new(2);
        for event in events {
            reduce(&mut expected, event);
        }
        assert_eq!(replayed, expected);
        std::fs::remove_file(path).expect("cleanup");
    }
}
