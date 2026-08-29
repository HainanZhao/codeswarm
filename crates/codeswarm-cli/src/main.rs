use std::{
    io::stdout,
    path::PathBuf,
    sync::mpsc::{self, Receiver, Sender},
    thread,
    time::Duration,
};

use codeswarm_adapters::{AcpAdapter, AdapterResult, AgentAdapter, AgyAdapter};
use codeswarm_core::AgentEvent;
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
    Cancel,
    Stop,
}

enum Launch {
    Preview,
    Agy { prompt: String },
    Acp { program: String, prompt: String },
}

fn main() -> std::io::Result<()> {
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    let alternate_screen = arguments.iter().any(|argument| argument == "--alt-screen");
    let launch = parse_launch(&arguments);
    let Some(launch) = launch else {
        println!("CodeSwarm Rust preview. Use --demo, --agy PROMPT, or --acp PROGRAM PROMPT.");
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
    let index = arguments.iter().position(|argument| argument == "--acp")?;
    let program = arguments.get(index + 1)?.clone();
    let prompt = arguments.get(index + 2)?.clone();
    Some(Launch::Acp { program, prompt })
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
    run_terminal(terminal, &mut app, None, None)
}

fn run_agy(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    prompt: String,
) -> std::io::Result<()> {
    let (events, controls) = spawn_agy(prompt);
    let mut app = App::default();
    app.set_header("Antigravity", "starting");
    run_terminal(terminal, &mut app, Some(events), Some(controls))
}

fn run_acp(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    program: String,
    prompt: String,
) -> std::io::Result<()> {
    let (events, controls) = spawn_acp(program.clone(), prompt);
    let mut app = App::default();
    app.set_header(program, "starting");
    run_terminal(terminal, &mut app, Some(events), Some(controls))
}

fn spawn_agy(
    prompt: String,
) -> (
    Receiver<AdapterResult<AgentEvent>>,
    tokio::sync::mpsc::UnboundedSender<AdapterControl>,
) {
    let (sender, receiver) = mpsc::channel();
    let (controls, control_receiver) = tokio::sync::mpsc::unbounded_channel();
    thread::spawn(move || run_agy_task(sender, control_receiver, prompt));
    (receiver, controls)
}

fn run_agy_task(
    sender: Sender<AdapterResult<AgentEvent>>,
    mut controls: tokio::sync::mpsc::UnboundedReceiver<AdapterControl>,
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
        let mut adapter = AgyAdapter::new(0, cwd, "agy");
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
                    Some(AdapterControl::Stop) | None => break,
                },
            }
        }
        let _ = adapter.stop().await;
    });
}

fn run_terminal(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    app: &mut App,
    events: Option<Receiver<AdapterResult<AgentEvent>>>,
    controls: Option<tokio::sync::mpsc::UnboundedSender<AdapterControl>>,
) -> std::io::Result<()> {
    loop {
        if let Some(events) = &events {
            while let Ok(event) = events.try_recv() {
                match event {
                    Ok(event) => app.apply_event(&event),
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
                KeyCode::Enter if !app.prompt.trim().is_empty() => {
                    if let Some(controls) = &controls {
                        let prompt = std::mem::take(&mut app.prompt);
                        let _ = controls.send(AdapterControl::Prompt(prompt));
                        app.status = "queued".into();
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

#[cfg(test)]
mod tests {
    use super::{Launch, parse_launch};

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
}
