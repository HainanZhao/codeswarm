//! Low-churn Ratatui rendering over the viewport transcript model.
//!
//! The renderer is intentionally stateless with respect to historical rows:
//! scrolling asks the transcript for a small cached slice and draws that slice.

use std::collections::BTreeMap;

use codeswarm_core::{AgentEvent, TerminalEvent, ToolStatus};
use codeswarm_transcript::{RenderRow, Transcript};
use ratatui::{
    Frame,
    buffer::Buffer,
    layout::{Constraint, Layout, Rect},
    style::{Color, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Paragraph, Widget},
};

pub mod frame_scheduler;

/// Keyboard actions understood by the focused permission prompt.
///
/// The terminal frontend maps its native key events to this small vocabulary,
/// keeping permission state and its tests independent from a particular input
/// backend.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PermissionKey {
    Up,
    Down,
    Confirm,
    Cancel,
}

/// Result of handling one focused permission key.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PermissionAction {
    Ignored,
    SelectionChanged {
        index: usize,
    },
    Answer {
        slot: usize,
        request_id: String,
        option_index: usize,
        option: String,
    },
    Cancel {
        slot: usize,
        request_id: String,
    },
}

/// One pending permission request owned by the TUI.
///
/// The request is intentionally copied from the normalized event. Adapters
/// can replace or reorder their native options without leaking protocol
/// objects into rendering, and the selected index remains deterministic until
/// the user confirms or cancels the request.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PermissionPrompt {
    pub slot: usize,
    pub request_id: String,
    pub title: String,
    pub options: Vec<String>,
    selected: usize,
}

impl PermissionPrompt {
    fn new(
        slot: usize,
        request_id: impl Into<String>,
        title: impl Into<String>,
        options: Vec<String>,
    ) -> Self {
        Self {
            slot,
            request_id: request_id.into(),
            title: title.into(),
            options,
            selected: 0,
        }
    }

    pub fn selected_index(&self) -> usize {
        self.selected
    }

    pub fn selected_option(&self) -> Option<&str> {
        self.options.get(self.selected).map(String::as_str)
    }

    fn move_selection(&mut self, down: bool) -> Option<usize> {
        if self.options.is_empty() {
            return None;
        }
        let previous = self.selected;
        self.selected = if down {
            self.selected.saturating_add(1).min(self.options.len() - 1)
        } else {
            self.selected.saturating_sub(1)
        };
        (self.selected != previous).then_some(self.selected)
    }
}

#[derive(Debug)]
pub struct App {
    pub transcript: Transcript,
    pub scroll_y: usize,
    pub follow_tail: bool,
    pub prompt: String,
    pub active_agent: String,
    pub status: String,
    pub permission: Option<PermissionPrompt>,
    streaming_blocks: BTreeMap<usize, u64>,
    focused_detail: Option<u64>,
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
            permission: None,
            streaming_blocks: BTreeMap::new(),
            focused_detail: None,
        }
    }
}

impl App {
    pub fn set_header(&mut self, active_agent: impl Into<String>, status: impl Into<String>) {
        self.active_agent = active_agent.into();
        self.status = status.into();
    }

    /// Apply normalized adapter state without exposing protocol-specific
    /// objects to the renderer. Text chunks are coalesced into one immutable
    /// transcript block per active agent turn.
    pub fn apply_event(&mut self, event: &AgentEvent) {
        match event {
            AgentEvent::Ready { slot, .. } => {
                self.active_agent = format!("Agent {slot}");
                self.status = "ready".into();
            }
            AgentEvent::ModesReplaced { .. } => {}
            AgentEvent::Text { slot, text } => {
                let block = self.streaming_blocks.get(slot).copied().unwrap_or_else(|| {
                    let id =
                        self.transcript
                            .append(codeswarm_transcript::BlockKind::Agent, "", false);
                    self.streaming_blocks.insert(*slot, id);
                    id
                });
                self.transcript.extend(block, text);
                self.active_agent = format!("Agent {slot}");
                self.status = "streaming".into();
            }
            AgentEvent::Thought { slot, text } => {
                let id = self.transcript.append(
                    codeswarm_transcript::BlockKind::Thought,
                    format!("Agent {slot}: {text}"),
                    true,
                );
                self.focused_detail = Some(id);
            }
            AgentEvent::Tool { slot, update } => {
                let state = match update.status {
                    ToolStatus::Pending => "pending",
                    ToolStatus::Running => "running",
                    ToolStatus::Completed => "completed",
                    ToolStatus::Failed => "failed",
                };
                let id = self.transcript.append(
                    codeswarm_transcript::BlockKind::Tool,
                    format!("Agent {slot}: {} · {state}", update.title),
                    true,
                );
                self.focused_detail = Some(id);
            }
            AgentEvent::Permission { slot, request } => {
                self.active_agent = format!("Agent {slot}");
                self.status = format!("permission: {}", request.title);
                self.permission = Some(PermissionPrompt::new(
                    *slot,
                    request.id.clone(),
                    request.title.clone(),
                    request.options.clone(),
                ));
            }
            AgentEvent::Terminal { slot, event } => {
                let text = match event {
                    TerminalEvent::Created { command, .. } => format!("Agent {slot}: {command}"),
                    TerminalEvent::Output { text, .. } => format!("Agent {slot}: {text}"),
                    TerminalEvent::Exited { code, .. } => format!("Agent {slot}: exited {code}"),
                    TerminalEvent::Released { .. } => format!("Agent {slot}: terminal released"),
                };
                self.focused_detail = Some(self.transcript.append(
                    codeswarm_transcript::BlockKind::Tool,
                    text,
                    true,
                ));
            }
            AgentEvent::TurnComplete { slot } => {
                self.streaming_blocks.remove(slot);
                if self
                    .permission
                    .as_ref()
                    .is_some_and(|request| request.slot == *slot)
                {
                    self.permission = None;
                }
                self.status = "idle".into();
            }
            AgentEvent::Failed {
                slot,
                started,
                detail,
            } => {
                self.streaming_blocks.remove(slot);
                if self
                    .permission
                    .as_ref()
                    .is_some_and(|request| request.slot == *slot)
                {
                    self.permission = None;
                }
                self.active_agent = format!("Agent {slot}");
                self.status = if *started {
                    format!("crashed: {detail}")
                } else {
                    format!("failed to start: {detail}")
                };
            }
        }
    }

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

    pub fn toggle_focused_detail(&mut self) -> Option<bool> {
        self.focused_detail
            .and_then(|id| self.transcript.toggle_collapsed(id))
    }

    /// Handle navigation or a response for the focused permission request.
    ///
    /// `Answer` and `Cancel` clear the pending prompt before returning so a
    /// caller cannot accidentally submit the same decision twice.
    pub fn handle_permission_key(&mut self, key: PermissionKey) -> PermissionAction {
        let Some(request) = self.permission.as_mut() else {
            return PermissionAction::Ignored;
        };
        match key {
            PermissionKey::Up => request
                .move_selection(false)
                .map_or(PermissionAction::Ignored, |index| {
                    PermissionAction::SelectionChanged { index }
                }),
            PermissionKey::Down => request
                .move_selection(true)
                .map_or(PermissionAction::Ignored, |index| {
                    PermissionAction::SelectionChanged { index }
                }),
            PermissionKey::Confirm => {
                let Some(option) = request.selected_option().map(str::to_owned) else {
                    return PermissionAction::Ignored;
                };
                let action = PermissionAction::Answer {
                    slot: request.slot,
                    request_id: request.request_id.clone(),
                    option_index: request.selected,
                    option,
                };
                self.permission = None;
                self.status = "permission answered".into();
                action
            }
            PermissionKey::Cancel => {
                let action = PermissionAction::Cancel {
                    slot: request.slot,
                    request_id: request.request_id.clone(),
                };
                self.permission = None;
                self.status = "permission cancelled".into();
                action
            }
        }
    }
}

pub fn render(frame: &mut Frame, app: &mut App) {
    let area = frame.area();
    let permission_height = app.permission.as_ref().map_or(0, |request| {
        request
            .options
            .len()
            .saturating_add(if request.options.is_empty() { 4 } else { 3 })
            .min(12) as u16
    });
    let rows = Layout::vertical([
        Constraint::Min(1),
        Constraint::Length(1),
        Constraint::Length(permission_height),
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
    if let Some(permission) = &app.permission {
        render_permission(frame.buffer_mut(), rows[2], permission);
    }
    Paragraph::new(format!("> {}", app.prompt))
        .block(Block::default().borders(Borders::TOP))
        .render(rows[3], frame.buffer_mut());
}

fn render_permission(buffer: &mut Buffer, area: Rect, request: &PermissionPrompt) {
    let mut lines = Vec::with_capacity(request.options.len().saturating_add(1));
    lines.push(Line::from(vec![
        Span::styled(" permission: ", Style::default().fg(Color::Yellow)),
        Span::raw(request.title.as_str()),
    ]));
    for (index, option) in request.options.iter().enumerate() {
        let marker = if index == request.selected {
            "▶"
        } else {
            " "
        };
        let style = if index == request.selected {
            Style::default().fg(Color::Yellow)
        } else {
            Style::default()
        };
        lines.push(Line::from(vec![
            Span::styled(format!(" {marker} {}. ", index + 1), style),
            Span::styled(option.as_str(), style),
        ]));
    }
    if request.options.is_empty() {
        lines.push(Line::styled(
            " no options · Esc to cancel",
            Style::default().fg(Color::DarkGray),
        ));
    }
    Paragraph::new(lines)
        .block(Block::default().borders(Borders::TOP | Borders::BOTTOM))
        .render(area, buffer);
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

    use super::{App, PermissionAction, PermissionKey, render};

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

    #[test]
    fn streamed_chunks_extend_one_response_block() {
        let mut app = App::default();
        app.apply_event(&codeswarm_core::AgentEvent::Text {
            slot: 0,
            text: "first ".into(),
        });
        app.apply_event(&codeswarm_core::AgentEvent::Text {
            slot: 0,
            text: "second".into(),
        });
        assert_eq!(app.transcript.len(), 1);
        assert_eq!(
            app.transcript.viewport(80, 0, 10, 0)[0].text,
            "first second"
        );
        app.apply_event(&codeswarm_core::AgentEvent::TurnComplete { slot: 0 });
        app.apply_event(&codeswarm_core::AgentEvent::Text {
            slot: 0,
            text: "next turn".into(),
        });
        assert_eq!(app.transcript.len(), 2);
    }

    #[test]
    fn tool_details_are_lazy_until_explicitly_expanded() {
        let mut app = App::default();
        app.apply_event(&codeswarm_core::AgentEvent::Tool {
            slot: 0,
            update: codeswarm_core::ToolUpdate {
                id: "tool-1".into(),
                title: "Run tests".into(),
                status: codeswarm_core::ToolStatus::Completed,
                detail: Some("large output".into()),
            },
        });
        assert_eq!(app.transcript.row_count(80), 1);
        assert_eq!(app.toggle_focused_detail(), Some(false));
        assert!(app.transcript.row_count(80) >= 1);
    }

    #[test]
    fn permission_selection_returns_stable_request_identity() {
        let mut app = App::default();
        app.apply_event(&codeswarm_core::AgentEvent::Permission {
            slot: 2,
            request: codeswarm_core::PermissionRequest {
                id: "permission-7".into(),
                title: "Write to the workspace".into(),
                options: vec!["Allow once".into(), "Always allow".into(), "Deny".into()],
            },
        });

        assert_eq!(app.permission.as_ref().map(|request| request.slot), Some(2));
        assert_eq!(
            app.handle_permission_key(PermissionKey::Down),
            PermissionAction::SelectionChanged { index: 1 }
        );
        assert_eq!(
            app.handle_permission_key(PermissionKey::Down),
            PermissionAction::SelectionChanged { index: 2 }
        );
        assert_eq!(
            app.handle_permission_key(PermissionKey::Confirm),
            PermissionAction::Answer {
                slot: 2,
                request_id: "permission-7".into(),
                option_index: 2,
                option: "Deny".into(),
            }
        );
        assert!(app.permission.is_none());
    }

    #[test]
    fn replacement_permission_resets_focus_and_cancel_clears_it() {
        let mut app = App::default();
        app.apply_event(&codeswarm_core::AgentEvent::Permission {
            slot: 0,
            request: codeswarm_core::PermissionRequest {
                id: "first".into(),
                title: "First".into(),
                options: vec!["one".into(), "two".into()],
            },
        });
        assert_eq!(
            app.handle_permission_key(PermissionKey::Down),
            PermissionAction::SelectionChanged { index: 1 }
        );
        app.apply_event(&codeswarm_core::AgentEvent::Permission {
            slot: 1,
            request: codeswarm_core::PermissionRequest {
                id: "replacement".into(),
                title: "Replacement".into(),
                options: vec!["only choice".into()],
            },
        });
        assert_eq!(
            app.permission
                .as_ref()
                .map(|request| request.selected_index()),
            Some(0)
        );
        assert_eq!(
            app.handle_permission_key(PermissionKey::Cancel),
            PermissionAction::Cancel {
                slot: 1,
                request_id: "replacement".into(),
            }
        );
        assert!(app.permission.is_none());
    }

    #[test]
    fn permission_prompt_renders_title_and_selected_option() {
        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App::default();
        app.apply_event(&codeswarm_core::AgentEvent::Permission {
            slot: 0,
            request: codeswarm_core::PermissionRequest {
                id: "permission-1".into(),
                title: "Run this command?".into(),
                options: vec!["Allow".into(), "Deny".into()],
            },
        });
        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("draw");
        let rendered = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(rendered.contains("permission: Run this command?"));
        assert!(rendered.contains("▶ 1. Allow"));
        assert!(rendered.contains("  2. Deny"));
    }

    #[test]
    fn permission_without_options_cannot_be_confirmed() {
        let mut app = App::default();
        app.apply_event(&codeswarm_core::AgentEvent::Permission {
            slot: 0,
            request: codeswarm_core::PermissionRequest {
                id: "empty".into(),
                title: "No choices".into(),
                options: Vec::new(),
            },
        });
        assert_eq!(
            app.handle_permission_key(PermissionKey::Confirm),
            PermissionAction::Ignored
        );
        assert!(app.permission.is_some());
    }
}
