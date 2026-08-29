use std::{
    io::stdout,
    path::PathBuf,
    sync::mpsc::{self, Receiver, Sender},
    thread,
    time::Duration,
};

use codeswarm_adapters::{AdapterResult, AgentAdapter, AgyAdapter};
use codeswarm_core::AgentEvent;
use codeswarm_transcript::{BlockKind, fixtures};
use codeswarm_tui::{App, render};
use crossterm::{
    event::{self, Event, KeyCode, KeyEventKind},
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use ratatui::{Terminal, TerminalOptions, Viewport, backend::CrosstermBackend};

enum Launch {
    Preview,
    Agy { prompt: String },
}

fn main() -> std::io::Result<()> {
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    let alternate_screen = arguments.iter().any(|argument| argument == "--alt-screen");
    let launch = parse_launch(&arguments);
    let Some(launch) = launch else {
        println!("CodeSwarm Rust preview. Use --demo or --agy PROMPT.");
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
    let index = arguments.iter().position(|argument| argument == "--agy")?;
    let prompt = arguments
        .get(index + 1)
        .filter(|prompt| !prompt.starts_with('-'))?
        .clone();
    Some(Launch::Agy { prompt })
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
    run_terminal(terminal, &mut app, None)
}

fn run_agy(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    prompt: String,
) -> std::io::Result<()> {
    let events = spawn_agy(prompt);
    let mut app = App::default();
    app.set_header("Antigravity", "starting");
    run_terminal(terminal, &mut app, Some(events))
}

fn spawn_agy(prompt: String) -> Receiver<AdapterResult<AgentEvent>> {
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || run_agy_task(sender, prompt));
    receiver
}

fn run_agy_task(sender: Sender<AdapterResult<AgentEvent>>, prompt: String) {
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
        while let Some(event) = adapter.next_event().await {
            let complete = matches!(event, Ok(AgentEvent::TurnComplete { .. }));
            if sender.send(event).is_err() || complete {
                break;
            }
        }
        let _ = adapter.stop().await;
    });
}

fn run_terminal(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    app: &mut App,
    events: Option<Receiver<AdapterResult<AgentEvent>>>,
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
                KeyCode::Char('q') | KeyCode::Esc => return Ok(()),
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
}
