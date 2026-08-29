use std::{
    collections::VecDeque,
    io::stdout,
    path::PathBuf,
    sync::mpsc::{self, Receiver, Sender},
    thread,
    time::Duration,
};

use codeswarm_adapters::{
    AcpAdapter, AdapterError, AdapterHost, AdapterResult, AgentAdapter, AgyAdapter, RelayHost,
};
use codeswarm_core::PermissionAnswer;
use codeswarm_core::launcher::{LaunchDecision, launch_decision};
use codeswarm_core::{AgentEvent, EventLog};
use codeswarm_transcript::{BlockKind, fixtures};
use codeswarm_tui::{
    App, Input, LocalCommand, PermissionAction, PermissionKey, PromptAction, QueuedPrompt, render,
};
use crossterm::{
    event::{self, Event, KeyCode, KeyEventKind, KeyModifiers},
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use ratatui::{Terminal, TerminalOptions, Viewport, backend::CrosstermBackend};

#[derive(Debug)]
enum AdapterControl {
    Prompt(String),
    Queue {
        slot: usize,
        prompt: String,
    },
    Direct {
        slot: usize,
        prompt: String,
    },
    Permission {
        slot: usize,
        request_id: String,
        answer: PermissionAnswer,
    },
    Pause,
    Resume,
    Cancel,
    Stop,
}

fn control_for_queued(prompt: &QueuedPrompt) -> Option<AdapterControl> {
    if prompt.direct {
        return Some(AdapterControl::Direct {
            slot: prompt.target?,
            prompt: prompt.prompt.clone(),
        });
    }
    Some(match prompt.target {
        Some(slot) => AdapterControl::Queue {
            slot,
            prompt: prompt.prompt.clone(),
        },
        None => AdapterControl::Prompt(prompt.prompt.clone()),
    })
}

fn dispatch_queued_prompt(
    controls: Option<&tokio::sync::mpsc::UnboundedSender<AdapterControl>>,
    prompt: &QueuedPrompt,
) -> bool {
    let Some(control) = control_for_queued(prompt) else {
        return false;
    };
    controls.is_some_and(|controls| controls.send(control).is_ok())
}

fn dispatch_permission_action(
    controls: Option<&tokio::sync::mpsc::UnboundedSender<AdapterControl>>,
    action: PermissionAction,
) -> bool {
    let command = match action {
        PermissionAction::Answer {
            slot,
            request_id,
            option,
            ..
        } => AdapterControl::Permission {
            slot,
            request_id,
            answer: PermissionAnswer::Selected { option_id: option },
        },
        PermissionAction::Cancel { slot, request_id } => AdapterControl::Permission {
            slot,
            request_id,
            answer: PermissionAnswer::Cancelled,
        },
        PermissionAction::Ignored | PermissionAction::SelectionChanged { .. } => return false,
    };
    controls.is_some_and(|controls| controls.send(command).is_ok())
}

enum Launch {
    Preview,
    Store,
    Agy {
        prompt: String,
    },
    Acp {
        program: String,
        prompt: String,
    },
    Roster {
        specs: Vec<AgentSpec>,
        prompt: Option<String>,
        first_slot: usize,
        max_rounds: usize,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum AgentSpec {
    Agy(String),
    Acp(String),
}

fn main() -> std::io::Result<()> {
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    let alternate_screen = arguments.iter().any(|argument| argument == "--alt-screen");
    let launch = parse_launch(&arguments).or_else(|| arguments.is_empty().then(bare_launch));
    let Some(launch) = launch else {
        println!(
            "CodeSwarm Rust preview. Use --demo, --agy PROMPT, --acp PROGRAM PROMPT, or repeated --roster agy:COMMAND/acp:PROGRAM PROMPT."
        );
        return Ok(());
    };

    enable_raw_mode()?;
    let mut output = stdout();
    if alternate_screen {
        execute!(output, EnterAlternateScreen)?;
    }
    let backend = CrosstermBackend::new(output);
    let viewport = if alternate_screen {
        Viewport::Fullscreen
    } else {
        Viewport::Inline(24)
    };
    let mut terminal = Terminal::with_options(backend, TerminalOptions { viewport })?;
    let result = match launch {
        Launch::Preview => run_preview(&mut terminal),
        Launch::Store => run_store(&mut terminal),
        Launch::Agy { prompt } => run_agy(&mut terminal, prompt),
        Launch::Acp { program, prompt } => run_acp(&mut terminal, program, prompt),
        Launch::Roster {
            specs,
            prompt,
            first_slot,
            max_rounds,
        } => run_roster(&mut terminal, specs, prompt, first_slot, max_rounds),
    };
    disable_raw_mode()?;
    if alternate_screen {
        execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    }
    terminal.show_cursor()?;
    result
}

fn parse_launch(arguments: &[String]) -> Option<Launch> {
    if arguments.iter().any(|argument| argument == "--demo") {
        return Some(Launch::Preview);
    }
    if let Some(index) = arguments.iter().position(|argument| argument == "--agy")
        && let Some(prompt) = arguments
            .get(index + 1)
            .filter(|prompt| !prompt.starts_with('-'))
            .cloned()
    {
        return Some(Launch::Agy { prompt });
    }
    if arguments.iter().any(|argument| argument == "--roster") {
        return parse_roster_launch(arguments);
    }
    let index = arguments.iter().position(|argument| argument == "--acp")?;
    let program = arguments.get(index + 1)?.clone();
    let prompt = arguments.get(index + 2)?.clone();
    Some(Launch::Acp { program, prompt })
}

fn bare_launch() -> Launch {
    let settings = settings_path()
        .and_then(|path| std::fs::read_to_string(path).ok())
        .unwrap_or_default();
    bare_launch_from_settings(&settings)
}

fn bare_launch_from_settings(settings: &str) -> Launch {
    let catalog = agent_catalog();
    let identities = catalog
        .iter()
        .map(|(identity, _)| (*identity).to_owned())
        .collect::<Vec<_>>();
    match launch_decision(settings, &identities) {
        LaunchDecision::Restore { identities } => {
            let specs = identities
                .iter()
                .filter_map(|identity| {
                    catalog
                        .iter()
                        .find(|(candidate, _)| candidate.eq_ignore_ascii_case(identity))
                        .map(|(_, spec)| spec.clone())
                })
                .collect::<Vec<_>>();
            if specs.is_empty() {
                Launch::Store
            } else {
                Launch::Roster {
                    specs,
                    prompt: None,
                    first_slot: 0,
                    max_rounds: 100,
                }
            }
        }
        LaunchDecision::OpenStore => Launch::Store,
    }
}

fn settings_path() -> Option<PathBuf> {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))
        .map(|root| root.join("codeswarm").join("codeswarm.json"))
}

fn agent_catalog() -> Vec<(&'static str, AgentSpec)> {
    vec![
        ("antigravity.google.com", AgentSpec::Agy("agy".into())),
        (
            "claude.com",
            AgentSpec::Acp("npx -y @agentclientprotocol/claude-agent-acp".into()),
        ),
        ("geminicli.com", AgentSpec::Acp("gemini --acp".into())),
        (
            "openai.com",
            AgentSpec::Acp("npx -y @agentclientprotocol/codex-acp".into()),
        ),
        ("opencode.ai", AgentSpec::Acp("opencode acp".into())),
        ("qwen.ai", AgentSpec::Acp("qwen --acp".into())),
    ]
}

fn parse_roster_launch(arguments: &[String]) -> Option<Launch> {
    let mut specs = Vec::new();
    let mut prompt = None;
    let mut first_slot = 0;
    let mut max_rounds = 100;
    let mut index = 0;
    while index < arguments.len() {
        match arguments[index].as_str() {
            "--roster" => {
                let value = arguments.get(index + 1)?;
                specs.push(parse_agent_spec(value)?);
                index += 2;
            }
            "--first" => {
                first_slot = arguments.get(index + 1)?.parse().ok()?;
                index += 2;
            }
            "--max-rounds" => {
                max_rounds = arguments.get(index + 1)?.parse().ok()?;
                if max_rounds == 0 {
                    return None;
                }
                index += 2;
            }
            "--alt-screen" | "--demo" => index += 1,
            value if !value.starts_with('-') => {
                if prompt.is_some() {
                    return None;
                }
                prompt = Some(value.to_owned());
                index += 1;
            }
            _ => return None,
        }
    }
    if specs.is_empty() || first_slot >= specs.len() {
        return None;
    }
    Some(Launch::Roster {
        specs,
        prompt: Some(prompt?),
        first_slot,
        max_rounds,
    })
}

fn parse_agent_spec(value: &str) -> Option<AgentSpec> {
    let (kind, command) = value.split_once(':')?;
    if command.is_empty() {
        return None;
    }
    match kind.to_ascii_lowercase().as_str() {
        "agy" | "native" => Some(AgentSpec::Agy(command.to_owned())),
        "acp" => Some(AgentSpec::Acp(command.to_owned())),
        _ => None,
    }
}

fn split_command(command: &str) -> (String, Vec<String>) {
    let mut parts = command.split_whitespace();
    let program = parts.next().unwrap_or_default().to_owned();
    (program, parts.map(ToOwned::to_owned).collect())
}

fn display_agent_name(command: &str) -> String {
    let lower = command.to_ascii_lowercase();
    if lower.contains("claude") {
        "Claude Code".into()
    } else if lower.contains("codex") {
        "Codex CLI".into()
    } else if lower.contains("qwen") {
        "Qwen Code".into()
    } else if lower.contains("gemini") {
        "Gemini CLI".into()
    } else if lower == "agy" || lower.contains("antigravity") {
        "Antigravity CLI".into()
    } else {
        command.into()
    }
}

fn run_store(terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>) -> std::io::Result<()> {
    let mut app = App::default();
    app.set_header("CodeSwarm agent store", "select a roster with --roster");
    app.transcript.append(
        BlockKind::Notice,
        "No usable saved roster was found. Start an explicit roster with repeated --roster flags.",
        false,
    );
    run_terminal(terminal, &mut app, None, None, None)
}

fn run_preview(terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>) -> std::io::Result<()> {
    let mut app = App::default();
    app.set_header("CodeSwarm preview", "press q to quit");
    app.transcript.append(
        BlockKind::Notice,
        "Ratatui preview uses a viewport-only transcript.",
        false,
    );
    app.transcript.append(
        BlockKind::Agent,
        fixtures::five_thousand_word_reply(),
        false,
    );
    run_terminal(terminal, &mut app, None, None, None)
}

fn run_agy(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    prompt: String,
) -> std::io::Result<()> {
    run_agy_command(terminal, Some(prompt), "agy")
}

fn run_agy_command(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    prompt: Option<String>,
    command: &str,
) -> std::io::Result<()> {
    let initial_prompt = prompt.clone();
    let (events, controls) = spawn_agy_command(prompt, command.to_owned());
    let mut app = App::default();
    app.set_agent_name(0, display_agent_name(command));
    app.set_header(command, "starting");
    if let Some(prompt) = initial_prompt {
        app.record_human_message(&prompt, false);
    }
    run_terminal(terminal, &mut app, Some(events), Some(controls), None)
}

fn run_acp(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    program: String,
    prompt: String,
) -> std::io::Result<()> {
    run_acp_program(terminal, program, Some(prompt))
}

fn run_acp_program(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    program: String,
    prompt: Option<String>,
) -> std::io::Result<()> {
    let initial_prompt = prompt.clone();
    let (events, controls) = spawn_acp(program.clone(), prompt);
    let mut app = App::default();
    app.set_agent_name(0, display_agent_name(&program));
    app.set_header(program, "starting");
    if let Some(prompt) = initial_prompt {
        app.record_human_message(&prompt, false);
    }
    run_terminal(terminal, &mut app, Some(events), Some(controls), None)
}

fn run_roster(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    specs: Vec<AgentSpec>,
    prompt: Option<String>,
    first_slot: usize,
    max_rounds: usize,
) -> std::io::Result<()> {
    if specs.len() == 1 {
        return match &specs[0] {
            AgentSpec::Agy(command) => run_agy_command(terminal, prompt, command),
            AgentSpec::Acp(program) => run_acp_program(terminal, program.clone(), prompt),
        };
    }
    let mut app = App::default();
    for (slot, spec) in specs.iter().enumerate() {
        let name = match spec {
            AgentSpec::Agy(command) | AgentSpec::Acp(command) => command,
        };
        app.set_agent_name(slot, display_agent_name(name));
    }
    if let Some(prompt) = prompt.as_ref() {
        app.record_human_message(prompt, false);
    }
    let (events, controls) = spawn_relay(specs, prompt, first_slot, max_rounds);
    app.set_header("CodeSwarm roster", "starting");
    run_terminal(
        terminal,
        &mut app,
        Some(events),
        Some(controls),
        Some(first_slot),
    )
}

fn spawn_agy_command(
    prompt: Option<String>,
    command: String,
) -> (
    Receiver<AdapterResult<AgentEvent>>,
    tokio::sync::mpsc::UnboundedSender<AdapterControl>,
) {
    let (sender, receiver) = mpsc::channel();
    let (controls, control_receiver) = tokio::sync::mpsc::unbounded_channel();
    thread::spawn(move || run_agy_task(sender, control_receiver, prompt, command));
    (receiver, controls)
}

fn run_agy_task(
    sender: Sender<AdapterResult<AgentEvent>>,
    mut controls: tokio::sync::mpsc::UnboundedReceiver<AdapterControl>,
    prompt: Option<String>,
    command: String,
) {
    let runtime = match tokio::runtime::Builder::new_current_thread()
        .enable_io()
        .build()
    {
        Ok(runtime) => runtime,
        Err(error) => {
            let _ = sender.send(Err(codeswarm_adapters::AdapterError::Transport(
                error.to_string(),
            )));
            return;
        }
    };
    runtime.block_on(async move {
        let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let mut adapter = AgyAdapter::new(0, cwd, command);
        if let Err(error) = adapter.start().await {
            let _ = sender.send(Err(error));
            return;
        }
        if let Some(prompt) = prompt
            && let Err(error) = adapter.send_prompt(prompt).await
        {
            let _ = sender.send(Err(error));
            return;
        }
        loop {
            tokio::select! {
                event = adapter.next_event() => match event {
                    Some(event) => {
                        if sender.send(event).is_err() {
                            break;
                        }
                    }
                    None => break,
                },
                command = controls.recv() => match command {
                    Some(AdapterControl::Prompt(prompt)) => {
                        if let Err(error) = adapter.send_prompt(prompt).await {
                            let _ = sender.send(Err(error));
                        }
                    }
                    Some(AdapterControl::Cancel) => {
                        if let Err(error) = adapter.cancel().await {
                            let _ = sender.send(Err(error));
                        }
                    }
                    Some(AdapterControl::Queue { .. })
                    | Some(AdapterControl::Direct { .. })
                    | Some(AdapterControl::Permission { .. })
                    | Some(AdapterControl::Pause)
                    | Some(AdapterControl::Resume) => {}
                    Some(AdapterControl::Stop) | None => break,
                },
            }
        }
        let _ = adapter.stop().await;
    });
}

fn spawn_acp(
    program: String,
    prompt: Option<String>,
) -> (
    Receiver<AdapterResult<AgentEvent>>,
    tokio::sync::mpsc::UnboundedSender<AdapterControl>,
) {
    let (sender, receiver) = mpsc::channel();
    let (controls, control_receiver) = tokio::sync::mpsc::unbounded_channel();
    let (program, args) = split_command(&program);
    thread::spawn(move || run_acp_task(sender, control_receiver, program, args, prompt));
    (receiver, controls)
}

fn run_acp_task(
    sender: Sender<AdapterResult<AgentEvent>>,
    mut controls: tokio::sync::mpsc::UnboundedReceiver<AdapterControl>,
    program: String,
    args: Vec<String>,
    prompt: Option<String>,
) {
    let runtime = match tokio::runtime::Builder::new_current_thread()
        .enable_io()
        .build()
    {
        Ok(runtime) => runtime,
        Err(error) => {
            let _ = sender.send(Err(codeswarm_adapters::AdapterError::Transport(
                error.to_string(),
            )));
            return;
        }
    };
    runtime.block_on(async move {
        let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let mut adapter = AcpAdapter::new(0, cwd, program, args);
        if let Err(error) = adapter.start().await {
            let _ = sender.send(Err(error));
            return;
        }
        if let Some(prompt) = prompt
            && let Err(error) = adapter.send_prompt(prompt).await
        {
            let _ = sender.send(Err(error));
            return;
        }
        loop {
            tokio::select! {
                event = adapter.next_event() => match event {
                    Some(event) => {
                        if sender.send(event).is_err() {
                            break;
                        }
                    }
                    None => break,
                },
                command = controls.recv() => match command {
                    Some(AdapterControl::Prompt(prompt)) => {
                        if let Err(error) = adapter.send_prompt(prompt).await {
                            let _ = sender.send(Err(error));
                        }
                    }
                    Some(AdapterControl::Cancel) => {
                        if let Err(error) = adapter.cancel().await {
                            let _ = sender.send(Err(error));
                        }
                    }
                    Some(AdapterControl::Queue { .. })
                    | Some(AdapterControl::Direct { .. })
                    | Some(AdapterControl::Permission { .. })
                    | Some(AdapterControl::Pause)
                    | Some(AdapterControl::Resume) => {}
                    Some(AdapterControl::Stop) | None => break,
                },
            }
        }
        let _ = adapter.stop().await;
    });
}

fn spawn_relay(
    specs: Vec<AgentSpec>,
    prompt: Option<String>,
    first_slot: usize,
    max_rounds: usize,
) -> (
    Receiver<AdapterResult<AgentEvent>>,
    tokio::sync::mpsc::UnboundedSender<AdapterControl>,
) {
    let (sender, receiver) = mpsc::channel();
    let (controls, control_receiver) = tokio::sync::mpsc::unbounded_channel();
    thread::spawn(move || {
        run_relay_task(
            sender,
            control_receiver,
            specs,
            prompt,
            first_slot,
            max_rounds,
        )
    });
    (receiver, controls)
}

async fn run_relay_turn_with_controls(
    relay: &mut RelayHost,
    controls: &mut tokio::sync::mpsc::UnboundedReceiver<AdapterControl>,
    sender: &Sender<AdapterResult<AgentEvent>>,
    task: String,
    first_slot: usize,
) -> (bool, Vec<AdapterControl>) {
    let cancellation = relay.cancellation();
    let turn = relay.run_turn(task, first_slot);
    tokio::pin!(turn);
    let mut deferred = Vec::new();
    let mut stopping = false;
    let result = loop {
        tokio::select! {
            result = &mut turn => break result,
            command = controls.recv(), if !stopping => match command {
                Some(AdapterControl::Cancel) => cancellation.request(),
                Some(AdapterControl::Stop) | None => {
                    stopping = true;
                    cancellation.request();
                }
                Some(command) => deferred.push(command),
            },
        }
    };
    if let Err(error) = result {
        let _ = sender.send(Err(error));
    }
    (stopping, deferred)
}

fn run_relay_task(
    sender: Sender<AdapterResult<AgentEvent>>,
    mut controls: tokio::sync::mpsc::UnboundedReceiver<AdapterControl>,
    specs: Vec<AgentSpec>,
    prompt: Option<String>,
    first_slot: usize,
    max_rounds: usize,
) {
    let runtime = match tokio::runtime::Builder::new_current_thread()
        .enable_io()
        .build()
    {
        Ok(runtime) => runtime,
        Err(error) => {
            let _ = sender.send(Err(AdapterError::Transport(error.to_string())));
            return;
        }
    };
    runtime.block_on(async move {
        let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let hosts = specs
            .into_iter()
            .enumerate()
            .map(|(slot, spec)| {
                let adapter: Box<dyn AgentAdapter> = match spec {
                    AgentSpec::Agy(command) => {
                        Box::new(AgyAdapter::new(slot, cwd.clone(), command))
                    }
                    AgentSpec::Acp(command) => {
                        let (program, args) = split_command(&command);
                        Box::new(AcpAdapter::new(slot, cwd.clone(), program, args))
                    }
                };
                AdapterHost::new(adapter, None)
            })
            .collect();
        let mut relay = match RelayHost::new(hosts, max_rounds) {
            Ok(relay) => relay,
            Err(error) => {
                let _ = sender.send(Err(error));
                return;
            }
        };
        let event_sender = sender.clone();
        relay.set_event_sink(move |event| {
            let _ = event_sender.send(Ok(event));
        });
        if let Err(error) = relay.start().await {
            let _ = sender.send(Err(error));
            return;
        }
        let mut pending_commands = VecDeque::new();
        if let Some(prompt) = prompt {
            let (stopping, deferred) = run_relay_turn_with_controls(
                &mut relay,
                &mut controls,
                &sender,
                prompt,
                first_slot,
            )
            .await;
            if stopping {
                let _ = relay.stop().await;
                return;
            }
            pending_commands.extend(deferred);
        }
        loop {
            let command = match pending_commands.pop_front() {
                Some(command) => Some(command),
                None => controls.recv().await,
            };
            match command {
                Some(AdapterControl::Prompt(prompt)) => {
                    let selected = first_slot;
                    if !relay.relay_mut().enqueue_human(prompt, Some(selected)) {
                        let _ = sender.send(Err(AdapterError::Transport(
                            "unable to queue prompt for roster".into(),
                        )));
                        continue;
                    }
                    let (stopping, deferred) = run_relay_turn_with_controls(
                        &mut relay,
                        &mut controls,
                        &sender,
                        "".into(),
                        selected,
                    )
                    .await;
                    pending_commands.extend(deferred);
                    if stopping {
                        break;
                    }
                }
                Some(AdapterControl::Queue { slot, prompt }) => {
                    if !relay.relay_mut().enqueue_human(prompt, Some(slot)) {
                        let _ = sender.send(Err(AdapterError::Transport(
                            "unable to queue prompt for selected agent".into(),
                        )));
                        continue;
                    }
                    let (stopping, deferred) = run_relay_turn_with_controls(
                        &mut relay,
                        &mut controls,
                        &sender,
                        "".into(),
                        slot,
                    )
                    .await;
                    pending_commands.extend(deferred);
                    if stopping {
                        break;
                    }
                }
                Some(AdapterControl::Direct { slot, prompt }) => {
                    match relay.relay_mut().enqueue_direct(slot, prompt) {
                        Ok(true) => {}
                        Ok(false) => {
                            let _ = sender.send(Err(AdapterError::Transport(
                                "unable to queue direct prompt".into(),
                            )));
                            continue;
                        }
                        Err(error) => {
                            let _ = sender.send(Err(AdapterError::Transport(error.into())));
                            continue;
                        }
                    }
                    let (stopping, deferred) = run_relay_turn_with_controls(
                        &mut relay,
                        &mut controls,
                        &sender,
                        "".into(),
                        slot,
                    )
                    .await;
                    pending_commands.extend(deferred);
                    if stopping {
                        break;
                    }
                }
                Some(AdapterControl::Permission {
                    slot,
                    request_id,
                    answer,
                }) => {
                    if let Err(error) = relay.answer_permission(slot, request_id, answer).await {
                        let _ = sender.send(Err(error));
                    }
                }
                Some(AdapterControl::Pause) => relay.pause(),
                Some(AdapterControl::Resume) => relay.resume(),
                Some(AdapterControl::Cancel) => {
                    let _ = sender.send(Err(AdapterError::Unsupported("no active relay turn")));
                }
                Some(AdapterControl::Stop) | None => break,
            }
        }
        let _ = relay.stop().await;
    });
}

fn run_terminal(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    app: &mut App,
    events: Option<Receiver<AdapterResult<AgentEvent>>>,
    controls: Option<tokio::sync::mpsc::UnboundedSender<AdapterControl>>,
    selected_slot: Option<usize>,
) -> std::io::Result<()> {
    app.set_prompt_completions([
        "/cancel", "/clear", "/close", "/config", "/exit", "/help", "/pause", "/quit", "/resume",
    ]);
    let mut selected_slot = selected_slot;
    let event_log = event_log().ok();
    let mut pending_permission: Option<(usize, String)> = None;
    let mut turn_active = false;
    loop {
        if let Some(events) = &events {
            while let Ok(event) = events.try_recv() {
                match event {
                    Ok(event) => {
                        match &event {
                            AgentEvent::Text { .. }
                            | AgentEvent::Thought { .. }
                            | AgentEvent::Tool { .. }
                            | AgentEvent::Permission { .. }
                            | AgentEvent::Terminal { .. } => turn_active = true,
                            AgentEvent::TurnComplete { .. } => turn_active = false,
                            AgentEvent::Ready { .. }
                            | AgentEvent::ModesReplaced { .. }
                            | AgentEvent::Failed { .. } => {}
                        }
                        if let AgentEvent::Permission { slot, request } = &event {
                            pending_permission = Some((*slot, request.id.clone()));
                        }
                        if let AgentEvent::TurnComplete { .. } = &event {
                            pending_permission = None;
                        }
                        if let Some(log) = &event_log {
                            let _ = log.append(&event);
                        }
                        app.apply_event(&event);
                        if matches!(&event, AgentEvent::TurnComplete { .. })
                            && let Some(queued) = app.next_queued_prompt().cloned()
                            && dispatch_queued_prompt(controls.as_ref(), &queued)
                        {
                            app.remove_queued_prompt(queued.id);
                            turn_active = true;
                            app.status = "queued prompt dispatched".into();
                        }
                    }
                    Err(error) => {
                        turn_active = false;
                        pending_permission = None;
                        let active_agent = app.active_agent.clone();
                        app.set_header(active_agent, format!("error: {error}"));
                    }
                }
            }
        }
        terminal.draw(|frame| render(frame, app))?;
        if !event::poll(Duration::from_millis(50))? {
            continue;
        }
        if let Event::Key(key) = event::read()? {
            if key.kind != KeyEventKind::Press {
                continue;
            }
            let size = terminal.size()?;
            let interaction_height = size.height.min(24) as usize;
            match key.code {
                KeyCode::Char('q') if controls.is_none() && app.prompt.is_empty() => {
                    if let Some(controls) = &controls {
                        let _ = controls.send(AdapterControl::Stop);
                    }
                    return Ok(());
                }
                KeyCode::Esc if pending_permission.is_none() => {
                    if let Some(controls) = &controls {
                        let _ = controls.send(AdapterControl::Stop);
                    }
                    return Ok(());
                }
                KeyCode::Esc if pending_permission.is_some() => {
                    let action = app.handle_permission_key(PermissionKey::Cancel);
                    if dispatch_permission_action(controls.as_ref(), action) {
                        pending_permission = None;
                    }
                }
                KeyCode::Up if pending_permission.is_some() => {
                    let _ = app.handle_permission_key(PermissionKey::Up);
                }
                KeyCode::Down if pending_permission.is_some() => {
                    let _ = app.handle_permission_key(PermissionKey::Down);
                }
                KeyCode::Enter if pending_permission.is_some() => {
                    let action = app.handle_permission_key(PermissionKey::Confirm);
                    if dispatch_permission_action(controls.as_ref(), action) {
                        pending_permission = None;
                    }
                }
                KeyCode::Char('k') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                    if app.cancel_selected_queued().is_some() {
                        app.status = "queued prompt cancelled".into();
                    } else {
                        app.status = "queue empty".into();
                    }
                }
                KeyCode::Up if key.modifiers.contains(KeyModifiers::ALT) => {
                    if app.move_queue_selection(-1).is_some() {
                        app.status = "selected previous queued prompt".into();
                    }
                }
                KeyCode::Down if key.modifiers.contains(KeyModifiers::ALT) => {
                    if app.move_queue_selection(1).is_some() {
                        app.status = "selected next queued prompt".into();
                    }
                }
                KeyCode::Down => {
                    if matches!(
                        app.handle_prompt_input(Input::from(key)),
                        PromptAction::Ignored
                    ) {
                        app.scroll_by(
                            1,
                            size.width as usize,
                            app.content_height(interaction_height),
                        );
                    }
                }
                KeyCode::Up => {
                    if matches!(
                        app.handle_prompt_input(Input::from(key)),
                        PromptAction::Ignored
                    ) {
                        app.scroll_by(
                            -1,
                            size.width as usize,
                            app.content_height(interaction_height),
                        );
                    }
                }
                KeyCode::End => {
                    app.follow_tail(size.width as usize, app.content_height(interaction_height))
                }
                KeyCode::Tab => {
                    if app.prompt.trim_start().starts_with('/') {
                        if let PromptAction::Completion { index, total, .. } =
                            app.handle_prompt_input(Input::from(key))
                        {
                            app.status = format!("command completion {}/{}", index + 1, total);
                        }
                    } else if app.toggle_focused_detail().is_some() {
                        app.status = "detail toggled".into();
                    }
                }
                KeyCode::Char('?') if app.prompt.is_empty() => {
                    let visible = app.toggle_keyboard_help();
                    app.status = if visible {
                        "keyboard help shown".into()
                    } else {
                        "keyboard help hidden".into()
                    };
                }
                KeyCode::F(1) => {
                    let visible = app.toggle_keyboard_help();
                    app.status = if visible {
                        "keyboard help shown".into()
                    } else {
                        "keyboard help hidden".into()
                    };
                }
                KeyCode::Char(character)
                    if selected_slot.is_some()
                        && character.is_ascii_digit()
                        && key.modifiers.contains(KeyModifiers::ALT) =>
                {
                    let slot = character.to_digit(10).unwrap_or_default() as usize;
                    if slot > 0 {
                        selected_slot = Some(slot - 1);
                        app.status = format!("selected agent {}", slot - 1);
                    }
                }
                KeyCode::Enter
                    if selected_slot.is_some()
                        && key.modifiers.contains(KeyModifiers::CONTROL)
                        && !app.prompt.trim().is_empty() =>
                {
                    if let Some(controls) = &controls {
                        let prompt = app.prompt.clone();
                        let slot = selected_slot.expect("guarded selected slot");
                        app.record_human_message(&prompt, true);
                        if turn_active {
                            if app.queue_prompt(prompt, Some(slot), true).is_some() {
                                let _ = app.take_prompt();
                                app.status = "direct prompt queued".into();
                            } else {
                                app.status = "queue full or prompt empty".into();
                            }
                        } else if controls
                            .send(AdapterControl::Direct {
                                slot,
                                prompt: app.take_prompt(),
                            })
                            .is_ok()
                        {
                            turn_active = true;
                            app.status = "direct turn queued".into();
                        }
                    }
                }
                KeyCode::Enter => {
                    if let PromptAction::Submit(prompt) = app.handle_prompt_input(Input::from(key))
                    {
                        if let Some(local) = app.handle_local_command(&prompt) {
                            match local {
                                LocalCommand::Handled => {}
                                LocalCommand::Close => {
                                    if let Some(controls) = &controls {
                                        let _ = controls.send(AdapterControl::Stop);
                                    }
                                    return Ok(());
                                }
                                LocalCommand::Cancel => {
                                    if let Some(controls) = &controls {
                                        let _ = controls.send(AdapterControl::Cancel);
                                    }
                                    app.status = "cancelling".into();
                                }
                                LocalCommand::Pause => {
                                    if let Some(controls) = &controls {
                                        let _ = controls.send(AdapterControl::Pause);
                                    }
                                    app.status = "relay paused".into();
                                }
                                LocalCommand::Resume => {
                                    if let Some(controls) = &controls {
                                        let _ = controls.send(AdapterControl::Resume);
                                    }
                                    app.status = "relay resumed".into();
                                }
                            }
                        } else if let Some(controls) = &controls {
                            app.record_human_message(&prompt, false);
                            if turn_active {
                                if app.queue_prompt(prompt, selected_slot, false).is_some() {
                                    app.status = "prompt queued".into();
                                } else {
                                    app.status = "queue full or prompt empty".into();
                                }
                            } else {
                                let command = if let Some(slot) = selected_slot {
                                    AdapterControl::Queue { slot, prompt }
                                } else {
                                    AdapterControl::Prompt(prompt)
                                };
                                if controls.send(command).is_ok() {
                                    turn_active = true;
                                    app.status = "queued".into();
                                }
                            }
                        }
                    }
                }
                KeyCode::Char('p')
                    if selected_slot.is_some() && key.modifiers.contains(KeyModifiers::CONTROL) =>
                {
                    if let Some(controls) = &controls {
                        let _ = controls.send(AdapterControl::Pause);
                        app.status = "relay paused".into();
                    }
                }
                KeyCode::Char('r')
                    if selected_slot.is_some() && key.modifiers.contains(KeyModifiers::CONTROL) =>
                {
                    if let Some(controls) = &controls {
                        let _ = controls.send(AdapterControl::Resume);
                        app.status = "relay resumed".into();
                    }
                }
                KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                    if let Some(controls) = &controls {
                        let _ = controls.send(AdapterControl::Cancel);
                        app.status = "cancelling".into();
                    }
                }
                _ => match app.handle_prompt_input(Input::from(key)) {
                    PromptAction::Completion { index, total, .. } => {
                        app.status = format!("command completion {}/{}", index + 1, total);
                    }
                    PromptAction::Changed | PromptAction::Ignored | PromptAction::Submit(_) => {}
                },
            }
        }
    }
}

fn event_log() -> std::io::Result<EventLog> {
    let root = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/state")))
        .unwrap_or_else(|| PathBuf::from(".codeswarm-state"));
    let directory = root.join("codeswarm");
    std::fs::create_dir_all(&directory)?;
    Ok(EventLog::open(directory.join("rust-events.jsonl")))
}

#[cfg(test)]
mod tests {
    use super::{
        AdapterControl, AgentSpec, Launch, bare_launch_from_settings, dispatch_permission_action,
        dispatch_queued_prompt, parse_launch,
    };
    use codeswarm_core::PermissionAnswer;
    use codeswarm_tui::{PermissionAction, QueuedPrompt};

    #[test]
    fn parses_native_agent_prompt_without_treating_it_as_acp() {
        assert!(matches!(
            parse_launch(&["--agy".into(), "summarize".into()]),
            Some(Launch::Agy { prompt }) if prompt == "summarize"
        ));
    }

    #[test]
    fn parses_acp_program_and_prompt() {
        assert!(matches!(
            parse_launch(&["--acp".into(), "codex-acp".into(), "summarize".into()]),
            Some(Launch::Acp { program, prompt }) if program == "codex-acp" && prompt == "summarize"
        ));
    }

    #[test]
    fn parses_repeated_mixed_roster_with_selected_first_and_round_limit() {
        let args = vec![
            "--roster".into(),
            "agy:agy".into(),
            "--roster".into(),
            "acp:codex-acp".into(),
            "--first".into(),
            "1".into(),
            "--max-rounds".into(),
            "12".into(),
            "review the patch".into(),
        ];
        assert!(matches!(
            parse_launch(&args),
            Some(Launch::Roster {
                specs,
                prompt,
                first_slot: 1,
                max_rounds: 12,
            }) if specs == [AgentSpec::Agy("agy".into()), AgentSpec::Acp("codex-acp".into())]
                && prompt == Some("review the patch".into())
        ));
    }

    #[test]
    fn rejects_invalid_roster_kind_or_selected_slot() {
        assert!(parse_launch(&["--roster".into(), "bogus:agent".into(), "task".into()]).is_none());
        assert!(
            parse_launch(&[
                "--roster".into(),
                "agy:agy".into(),
                "--roster".into(),
                "acp:codex".into(),
                "--first".into(),
                "2".into(),
                "task".into(),
            ])
            .is_none()
        );
    }

    #[test]
    fn bare_launch_restores_catalogued_saved_roster() {
        assert!(matches!(
            bare_launch_from_settings(
                r#"{"launcher":{"roster":"OPENAI.COM\nantigravity.google.com"}}"#
            ),
            Launch::Roster { specs, prompt: None, first_slot: 0, max_rounds: 100 }
                if specs == [
                    AgentSpec::Acp("npx -y @agentclientprotocol/codex-acp".into()),
                    AgentSpec::Agy("agy".into())
                ]
        ));
    }

    #[test]
    fn bare_launch_opens_store_for_missing_or_stale_settings() {
        assert!(matches!(bare_launch_from_settings("{}"), Launch::Store));
        assert!(matches!(
            bare_launch_from_settings(r#"{"launcher":{"roster":"removed.ai"}}"#),
            Launch::Store
        ));
    }

    #[tokio::test]
    async fn permission_selection_routes_the_selected_option_to_the_adapter() {
        let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel();
        assert!(dispatch_permission_action(
            Some(&sender),
            PermissionAction::Answer {
                slot: 2,
                request_id: "request-7".into(),
                option_index: 1,
                option: "allow-once".into(),
            }
        ));
        assert!(matches!(
            receiver.recv().await,
            Some(AdapterControl::Permission {
                slot: 2,
                request_id,
                answer: PermissionAnswer::Selected { option_id },
            }) if request_id == "request-7" && option_id == "allow-once"
        ));
    }

    #[test]
    fn queued_direct_prompt_without_target_is_rejected_without_panic() {
        let (sender, _receiver) = tokio::sync::mpsc::unbounded_channel();
        let prompt = QueuedPrompt {
            id: 1,
            prompt: "private check".into(),
            target: None,
            direct: true,
        };
        assert!(!dispatch_queued_prompt(Some(&sender), &prompt));
    }
}
