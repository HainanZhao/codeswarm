//! Low-churn Ratatui rendering over the viewport transcript model.
//!
//! The renderer is intentionally stateless with respect to historical rows:
//! scrolling asks the transcript for a small cached slice and draws that slice.

use codeswarm_transcript::{RenderRow, Transcript};
use ratatui::{
    Frame,
    buffer::Buffer,
    layout::{Constraint, Layout, Rect},
    style::{Color, Style},
    text::Line,
    widgets::{Block, Borders, Paragraph, Widget},
};

#[derive(Debug)]
pub struct App {
    pub transcript: Transcript,
    pub scroll_y: usize,
    pub follow_tail: bool,
    pub prompt: String,
    pub active_agent: String,
    pub status: String,
}

impl Default for App {
    fn default() -> Self {
        Self {
            transcript: Transcript::default(),
            scroll_y: 0,
            follow_tail: true,
            prompt: String::new(),
            active_agent: "Initializing".into(),
            status: "idle".into(),
        }
    }
}

impl App {
    pub fn scroll_by(&mut self, delta: isize, width: usize, height: usize) {
        let max_scroll = self
            .transcript
            .row_count(width.saturating_sub(2))
            .saturating_sub(height);
        self.scroll_y = self.scroll_y.saturating_add_signed(delta).min(max_scroll);
        self.follow_tail = self.scroll_y == max_scroll;
    }

    pub fn follow_tail(&mut self, width: usize, height: usize) {
        self.scroll_y = self
            .transcript
            .row_count(width.saturating_sub(2))
            .saturating_sub(height);
        self.follow_tail = true;
    }
}

pub fn render(frame: &mut Frame, app: &mut App) {
    let area = frame.area();
    let rows = Layout::vertical([
        Constraint::Min(1),
        Constraint::Length(1),
        Constraint::Length(3),
    ])
    .split(area);
    let content_width = rows[0].width.saturating_sub(2) as usize;
    let content_height = rows[0].height as usize;
    if app.follow_tail {
        app.follow_tail(content_width, content_height);
    }
    let visible = app
        .transcript
        .viewport(content_width, app.scroll_y, content_height, 4);
    render_transcript(frame.buffer_mut(), rows[0], visible);

    let status = format!(
        " {} · {} · {}{}",
        app.active_agent,
        app.status,
        if app.follow_tail {
            "following"
        } else {
            "scrolling"
        },
        if app.follow_tail {
            ""
        } else {
            " · End to follow"
        },
    );
    Paragraph::new(status)
        .style(Style::default().fg(Color::DarkGray))
        .render(rows[1], frame.buffer_mut());
    Paragraph::new(format!("> {}", app.prompt))
        .block(Block::default().borders(Borders::TOP))
        .render(rows[2], frame.buffer_mut());
}

fn render_transcript(buffer: &mut Buffer, area: Rect, rows: Vec<RenderRow>) {
    let lines = rows
        .into_iter()
        .map(|row| Line::raw(row.text))
        .collect::<Vec<_>>();
    Paragraph::new(lines)
        .block(Block::default().borders(Borders::LEFT))
        .render(area, buffer);
}

#[cfg(test)]
mod tests {
    use codeswarm_transcript::BlockKind;
    use ratatui::{Terminal, backend::TestBackend};

    use super::{App, render};

    #[test]
    fn long_history_draws_only_a_terminal_viewport() {
        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App::default();
        app.transcript.append(
            BlockKind::Agent,
            (0..5_000)
                .map(|n| format!("word{n}"))
                .collect::<Vec<_>>()
                .join(" "),
            false,
        );
        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("draw");
        let rendered = terminal.backend().buffer();
        assert!(rendered.content().iter().any(|cell| cell.symbol() == "w"));
    }
}
