use std::io::stdout;

use codeswarm_transcript::{BlockKind, fixtures};
use codeswarm_tui::{App, render};
use crossterm::{
    event::{self, Event, KeyCode, KeyEventKind},
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use ratatui::{Terminal, TerminalOptions, Viewport, backend::CrosstermBackend};

fn main() -> std::io::Result<()> {
    let demo = std::env::args().any(|argument| argument == "--demo");
    let alternate_screen = std::env::args().any(|argument| argument == "--alt-screen");
    if !demo {
        println!("CodeSwarm Rust preview. Run codeswarm --demo for the terminal preview.");
        return Ok(());
    }

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
    let result = run_demo(&mut terminal);
    disable_raw_mode()?;
    if alternate_screen {
        execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    }
    terminal.show_cursor()?;
    result
}

fn run_demo(terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>) -> std::io::Result<()> {
    let mut app = App {
        active_agent: "CodeSwarm preview".into(),
        status: "press q to quit".into(),
        ..App::default()
    };
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

    loop {
        terminal.draw(|frame| render(frame, &mut app))?;
        if let Event::Key(key) = event::read()? {
            if key.kind != KeyEventKind::Press {
                continue;
            }
            let size = terminal.size()?;
            match key.code {
                KeyCode::Char('q') | KeyCode::Esc => return Ok(()),
                KeyCode::Down | KeyCode::Char('j') => {
                    app.scroll_by(
                        1,
                        size.width as usize,
                        size.height.saturating_sub(4) as usize,
                    );
                }
                KeyCode::Up | KeyCode::Char('k') => {
                    app.scroll_by(
                        -1,
                        size.width as usize,
                        size.height.saturating_sub(4) as usize,
                    );
                }
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
