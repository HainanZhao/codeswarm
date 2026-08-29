use std::{
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
use codeswarm_core::{AgentEvent, EventLog};
use codeswarm_transcript::{BlockKind, fixtures};
use codeswarm_tui::{App, render};
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

enum Launch {
    Preview,
    Agy {
        prompt: String,
    },
    Acp {
        program: String,
        prompt: String,
    },
    Roster {
        specs: Vec<AgentSpec>,
        prompt: String,
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
    let launch = parse_launch(&arguments);
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
        prompt: prompt?,
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
    run_agy_command(terminal, prompt, "agy")
}

fn run_agy_command(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    prompt: String,
    command: &str,
) -> std::io::Result<()> {
    let (events, controls) = spawn_agy_command(prompt, command.to_owned());
    let mut app = App::default();
    app.set_header(command, "starting");
    run_terminal(terminal, &mut app, Some(events), Some(controls), None)
}

fn run_acp(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    program: String,
    prompt: String,
) -> std::io::Result<()> {
    run_acp_program(terminal, program, prompt)
}

fn run_acp_program(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    program: String,
    prompt: String,
) -> std::io::Result<()> {
    let (events, controls) = spawn_acp(program.clone(), prompt);
    let mut app = App::default();
    app.set_header(program, "starting");
    run_terminal(terminal, &mut app, Some(events), Some(controls), None)
}

fn run_roster(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    specs: Vec<AgentSpec>,
    prompt: String,
    first_slot: usize,
    max_rounds: usize,
) -> std::io::Result<()> {
    if specs.len() == 1 {
        return match &specs[0] {
            AgentSpec::Agy(command) => run_agy_command(terminal, prompt, command),
            AgentSpec::Acp(program) => run_acp_program(terminal, program.clone(), prompt),
        };
    }
    let (events, controls) = spawn_relay(specs, prompt, first_slot, max_rounds);
    let mut app = App::default();
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
    prompt: String,
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
    prompt: String,
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
        if let Err(error) = adapter.send_prompt(prompt).await {
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
    prompt: String,
) -> (
    Receiver<AdapterResult<AgentEvent>>,
    tokio::sync::mpsc::UnboundedSender<AdapterControl>,
) {
    let (sender, receiver) = mpsc::channel();
    let (controls, control_receiver) = tokio::sync::mpsc::unbounded_channel();
    thread::spawn(move || run_acp_task(sender, control_receiver, program, prompt));
    (receiver, controls)
}

fn run_acp_task(
    sender: Sender<AdapterResult<AgentEvent>>,
    mut controls: tokio::sync::mpsc::UnboundedReceiver<AdapterControl>,
    program: String,
    prompt: String,
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
        let mut adapter = AcpAdapter::new(0, cwd, program, Vec::new());
        if let Err(error) = adapter.start().await {
            let _ = sender.send(Err(error));
            return;
        }
        if let Err(error) = adapter.send_prompt(prompt).await {
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
    prompt: String,
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

fn run_relay_task(
    sender: Sender<AdapterResult<AgentEvent>>,
    mut controls: tokio::sync::mpsc::UnboundedReceiver<AdapterControl>,
    specs: Vec<AgentSpec>,
    prompt: String,
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
                    AgentSpec::Acp(program) => {
                        Box::new(AcpAdapter::new(slot, cwd.clone(), program, Vec::new()))
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
        if let Err(error) = relay.run_turn(prompt, first_slot).await {
            let _ = sender.send(Err(error));
            return;
        }
        loop {
            match controls.recv().await {
                Some(AdapterControl::Prompt(prompt)) => {
                    let selected = first_slot;
                    if !relay.relay_mut().enqueue_human(prompt, Some(selected)) {
                        let _ = sender.send(Err(AdapterError::Transport(
                            "unable to queue prompt for roster".into(),
                        )));
                        continue;
                    }
                    if let Err(error) = relay.run_turn("", selected).await {
                        let _ = sender.send(Err(error));
                    }
                }
                Some(AdapterControl::Queue { slot, prompt }) => {
                    if !relay.relay_mut().enqueue_human(prompt, Some(slot)) {
                        let _ = sender.send(Err(AdapterError::Transport(
                            "unable to queue prompt for selected agent".into(),
                        )));
                        continue;
                    }
                    if let Err(error) = relay.run_turn("", slot).await {
                        let _ = sender.send(Err(error));
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
                    if let Err(error) = relay.run_turn("", slot).await {
                        let _ = sender.send(Err(error));
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
                    let _ = sender.send(Err(AdapterError::Unsupported("relay cancellation")));
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
    let mut selected_slot = selected_slot;
    let event_log = event_log().ok();
    let mut pending_permission: Option<(usize, String)> = None;
    loop {
        if let Some(events) = &events {
            while let Ok(event) = events.try_recv() {
                match event {
                    Ok(event) => {
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
                    }
                    Err(error) => app.set_header("Agent", error.to_string()),
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
            match key.code {
                KeyCode::Char('q') | KeyCode::Esc => {
                    if let Some(controls) = &controls {
                        let _ = controls.send(AdapterControl::Stop);
                    }
                    return Ok(());
                }
                KeyCode::Down | KeyCode::Char('j') => app.scroll_by(
                    1,
                    size.width as usize,
                    size.height.saturating_sub(4) as usize,
                ),
                KeyCode::Up | KeyCode::Char('k') => app.scroll_by(
                    -1,
                    size.width as usize,
                    size.height.saturating_sub(4) as usize,
                ),
                KeyCode::End => {
                    app.follow_tail(size.width as usize, size.height.saturating_sub(4) as usize)
                }
                KeyCode::Tab => {
                    if app.toggle_focused_detail().is_some() {
                        app.status = "detail toggled".into();
                    }
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
                KeyCode::Enter if pending_permission.is_some() && !app.prompt.trim().is_empty() => {
                    if let Some(controls) = &controls {
                        let prompt = std::mem::take(&mut app.prompt);
                        let answer = if prompt.eq_ignore_ascii_case("/cancel")
                            || prompt.eq_ignore_ascii_case("/deny")
                        {
                            PermissionAnswer::Cancelled
                        } else {
                            PermissionAnswer::Selected { option_id: prompt }
                        };
                        let (slot, request_id) = pending_permission
                            .take()
                            .expect("permission guard ensures pending request");
                        let _ = controls.send(AdapterControl::Permission {
                            slot,
                            request_id,
                            answer,
                        });
                        app.status = "permission answered".into();
                    }
                }
                KeyCode::Enter
                    if selected_slot.is_some()
                        && key.modifiers.contains(KeyModifiers::CONTROL)
                        && !app.prompt.trim().is_empty() =>
                {
                    if let Some(controls) = &controls {
                        let prompt = std::mem::take(&mut app.prompt);
                        let _ = controls.send(AdapterControl::Direct {
                            slot: selected_slot.expect("guarded selected slot"),
                            prompt,
                        });
                        app.status = "direct turn queued".into();
                    }
                }
                KeyCode::Enter if !app.prompt.trim().is_empty() => {
                    if let Some(controls) = &controls {
                        let prompt = std::mem::take(&mut app.prompt);
                        let command = selected_slot
                            .map_or(AdapterControl::Prompt(prompt.clone()), |slot| {
                                AdapterControl::Queue { slot, prompt }
                            });
                        let _ = controls.send(command);
                        app.status = "queued".into();
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
                KeyCode::Char(character) => app.prompt.push(character),
                KeyCode::Backspace => {
                    app.prompt.pop();
                }
                _ => {}
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
    use super::{AgentSpec, Launch, parse_launch};

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
                && prompt == "review the patch"
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
}
