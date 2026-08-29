//! Protocol adapters for CodeSwarm.
//!
//! ACP and native CLI protocols are intentionally peers here. They emit the
//! same core events and advertise only the capabilities they actually provide.

use std::collections::VecDeque;
use std::path::PathBuf;
use std::process::Stdio;

use async_trait::async_trait;
use codeswarm_core::{AgentCapabilities, AgentEvent, Mode, RosterSlot};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::mpsc;

pub type AdapterResult<T> = Result<T, AdapterError>;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AdapterError {
    Unsupported(&'static str),
    Spawn(String),
    Transport(String),
    Protocol(String),
}

impl std::fmt::Display for AdapterError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unsupported(operation) => write!(formatter, "unsupported operation: {operation}"),
            Self::Spawn(error) => write!(formatter, "unable to launch agent: {error}"),
            Self::Transport(error) => write!(formatter, "agent transport error: {error}"),
            Self::Protocol(error) => write!(formatter, "agent protocol error: {error}"),
        }
    }
}

impl std::error::Error for AdapterError {}

/// Uniform control plane for ACP and custom command-line adapters.
#[async_trait]
pub trait AgentAdapter: Send {
    fn slot(&self) -> RosterSlot;
    fn capabilities(&self) -> AgentCapabilities;
    async fn start(&mut self) -> AdapterResult<()>;
    async fn send_prompt(&mut self, prompt: String) -> AdapterResult<()>;
    async fn cancel(&mut self) -> AdapterResult<bool>;
    async fn set_mode(&mut self, mode: String) -> AdapterResult<()>;
    async fn reload(&mut self) -> AdapterResult<()>;
    async fn stop(&mut self) -> AdapterResult<()>;
    async fn next_event(&mut self) -> Option<AdapterResult<AgentEvent>>;
}

/// Deterministic in-memory adapter used for contract and relay tests.
#[derive(Debug)]
pub struct ScriptedAdapter {
    slot: RosterSlot,
    capabilities: AgentCapabilities,
    events: VecDeque<AdapterResult<AgentEvent>>,
    prompts: Vec<String>,
}

impl ScriptedAdapter {
    pub fn new(
        slot: RosterSlot,
        capabilities: AgentCapabilities,
        events: impl IntoIterator<Item = AgentEvent>,
    ) -> Self {
        Self {
            slot,
            capabilities,
            events: events.into_iter().map(Ok).collect(),
            prompts: Vec::new(),
        }
    }

    pub fn prompts(&self) -> &[String] {
        &self.prompts
    }
}

#[async_trait]
impl AgentAdapter for ScriptedAdapter {
    fn slot(&self) -> RosterSlot {
        self.slot
    }

    fn capabilities(&self) -> AgentCapabilities {
        self.capabilities.clone()
    }

    async fn start(&mut self) -> AdapterResult<()> {
        Ok(())
    }

    async fn send_prompt(&mut self, prompt: String) -> AdapterResult<()> {
        self.prompts.push(prompt);
        Ok(())
    }

    async fn cancel(&mut self) -> AdapterResult<bool> {
        Ok(self.capabilities.supports_cancel)
    }

    async fn set_mode(&mut self, _mode: String) -> AdapterResult<()> {
        if self.capabilities.supports_modes {
            Ok(())
        } else {
            Err(AdapterError::Unsupported("set_mode"))
        }
    }

    async fn reload(&mut self) -> AdapterResult<()> {
        Ok(())
    }

    async fn stop(&mut self) -> AdapterResult<()> {
        Ok(())
    }

    async fn next_event(&mut self) -> Option<AdapterResult<AgentEvent>> {
        self.events.pop_front()
    }
}

/// A direct stream-JSON adapter for Antigravity. It deliberately does not
/// pretend to be ACP; it translates its documented events into core events.
#[derive(Debug)]
pub struct AgyAdapter {
    slot: RosterSlot,
    cwd: PathBuf,
    command: String,
    mode: String,
    session_id: Option<String>,
    child: Option<Child>,
    sender: mpsc::Sender<AdapterResult<AgentEvent>>,
    receiver: mpsc::Receiver<AdapterResult<AgentEvent>>,
}

impl AgyAdapter {
    pub fn new(slot: RosterSlot, cwd: PathBuf, command: impl Into<String>) -> Self {
        let (sender, receiver) = mpsc::channel(256);
        Self {
            slot,
            cwd,
            command: command.into(),
            mode: "default".into(),
            session_id: None,
            child: None,
            sender,
            receiver,
        }
    }

    fn modes() -> Vec<Mode> {
        vec![
            Mode {
                id: "default".into(),
                label: "Agent Default".into(),
            },
            Mode {
                id: "accept-edits".into(),
                label: "Accept Edits".into(),
            },
            Mode {
                id: "plan".into(),
                label: "Plan".into(),
            },
        ]
    }

    async fn emit(&self, event: AdapterResult<AgentEvent>) {
        let _ = self.sender.send(event).await;
    }
}

#[async_trait]
impl AgentAdapter for AgyAdapter {
    fn slot(&self) -> RosterSlot {
        self.slot
    }

    fn capabilities(&self) -> AgentCapabilities {
        AgentCapabilities {
            supports_cancel: true,
            supports_modes: true,
            supports_permissions: false,
            supports_terminals: true,
            supports_session_load: true,
        }
    }

    async fn start(&mut self) -> AdapterResult<()> {
        self.emit(Ok(AgentEvent::Ready {
            slot: self.slot,
            capabilities: self.capabilities(),
        }))
        .await;
        self.emit(Ok(AgentEvent::ModesReplaced {
            slot: self.slot,
            modes: Self::modes(),
            current_mode: Some(self.mode.clone()),
        }))
        .await;
        Ok(())
    }

    async fn send_prompt(&mut self, prompt: String) -> AdapterResult<()> {
        if self.child.is_some() {
            return Err(AdapterError::Transport(
                "agent is already handling a turn".into(),
            ));
        }
        let mut command = Command::new(&self.command);
        command
            .arg("--print")
            .arg(prompt)
            .arg("--print-timeout")
            .arg("60m")
            .arg("--output-format")
            .arg("stream-json")
            .current_dir(&self.cwd)
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        if let Some(session_id) = &self.session_id {
            command.arg("--conversation").arg(session_id);
        }
        if self.mode != "default" {
            command.arg("--mode").arg(&self.mode);
        }
        let mut child = command
            .spawn()
            .map_err(|error| AdapterError::Spawn(error.to_string()))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| AdapterError::Transport("agent has no stdout".into()))?;
        let sender = self.sender.clone();
        let slot = self.slot;
        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                match parse_agy_line(slot, &line) {
                    Ok(Some(event)) => {
                        if sender.send(Ok(event)).await.is_err() {
                            break;
                        }
                    }
                    Ok(None) => {}
                    Err(error) => {
                        let _ = sender.send(Err(error)).await;
                    }
                }
            }
            let _ = sender.send(Ok(AgentEvent::TurnComplete { slot })).await;
        });
        self.child = Some(child);
        Ok(())
    }

    async fn cancel(&mut self) -> AdapterResult<bool> {
        let Some(child) = self.child.as_mut() else {
            return Ok(false);
        };
        child
            .start_kill()
            .map_err(|error| AdapterError::Transport(error.to_string()))?;
        Ok(true)
    }

    async fn set_mode(&mut self, mode: String) -> AdapterResult<()> {
        if !Self::modes().iter().any(|candidate| candidate.id == mode) {
            return Err(AdapterError::Unsupported("requested Agy mode"));
        }
        self.mode = mode.clone();
        self.emit(Ok(AgentEvent::ModesReplaced {
            slot: self.slot,
            modes: Self::modes(),
            current_mode: Some(mode),
        }))
        .await;
        Ok(())
    }

    async fn reload(&mut self) -> AdapterResult<()> {
        self.stop().await?;
        self.start().await
    }

    async fn stop(&mut self) -> AdapterResult<()> {
        let _ = self.cancel().await?;
        self.child = None;
        Ok(())
    }

    async fn next_event(&mut self) -> Option<AdapterResult<AgentEvent>> {
        self.receiver.recv().await
    }
}

fn parse_agy_line(slot: RosterSlot, line: &str) -> AdapterResult<Option<AgentEvent>> {
    let value: Value =
        serde_json::from_str(line).map_err(|error| AdapterError::Protocol(error.to_string()))?;
    let event = value.get("event").and_then(Value::as_str);
    match event {
        Some("step_update") => {
            let Some(update) = value.get("step_update") else {
                return Ok(None);
            };
            let is_response = update
                .get("step_type")
                .and_then(Value::as_str)
                .is_some_and(|kind| kind == "agent_response");
            let text = update.get("text_delta").and_then(Value::as_str);
            Ok(is_response
                .then(|| text.map(str::to_owned))
                .flatten()
                .filter(|text| !text.is_empty())
                .map(|text| AgentEvent::Text { slot, text }))
        }
        _ => Ok(None),
    }
}

/// Stdio ACP transport. Protocol-specific response handling belongs here,
/// keeping JSON-RPC framing outside the core and terminal renderer.
#[derive(Debug)]
pub struct AcpAdapter {
    slot: RosterSlot,
    program: String,
    args: Vec<String>,
    cwd: PathBuf,
    child: Option<Child>,
    sender: mpsc::Sender<AdapterResult<AgentEvent>>,
    receiver: mpsc::Receiver<AdapterResult<AgentEvent>>,
}

impl AcpAdapter {
    pub fn new(
        slot: RosterSlot,
        cwd: PathBuf,
        program: impl Into<String>,
        args: Vec<String>,
    ) -> Self {
        let (sender, receiver) = mpsc::channel(256);
        Self {
            slot,
            program: program.into(),
            args,
            cwd,
            child: None,
            sender,
            receiver,
        }
    }
}

#[async_trait]
impl AgentAdapter for AcpAdapter {
    fn slot(&self) -> RosterSlot {
        self.slot
    }

    fn capabilities(&self) -> AgentCapabilities {
        AgentCapabilities::default()
    }

    async fn start(&mut self) -> AdapterResult<()> {
        let mut child = Command::new(&self.program)
            .args(&self.args)
            .current_dir(&self.cwd)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|error| AdapterError::Spawn(error.to_string()))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| AdapterError::Transport("agent has no stdout".into()))?;
        let sender = self.sender.clone();
        let slot = self.slot;
        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                match parse_acp_notification(slot, &line) {
                    Ok(Some(event)) => {
                        if sender.send(Ok(event)).await.is_err() {
                            break;
                        }
                    }
                    Ok(None) => {}
                    Err(error) => {
                        let _ = sender.send(Err(error)).await;
                    }
                }
            }
            let _ = sender
                .send(Ok(AgentEvent::Failed {
                    slot,
                    started: true,
                    detail: "ACP stream closed".into(),
                }))
                .await;
        });
        self.child = Some(child);
        self.sender
            .send(Ok(AgentEvent::Ready {
                slot: self.slot,
                capabilities: self.capabilities(),
            }))
            .await
            .map_err(|error| AdapterError::Transport(error.to_string()))
    }

    async fn send_prompt(&mut self, prompt: String) -> AdapterResult<()> {
        let child = self
            .child
            .as_mut()
            .ok_or_else(|| AdapterError::Transport("ACP agent is not running".into()))?;
        let stdin = child
            .stdin
            .as_mut()
            .ok_or_else(|| AdapterError::Transport("ACP agent has no stdin".into()))?;
        let request = serde_json::json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/prompt",
            "params": {"prompt": [{"type": "text", "text": prompt}]},
        });
        stdin
            .write_all(request.to_string().as_bytes())
            .await
            .map_err(|error| AdapterError::Transport(error.to_string()))?;
        stdin
            .write_all(b"\n")
            .await
            .map_err(|error| AdapterError::Transport(error.to_string()))
    }

    async fn cancel(&mut self) -> AdapterResult<bool> {
        Ok(false)
    }

    async fn set_mode(&mut self, _mode: String) -> AdapterResult<()> {
        Err(AdapterError::Unsupported("set_mode"))
    }

    async fn reload(&mut self) -> AdapterResult<()> {
        self.stop().await?;
        self.start().await
    }

    async fn stop(&mut self) -> AdapterResult<()> {
        if let Some(mut child) = self.child.take() {
            let _ = child.start_kill();
        }
        Ok(())
    }

    async fn next_event(&mut self) -> Option<AdapterResult<AgentEvent>> {
        self.receiver.recv().await
    }
}

fn parse_acp_notification(slot: RosterSlot, line: &str) -> AdapterResult<Option<AgentEvent>> {
    let value: Value =
        serde_json::from_str(line).map_err(|error| AdapterError::Protocol(error.to_string()))?;
    if value.get("method").and_then(Value::as_str) != Some("session/update") {
        return Ok(None);
    }
    let Some(update) = value.get("params").and_then(|params| params.get("update")) else {
        return Ok(None);
    };
    let kind = update.get("sessionUpdate").and_then(Value::as_str);
    let text = update
        .get("content")
        .and_then(|content| content.get("text"))
        .and_then(Value::as_str)
        .map(str::to_owned);
    match (kind, text) {
        (Some("agent_message_chunk"), Some(text)) if !text.is_empty() => {
            Ok(Some(AgentEvent::Text { slot, text }))
        }
        (Some("agent_thought_chunk"), Some(text)) if !text.is_empty() => {
            Ok(Some(AgentEvent::Thought { slot, text }))
        }
        _ => Ok(None),
    }
}

#[cfg(test)]
mod tests {
    use codeswarm_core::AgentEvent;

    use super::{parse_acp_notification, parse_agy_line};

    #[test]
    fn parses_acp_text_without_ui_dependency() {
        let event = parse_acp_notification(
            2,
            r#"{"method":"session/update","params":{"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"hello"}}}}"#,
        )
        .expect("valid ACP")
        .expect("text event");
        assert_eq!(
            event,
            AgentEvent::Text {
                slot: 2,
                text: "hello".into(),
            }
        );
    }

    #[test]
    fn parses_native_agy_text_without_acp_bridge() {
        let event = parse_agy_line(
            1,
            r#"{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"hello"}}"#,
        )
        .expect("valid stream-json")
        .expect("text event");
        assert_eq!(
            event,
            AgentEvent::Text {
                slot: 1,
                text: "hello".into(),
            }
        );
    }
}
