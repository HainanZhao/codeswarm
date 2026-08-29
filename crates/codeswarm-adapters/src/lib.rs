//! Protocol adapters for CodeSwarm.
//!
//! ACP and native CLI protocols are intentionally peers here. They emit the
//! same core events and expose capabilities through the same adapter boundary.

use std::collections::VecDeque;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};

use async_trait::async_trait;
use codeswarm_core::{
    AgentCapabilities, AgentEvent, Effect, EventLog, Mode, PermissionAnswer, PermissionRequest,
    RosterSlot, SessionState, TerminalEvent, ToolStatus, ToolUpdate, reduce,
    relay::{DEFAULT_STOP_ACKNOWLEDGMENT, Relay, RelayDecision, strip_stop_token},
};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdout, Command};
use tokio::sync::{Notify, mpsc};

pub type AdapterResult<T> = Result<T, AdapterError>;

#[derive(Clone, Debug)]
pub struct HostUpdate {
    pub event: AgentEvent,
    pub effects: Vec<Effect>,
}

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
    async fn answer_permission(
        &mut self,
        request_id: String,
        answer: PermissionAnswer,
    ) -> AdapterResult<()>;
    async fn set_mode(&mut self, mode: String) -> AdapterResult<()>;
    async fn reload(&mut self) -> AdapterResult<()>;
    async fn stop(&mut self) -> AdapterResult<()>;
    async fn next_event(&mut self) -> Option<AdapterResult<AgentEvent>>;
}

/// Owns one adapter and feeds normalized events through the deterministic core
/// reducer. The UI consumes effects and state snapshots.
pub struct AdapterHost {
    adapter: Box<dyn AgentAdapter>,
    pub state: SessionState,
    pub last_error: Option<String>,
    event_log: Option<EventLog>,
}

impl std::fmt::Debug for AdapterHost {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("AdapterHost")
            .field("state", &self.state)
            .field("last_error", &self.last_error)
            .field("event_log", &self.event_log)
            .finish_non_exhaustive()
    }
}

impl AdapterHost {
    pub fn new(adapter: Box<dyn AgentAdapter>, event_log: Option<EventLog>) -> Self {
        let slot = adapter.slot();
        Self {
            adapter,
            state: SessionState::new(slot.saturating_add(1)),
            last_error: None,
            event_log,
        }
    }

    pub async fn start(&mut self) -> AdapterResult<()> {
        self.adapter.start().await
    }

    pub async fn send_prompt(&mut self, prompt: String) -> AdapterResult<()> {
        self.adapter.send_prompt(prompt).await
    }

    pub async fn cancel(&mut self) -> AdapterResult<bool> {
        self.adapter.cancel().await
    }

    pub async fn answer_permission(
        &mut self,
        request_id: String,
        answer: PermissionAnswer,
    ) -> AdapterResult<()> {
        self.adapter.answer_permission(request_id, answer).await
    }

    pub async fn set_mode(&mut self, mode: String) -> AdapterResult<()> {
        self.adapter.set_mode(mode).await
    }

    pub async fn reload(&mut self) -> AdapterResult<()> {
        self.adapter.reload().await?;
        let slot = self.adapter.slot();
        if let Some(agent) = self.state.slots.get_mut(slot) {
            agent.active = true;
            agent.capabilities = self.adapter.capabilities();
        }
        self.last_error = None;
        Ok(())
    }

    pub async fn stop(&mut self) -> AdapterResult<()> {
        self.adapter.stop().await
    }

    pub async fn next_effects(&mut self) -> Option<AdapterResult<Vec<Effect>>> {
        Some(self.next_update().await?.map(|update| update.effects))
    }

    pub async fn next_update(&mut self) -> Option<AdapterResult<HostUpdate>> {
        let event = match self.adapter.next_event().await {
            None => return None,
            Some(Err(error)) => {
                self.last_error = Some(error.to_string());
                let slot = self.adapter.slot();
                let failure = AgentEvent::Failed {
                    slot,
                    started: true,
                    detail: error.to_string(),
                };
                let effects = reduce(&mut self.state, failure.clone());
                return Some(Ok(HostUpdate {
                    event: failure,
                    effects,
                }));
            }
            Some(Ok(event)) => event,
        };
        if let Some(log) = &self.event_log
            && let Err(error) = log.append(&event)
        {
            return Some(Err(AdapterError::Transport(error.to_string())));
        }
        let effects = reduce(&mut self.state, event.clone());
        Some(Ok(HostUpdate { event, effects }))
    }

    pub fn adapter(&self) -> &dyn AgentAdapter {
        &*self.adapter
    }
}

/// Sequential multi-adapter runner. It intentionally never polls two
/// adapters concurrently: the next prompt depends on the prior response.
pub struct RelayHost {
    hosts: Vec<AdapterHost>,
    relay: Relay,
    dispatches: Vec<(RosterSlot, String)>,
    event_sink: Option<Arc<dyn Fn(AgentEvent) + Send + Sync>>,
    cancel_requested: Arc<AtomicBool>,
    cancel_notify: Arc<Notify>,
}

/// A clonable signal used by a terminal control loop to interrupt the active
/// relay turn without borrowing the relay while its adapter is being polled.
#[derive(Clone, Debug)]
pub struct RelayCancellation {
    requested: Arc<AtomicBool>,
    notify: Arc<Notify>,
}

impl RelayCancellation {
    pub fn request(&self) {
        self.requested.store(true, Ordering::Release);
        self.notify.notify_one();
    }
}

impl std::fmt::Debug for RelayHost {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RelayHost")
            .field("hosts", &self.hosts)
            .field("relay", &self.relay)
            .field("dispatches", &self.dispatches)
            .field("event_sink", &self.event_sink.is_some())
            .field(
                "cancel_requested",
                &self.cancel_requested.load(Ordering::Acquire),
            )
            .finish()
    }
}

impl RelayHost {
    pub fn new(hosts: Vec<AdapterHost>, max_rounds: usize) -> Result<Self, AdapterError> {
        if hosts.len() < 2 {
            return Err(AdapterError::Unsupported("relay requires two adapters"));
        }
        Ok(Self {
            relay: Relay::new(hosts.len(), max_rounds),
            hosts,
            dispatches: Vec::new(),
            event_sink: None,
            cancel_requested: Arc::new(AtomicBool::new(false)),
            cancel_notify: Arc::new(Notify::new()),
        })
    }

    /// Send each normalized event to a client while a turn is being drained.
    /// The callback runs synchronously on the relay task and should only
    /// enqueue the event; expensive rendering must happen outside the callback.
    pub fn set_event_sink<F>(&mut self, sink: F)
    where
        F: Fn(AgentEvent) + Send + Sync + 'static,
    {
        self.event_sink = Some(Arc::new(sink));
    }

    pub async fn start(&mut self) -> AdapterResult<()> {
        for host in &mut self.hosts {
            host.start().await?;
        }
        Ok(())
    }

    pub async fn stop(&mut self) -> AdapterResult<()> {
        for host in &mut self.hosts {
            host.stop().await?;
        }
        Ok(())
    }

    /// Forward a normalized permission answer to the adapter owning `slot`.
    /// This keeps protocol-specific response framing out of the relay and UI.
    pub async fn answer_permission(
        &mut self,
        slot: RosterSlot,
        request_id: String,
        answer: PermissionAnswer,
    ) -> AdapterResult<()> {
        let host = self
            .hosts
            .get_mut(slot)
            .ok_or_else(|| AdapterError::Transport("permission target is missing".into()))?;
        host.answer_permission(request_id, answer).await
    }

    pub fn pause(&mut self) {
        self.relay.pause();
    }

    pub fn resume(&mut self) {
        self.relay.resume();
    }

    pub fn relay(&self) -> &Relay {
        &self.relay
    }

    pub fn relay_mut(&mut self) -> &mut Relay {
        &mut self.relay
    }

    pub fn cancellation(&self) -> RelayCancellation {
        RelayCancellation {
            requested: Arc::clone(&self.cancel_requested),
            notify: Arc::clone(&self.cancel_notify),
        }
    }

    /// Prompts sent to adapters, in causal dispatch order. This is useful to
    /// diagnostics and makes the context-routing boundary observable without
    /// exposing protocol-specific adapter internals.
    pub fn dispatches(&self) -> &[(RosterSlot, String)] {
        &self.dispatches
    }

    pub async fn run_turn(
        &mut self,
        task: impl Into<String>,
        first_slot: RosterSlot,
    ) -> AdapterResult<RelayDecision> {
        let task = task.into();
        if self.relay.shared_task().is_none() {
            self.relay.set_shared_task(task.clone());
        }
        let decision = self.relay.begin(task, first_slot);
        let RelayDecision::Dispatch {
            slot,
            prompt,
            direct,
            can_stop,
        } = &decision
        else {
            return Ok(decision);
        };
        let unseen = self.relay.unseen_context(*slot);
        let prompt = if unseen.is_empty() {
            prompt.clone()
        } else {
            format!("{prompt}\n\nPublic updates:\n{unseen}")
        };
        let host = self
            .hosts
            .get_mut(*slot)
            .ok_or_else(|| AdapterError::Transport("relay selected missing adapter".into()))?;
        host.send_prompt(prompt.clone()).await?;
        self.dispatches.push((*slot, prompt));
        let mut response = String::new();
        loop {
            let update = tokio::select! {
                update = host.next_update() => update
                    .ok_or_else(|| AdapterError::Transport("adapter ended during turn".into()))??,
                _ = self.cancel_notify.notified() => {
                    self.cancel_requested.store(false, Ordering::Release);
                    let _ = host.cancel().await?;
                    return Err(AdapterError::Transport("relay turn cancelled".into()));
                }
            };
            if let Some(sink) = &self.event_sink {
                sink(update.event.clone());
            }
            match &update.event {
                AgentEvent::Text { text, .. } => response.push_str(text),
                AgentEvent::TurnComplete { .. } => {
                    self.cancel_requested.store(false, Ordering::Release);
                    break;
                }
                AgentEvent::Failed { detail, .. } => {
                    return Err(AdapterError::Transport(detail.clone()));
                }
                _ => {}
            }
        }
        let (response, requested_stop) = strip_stop_token(&response);
        let accepted_stop = requested_stop && *can_stop;
        let response = if accepted_stop && response.is_empty() {
            DEFAULT_STOP_ACKNOWLEDGMENT.to_owned()
        } else {
            response
        };
        if !*direct && !response.is_empty() {
            self.relay.record_public(format!("Agent {slot}"), response);
        }
        self.relay.mark_context_seen(*slot);
        self.relay.finish(*slot, *direct, accepted_stop);
        Ok(decision)
    }
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

    async fn answer_permission(
        &mut self,
        _request_id: String,
        _answer: PermissionAnswer,
    ) -> AdapterResult<()> {
        if self.capabilities.supports_permissions {
            Ok(())
        } else {
            Err(AdapterError::Unsupported("permission answer"))
        }
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

    async fn answer_permission(
        &mut self,
        _request_id: String,
        _answer: PermissionAnswer,
    ) -> AdapterResult<()> {
        Err(AdapterError::Unsupported("permission answer"))
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
        let event = self.receiver.recv().await;
        if matches!(event.as_ref(), Some(Ok(AgentEvent::TurnComplete { .. })))
            && let Some(mut child) = self.child.take()
        {
            let _ = child.wait().await;
        }
        event
    }
}

fn parse_agy_line(slot: RosterSlot, line: &str) -> AdapterResult<Option<AgentEvent>> {
    let value: Value =
        serde_json::from_str(line).map_err(|error| AdapterError::Protocol(error.to_string()))?;
    let event = value.get("event").and_then(Value::as_str);
    if let Some(terminal) = parse_terminal_event(&value, event) {
        return Ok(Some(AgentEvent::Terminal {
            slot,
            event: terminal,
        }));
    }
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

    pub fn with_session_id(
        slot: RosterSlot,
        cwd: PathBuf,
        program: impl Into<String>,
        args: Vec<String>,
        session_id: impl Into<String>,
    ) -> Self {
        let mut adapter = Self::new(slot, cwd, program, args);
        adapter.session_id = Some(session_id.into());
        adapter
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
        let session = if let Some(session_id) = self.session_id.clone() {
            if !self.capabilities.supports_session_load {
                return Err(AdapterError::Unsupported("session/load"));
            }
            self.request(
                "session/load",
                serde_json::json!({
                    "cwd": self.cwd,
                    "mcpServers": [],
                    "sessionId": session_id,
                }),
            )
            .await?
        } else {
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
            session
        };
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

    async fn answer_permission(
        &mut self,
        request_id: String,
        answer: PermissionAnswer,
    ) -> AdapterResult<()> {
        let id = request_id
            .parse::<u64>()
            .map(Value::from)
            .unwrap_or_else(|_| Value::String(request_id));
        let outcome = match answer {
            PermissionAnswer::Selected { option_id } => {
                serde_json::json!({"outcome": "selected", "optionId": option_id})
            }
            PermissionAnswer::Cancelled => serde_json::json!({"outcome": "cancelled"}),
        };
        self.write_json(serde_json::json!({
            "jsonrpc": "2.0",
            "id": id,
            "result": {"outcome": outcome},
        }))
        .await
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
            match parse_acp_notification(self.slot, &line) {
                Ok(Some(event)) => return Some(Ok(event)),
                Ok(None) => {}
                Err(error) => return Some(Err(error)),
            }
            if value.get("id").is_some() {
                return Some(Ok(AgentEvent::TurnComplete { slot: self.slot }));
            }
        }
    }
}

fn parse_acp_notification(slot: RosterSlot, line: &str) -> AdapterResult<Option<AgentEvent>> {
    let value: Value =
        serde_json::from_str(line).map_err(|error| AdapterError::Protocol(error.to_string()))?;
    let method = value.get("method").and_then(Value::as_str);
    if method == Some("session/request_permission") {
        let params = value.get("params").cloned().unwrap_or(Value::Null);
        let request_id = value
            .get("id")
            .map(rpc_id_to_string)
            .unwrap_or_else(|| "permission".into());
        return Ok(parse_permission_event(
            slot,
            &params,
            &request_id,
            params.get("options"),
        ));
    }
    if method != Some("session/update") {
        return Ok(None);
    }
    let Some(update) = value.get("params").and_then(|params| params.get("update")) else {
        return Ok(None);
    };
    let kind = update.get("sessionUpdate").and_then(Value::as_str);
    if kind == Some("request_permission") {
        let request_id = update
            .get("toolCall")
            .and_then(|tool| tool.get("toolCallId"))
            .and_then(Value::as_str)
            .unwrap_or("permission");
        return Ok(parse_permission_event(
            slot,
            update,
            request_id,
            update.get("options"),
        ));
    }
    if let Some(terminal) = parse_terminal_event(update, kind) {
        return Ok(Some(AgentEvent::Terminal {
            slot,
            event: terminal,
        }));
    }
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

/// Normalize terminal lifecycle updates emitted by ACP-compatible bridges and
/// native stream adapters. Protocols have used both snake_case update names
/// and a nested `terminal` object, so accept either without leaking that
/// shape beyond the adapter boundary.
fn parse_terminal_event(value: &Value, kind: Option<&str>) -> Option<TerminalEvent> {
    let nested = value.get("terminal").unwrap_or(value);
    let kind = kind.or_else(|| value.get("event").and_then(Value::as_str))?;
    let id = nested
        .get("terminalId")
        .or_else(|| nested.get("terminal_id"))
        .or_else(|| nested.get("id"))
        .and_then(Value::as_str)
        .unwrap_or("terminal")
        .to_owned();
    match kind {
        "terminal_created" | "terminal_create" | "terminal_started" => {
            let command = nested
                .get("command")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            Some(TerminalEvent::Created { id, command })
        }
        "terminal_output" | "terminal_output_chunk" => {
            let text = nested
                .get("output")
                .or_else(|| nested.get("text"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            Some(TerminalEvent::Output { id, text })
        }
        "terminal_exited" | "terminal_exit" => {
            let code = nested
                .get("exitCode")
                .or_else(|| nested.get("exit_code"))
                .or_else(|| nested.get("code"))
                .and_then(Value::as_i64)
                .unwrap_or(0) as i32;
            Some(TerminalEvent::Exited { id, code })
        }
        "terminal_released" | "terminal_release" => Some(TerminalEvent::Released { id }),
        _ => None,
    }
}

fn parse_permission_event(
    slot: RosterSlot,
    value: &Value,
    request_id: &str,
    options: Option<&Value>,
) -> Option<AgentEvent> {
    let tool = value.get("toolCall").unwrap_or(value);
    let title = tool
        .get("title")
        .and_then(Value::as_str)
        .unwrap_or("Agent requests permission")
        .to_owned();
    let options = options
        .and_then(Value::as_array)
        .map(|options| {
            options
                .iter()
                .filter_map(|option| {
                    option
                        .get("name")
                        .or_else(|| option.get("optionId"))
                        .and_then(Value::as_str)
                        .map(str::to_owned)
                })
                .collect()
        })
        .unwrap_or_default();
    Some(AgentEvent::Permission {
        slot,
        request: PermissionRequest {
            id: request_id.to_owned(),
            title,
            options,
        },
    })
}

fn rpc_id_to_string(value: &Value) -> String {
    value
        .as_str()
        .map(str::to_owned)
        .or_else(|| value.as_u64().map(|id| id.to_string()))
        .unwrap_or_else(|| value.to_string())
}

#[cfg(test)]
mod tests {
    use async_trait::async_trait;
    use codeswarm_core::TerminalEvent;
    use codeswarm_core::{AgentCapabilities, AgentEvent, EventLog, PermissionAnswer, ToolStatus};

    use super::{
        AcpAdapter, AdapterHost, AgentAdapter, AgyAdapter, ScriptedAdapter, parse_acp_notification,
        parse_agy_line,
    };
    use serde_json::Value;

    #[derive(Debug)]
    struct PendingAdapter {
        slot: usize,
    }

    #[async_trait]
    impl AgentAdapter for PendingAdapter {
        fn slot(&self) -> usize {
            self.slot
        }

        fn capabilities(&self) -> AgentCapabilities {
            AgentCapabilities {
                supports_cancel: true,
                ..AgentCapabilities::default()
            }
        }

        async fn start(&mut self) -> super::AdapterResult<()> {
            Ok(())
        }

        async fn send_prompt(&mut self, _prompt: String) -> super::AdapterResult<()> {
            Ok(())
        }

        async fn cancel(&mut self) -> super::AdapterResult<bool> {
            Ok(true)
        }

        async fn answer_permission(
            &mut self,
            _request_id: String,
            _answer: PermissionAnswer,
        ) -> super::AdapterResult<()> {
            Ok(())
        }

        async fn set_mode(&mut self, _mode: String) -> super::AdapterResult<()> {
            Ok(())
        }

        async fn reload(&mut self) -> super::AdapterResult<()> {
            Ok(())
        }

        async fn stop(&mut self) -> super::AdapterResult<()> {
            Ok(())
        }

        async fn next_event(&mut self) -> Option<super::AdapterResult<AgentEvent>> {
            std::future::pending().await
        }
    }

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

    #[test]
    fn parses_terminal_lifecycle_from_acp_and_native_events() {
        let created = parse_acp_notification(
            0,
            r#"{"method":"session/update","params":{"update":{"sessionUpdate":"terminal_created","terminalId":"term-1","command":"cargo test"}}}"#,
        )
        .expect("valid ACP terminal")
        .expect("terminal event");
        assert_eq!(
            created,
            AgentEvent::Terminal {
                slot: 0,
                event: TerminalEvent::Created {
                    id: "term-1".into(),
                    command: "cargo test".into(),
                },
            }
        );
        let output = parse_agy_line(
            1,
            r#"{"event":"terminal_output","terminalId":"term-1","output":"ok\n"}"#,
        )
        .expect("valid native terminal")
        .expect("terminal event");
        assert_eq!(
            output,
            AgentEvent::Terminal {
                slot: 1,
                event: TerminalEvent::Output {
                    id: "term-1".into(),
                    text: "ok\n".into(),
                },
            }
        );
        let released = parse_agy_line(1, r#"{"event":"terminal_released","terminalId":"term-1"}"#)
            .expect("valid native release")
            .expect("terminal event");
        assert!(matches!(
            released,
            AgentEvent::Terminal {
                event: TerminalEvent::Released { id },
                ..
            } if id == "term-1"
        ));
    }

    #[test]
    fn parses_acp_permission_requests() {
        let event = parse_acp_notification(
            0,
            r#"{"method":"session/update","params":{"update":{"sessionUpdate":"request_permission","toolCall":{"toolCallId":"t1","title":"Write file"},"options":[{"name":"Allow once"},{"optionId":"Reject"}]}}}"#,
        )
        .expect("valid permission")
        .expect("permission event");
        assert!(matches!(
            event,
            AgentEvent::Permission { request, .. }
                if request.id == "t1"
                    && request.title == "Write file"
                    && request.options == ["Allow once", "Reject"]
        ));
    }

    #[test]
    fn parses_acp_permission_request_as_json_rpc_request() {
        let event = parse_acp_notification(
            2,
            r#"{"jsonrpc":"2.0","id":17,"method":"session/request_permission","params":{"sessionId":"s1","toolCall":{"title":"Write file"},"options":[{"optionId":"allow-once"},{"name":"reject"}]}}"#,
        )
        .expect("valid permission request")
        .expect("permission event");
        assert!(matches!(
            event,
            AgentEvent::Permission { request, .. }
                if request.id == "17"
                    && request.title == "Write file"
                    && request.options == ["allow-once", "reject"]
        ));
    }

    #[tokio::test]
    async fn native_adapter_explicitly_rejects_permission_answers() {
        let mut adapter = AgyAdapter::new(0, std::env::current_dir().expect("cwd"), "agy");
        assert_eq!(
            adapter
                .answer_permission(
                    "request".into(),
                    PermissionAnswer::Selected {
                        option_id: "allow".into()
                    },
                )
                .await,
            Err(super::AdapterError::Unsupported("permission answer"))
        );
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

    #[tokio::test]
    async fn acp_adapter_loads_existing_session_when_capability_allows_it() {
        let script = r#"read _; echo '{"jsonrpc":"2.0","id":1,"result":{"agentCapabilities":{"loadSession":true}}}'; read _; echo '{"jsonrpc":"2.0","id":2,"result":{}}'"#;
        let cwd = std::env::current_dir().expect("cwd");
        let mut adapter = AcpAdapter::with_session_id(
            0,
            cwd,
            "sh",
            vec!["-c".into(), script.into()],
            "existing-session",
        );
        adapter.start().await.expect("load existing session");
        assert!(matches!(
            adapter.next_event().await,
            Some(Ok(AgentEvent::Ready { .. }))
        ));
    }

    #[tokio::test]
    async fn acp_adapter_answers_permission_json_rpc_requests() {
        let path = std::env::temp_dir().join(format!(
            "codeswarm-permission-answer-{}",
            std::process::id()
        ));
        let script = format!(
            r#"read _; echo '{{"jsonrpc":"2.0","id":1,"result":{{"agentCapabilities":{{}}}}}}'; read _; echo '{{"jsonrpc":"2.0","id":2,"result":{{"sessionId":"s1"}}}}'; read _; echo '{{"jsonrpc":"2.0","id":9,"method":"session/request_permission","params":{{"toolCall":{{"title":"Write file"}},"options":[{{"optionId":"allow-once"}}]}}}}'; read answer; printf '%s' "$answer" > '{}'; echo '{{"jsonrpc":"2.0","id":4,"result":{{"stopReason":"end_turn"}}}}'"#,
            path.display()
        );
        let mut adapter = AcpAdapter::new(
            0,
            std::env::current_dir().expect("cwd"),
            "sh",
            vec!["-c".into(), script],
        );
        adapter.start().await.expect("start ACP");
        assert!(matches!(
            adapter.next_event().await,
            Some(Ok(AgentEvent::Ready { .. }))
        ));
        adapter.send_prompt("do it".into()).await.expect("prompt");
        assert!(matches!(
            adapter.next_event().await,
            Some(Ok(AgentEvent::Permission { request, .. }))
                if request.id == "9"
        ));
        adapter
            .answer_permission(
                "9".into(),
                PermissionAnswer::Selected {
                    option_id: "allow-once".into(),
                },
            )
            .await
            .expect("permission answer");
        assert!(matches!(
            adapter.next_event().await,
            Some(Ok(AgentEvent::TurnComplete { .. }))
        ));
        let answer: Value = serde_json::from_str(
            &std::fs::read_to_string(&path).expect("captured permission answer"),
        )
        .expect("valid JSON-RPC answer");
        assert_eq!(answer["id"], 9);
        assert_eq!(answer["result"]["outcome"]["outcome"], "selected");
        assert_eq!(answer["result"]["outcome"]["optionId"], "allow-once");
        std::fs::remove_file(path).expect("cleanup");
    }

    #[tokio::test]
    async fn host_reduces_and_persists_adapter_events() {
        let path =
            std::env::temp_dir().join(format!("codeswarm-host-{}.jsonl", std::process::id()));
        let adapter = ScriptedAdapter::new(
            0,
            AgentCapabilities::default(),
            [AgentEvent::Text {
                slot: 0,
                text: "hello".into(),
            }],
        );
        let mut host = AdapterHost::new(Box::new(adapter), Some(EventLog::open(&path)));
        host.start().await.expect("start");
        host.next_effects()
            .await
            .expect("event")
            .expect("valid event");
        assert_eq!(host.state.public_text[0].1, "hello");
        assert_eq!(EventLog::open(&path).read().expect("read").len(), 1);
        std::fs::remove_file(path).expect("cleanup");
    }

    #[tokio::test]
    async fn relay_host_dispatches_turns_sequentially() {
        let capabilities = AgentCapabilities {
            supports_cancel: true,
            ..AgentCapabilities::default()
        };
        let first = ScriptedAdapter::new(
            0,
            capabilities.clone(),
            [
                AgentEvent::Text {
                    slot: 0,
                    text: "first".into(),
                },
                AgentEvent::TurnComplete { slot: 0 },
            ],
        );
        let second = ScriptedAdapter::new(
            1,
            capabilities,
            [
                AgentEvent::Text {
                    slot: 1,
                    text: "review".into(),
                },
                AgentEvent::TurnComplete { slot: 1 },
            ],
        );
        let hosts = vec![
            AdapterHost::new(Box::new(first), None),
            AdapterHost::new(Box::new(second), None),
        ];
        let mut relay = super::RelayHost::new(hosts, 4).expect("relay");
        relay.start().await.expect("start");
        assert!(matches!(
            relay.run_turn("task", 0).await.expect("first turn"),
            codeswarm_core::relay::RelayDecision::Dispatch { slot: 0, .. }
        ));
        assert!(matches!(
            relay.run_turn("first", 0).await.expect("second turn"),
            codeswarm_core::relay::RelayDecision::Dispatch {
                slot: 1,
                can_stop: true,
                ..
            }
        ));
        assert_eq!(
            relay
                .dispatches()
                .iter()
                .map(|(slot, _)| *slot)
                .collect::<Vec<_>>(),
            [0, 1]
        );
    }

    #[tokio::test]
    async fn relay_cancellation_interrupts_a_waiting_adapter_turn() {
        let first = AdapterHost::new(Box::new(PendingAdapter { slot: 0 }), None);
        let second = AdapterHost::new(Box::new(PendingAdapter { slot: 1 }), None);
        let mut relay = super::RelayHost::new(vec![first, second], 4).expect("relay");
        relay.start().await.expect("start");
        let cancellation = relay.cancellation();
        let turn = relay.run_turn("task", 0);
        tokio::pin!(turn);
        cancellation.request();
        let error = turn.await.expect_err("cancellation should stop turn");
        assert!(error.to_string().contains("relay turn cancelled"));
    }

    #[tokio::test]
    async fn relay_host_pause_and_single_agent_collapse_without_dispatching() {
        let event = [AgentEvent::TurnComplete { slot: 0 }];
        let first = AdapterHost::new(
            Box::new(ScriptedAdapter::new(
                0,
                AgentCapabilities::default(),
                event.clone(),
            )),
            None,
        );
        let second = AdapterHost::new(
            Box::new(ScriptedAdapter::new(
                1,
                AgentCapabilities::default(),
                [AgentEvent::TurnComplete { slot: 1 }],
            )),
            None,
        );
        let mut relay = super::RelayHost::new(vec![first, second], 4).expect("relay");
        relay.start().await.expect("start");

        relay.pause();
        assert_eq!(
            relay.run_turn("paused", 0).await.expect("paused turn"),
            codeswarm_core::relay::RelayDecision::Paused
        );
        assert!(relay.dispatches().is_empty());

        relay.resume();
        relay.relay_mut().drop_agent(1).expect("drop reviewer");
        assert_eq!(
            relay
                .run_turn("collapsed", 0)
                .await
                .expect("collapsed turn"),
            codeswarm_core::relay::RelayDecision::Collapsed
        );
        assert!(relay.dispatches().is_empty());
    }

    #[tokio::test]
    async fn relay_host_routes_unseen_public_context_to_next_agent() {
        let first = AdapterHost::new(
            Box::new(ScriptedAdapter::new(
                0,
                AgentCapabilities::default(),
                [
                    AgentEvent::Text {
                        slot: 0,
                        text: "implemented the fix".into(),
                    },
                    AgentEvent::TurnComplete { slot: 0 },
                ],
            )),
            None,
        );
        let second = AdapterHost::new(
            Box::new(ScriptedAdapter::new(
                1,
                AgentCapabilities::default(),
                [AgentEvent::TurnComplete { slot: 1 }],
            )),
            None,
        );
        let mut relay = super::RelayHost::new(vec![first, second], 4).expect("relay");
        relay.start().await.expect("start");
        relay.run_turn("task", 0).await.expect("first turn");
        relay.run_turn("review this", 0).await.expect("review turn");

        assert_eq!(relay.dispatches().len(), 2);
        assert_eq!(relay.dispatches()[0], (0, "task".into()));
        assert_eq!(relay.dispatches()[1].0, 1);
        assert!(relay.dispatches()[1].1.contains("review this"));
        assert!(
            relay.dispatches()[1]
                .1
                .contains("Agent 0:\nimplemented the fix")
        );
    }
}
