//! Low-churn Ratatui rendering over the viewport transcript model.
//!
//! The renderer is intentionally stateless with respect to historical rows:
//! after the transcript cache is warm, scrolling asks for a small cached slice
//! and draws that slice.

use std::collections::{BTreeMap, VecDeque};

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

const MAX_QUEUED_PROMPTS: usize = 100;

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

/// A prompt waiting for the currently active turn to finish.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QueuedPrompt {
    pub id: u64,
    pub prompt: String,
    pub target: Option<usize>,
    pub direct: bool,
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
    queued_prompts: VecDeque<QueuedPrompt>,
    next_queue_id: u64,
    selected_queue: Option<usize>,
    keyboard_help: bool,
    streaming_blocks: BTreeMap<(usize, codeswarm_transcript::BlockKind), u64>,
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
            queued_prompts: VecDeque::new(),
            next_queue_id: 0,
            selected_queue: None,
            keyboard_help: false,
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
    /// objects to the renderer. Text chunks are coalesced into one transcript
    /// block per active agent turn.
    pub fn apply_event(&mut self, event: &AgentEvent) {
        match event {
            AgentEvent::Ready { slot, .. } => {
                self.active_agent = format!("Agent {slot}");
                self.status = "ready".into();
            }
            AgentEvent::ModesReplaced { .. } => {}
            AgentEvent::Text { slot, text } => {
                let key = (*slot, codeswarm_transcript::BlockKind::Agent);
                let block = self.streaming_blocks.get(&key).copied().unwrap_or_else(|| {
                    let id =
                        self.transcript
                            .append(codeswarm_transcript::BlockKind::Agent, "", false);
                    self.streaming_blocks.insert(key, id);
                    id
                });
                self.transcript.extend(block, text);
                self.active_agent = format!("Agent {slot}");
                self.status = "streaming".into();
            }
            AgentEvent::Thought { slot, text } => {
                let key = (*slot, codeswarm_transcript::BlockKind::Thought);
                let id = self.streaming_blocks.get(&key).copied().unwrap_or_else(|| {
                    let id = self.transcript.append(
                        codeswarm_transcript::BlockKind::Thought,
                        format!("Agent {slot}: "),
                        true,
                    );
                    self.streaming_blocks.insert(key, id);
                    id
                });
                self.transcript.extend(id, text);
                self.active_agent = format!("Agent {slot}");
                self.status = "thinking".into();
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
                self.streaming_blocks
                    .remove(&(*slot, codeswarm_transcript::BlockKind::Agent));
                self.streaming_blocks
                    .remove(&(*slot, codeswarm_transcript::BlockKind::Thought));
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
                self.streaming_blocks
                    .remove(&(*slot, codeswarm_transcript::BlockKind::Agent));
                self.streaming_blocks
                    .remove(&(*slot, codeswarm_transcript::BlockKind::Thought));
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

    /// Add a prompt to the local queue while another turn is active.
    ///
    /// The queue is deliberately UI-owned: the CLI can display and cancel a
    /// prompt before it is handed to the relay, while the relay remains the
    /// authority once dispatch begins.
    pub fn queue_prompt(
        &mut self,
        prompt: impl Into<String>,
        target: Option<usize>,
        direct: bool,
    ) -> Option<u64> {
        let prompt = prompt.into();
        if prompt.trim().is_empty() || self.queued_prompts.len() >= MAX_QUEUED_PROMPTS {
            return None;
        }
        let id = self.next_queue_id;
        self.next_queue_id = self.next_queue_id.saturating_add(1);
        self.queued_prompts.push_back(QueuedPrompt {
            id,
            prompt,
            target,
            direct,
        });
        self.selected_queue = Some(self.queued_prompts.len() - 1);
        Some(id)
    }

    pub fn queued_prompts(&self) -> &VecDeque<QueuedPrompt> {
        &self.queued_prompts
    }

    pub fn queued_count(&self) -> usize {
        self.queued_prompts.len()
    }

    pub fn selected_queue_index(&self) -> Option<usize> {
        self.selected_queue
    }

    pub fn next_queued_prompt(&self) -> Option<&QueuedPrompt> {
        self.queued_prompts.front()
    }

    pub fn remove_queued_prompt(&mut self, id: u64) -> Option<QueuedPrompt> {
        let index = self
            .queued_prompts
            .iter()
            .position(|prompt| prompt.id == id)?;
        let removed = self.queued_prompts.remove(index);
        self.selected_queue = match self.queued_prompts.len() {
            0 => None,
            length => Some(self.selected_queue.unwrap_or(0).min(length - 1)),
        };
        removed
    }

    pub fn cancel_selected_queued(&mut self) -> Option<QueuedPrompt> {
        let index = self.selected_queue?;
        let id = self.queued_prompts.get(index)?.id;
        self.remove_queued_prompt(id)
    }

    pub fn move_queue_selection(&mut self, delta: isize) -> Option<usize> {
        if self.queued_prompts.is_empty() {
            return None;
        }
        let current = self.selected_queue.unwrap_or(self.queued_prompts.len() - 1);
        let next = current
            .saturating_add_signed(delta)
            .min(self.queued_prompts.len() - 1);
        self.selected_queue = Some(next);
        Some(next)
    }

    pub fn toggle_keyboard_help(&mut self) -> bool {
        self.keyboard_help = !self.keyboard_help;
        self.keyboard_help
    }

    pub fn keyboard_help_visible(&self) -> bool {
        self.keyboard_help
    }

    /// Return the available transcript viewport height for a terminal of
    /// `terminal_height`.
    ///
    /// Input handlers use this alongside `scroll_by`/`follow_tail` so adding
    /// a queue, permission prompt, or help panel cannot make End follow an
    /// off-screen row.
    pub fn content_height(&self, terminal_height: usize) -> usize {
        terminal_height.saturating_sub(
            4 + usize::from(self.queue_height())
                + usize::from(self.permission_height())
                + usize::from(self.help_height()),
        )
    }

    fn queue_height(&self) -> u16 {
        if self.queued_prompts.is_empty() {
            0
        } else {
            self.queued_prompts.len().min(6).saturating_add(3) as u16
        }
    }

    fn permission_height(&self) -> u16 {
        self.permission.as_ref().map_or(0, |request| {
            request
                .options
                .len()
                .saturating_add(if request.options.is_empty() { 4 } else { 3 })
                .min(12) as u16
        })
    }

    fn help_height(&self) -> u16 {
        if self.keyboard_help_visible() { 3 } else { 0 }
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
    let rows = Layout::vertical([
        Constraint::Min(1),
        Constraint::Length(1),
        Constraint::Length(app.queue_height()),
        Constraint::Length(app.permission_height()),
        Constraint::Length(app.help_height()),
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

    let follow_label = if app.follow_tail {
        "FOLLOWING"
    } else {
        "SCROLLING"
    };
    let status_line = Line::from(vec![
        Span::styled(" ✈ ", Style::default().fg(Color::Cyan).bold()),
        Span::styled(
            app.active_agent.as_str(),
            Style::default().fg(Color::White).bold(),
        ),
        Span::styled("  ·  ", Style::default().fg(Color::DarkGray)),
        Span::styled(
            app.status.as_str(),
            Style::default().fg(status_color(&app.status)),
        ),
        Span::styled("  ·  ", Style::default().fg(Color::DarkGray)),
        Span::styled(
            follow_label,
            Style::default().fg(if app.follow_tail {
                Color::Green
            } else {
                Color::Yellow
            }),
        ),
        if app.queued_count() > 0 {
            Span::styled(
                format!("  ·  {} queued", app.queued_count()),
                Style::default().fg(Color::Yellow),
            )
        } else {
            Span::raw("")
        },
    ]);
    Paragraph::new(status_line)
        .style(Style::default().bg(Color::Rgb(22, 28, 38)))
        .render(rows[1], frame.buffer_mut());
    if app.queued_count() > 0 {
        render_queue(frame.buffer_mut(), rows[2], app);
    }
    if let Some(permission) = &app.permission {
        render_permission(frame.buffer_mut(), rows[3], permission);
    }
    if app.keyboard_help_visible() {
        render_keyboard_help(frame.buffer_mut(), rows[4]);
    }
    Paragraph::new(Line::from(vec![
        Span::styled(" › ", Style::default().fg(Color::Cyan).bold()),
        Span::styled(app.prompt.as_str(), Style::default().fg(Color::White)),
    ]))
    .style(Style::default().bg(Color::Rgb(18, 23, 32)))
    .block(
        Block::default()
            .title(" Prompt ")
            .title_style(Style::default().fg(Color::Cyan).bold())
            .borders(Borders::TOP)
            .border_style(Style::default().fg(Color::Rgb(55, 75, 95))),
    )
    .render(rows[5], frame.buffer_mut());
}

fn status_color(status: &str) -> Color {
    let status = status.to_ascii_lowercase();
    if status.contains("error") || status.contains("failed") || status.contains("crashed") {
        Color::Red
    } else if status.contains("permission") || status.contains("cancell") {
        Color::Yellow
    } else if status.contains("stream") || status.contains("think") || status.contains("running") {
        Color::Cyan
    } else if status == "ready" || status == "idle" {
        Color::Green
    } else {
        Color::Gray
    }
}

fn render_queue(buffer: &mut Buffer, area: Rect, app: &App) {
    let visible = app.queued_prompts.len().min(6);
    let mut lines = Vec::with_capacity(visible.saturating_add(1));
    lines.push(Line::styled(
        format!(
            " queue ({}) · Alt+↑/↓ select · Ctrl+K cancel",
            app.queued_count()
        ),
        Style::default().fg(Color::DarkGray),
    ));
    for (index, queued) in app.queued_prompts.iter().take(visible).enumerate() {
        let marker = if app.selected_queue == Some(index) {
            "▶"
        } else {
            " "
        };
        let target = queued
            .target
            .map_or_else(|| "next".to_owned(), |slot| format!("agent {slot}"));
        let kind = if queued.direct { "direct" } else { "queued" };
        let style = if app.selected_queue == Some(index) {
            Style::default().fg(Color::Yellow)
        } else {
            Style::default()
        };
        lines.push(Line::from(vec![
            Span::styled(format!(" {marker} {kind} → {target}: "), style),
            Span::styled(queued.prompt.as_str(), style),
        ]));
    }
    Paragraph::new(lines)
        .block(Block::default().borders(Borders::TOP | Borders::BOTTOM))
        .render(area, buffer);
}

fn render_keyboard_help(buffer: &mut Buffer, area: Rect) {
    Paragraph::new(Line::raw(
        " keys: ↑/↓ scroll · End follow tail · Tab details · Ctrl+K cancel queue · ? hide help",
    ))
    .style(Style::default().fg(Color::DarkGray))
    .block(Block::default().borders(Borders::TOP | Borders::BOTTOM))
    .render(area, buffer);
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
        .map(|row| Line::styled(row.text, block_style(row.kind)))
        .collect::<Vec<_>>();
    Paragraph::new(lines)
        .style(Style::default().bg(Color::Rgb(12, 16, 23)))
        .block(
            Block::default()
                .title(" Conversation ")
                .title_style(Style::default().fg(Color::Rgb(120, 145, 165)).bold())
                .borders(Borders::LEFT | Borders::RIGHT)
                .border_style(Style::default().fg(Color::Rgb(45, 60, 75))),
        )
        .render(area, buffer);
}

fn block_style(kind: codeswarm_transcript::BlockKind) -> Style {
    match kind {
        codeswarm_transcript::BlockKind::Human => Style::default().fg(Color::Blue),
        codeswarm_transcript::BlockKind::Agent => Style::default().fg(Color::White),
        codeswarm_transcript::BlockKind::Thought => Style::default().fg(Color::DarkGray).italic(),
        codeswarm_transcript::BlockKind::Tool => Style::default().fg(Color::Yellow),
        codeswarm_transcript::BlockKind::Diff => Style::default().fg(Color::Magenta),
        codeswarm_transcript::BlockKind::Notice => Style::default().fg(Color::Cyan),
    }
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
    fn streamed_thought_chunks_extend_one_collapsed_detail() {
        let mut app = App::default();
        app.apply_event(&codeswarm_core::AgentEvent::Thought {
            slot: 0,
            text: "first ".into(),
        });
        app.apply_event(&codeswarm_core::AgentEvent::Thought {
            slot: 0,
            text: "second".into(),
        });
        assert_eq!(app.transcript.len(), 1);
        assert_eq!(app.status, "thinking");
        app.apply_event(&codeswarm_core::AgentEvent::TurnComplete { slot: 0 });
        app.apply_event(&codeswarm_core::AgentEvent::Thought {
            slot: 0,
            text: "new turn".into(),
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

    #[test]
    fn queued_prompts_are_selectable_and_cancellable() {
        let mut app = App::default();
        let first = app
            .queue_prompt("first review", Some(1), false)
            .expect("first prompt id");
        let second = app
            .queue_prompt("private check", Some(2), true)
            .expect("second prompt id");
        assert_eq!(app.queued_count(), 2);
        assert_eq!(app.selected_queue_index(), Some(1));
        assert_eq!(
            app.next_queued_prompt().map(|prompt| prompt.id),
            Some(first)
        );

        assert_eq!(app.move_queue_selection(-1), Some(0));
        assert_eq!(
            app.cancel_selected_queued().map(|prompt| prompt.id),
            Some(first)
        );
        assert_eq!(app.queued_count(), 1);
        assert_eq!(app.selected_queue_index(), Some(0));
        assert_eq!(
            app.remove_queued_prompt(second).map(|prompt| prompt.prompt),
            Some("private check".into())
        );
        assert_eq!(app.queued_count(), 0);
        assert_eq!(app.selected_queue_index(), None);
    }

    #[test]
    fn follow_tail_stops_moving_when_scrolled_and_end_restores_it() {
        let mut app = App::default();
        app.transcript.append(
            BlockKind::Agent,
            (0..500)
                .map(|n| format!("word{n}"))
                .collect::<Vec<_>>()
                .join(" "),
            false,
        );
        app.follow_tail(80, 10);
        let tail = app.scroll_y;
        assert!(app.follow_tail);
        app.scroll_by(-1, 80, 10);
        assert!(!app.follow_tail);
        let scrolled = app.scroll_y;
        app.transcript
            .append(BlockKind::Agent, "new response", false);
        assert_eq!(app.scroll_y, scrolled);
        app.follow_tail(80, 10);
        assert!(app.follow_tail);
        assert!(app.scroll_y >= tail);
        let base_height = app.content_height(24);
        app.queue_prompt("queued", Some(1), false);
        assert!(app.content_height(24) < base_height);
        app.toggle_keyboard_help();
        assert!(app.content_height(24) < base_height);
    }

    #[test]
    fn queue_and_keyboard_help_render_as_separate_inline_regions() {
        let backend = TestBackend::new(100, 30);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App::default();
        app.queue_prompt("review queued changes", Some(1), false);
        assert!(app.toggle_keyboard_help());
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
        assert!(rendered.contains("queue (1)"));
        assert!(rendered.contains("review queued changes"));
        assert!(rendered.contains("Ctrl+K cancel queue"));
        assert!(rendered.contains("End follow tail"));
    }
}
