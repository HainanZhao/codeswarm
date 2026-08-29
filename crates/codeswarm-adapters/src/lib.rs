//! Protocol adapters for CodeSwarm.
//!
//! ACP and native CLI protocols are intentionally peers here. They emit the
//! same core events and advertise only the capabilities they actually provide.

use std::collections::VecDeque;
use std::path::PathBuf;
use std::process::Stdio;

use async_trait::async_trait;
use codeswarm_core::{AgentCapabilities, AgentEvent, Mode, RosterSlot, ToolStatus, ToolUpdate};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdout, Command};
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
            let response = is_response
                .then(|| text.map(str::to_owned))
                .flatten()
                .filter(|text| !text.is_empty())
                .map(|text| AgentEvent::Text { slot, text });
            Ok(response.or_else(|| parse_agy_tool(slot, &value)))
        }
        _ => Ok(None),
    }
}

fn parse_agy_tool(slot: RosterSlot, value: &Value) -> Option<AgentEvent> {
    let update = value.get("step_update")?;
    if update.get("step_type")?.as_str()? != "tool" {
        return None;
    }
    let step_index = update.get("step_index")?.as_i64()?;
    let title = update
        .get("tool_name")
        .and_then(Value::as_str)
        .unwrap_or("Tool call")
        .replace('_', " ");
    let status = match update.get("state").and_then(Value::as_str) {
        Some("DONE") => ToolStatus::Completed,
        Some("FAILED") => ToolStatus::Failed,
        Some("ACTIVE") => ToolStatus::Running,
        _ => ToolStatus::Pending,
    };
    let detail = update
        .get("tool_info")
        .and_then(|info| info.get("output"))
        .and_then(Value::as_str)
        .map(str::to_owned);
    Some(AgentEvent::Tool {
        slot,
        update: ToolUpdate {
            id: format!("agy-tool-{step_index}"),
            title,
            status,
            detail,
        },
    })
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
    reader: Option<tokio::io::Lines<BufReader<ChildStdout>>>,
    capabilities: AgentCapabilities,
    session_id: Option<String>,
    next_request_id: u64,
    queued_events: VecDeque<AdapterResult<AgentEvent>>,
}

impl AcpAdapter {
    pub fn new(
        slot: RosterSlot,
        cwd: PathBuf,
        program: impl Into<String>,
        args: Vec<String>,
    ) -> Self {
        Self {
            slot,
            program: program.into(),
            args,
            cwd,
            child: None,
            reader: None,
            capabilities: AgentCapabilities::default(),
            session_id: None,
            next_request_id: 1,
            queued_events: VecDeque::new(),
        }
    }

    async fn request(&mut self, method: &str, params: Value) -> AdapterResult<Value> {
        let request_id = self.next_request_id;
        self.next_request_id += 1;
        self.write_json(serde_json::json!({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }))
        .await?;
        loop {
            let line = self.read_line().await?;
            let value: Value = serde_json::from_str(&line)
                .map_err(|error| AdapterError::Protocol(error.to_string()))?;
            if value.get("id").and_then(Value::as_u64) == Some(request_id) {
                if let Some(error) = value.get("error") {
                    return Err(AdapterError::Protocol(error.to_string()));
                }
                return value
                    .get("result")
                    .cloned()
                    .ok_or_else(|| AdapterError::Protocol("response has no result".into()));
            }
            if let Some(event) = parse_acp_notification(self.slot, &line)? {
                self.queued_events.push_back(Ok(event));
            }
        }
    }

    async fn write_json(&mut self, value: Value) -> AdapterResult<()> {
        let child = self
            .child
            .as_mut()
            .ok_or_else(|| AdapterError::Transport("ACP agent is not running".into()))?;
        let stdin = child
            .stdin
            .as_mut()
            .ok_or_else(|| AdapterError::Transport("ACP agent has no stdin".into()))?;
        stdin
            .write_all(value.to_string().as_bytes())
            .await
            .map_err(|error| AdapterError::Transport(error.to_string()))?;
        stdin
            .write_all(b"\n")
            .await
            .map_err(|error| AdapterError::Transport(error.to_string()))
    }

    async fn read_line(&mut self) -> AdapterResult<String> {
        self.reader
            .as_mut()
            .ok_or_else(|| AdapterError::Transport("ACP agent has no stdout".into()))?
            .next_line()
            .await
            .map_err(|error| AdapterError::Transport(error.to_string()))?
            .ok_or_else(|| AdapterError::Transport("ACP stream closed".into()))
    }
}

#[async_trait]
impl AgentAdapter for AcpAdapter {
    fn slot(&self) -> RosterSlot {
        self.slot
    }

    fn capabilities(&self) -> AgentCapabilities {
        self.capabilities.clone()
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
        self.child = Some(child);
        self.reader = Some(BufReader::new(stdout).lines());

        let initialize = self
            .request(
                "initialize",
                serde_json::json!({
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": true, "writeTextFile": true},
                        "terminal": true,
                    },
                    "clientInfo": {
                        "name": "CodeSwarm",
                        "title": "CodeSwarm",
                        "version": env!("CARGO_PKG_VERSION"),
                    },
                }),
            )
            .await?;
        let agent_capabilities = initialize
            .get("agentCapabilities")
            .cloned()
            .unwrap_or(Value::Null);
        self.capabilities = AgentCapabilities {
            supports_cancel: true,
            supports_modes: true,
            supports_permissions: true,
            supports_terminals: true,
            supports_session_load: agent_capabilities
                .get("loadSession")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        };
        let session = self
            .request(
                "session/new",
                serde_json::json!({"cwd": self.cwd, "mcpServers": []}),
            )
            .await?;
        self.session_id = session
            .get("sessionId")
            .and_then(Value::as_str)
            .map(str::to_owned);
        if self.session_id.is_none() {
            return Err(AdapterError::Protocol(
                "session/new returned no sessionId".into(),
            ));
        }
        if let Some(modes) = session.get("modes") {
            let available = modes
                .get("availableModes")
                .and_then(Value::as_array)
                .map(|modes| {
                    modes
                        .iter()
                        .filter_map(|mode| {
                            Some(Mode {
                                id: mode.get("id")?.as_str()?.to_owned(),
                                label: mode.get("name")?.as_str()?.to_owned(),
                            })
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            self.queued_events.push_back(Ok(AgentEvent::ModesReplaced {
                slot: self.slot,
                modes: available,
                current_mode: modes
                    .get("currentModeId")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
            }));
        }
        self.queued_events.push_back(Ok(AgentEvent::Ready {
            slot: self.slot,
            capabilities: self.capabilities(),
        }));
        Ok(())
    }

    async fn send_prompt(&mut self, prompt: String) -> AdapterResult<()> {
        let session_id = self
            .session_id
            .as_ref()
            .ok_or_else(|| AdapterError::Transport("ACP session is not initialized".into()))?;
        let request_id = self.next_request_id;
        self.next_request_id += 1;
        self.write_json(serde_json::json!({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": prompt}],
            },
        }))
        .await
    }

    async fn cancel(&mut self) -> AdapterResult<bool> {
        let Some(session_id) = &self.session_id else {
            return Ok(false);
        };
        self.write_json(serde_json::json!({
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": session_id, "_meta": {}},
        }))
        .await?;
        Ok(true)
    }

    async fn set_mode(&mut self, mode: String) -> AdapterResult<()> {
        let session_id = self
            .session_id
            .as_ref()
            .ok_or_else(|| AdapterError::Transport("ACP session is not initialized".into()))?;
        let _ = self
            .request(
                "session/set_mode",
                serde_json::json!({"sessionId": session_id, "modeId": mode}),
            )
            .await?;
        Ok(())
    }

    async fn reload(&mut self) -> AdapterResult<()> {
        self.stop().await?;
        self.start().await
    }

    async fn stop(&mut self) -> AdapterResult<()> {
        if let Some(mut child) = self.child.take() {
            let _ = child.start_kill();
        }
        self.reader = None;
        self.session_id = None;
        Ok(())
    }

    async fn next_event(&mut self) -> Option<AdapterResult<AgentEvent>> {
        if let Some(event) = self.queued_events.pop_front() {
            return Some(event);
        }
        loop {
            let line = match self.read_line().await {
                Ok(line) => line,
                Err(error) => return Some(Err(error)),
            };
            let value: Value = match serde_json::from_str(&line) {
                Ok(value) => value,
                Err(error) => return Some(Err(AdapterError::Protocol(error.to_string()))),
            };
            if value.get("id").is_some() {
                return Some(Ok(AgentEvent::TurnComplete { slot: self.slot }));
            }
            match parse_acp_notification(self.slot, &line) {
                Ok(Some(event)) => return Some(Ok(event)),
                Ok(None) => {}
                Err(error) => return Some(Err(error)),
            }
        }
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
        (Some("tool_call"), _) | (Some("tool_call_update"), _) => {
            let id = update
                .get("toolCallId")
                .and_then(Value::as_str)
                .unwrap_or("unknown-tool")
                .to_owned();
            let title = update
                .get("title")
                .and_then(Value::as_str)
                .unwrap_or("Tool call")
                .to_owned();
            let status = match update.get("status").and_then(Value::as_str) {
                Some("completed") => ToolStatus::Completed,
                Some("failed") => ToolStatus::Failed,
                Some("in_progress") => ToolStatus::Running,
                _ => ToolStatus::Pending,
            };
            Ok(Some(AgentEvent::Tool {
                slot,
                update: ToolUpdate {
                    id,
                    title,
                    status,
                    detail: None,
                },
            }))
        }
        _ => Ok(None),
    }
}

#[cfg(test)]
mod tests {
    use codeswarm_core::{AgentEvent, ToolStatus};

    use super::{AcpAdapter, AgentAdapter, parse_acp_notification, parse_agy_line};

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

    #[test]
    fn parses_tool_lifecycle_from_each_protocol() {
        let agy = parse_agy_line(
            1,
            r#"{"event":"step_update","step_update":{"step_type":"tool","step_index":4,"tool_name":"run_command","state":"DONE","tool_info":{"output":"ok"}}}"#,
        )
        .expect("valid native tool")
        .expect("tool event");
        assert!(matches!(
            agy,
            AgentEvent::Tool {
                update: codeswarm_core::ToolUpdate {
                    status: ToolStatus::Completed,
                    ..
                },
                ..
            }
        ));

        let acp = parse_acp_notification(
            1,
            r#"{"method":"session/update","params":{"update":{"sessionUpdate":"tool_call_update","toolCallId":"t1","title":"Run tests","status":"failed"}}}"#,
        )
        .expect("valid ACP tool")
        .expect("tool event");
        assert!(matches!(
            acp,
            AgentEvent::Tool {
                update: codeswarm_core::ToolUpdate {
                    status: ToolStatus::Failed,
                    ..
                },
                ..
            }
        ));
    }

    #[tokio::test]
    async fn acp_adapter_initializes_session_and_completes_a_prompt() {
        let script = r#"read _; echo '{"jsonrpc":"2.0","id":1,"result":{"agentCapabilities":{"loadSession":true}}}'; read _; echo '{"jsonrpc":"2.0","id":2,"result":{"sessionId":"session-1","modes":{"currentModeId":"plan","availableModes":[{"id":"plan","name":"Plan"}]}}}'; read _; echo '{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"hello"}}}}'; echo '{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}'"#;
        let cwd = std::env::current_dir().expect("cwd");
        let mut adapter = AcpAdapter::new(0, cwd, "sh", vec!["-c".into(), script.into()]);
        adapter.start().await.expect("initialize");
        assert!(matches!(
            adapter.next_event().await,
            Some(Ok(AgentEvent::ModesReplaced { .. }))
        ));
        assert!(matches!(
            adapter.next_event().await,
            Some(Ok(AgentEvent::Ready { .. }))
        ));
        adapter.send_prompt("hello".into()).await.expect("prompt");
        assert!(matches!(
            adapter.next_event().await,
            Some(Ok(AgentEvent::Text { text, .. })) if text == "hello"
        ));
        assert!(matches!(
            adapter.next_event().await,
            Some(Ok(AgentEvent::TurnComplete { .. }))
        ));
    }
}
