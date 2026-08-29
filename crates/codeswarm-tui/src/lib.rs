//! Low-churn Ratatui rendering over the viewport transcript model.
//!
//! The renderer is intentionally stateless with respect to historical rows:
//! after the transcript cache is warm, scrolling asks for a small cached slice
//! and draws that slice.

use std::{
    collections::{BTreeMap, VecDeque},
    path::PathBuf,
};

use codeswarm_core::{AgentEvent, TerminalEvent, ToolStatus};
use codeswarm_transcript::{RenderRow, Transcript};
use ratatui::{
    Frame,
    buffer::Buffer,
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph, Widget},
};
pub use tui_textarea::{Input, Key};
use tui_textarea::{TextArea, WrapMode};

pub mod frame_scheduler;
pub mod path_index;
pub use path_index::{
    MAX_INDEX_ENTRIES, MAX_PATH_RESULTS, MIN_PATH_QUERY_CHARS, PathCandidate, PathIndex,
    PathIndexUpdate, PathMatch, completion_values, insertion_text, rank_matches, scan_workspace,
};

const MAX_QUEUED_PROMPTS: usize = 100;
const TRANSCRIPT_BG: Color = Color::Rgb(12, 16, 23);
const STATUS_BG: Color = Color::Rgb(22, 28, 38);
const PANEL_BG: Color = Color::Rgb(18, 23, 32);

fn normalized_mode(value: &str) -> Option<(&'static str, &'static str)> {
    match value.to_ascii_lowercase().as_str() {
        "plan" | "readonly" | "planmode" => Some(("plan", "Plan")),
        "manual" | "ask" | "default" => Some(("default", "Manual")),
        "accept-edits" | "acceptedits" | "autoedit" => Some(("accept-edits", "Accept Edits")),
        "full-access" | "fullaccess" | "auto" | "autopilot" | "yolo" => {
            Some(("full-access", "Auto pilot"))
        }
        _ => None,
    }
}

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
        option_id: String,
    },
    Cancel {
        slot: usize,
        request_id: String,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum LocalCommand {
    Handled,
    Close,
    Cancel,
    Pause,
    Resume,
    Mode,
    Collaboration,
    Export,
    Agents,
    Add(String),
    Reload,
    Drop,
    DropSlot(usize),
    Promote(usize),
    Swap(usize, usize),
    Directory(String),
    Diff,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConfigKey {
    Up,
    Down,
    MoveUp,
    MoveDown,
    Confirm,
    Save,
    Cancel,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConfigAction {
    Ignored,
    Changed,
    Close,
}

/// When CodeSwarm may forward completion and permission events to the
/// operating system.  This preserves the Python client's three-way setting
/// while keeping the common tmux case cheap and deterministic.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum NotificationPolicy {
    /// Never send an OS notification or completion bell.
    Never,
    /// Notify only after the terminal reports that it is unfocused.
    #[default]
    Blur,
    /// Notify regardless of terminal focus.
    Always,
}

impl NotificationPolicy {
    pub fn from_setting(value: &str) -> Self {
        match value.to_ascii_lowercase().as_str() {
            "always" => Self::Always,
            "blur" | "unfocused" => Self::Blur,
            _ => Self::Never,
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Never => "never",
            Self::Blur => "blur",
            Self::Always => "always",
        }
    }

    pub const fn label(self) -> &'static str {
        match self {
            Self::Never => "Never",
            Self::Blur => "When unfocused",
            Self::Always => "Always",
        }
    }

    const fn next(self) -> Self {
        match self {
            Self::Never => Self::Blur,
            Self::Blur => Self::Always,
            Self::Always => Self::Never,
        }
    }
}

/// How much vertical space the conversation surface gives to editor text.
/// The setting is intentionally binary to stay predictable in a small tmux
/// pane while retaining the Python client's comfortable/compact contract.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum Density {
    #[default]
    Comfortable,
    Compact,
}

impl Density {
    pub fn from_setting(value: &str) -> Self {
        if value.eq_ignore_ascii_case("compact") {
            Self::Compact
        } else {
            Self::Comfortable
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Comfortable => "comfortable",
            Self::Compact => "compact",
        }
    }
}

/// Policy for materializing tool output details.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum ToolExpandPolicy {
    /// Expand failed tools, subject to the global collapsed-details toggle.
    #[default]
    Fail,
    /// Expand every tool detail.
    Always,
    /// Keep every tool detail collapsed.
    Never,
}

impl ToolExpandPolicy {
    pub fn from_setting(value: &str) -> Self {
        match value.to_ascii_lowercase().as_str() {
            "always" => Self::Always,
            "never" => Self::Never,
            _ => Self::Fail,
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Fail => "fail",
            Self::Always => "always",
            Self::Never => "never",
        }
    }

    const fn next(self) -> Self {
        match self {
            Self::Fail => Self::Always,
            Self::Always => Self::Never,
            Self::Never => Self::Fail,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StoreKey {
    Up,
    Down,
    Toggle,
    Save,
    MoveUp,
    MoveDown,
    Confirm,
    Cancel,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StoreAgent {
    pub identity: String,
    pub name: String,
    pub adapter: String,
    pub command: String,
    pub available: bool,
    pub selected: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StoreAction {
    Ignored,
    Changed,
    Save(Vec<usize>),
    Directory(String),
    Launch(Vec<usize>),
    Close,
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
    pub option_ids: Vec<String>,
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

/// The result of handling one prompt-editor input.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PromptAction {
    /// The key was not consumed by the editor.
    Ignored,
    /// The editor content or cursor changed.
    Changed,
    /// A non-empty prompt was submitted. The editor is cleared afterwards.
    Submit(String),
    /// A completion was applied. `index` and `total` let a caller display a
    /// lightweight completion status without rebuilding the prompt widget.
    Completion {
        value: String,
        index: usize,
        total: usize,
    },
}

/// Result of a key handled by the asynchronous workspace path picker.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PathPickerAction {
    Ignored,
    Changed,
    Insert(String),
    Dismiss,
}

/// A low-churn, multiline prompt editor backed by `tui-textarea`.
///
/// The editor owns cursor movement, Unicode-safe insertion/deletion, wrapped
/// rendering, undo/redo, and bounded submission history. CodeSwarm-specific
/// command completion is kept as a small candidate layer around the mature
/// widget, so prompt editing does not add work to transcript rendering.
#[derive(Debug)]
pub struct PromptEditor {
    textarea: TextArea<'static>,
    history: VecDeque<String>,
    history_position: Option<usize>,
    completion_candidates: Vec<String>,
    completion_matches: Vec<String>,
    completion_index: Option<usize>,
}

const MAX_PROMPT_HISTORY: usize = 50;

impl Default for PromptEditor {
    fn default() -> Self {
        let mut textarea = TextArea::default();
        textarea.set_block(
            Block::default()
                .title(" Prompt ")
                .title_style(Style::default().fg(Color::Cyan).bold())
                .borders(Borders::TOP)
                .border_style(Style::default().fg(Color::Rgb(55, 75, 95))),
        );
        textarea.set_style(Style::default().fg(Color::White).bg(Color::Rgb(18, 23, 32)));
        textarea.set_cursor_style(Style::default().fg(Color::Black).bg(Color::Cyan));
        textarea.set_wrap_mode(WrapMode::Word);
        textarea.set_min_rows(1);
        textarea.set_max_rows(8);
        textarea.set_placeholder_text("Ask an agent…");
        Self {
            textarea,
            history: VecDeque::new(),
            history_position: None,
            completion_candidates: Vec::new(),
            completion_matches: Vec::new(),
            completion_index: None,
        }
    }
}

impl PromptEditor {
    /// Create an editor initialized with text. Newlines are preserved.
    pub fn from_text(text: impl Into<String>) -> Self {
        let mut editor = Self::default();
        editor.set_text(text);
        editor
    }

    /// Return the complete prompt, including embedded newlines.
    pub fn text(&self) -> String {
        self.textarea.lines().join("\n")
    }

    /// Return the cursor as a zero-based `(line, character)` pair.
    pub fn cursor(&self) -> (usize, usize) {
        self.textarea.cursor()
    }

    /// Return the logical lines currently in the editor.
    pub fn lines(&self) -> &[String] {
        self.textarea.lines()
    }

    pub fn is_empty(&self) -> bool {
        self.textarea.is_empty()
    }

    /// Replace the editor content and place the cursor at its end.
    pub fn set_text(&mut self, text: impl Into<String>) {
        let text = text.into();
        let mut lines = text.split('\n').map(ToOwned::to_owned).collect::<Vec<_>>();
        if lines.is_empty() {
            lines.push(String::new());
        }
        let row = lines.len() - 1;
        let col = lines[row].chars().count();
        self.textarea.set_lines(lines, (row, col));
        self.history_position = None;
        self.reset_completion();
    }

    /// Clear the editor and return it to its initial cursor position.
    pub fn clear(&mut self) {
        self.set_text("");
    }

    /// Set slash-command candidates. Candidate order is preserved when Tab
    /// cycles through matches.
    pub fn set_completion_candidates<I, S>(&mut self, candidates: I)
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.completion_candidates = candidates.into_iter().map(Into::into).collect();
        self.reset_completion();
    }

    /// Current candidates matching the token before the cursor.
    pub fn completion_matches(&self) -> &[String] {
        &self.completion_matches
    }

    /// Record a successful submission in the bounded history.
    pub fn remember(&mut self, prompt: impl Into<String>) {
        let prompt = prompt.into();
        if prompt.trim().is_empty() {
            return;
        }
        if self.history.back() == Some(&prompt) {
            self.history_position = None;
            return;
        }
        self.history.push_back(prompt);
        while self.history.len() > MAX_PROMPT_HISTORY {
            self.history.pop_front();
        }
        self.history_position = None;
    }

    pub fn history(&self) -> &VecDeque<String> {
        &self.history
    }

    /// Move to the previous submitted prompt.
    pub fn history_previous(&mut self) -> bool {
        let next = self
            .history_position
            .unwrap_or(self.history.len())
            .checked_sub(1);
        let Some(next) = next else { return false };
        let Some(prompt) = self.history.get(next).cloned() else {
            return false;
        };
        self.set_text(prompt);
        self.history_position = Some(next);
        true
    }

    /// Move to the next submitted prompt, or to a blank draft after the newest.
    pub fn history_next(&mut self) -> bool {
        let Some(current) = self.history_position else {
            return false;
        };
        if let Some(prompt) = self.history.get(current + 1).cloned() {
            self.set_text(prompt);
            self.history_position = Some(current + 1);
        } else {
            self.history_position = None;
            self.set_text("");
        }
        true
    }

    /// Apply one backend-agnostic key. Plain Enter submits; Shift+Enter (or
    /// Ctrl+Enter) inserts a newline. Tab cycles slash-command completions.
    pub fn handle_input(&mut self, input: Input) -> PromptAction {
        if input.key == Key::Enter && !input.ctrl && !input.alt && !input.shift {
            let prompt = self.text();
            return if prompt.trim().is_empty() {
                PromptAction::Ignored
            } else {
                self.remember(prompt.clone());
                self.clear();
                PromptAction::Submit(prompt)
            };
        }
        if input.key == Key::Tab && !input.ctrl && !input.alt && !input.shift {
            return self.complete();
        }
        if input.key == Key::Up
            && !input.ctrl
            && !input.alt
            && !input.shift
            && self.lines().len() == 1
            && self.history_previous()
        {
            return PromptAction::Changed;
        }
        if input.key == Key::Down
            && !input.ctrl
            && !input.alt
            && !input.shift
            && self.lines().len() == 1
            && self.cursor_at_end()
            && self.history_position.is_some()
            && self.history_next()
        {
            return PromptAction::Changed;
        }
        let cursor_before = self.cursor();
        let modified = self.textarea.input(input);
        let cursor_moved = self.cursor() != cursor_before;
        if modified {
            self.history_position = None;
            self.reset_completion();
            PromptAction::Changed
        } else if cursor_moved {
            PromptAction::Changed
        } else {
            PromptAction::Ignored
        }
    }

    /// Render the editor as a Ratatui widget. Only the editor viewport is
    /// measured; transcript history is not touched.
    pub fn render(&self, frame: &mut Frame, area: Rect) {
        frame.render_widget(&self.textarea, area);
    }

    /// Return the widget's preferred outer height for the supplied width.
    /// This is bounded by the editor's configured maximum so a pasted prompt
    /// cannot consume the whole tmux pane.
    pub fn preferred_height(&mut self, width: u16) -> u16 {
        self.textarea.measure(width).preferred_rows.clamp(2, 8)
    }

    fn cursor_at_end(&self) -> bool {
        let (row, col) = self.cursor();
        self.lines()
            .get(row)
            .is_some_and(|line| row + 1 == self.lines().len() && col == line.chars().count())
    }

    fn complete(&mut self) -> PromptAction {
        let Some((start, prefix)) = self.completion_prefix() else {
            self.reset_completion();
            return PromptAction::Ignored;
        };
        if self.completion_matches.is_empty() {
            self.completion_matches = if prefix.starts_with('@') {
                // The Python path picker deliberately waits for three
                // characters before searching.  Keeping that guard in the
                // editor avoids cycling through a large repository-wide
                // candidate set after typing a bare `@`, while still keeping
                // slash-command completion immediate.
                if prefix
                    .strip_prefix('@')
                    .is_some_and(|query| query.chars().count() < 3)
                {
                    Vec::new()
                } else {
                    let mut matches = self
                        .completion_candidates
                        .iter()
                        .filter_map(|candidate| {
                            fuzzy_completion_score(&prefix, candidate)
                                .map(|score| (score, candidate.clone()))
                        })
                        .collect::<Vec<_>>();
                    matches.sort_by(|left, right| {
                        right.0.cmp(&left.0).then_with(|| left.1.cmp(&right.1))
                    });
                    matches
                        .into_iter()
                        .map(|(_, candidate)| candidate)
                        .collect()
                }
            } else {
                self.completion_candidates
                    .iter()
                    .filter(|candidate| candidate.starts_with(&prefix))
                    .cloned()
                    .collect()
            };
        }
        if self.completion_matches.is_empty() {
            return PromptAction::Ignored;
        }
        let index = self
            .completion_index
            .map_or(0, |index| (index + 1) % self.completion_matches.len());
        let candidate = self.completion_matches[index].clone();
        self.replace_token(start, &candidate);
        self.completion_index = Some(index);
        PromptAction::Completion {
            value: candidate,
            index,
            total: self.completion_matches.len(),
        }
    }

    fn completion_prefix(&self) -> Option<(usize, String)> {
        let (row, col) = self.cursor();
        let line = self.lines().get(row)?;
        let chars = line.chars().collect::<Vec<_>>();
        let end = col.min(chars.len());
        let mut start = chars[..end]
            .iter()
            .rposition(|character| character.is_whitespace())
            .map_or(0, |index| index + 1);
        // Keep a quoted `@path` together when the path itself contains
        // spaces.  The Python picker treats the opening `@"` as the token
        // start until the matching quote is entered.
        if let Some(quoted_start) = chars[..end]
            .windows(2)
            .rposition(|window| window == ['@', '"'])
            && chars[quoted_start + 2..end]
                .iter()
                .filter(|character| **character == '"')
                .count()
                .is_multiple_of(2)
        {
            start = quoted_start;
        }
        let prefix = chars[start..end].iter().collect::<String>();
        (prefix.starts_with('/') || prefix.starts_with('@')).then_some((start, prefix))
    }

    fn replace_token(&mut self, start: usize, replacement: &str) {
        let (row, col) = self.cursor();
        let mut chars = self.lines()[row].chars().collect::<Vec<_>>();
        let end = col.min(chars.len());
        chars.splice(start..end, replacement.chars());
        let mut lines = self.lines().to_vec();
        lines[row] = chars.into_iter().collect();
        let cursor = start + replacement.chars().count();
        self.textarea.set_lines(lines, (row, cursor));
    }

    fn reset_completion(&mut self) {
        self.completion_matches.clear();
        self.completion_index = None;
    }

    /// Replace the `@path` token immediately before the cursor.  The picker
    /// uses this instead of rebuilding the whole prompt, preserving cursor
    /// and multiline-editor state while avoiding an allocation for unrelated
    /// lines.
    pub fn replace_current_token(&mut self, replacement: &str) -> bool {
        let Some((start, _prefix)) = self.completion_prefix() else {
            return false;
        };
        self.replace_token(start, replacement);
        self.reset_completion();
        true
    }
}

fn fuzzy_completion_score(query: &str, candidate: &str) -> Option<usize> {
    let query = query.to_ascii_lowercase();
    let candidate = candidate.to_ascii_lowercase();
    let mut cursor = 0;
    let mut score = 0;
    let mut previous = None;
    for character in query.chars() {
        let position = candidate[cursor..].find(character)? + cursor;
        score += if previous == Some(position.saturating_sub(1)) {
            3
        } else if position == 0 || candidate.as_bytes().get(position - 1) == Some(&b'/') {
            2
        } else {
            1
        };
        previous = Some(position);
        cursor = position + character.len_utf8();
    }
    Some(score)
}

impl PermissionPrompt {
    fn new(
        slot: usize,
        request_id: impl Into<String>,
        title: impl Into<String>,
        options: Vec<String>,
        option_ids: Vec<String>,
    ) -> Self {
        Self {
            slot,
            request_id: request_id.into(),
            title: title.into(),
            options,
            option_ids,
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
    mode: String,
    requested_mode: Option<String>,
    collaboration: String,
    pub permission: Option<PermissionPrompt>,
    config_visible: bool,
    config_selected: usize,
    collapse_details: bool,
    notification_policy: NotificationPolicy,
    sounds: bool,
    blink_title: bool,
    terminal_title_flash: usize,
    terminal_title_blink: bool,
    terminal_focused: bool,
    show_thoughts: bool,
    expand_tools: bool,
    density: Density,
    tool_expand_policy: ToolExpandPolicy,
    show_scrollbar: bool,
    diff_split: bool,
    store_visible: bool,
    store_selected: usize,
    store_agents: Vec<StoreAgent>,
    store_status: String,
    store_directory: String,
    store_editing_directory: bool,
    config_agents: Vec<StoreAgent>,
    config_roster_dirty: bool,
    prompt_editor: PromptEditor,
    agent_names: BTreeMap<usize, String>,
    agent_states: BTreeMap<usize, String>,
    agent_modes: BTreeMap<usize, (Vec<codeswarm_core::Mode>, Option<String>)>,
    failed_agent: Option<usize>,
    queued_prompts: VecDeque<QueuedPrompt>,
    next_queue_id: u64,
    selected_queue: Option<usize>,
    keyboard_help: bool,
    streaming_blocks: BTreeMap<(usize, codeswarm_transcript::BlockKind), u64>,
    focused_detail: Option<u64>,
    /// Background workspace index used only by the optional `@path` picker.
    /// It is deliberately separate from transcript state so index updates do
    /// not invalidate the virtualized scroll cache.
    path_index: Option<PathIndex>,
    path_query: String,
    path_matches: Vec<PathMatch>,
    path_selection: usize,
}

const CONFIG_SETTING_COUNT: usize = 14;

impl Default for App {
    fn default() -> Self {
        Self {
            transcript: Transcript::default(),
            scroll_y: 0,
            follow_tail: true,
            prompt: String::new(),
            active_agent: "Initializing".into(),
            status: "idle".into(),
            mode: "Auto pilot".into(),
            requested_mode: None,
            collaboration: "Roster relay".into(),
            permission: None,
            config_visible: false,
            config_selected: 0,
            collapse_details: true,
            notification_policy: NotificationPolicy::Blur,
            sounds: true,
            blink_title: true,
            terminal_title_flash: 0,
            terminal_title_blink: false,
            terminal_focused: true,
            show_thoughts: false,
            expand_tools: false,
            density: Density::Comfortable,
            tool_expand_policy: ToolExpandPolicy::Fail,
            show_scrollbar: true,
            diff_split: false,
            store_visible: false,
            store_selected: 0,
            store_agents: Vec::new(),
            store_status: String::new(),
            store_directory: String::new(),
            store_editing_directory: false,
            config_agents: Vec::new(),
            config_roster_dirty: false,
            prompt_editor: PromptEditor::default(),
            agent_names: BTreeMap::new(),
            agent_states: BTreeMap::new(),
            agent_modes: BTreeMap::new(),
            failed_agent: None,
            queued_prompts: VecDeque::new(),
            next_queue_id: 0,
            selected_queue: None,
            keyboard_help: false,
            streaming_blocks: BTreeMap::new(),
            focused_detail: None,
            path_index: None,
            path_query: String::new(),
            path_matches: Vec::new(),
            path_selection: 0,
        }
    }
}

impl App {
    pub fn handle_local_command(&mut self, input: &str) -> Option<LocalCommand> {
        let mut parts = input.split_whitespace();
        let command = parts.next()?.to_ascii_lowercase();
        let argument = parts.collect::<Vec<_>>().join(" ");
        let result = match command.as_str() {
            "/quit" | "/exit" | "/close" => LocalCommand::Close,
            "/cancel" => LocalCommand::Cancel,
            "/pause" => LocalCommand::Pause,
            "/resume" => LocalCommand::Resume,
            "/mode" => {
                if argument.is_empty() {
                    self.config_visible = true;
                    self.config_selected = 3;
                    self.status = "mode configuration".into();
                    LocalCommand::Handled
                } else if matches!(
                    argument.to_ascii_lowercase().as_str(),
                    "chat" | "discuss" | "discussion"
                ) {
                    self.mode = "Chat".into();
                    self.requested_mode = None;
                    self.status = "mode set to Chat".into();
                    LocalCommand::Mode
                } else if let Some(mode) = normalized_mode(&argument) {
                    self.mode = mode.1.into();
                    self.requested_mode = Some(mode.0.into());
                    self.status = format!("mode set to {}", self.mode);
                    LocalCommand::Mode
                } else {
                    self.status = "Use /mode to choose a mode".into();
                    LocalCommand::Handled
                }
            }
            "/collab" | "/collaboration" => {
                if argument.is_empty() {
                    self.config_visible = true;
                    self.config_selected = 4;
                    self.status = "collaboration configuration".into();
                    LocalCommand::Handled
                } else {
                    match argument.to_ascii_lowercase().as_str() {
                        "roster" => self.collaboration = "Roster relay".into(),
                        "manual" => self.collaboration = "Manual routing".into(),
                        "pair" => self.collaboration = "Pair review".into(),
                        _ => {
                            self.status =
                                "Use /collab roster, /collab manual, or /collab pair".into();
                            return Some(LocalCommand::Handled);
                        }
                    }
                    self.status = format!("collaboration set to {}", self.collaboration);
                    LocalCommand::Collaboration
                }
            }
            "/export" => LocalCommand::Export,
            "/agents" => LocalCommand::Agents,
            "/add" => {
                if argument.is_empty() {
                    self.status = "usage: /add AGENT, agy:COMMAND, or acp:COMMAND".into();
                    LocalCommand::Handled
                } else {
                    LocalCommand::Add(argument)
                }
            }
            "/reload" => LocalCommand::Reload,
            "/drop" => argument
                .parse::<usize>()
                .map_or(LocalCommand::Drop, LocalCommand::DropSlot),
            "/promote" => argument
                .parse::<usize>()
                .map_or(LocalCommand::Handled, LocalCommand::Promote),
            "/swap" => {
                let mut slots = argument
                    .split_whitespace()
                    .filter_map(|value| value.parse::<usize>().ok());
                match (slots.next(), slots.next()) {
                    (Some(first), Some(second)) if slots.next().is_none() => {
                        LocalCommand::Swap(first, second)
                    }
                    _ => {
                        self.status = "usage: /swap SLOT SLOT".into();
                        LocalCommand::Handled
                    }
                }
            }
            "/diff" => {
                match argument.to_ascii_lowercase().as_str() {
                    "split" => self.diff_split = true,
                    "unified" | "inline" => self.diff_split = false,
                    _ => {
                        self.status = "usage: /diff split or /diff unified".into();
                        return Some(LocalCommand::Handled);
                    }
                }
                self.status = if self.diff_split {
                    "diff view set to split".into()
                } else {
                    "diff view set to unified".into()
                };
                LocalCommand::Diff
            }
            "/cd" => {
                if argument.is_empty() {
                    self.status = "usage: /cd PATH".into();
                    LocalCommand::Handled
                } else {
                    LocalCommand::Directory(argument)
                }
            }
            "/help" => {
                self.keyboard_help = true;
                self.status = "keyboard help shown".into();
                LocalCommand::Handled
            }
            "/clear" => {
                self.transcript.clear();
                self.scroll_y = 0;
                self.streaming_blocks.clear();
                self.focused_detail = None;
                self.status = "conversation cleared".into();
                LocalCommand::Handled
            }
            "/config" => {
                self.config_visible = true;
                self.config_selected = 0;
                self.status = "configuration".into();
                LocalCommand::Handled
            }
            _ if command.starts_with('/') => {
                self.status = format!("unknown command: {input}");
                LocalCommand::Handled
            }
            _ => return None,
        };
        Some(result)
    }

    pub fn config_visible(&self) -> bool {
        self.config_visible
    }

    pub fn reopen_config(&mut self) {
        self.config_visible = true;
    }

    /// Install the catalog rows shown by the in-session configuration panel.
    /// Rows retain catalog order while their `selected` bit represents the
    /// desired next roster order.
    pub fn set_config_agents(&mut self, agents: Vec<StoreAgent>) {
        self.config_agents = agents;
        self.config_roster_dirty = false;
        let max = CONFIG_SETTING_COUNT
            .saturating_add(self.config_agents.len())
            .saturating_sub(1);
        self.config_selected = self.config_selected.min(max);
    }

    pub fn config_agents(&self) -> &[StoreAgent] {
        &self.config_agents
    }

    pub fn config_roster_dirty(&self) -> bool {
        self.config_roster_dirty
    }

    pub fn mark_config_roster_saved(&mut self) {
        self.config_roster_dirty = false;
    }

    /// Return selected catalog identities in their current editor order.
    pub fn config_roster_identities(&self) -> Vec<String> {
        self.config_agents
            .iter()
            .filter(|agent| agent.selected)
            .map(|agent| agent.identity.clone())
            .collect()
    }

    pub fn show_store(&mut self, agents: Vec<StoreAgent>) {
        self.store_agents = agents;
        self.store_selected = 0;
        self.store_visible = true;
        self.store_status.clear();
        self.store_editing_directory = false;
    }

    pub fn set_store_directory(&mut self, directory: impl Into<String>) {
        self.store_directory = directory.into();
    }

    pub fn store_directory(&self) -> &str {
        &self.store_directory
    }

    pub fn store_editing_directory(&self) -> bool {
        self.store_editing_directory
    }

    pub fn begin_store_directory_edit(&mut self) {
        self.store_editing_directory = true;
        self.prompt_editor.set_text(self.store_directory.clone());
    }

    pub fn cancel_store_directory_edit(&mut self) {
        self.store_editing_directory = false;
        self.prompt_editor.clear();
    }

    pub fn handle_store_directory_input(&mut self, input: Input) -> StoreAction {
        if !self.store_editing_directory {
            return StoreAction::Ignored;
        }
        match self.prompt_editor.handle_input(input) {
            PromptAction::Submit(directory) => {
                self.store_directory = directory.clone();
                self.store_editing_directory = false;
                StoreAction::Directory(directory)
            }
            PromptAction::Changed | PromptAction::Completion { .. } => StoreAction::Changed,
            PromptAction::Ignored => StoreAction::Ignored,
        }
    }

    pub fn store_visible(&self) -> bool {
        self.store_visible
    }

    pub fn store_agents(&self) -> &[StoreAgent] {
        &self.store_agents
    }

    pub fn set_store_status(&mut self, status: impl Into<String>) {
        self.store_status = status.into();
    }

    pub fn handle_store_key(&mut self, key: StoreKey) -> StoreAction {
        if !self.store_visible || self.store_agents.is_empty() {
            return StoreAction::Ignored;
        }
        match key {
            StoreKey::Cancel => {
                self.store_visible = false;
                StoreAction::Close
            }
            StoreKey::Up => {
                self.store_selected = self.store_selected.saturating_sub(1);
                StoreAction::Changed
            }
            StoreKey::Down => {
                self.store_selected = (self.store_selected + 1).min(self.store_agents.len() - 1);
                StoreAction::Changed
            }
            StoreKey::Toggle => {
                self.store_agents[self.store_selected].selected =
                    !self.store_agents[self.store_selected].selected;
                StoreAction::Changed
            }
            StoreKey::Save => {
                let selected = self
                    .store_agents
                    .iter()
                    .enumerate()
                    .filter_map(|(index, agent)| agent.selected.then_some(index))
                    .collect::<Vec<_>>();
                let selected = if selected.is_empty() {
                    vec![self.store_selected]
                } else {
                    selected
                };
                self.store_status = "Roster saved".into();
                StoreAction::Save(selected)
            }
            StoreKey::MoveUp if self.store_selected > 0 => {
                self.store_agents
                    .swap(self.store_selected, self.store_selected - 1);
                self.store_selected -= 1;
                StoreAction::Changed
            }
            StoreKey::MoveDown if self.store_selected + 1 < self.store_agents.len() => {
                self.store_agents
                    .swap(self.store_selected, self.store_selected + 1);
                self.store_selected += 1;
                StoreAction::Changed
            }
            StoreKey::MoveUp | StoreKey::MoveDown => StoreAction::Ignored,
            StoreKey::Confirm => {
                let selected = self
                    .store_agents
                    .iter()
                    .enumerate()
                    .filter_map(|(index, agent)| agent.selected.then_some(index))
                    .collect::<Vec<_>>();
                let selected = if selected.is_empty() {
                    vec![self.store_selected]
                } else {
                    selected
                };
                self.store_visible = false;
                StoreAction::Launch(selected)
            }
        }
    }

    pub fn handle_config_key(&mut self, key: ConfigKey) -> ConfigAction {
        if !self.config_visible {
            return ConfigAction::Ignored;
        }
        match key {
            ConfigKey::Cancel => {
                self.config_visible = false;
                self.status = "configuration closed".into();
                ConfigAction::Close
            }
            ConfigKey::Save => {
                self.config_visible = false;
                self.status = "configuration saved".into();
                ConfigAction::Close
            }
            ConfigKey::Up => {
                self.config_selected = self.config_selected.saturating_sub(1);
                ConfigAction::Changed
            }
            ConfigKey::Down => {
                let max = CONFIG_SETTING_COUNT
                    .saturating_add(self.config_agents.len())
                    .saturating_sub(1);
                self.config_selected = self.config_selected.saturating_add(1).min(max);
                ConfigAction::Changed
            }
            ConfigKey::MoveUp | ConfigKey::MoveDown
                if self.config_selected >= CONFIG_SETTING_COUNT =>
            {
                let index = self.config_selected - CONFIG_SETTING_COUNT;
                let target = if key == ConfigKey::MoveUp {
                    index.checked_sub(1)
                } else {
                    (index + 1 < self.config_agents.len()).then_some(index + 1)
                };
                if let Some(target) = target {
                    self.config_agents.swap(index, target);
                    self.config_selected = CONFIG_SETTING_COUNT + target;
                    self.config_roster_dirty = true;
                }
                ConfigAction::Changed
            }
            ConfigKey::MoveUp | ConfigKey::MoveDown => ConfigAction::Ignored,
            ConfigKey::Confirm => {
                if self.config_selected >= CONFIG_SETTING_COUNT {
                    let index = self.config_selected - CONFIG_SETTING_COUNT;
                    if let Some(agent) = self.config_agents.get_mut(index) {
                        agent.selected = !agent.selected;
                        self.config_roster_dirty = true;
                        self.status = if agent.selected {
                            format!("{} added to roster", agent.name)
                        } else {
                            format!("{} removed from roster", agent.name)
                        };
                    }
                    return ConfigAction::Changed;
                }
                match self.config_selected {
                    0 => self.follow_tail = !self.follow_tail,
                    1 => self.collapse_details = !self.collapse_details,
                    2 => self.notification_policy = self.notification_policy.next(),
                    3 => {
                        let options = self.mode_options();
                        if !options.is_empty() {
                            let index = options
                                .iter()
                                .position(|option| option.label == self.mode)
                                .map_or(0, |index| (index + 1) % options.len());
                            let next = &options[index];
                            self.mode = next.label.clone();
                            self.requested_mode = Some(next.id.clone());
                        } else if self.mode == "Auto pilot" {
                            self.mode = "Chat".into();
                            self.requested_mode = None;
                        } else if self.mode == "Chat" {
                            self.mode = "Plan".into();
                            self.requested_mode = Some("plan".into());
                        } else if self.mode == "Plan" {
                            self.mode = "Accept Edits".into();
                            self.requested_mode = Some("accept-edits".into());
                        } else {
                            self.mode = "Auto pilot".into();
                            self.requested_mode = Some("full-access".into());
                        }
                    }
                    4 => {
                        self.collaboration = match self.collaboration.as_str() {
                            "Roster relay" => "Manual routing".into(),
                            "Manual routing" => "Pair review".into(),
                            _ => "Roster relay".into(),
                        };
                    }
                    5 => self.diff_split = !self.diff_split,
                    6 => self.show_thoughts = !self.show_thoughts,
                    7 => {
                        self.tool_expand_policy = self.tool_expand_policy.next();
                        self.expand_tools = self.tool_expand_policy == ToolExpandPolicy::Always;
                    }
                    8 => {
                        self.density = match self.density {
                            Density::Comfortable => Density::Compact,
                            Density::Compact => Density::Comfortable,
                        };
                    }
                    9 => self.show_scrollbar = !self.show_scrollbar,
                    10 => self.sounds = !self.sounds,
                    11 => {
                        self.blink_title = !self.blink_title;
                        if !self.blink_title {
                            self.terminal_title_blink = false;
                        }
                    }
                    12..=14 => {}
                    _ => return ConfigAction::Ignored,
                }
                self.status = "configuration updated".into();
                ConfigAction::Changed
            }
        }
    }

    pub fn collapse_details(&self) -> bool {
        self.collapse_details
    }

    pub fn set_collapse_details(&mut self, collapsed: bool) {
        self.collapse_details = collapsed;
    }

    pub fn notifications_enabled(&self) -> bool {
        self.notification_policy != NotificationPolicy::Never
    }

    pub fn set_notifications_enabled(&mut self, enabled: bool) {
        self.notification_policy = if enabled {
            NotificationPolicy::Blur
        } else {
            NotificationPolicy::Never
        };
    }

    pub fn notification_policy(&self) -> NotificationPolicy {
        self.notification_policy
    }

    pub fn set_notification_policy(&mut self, policy: &str) {
        self.notification_policy = NotificationPolicy::from_setting(policy);
    }

    /// Return whether an event may be surfaced outside the terminal.
    pub fn should_notify_system(&self) -> bool {
        match self.notification_policy {
            NotificationPolicy::Never => false,
            NotificationPolicy::Blur => !self.terminal_focused,
            NotificationPolicy::Always => true,
        }
    }

    pub fn sounds_enabled(&self) -> bool {
        self.sounds
    }

    pub fn set_sounds_enabled(&mut self, enabled: bool) {
        self.sounds = enabled;
    }

    pub fn blink_title_enabled(&self) -> bool {
        self.blink_title
    }

    pub fn set_blink_title_enabled(&mut self, enabled: bool) {
        self.blink_title = enabled;
        if !enabled {
            self.terminal_title_blink = false;
        }
    }

    /// Mark the terminal title as needing attention until the user handles
    /// the pending prompt. The counter mirrors the Python client's
    /// reference-counted alerts so overlapping completion and permission
    /// events cannot accidentally clear each other.
    pub fn terminal_alert(&mut self, flash: bool) {
        if flash {
            self.terminal_title_flash = self.terminal_title_flash.saturating_add(1);
        } else {
            self.terminal_title_flash = self.terminal_title_flash.saturating_sub(1);
            if self.terminal_title_flash == 0 {
                self.terminal_title_blink = false;
            }
        }
    }

    pub fn terminal_alert_active(&self) -> bool {
        self.terminal_title_flash > 0
    }

    pub fn clear_terminal_alerts(&mut self) {
        self.terminal_title_flash = 0;
        self.terminal_title_blink = false;
    }

    pub fn terminal_title_blink(&self) -> bool {
        self.terminal_title_blink
    }

    pub fn toggle_terminal_title_blink(&mut self) {
        if self.blink_title && self.terminal_alert_active() {
            self.terminal_title_blink = !self.terminal_title_blink;
        } else {
            self.terminal_title_blink = false;
        }
    }

    /// Return a sanitized OSC title. Agent names can originate in external
    /// catalog/configuration files, so control characters must never reach
    /// the terminal title escape sequence.
    pub fn terminal_title(&self) -> String {
        let agent = self
            .active_agent
            .chars()
            .filter(|character| !character.is_control())
            .collect::<String>();
        let title = if agent.trim().is_empty() {
            "CodeSwarm".to_owned()
        } else {
            format!("{agent} · CodeSwarm")
        };
        if self.terminal_title_blink {
            format!("👉 {title}")
        } else {
            format!("✈ {title}")
        }
    }

    /// Track terminal focus separately from the notification preference. A
    /// focused terminal should not trigger an OS notification; this mirrors
    /// the previous client's blur-only system notification policy while
    /// keeping the setting useful in tmux where focus events may be absent.
    pub fn set_terminal_focused(&mut self, focused: bool) {
        self.terminal_focused = focused;
    }

    pub fn terminal_focused(&self) -> bool {
        self.terminal_focused
    }

    pub fn thoughts_enabled(&self) -> bool {
        self.show_thoughts
    }

    pub fn set_thoughts_enabled(&mut self, enabled: bool) {
        self.show_thoughts = enabled;
    }

    pub fn tools_expanded(&self) -> bool {
        self.expand_tools
    }

    pub fn set_tools_expanded(&mut self, expanded: bool) {
        self.tool_expand_policy = if expanded {
            ToolExpandPolicy::Always
        } else {
            ToolExpandPolicy::Fail
        };
        self.expand_tools = expanded;
    }

    pub fn tool_expand_policy(&self) -> &'static str {
        self.tool_expand_policy.as_str()
    }

    pub fn set_tool_expand_policy(&mut self, policy: &str) {
        self.tool_expand_policy = ToolExpandPolicy::from_setting(policy);
        self.expand_tools = self.tool_expand_policy == ToolExpandPolicy::Always;
    }

    pub fn density(&self) -> &'static str {
        self.density.as_str()
    }

    pub fn set_density(&mut self, density: &str) {
        self.density = Density::from_setting(density);
    }

    pub fn scrollbar_visible(&self) -> bool {
        self.show_scrollbar
    }

    pub fn set_scrollbar_visible(&mut self, visible: bool) {
        self.show_scrollbar = visible;
    }

    pub fn diff_split(&self) -> bool {
        self.diff_split
    }

    pub fn set_diff_split(&mut self, split: bool) {
        self.diff_split = split;
    }

    pub fn mode(&self) -> &str {
        &self.mode
    }

    pub fn take_requested_mode(&mut self) -> Option<String> {
        self.requested_mode.take()
    }

    pub fn collaboration(&self) -> &str {
        &self.collaboration
    }

    pub fn set_mode(&mut self, mode: impl Into<String>) {
        self.mode = mode.into();
    }

    pub fn mode_options(&self) -> Vec<codeswarm_core::Mode> {
        let sets = self
            .agent_modes
            .values()
            .map(|(modes, _)| modes.clone())
            .collect::<Vec<_>>();
        codeswarm_core::policy::shared_modes(&sets)
    }

    pub fn set_collaboration(&mut self, collaboration: impl Into<String>) {
        self.collaboration = collaboration.into();
    }

    pub fn export_markdown(&self) -> String {
        self.transcript.markdown()
    }

    pub fn set_agent_name(&mut self, slot: usize, name: impl Into<String>) {
        self.agent_names.insert(slot, name.into());
        self.agent_states
            .entry(slot)
            .or_insert_with(|| "starting".into());
    }

    /// Remove a roster entry that failed before its adapter became visible.
    /// Live add startup is transactional at the coordinator, so the UI must
    /// discard its optimistic label when that transaction rolls back.
    pub fn remove_agent(&mut self, slot: usize) -> bool {
        let removed = self.agent_names.remove(&slot).is_some();
        self.agent_states.remove(&slot);
        self.agent_modes.remove(&slot);
        removed
    }

    /// Move the visible identity and per-agent UI state alongside a promoted
    /// roster member. The coordinator keeps numeric slots stable, but slot
    /// zero's displayed owner must follow the adapter that now owns it.
    pub fn promote_agent(&mut self, slot: usize) -> bool {
        if slot == 0
            || !self.agent_names.contains_key(&slot)
            || self
                .agent_states
                .get(&slot)
                .is_some_and(|state| state == "dropped")
        {
            return false;
        }
        let Some(promoted_name) = self.agent_names.remove(&slot) else {
            return false;
        };
        let owner_name = self.agent_names.remove(&0);
        self.agent_names.insert(0, promoted_name);
        if let Some(owner_name) = owner_name {
            self.agent_names.insert(slot, owner_name);
        }

        let promoted_state = self.agent_states.remove(&slot);
        self.agent_states.remove(&0);
        if let Some(state) = promoted_state {
            self.agent_states.insert(0, state);
        }
        self.agent_states.insert(slot, "dropped".into());

        if let Some(promoted_modes) = self.agent_modes.remove(&slot) {
            let owner_modes = self.agent_modes.remove(&0);
            self.agent_modes.insert(0, promoted_modes);
            if let Some(modes) = owner_modes {
                self.agent_modes.insert(slot, modes);
            }
        }
        self.active_agent = self.agent_name(0);
        true
    }

    pub fn agent_count(&self) -> usize {
        self.agent_names.len()
    }

    /// Return the raw catalog/display names in stable roster order. This is
    /// used by the CLI to seed the in-session catalog editor without exposing
    /// the renderer's duplicate-name suffixes.
    pub fn raw_agent_names(&self) -> Vec<String> {
        self.agent_names.values().cloned().collect()
    }

    /// Stable slots that are currently eligible for dispatch. Dropped slots
    /// remain in `agent_names` so their numeric identity is preserved, but do
    /// not participate in live config reconciliation or roster ordering.
    pub fn active_roster_slots(&self) -> Vec<usize> {
        self.agent_names
            .keys()
            .copied()
            .filter(|slot| {
                self.agent_states
                    .get(slot)
                    .is_none_or(|state| state != "dropped")
            })
            .collect()
    }

    pub fn record_human_message(&mut self, prompt: &str, direct: bool) {
        let prefix = if direct { "You → direct: " } else { "You: " };
        self.transcript.append(
            codeswarm_transcript::BlockKind::Human,
            format!("{prefix}{prompt}"),
            false,
        );
    }

    pub fn agent_name(&self, slot: usize) -> String {
        let name = self
            .agent_names
            .get(&slot)
            .cloned()
            .unwrap_or_else(|| format!("Agent {slot}"));
        let duplicate_slots = self
            .agent_names
            .iter()
            .filter_map(|(candidate_slot, candidate_name)| {
                (candidate_name == &name).then_some(*candidate_slot)
            })
            .collect::<Vec<_>>();
        if duplicate_slots.len() <= 1 {
            return name;
        }
        let roster_number = duplicate_slots
            .iter()
            .position(|candidate_slot| *candidate_slot == slot)
            .map_or(slot, |position| position + 1);
        format!("{name} #{roster_number}")
    }

    /// Return a bounded, stable roster label for the status HUD. This keeps
    /// loaded agent identity visible before the first response without adding
    /// a second layout row or walking transcript history.
    pub fn roster_summary(&self) -> String {
        let names = self
            .agent_names
            .keys()
            .map(|slot| self.agent_name(*slot))
            .collect::<Vec<_>>();
        if names.is_empty() {
            return String::new();
        }
        compact_label(&names.join(" · "), 42)
    }

    pub fn active_agents_summary(&self) -> String {
        let summary = self
            .agent_names
            .keys()
            .map(|slot| {
                let name = self.agent_name(*slot);
                let state = self
                    .agent_states
                    .get(slot)
                    .map(String::as_str)
                    .unwrap_or("starting");
                format!(
                    "{} {} · {}",
                    if state == "working" { "●" } else { "○" },
                    name,
                    state
                )
            })
            .collect::<Vec<_>>()
            .join("   ");
        compact_label(&summary, 80)
    }

    pub fn failed_agent(&self) -> Option<usize> {
        self.failed_agent
    }

    pub fn mark_agent_reloaded(&mut self, slot: usize) {
        self.failed_agent = None;
        self.agent_states.insert(slot, "starting".into());
        self.status = "reloading agent".into();
    }

    pub fn mark_agent_dropped(&mut self, slot: usize) {
        self.agent_states.insert(slot, "dropped".into());
        self.status = format!("agent {slot} dropped");
    }

    /// Move two visible roster identities and their per-agent state together.
    pub fn swap_agents(&mut self, first: usize, second: usize) -> bool {
        if first == second
            || !self.agent_names.contains_key(&first)
            || !self.agent_names.contains_key(&second)
        {
            return false;
        }
        if self
            .agent_states
            .get(&first)
            .is_some_and(|state| state == "dropped")
            || self
                .agent_states
                .get(&second)
                .is_some_and(|state| state == "dropped")
        {
            return false;
        }
        let first_name = self.agent_names.remove(&first);
        let second_name = self.agent_names.remove(&second);
        if let (Some(first_name), Some(second_name)) = (first_name, second_name) {
            self.agent_names.insert(first, second_name);
            self.agent_names.insert(second, first_name);
        }
        let first_state = self.agent_states.remove(&first);
        let second_state = self.agent_states.remove(&second);
        if let (Some(first_state), Some(second_state)) = (first_state, second_state) {
            self.agent_states.insert(first, second_state);
            self.agent_states.insert(second, first_state);
        }
        let first_modes = self.agent_modes.remove(&first);
        let second_modes = self.agent_modes.remove(&second);
        if let (Some(first_modes), Some(second_modes)) = (first_modes, second_modes) {
            self.agent_modes.insert(first, second_modes);
            self.agent_modes.insert(second, first_modes);
        }
        if self.failed_agent == Some(first) {
            self.failed_agent = Some(second);
        } else if self.failed_agent == Some(second) {
            self.failed_agent = Some(first);
        }
        self.active_agent = self.agent_name(first);
        true
    }

    pub fn set_header(&mut self, active_agent: impl Into<String>, status: impl Into<String>) {
        self.active_agent = active_agent.into();
        self.status = status.into();
    }

    fn sync_prompt_editor(&mut self) {
        if self.prompt_editor.text() != self.prompt {
            self.prompt_editor.set_text(self.prompt.clone());
        }
    }

    /// Apply one terminal key to the focused prompt editor and mirror its
    /// complete text into the compatibility `prompt` field used by callers.
    /// Keeping this boundary in the TUI prevents the CLI from accidentally
    /// bypassing multiline editing, history, and slash completion.
    pub fn handle_prompt_input(&mut self, input: Input) -> PromptAction {
        self.sync_prompt_editor();
        let action = self.prompt_editor.handle_input(input);
        self.prompt = self.prompt_editor.text();
        self.update_path_query();
        action
    }

    fn update_path_query(&mut self) {
        // Use the editor's cursor-aware token rather than the final line:
        // Python's picker follows the line under the cursor in a multiline
        // prompt, and a path typed earlier in the draft must remain editable.
        let token = self
            .prompt_editor
            .completion_prefix()
            .map_or_else(String::new, |(_, prefix)| prefix);
        let normalized_token = token
            .strip_prefix("@\"")
            .map_or_else(|| token.clone(), |query| format!("@{query}"));
        let valid = normalized_token.starts_with('@')
            && normalized_token
                .strip_prefix('@')
                .is_some_and(|query| query.chars().count() >= MIN_PATH_QUERY_CHARS);
        if !valid {
            self.path_query.clear();
            self.path_matches.clear();
            self.path_selection = 0;
            return;
        }
        if self.path_query != normalized_token {
            self.path_query = normalized_token.clone();
            self.path_selection = 0;
            if let Some(index) = &mut self.path_index {
                index.query(normalized_token);
            }
        }
    }

    /// Remove the current prompt from both the compatibility field and the
    /// focused editor, preserving editor history and cursor invariants.
    pub fn take_prompt(&mut self) -> String {
        self.sync_prompt_editor();
        let prompt = std::mem::take(&mut self.prompt);
        self.prompt_editor.clear();
        prompt
    }

    /// Install the local command vocabulary used by prompt Tab completion.
    pub fn set_prompt_completions<I, S>(&mut self, candidates: I)
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.prompt_editor.set_completion_candidates(candidates);
    }

    /// Start a lazy workspace index for `@path` prompt references.  Starting
    /// this index never scans synchronously; callers may invoke it when a
    /// session starts or after `/cd` changes the workspace.
    pub fn set_workspace_root(&mut self, root: impl Into<PathBuf>) {
        self.path_index = Some(PathIndex::new(root));
        self.path_query.clear();
        self.path_matches.clear();
        self.path_selection = 0;
    }

    /// Request a fresh index after the workspace changes.  Existing matches
    /// remain visible until replacement results arrive.
    pub fn refresh_workspace_root(&mut self, root: impl Into<PathBuf>) {
        if let Some(index) = &mut self.path_index {
            index.rescan(root);
        } else {
            self.set_workspace_root(root);
        }
    }

    /// Drain background index messages without waiting.  This is safe to call
    /// once per terminal frame and intentionally ignores stale queries.
    pub fn poll_path_index(&mut self) {
        let generation = self.path_index.as_ref().map(PathIndex::generation);
        let updates = self
            .path_index
            .as_ref()
            .map_or_else(Vec::new, PathIndex::poll);
        for update in updates {
            match update {
                PathIndexUpdate::Ready { .. } => {
                    // Re-submit the current token after a rescan.  A scan may
                    // finish after the user's first query and should still
                    // populate the picker without another keystroke.
                    let query = self.path_query.clone();
                    if !query.is_empty()
                        && let Some(index) = &mut self.path_index
                    {
                        index.query(query);
                    }
                }
                PathIndexUpdate::Matches {
                    generation: update_generation,
                    query,
                    matches,
                } if Some(update_generation) == generation && query == self.path_query => {
                    self.path_matches = matches;
                    self.path_selection = self
                        .path_selection
                        .min(self.path_matches.len().saturating_sub(1));
                }
                PathIndexUpdate::Matches { .. } => {}
            }
        }
    }

    /// Return whether the compact file picker should be rendered.
    pub fn path_picker_visible(&self) -> bool {
        !self.path_matches.is_empty() && !self.path_query.is_empty()
    }

    pub fn path_matches(&self) -> &[PathMatch] {
        &self.path_matches
    }

    pub fn dismiss_path_picker(&mut self) {
        self.path_query.clear();
        self.path_matches.clear();
        self.path_selection = 0;
    }

    pub fn path_selection(&self) -> usize {
        self.path_selection
    }

    /// Height needed for the picker, capped to six rows so it cannot steal
    /// the whole tmux pane from the transcript or prompt.
    pub fn path_picker_height(&self) -> u16 {
        if !self.path_picker_visible() {
            0
        } else {
            self.path_matches.len().min(5).saturating_add(2) as u16
        }
    }

    /// Handle navigation in the path picker.  The caller can pass this before
    /// regular prompt/scroll handling for Up/Down/Enter/Esc keys.
    pub fn handle_path_picker_key(&mut self, key: Key) -> PathPickerAction {
        if !self.path_picker_visible() {
            return PathPickerAction::Ignored;
        }
        match key {
            Key::Up => {
                let previous = self.path_selection;
                self.path_selection = self.path_selection.saturating_sub(1);
                if self.path_selection != previous {
                    PathPickerAction::Changed
                } else {
                    PathPickerAction::Ignored
                }
            }
            Key::Down => {
                let previous = self.path_selection;
                self.path_selection = self
                    .path_selection
                    .saturating_add(1)
                    .min(self.path_matches.len().saturating_sub(1));
                if self.path_selection != previous {
                    PathPickerAction::Changed
                } else {
                    PathPickerAction::Ignored
                }
            }
            Key::Enter => {
                let Some(selected) = self.path_matches.get(self.path_selection) else {
                    return PathPickerAction::Ignored;
                };
                let value = insertion_text(&selected.path, selected.directory);
                if self.prompt_editor.replace_current_token(&value) {
                    self.prompt = self.prompt_editor.text();
                    self.path_query.clear();
                    self.path_matches.clear();
                    self.path_selection = 0;
                    PathPickerAction::Insert(value)
                } else {
                    PathPickerAction::Ignored
                }
            }
            Key::Esc => {
                self.dismiss_path_picker();
                PathPickerAction::Dismiss
            }
            _ => PathPickerAction::Ignored,
        }
    }

    pub fn load_prompt_history<I, S>(&mut self, entries: I)
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        for entry in entries {
            self.prompt_editor.remember(entry);
        }
    }

    /// Apply normalized adapter state without exposing protocol-specific
    /// objects to the renderer. Text chunks are coalesced into one transcript
    /// block per active agent turn.
    pub fn apply_event(&mut self, event: &AgentEvent) {
        match event {
            AgentEvent::Ready { slot, .. } => {
                self.active_agent = self.agent_name(*slot);
                self.agent_states.insert(*slot, "ready".into());
                if self.failed_agent == Some(*slot) {
                    self.failed_agent = None;
                }
                self.status = "ready".into();
            }
            AgentEvent::ModesReplaced {
                slot,
                modes,
                current_mode,
            } => {
                self.agent_modes
                    .insert(*slot, (modes.clone(), current_mode.clone()));
                if *slot == 0
                    && let Some(current) = current_mode
                    && let Some(mode) = modes.iter().find(|mode| mode.id == *current)
                {
                    self.mode = mode.label.clone();
                }
            }
            AgentEvent::Text { slot, text } => {
                let key = (*slot, codeswarm_transcript::BlockKind::Agent);
                let block = self.streaming_blocks.get(&key).copied().unwrap_or_else(|| {
                    let id = self.transcript.append(
                        codeswarm_transcript::BlockKind::Agent,
                        format!("{}: ", self.agent_name(*slot)),
                        false,
                    );
                    self.streaming_blocks.insert(key, id);
                    id
                });
                self.transcript.extend(block, text);
                self.active_agent = self.agent_name(*slot);
                self.agent_states.insert(*slot, "working".into());
                self.status = "streaming".into();
            }
            AgentEvent::Thought { slot, text } => {
                let key = (*slot, codeswarm_transcript::BlockKind::Thought);
                let id = self.streaming_blocks.get(&key).copied().unwrap_or_else(|| {
                    let id = self.transcript.append(
                        codeswarm_transcript::BlockKind::Thought,
                        format!("{}: ", self.agent_name(*slot)),
                        !self.show_thoughts,
                    );
                    self.streaming_blocks.insert(key, id);
                    id
                });
                self.transcript.extend(id, text);
                self.active_agent = self.agent_name(*slot);
                self.agent_states.insert(*slot, "working".into());
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
                let kind = update
                    .detail
                    .as_deref()
                    .filter(|detail| looks_like_unified_diff(detail))
                    .map_or(codeswarm_transcript::BlockKind::Tool, |_| {
                        codeswarm_transcript::BlockKind::Diff
                    });
                let source = update.detail.as_deref().map_or_else(
                    || format!("{}: {} · {state}", self.agent_name(*slot), update.title),
                    |detail| {
                        format!(
                            "{}: {} · {state}\n{detail}",
                            self.agent_name(*slot),
                            update.title
                        )
                    },
                );
                let collapsed = match self.tool_expand_policy {
                    ToolExpandPolicy::Always => false,
                    ToolExpandPolicy::Never => true,
                    // The legacy "fail" default expands failures when the
                    // global detail setting is collapsed, while respecting a
                    // user's explicit request to show all details.
                    ToolExpandPolicy::Fail => {
                        self.collapse_details && update.status != ToolStatus::Failed
                    }
                };
                let id = self.transcript.append(kind, source, collapsed);
                self.focused_detail = Some(id);
            }
            AgentEvent::Permission { slot, request } => {
                self.active_agent = self.agent_name(*slot);
                self.agent_states.insert(*slot, "working".into());
                self.status = format!("permission: {}", request.title);
                self.permission = Some(PermissionPrompt::new(
                    *slot,
                    request.id.clone(),
                    request.title.clone(),
                    request.options.clone(),
                    request.option_ids.clone(),
                ));
            }
            AgentEvent::Terminal { slot, event } => {
                let text = match event {
                    TerminalEvent::Created { command, .. } => {
                        format!("{}: {command}", self.agent_name(*slot))
                    }
                    TerminalEvent::Output { text, .. } => {
                        format!("{}: {text}", self.agent_name(*slot))
                    }
                    TerminalEvent::Exited { code, .. } => {
                        format!("{}: exited {code}", self.agent_name(*slot))
                    }
                    TerminalEvent::Released { .. } => {
                        format!("{}: terminal released", self.agent_name(*slot))
                    }
                };
                let expanded =
                    matches!(event, TerminalEvent::Output { id, .. } if id == "local-shell");
                self.focused_detail = Some(self.transcript.append(
                    codeswarm_transcript::BlockKind::Tool,
                    text,
                    !expanded,
                ));
                self.agent_states.insert(*slot, "working".into());
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
                self.agent_states.insert(*slot, "ready".into());
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
                self.active_agent = self.agent_name(*slot);
                self.agent_states.insert(*slot, "error".into());
                self.failed_agent = Some(*slot);
                self.status = if *started {
                    format!("crashed: {detail} · /reload or /drop")
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
            self.prompt_height_hint()
                + 1
                + 1
                + usize::from(self.queue_height())
                + usize::from(self.permission_height())
                + usize::from(self.help_height())
                + usize::from(self.path_picker_height()),
        )
    }

    fn prompt_height_hint(&self) -> usize {
        self.prompt_editor.lines().len().saturating_add(1).clamp(
            2,
            if self.density == Density::Compact {
                3
            } else {
                8
            },
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
        if self.keyboard_help_visible() { 8 } else { 0 }
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
                let option_id = request
                    .option_ids
                    .get(request.selected)
                    .cloned()
                    .filter(|id| !id.is_empty())
                    .unwrap_or_else(|| option.clone());
                let action = PermissionAction::Answer {
                    slot: request.slot,
                    request_id: request.request_id.clone(),
                    option_index: request.selected,
                    option,
                    option_id,
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
    app.sync_prompt_editor();
    app.poll_path_index();
    let area = frame.area();
    if app.store_visible {
        if app.store_editing_directory {
            render_store_directory(frame, app, area);
            return;
        }
        render_store(frame, app, area);
        return;
    }
    if app.config_visible {
        render_config(frame, app, area);
        return;
    }
    if area.width < 36 || area.height < 7 {
        render_compact(frame, app, area);
        return;
    }
    let total_height = usize::from(area.height);
    let status_height = if area.height == 0 {
        0
    } else {
        1 + usize::from(app.agent_names.len() > 1)
    };
    let minimum_prompt_height = total_height.saturating_sub(status_height).min(2);
    let reserve_content =
        usize::from(total_height > status_height.saturating_add(minimum_prompt_height));
    let mut optional_height = total_height.saturating_sub(
        status_height
            .saturating_add(minimum_prompt_height)
            .saturating_add(reserve_content),
    );
    let permission_height = usize::from(app.permission_height()).min(optional_height);
    optional_height = optional_height.saturating_sub(permission_height);
    let queue_height = usize::from(app.queue_height()).min(optional_height);
    optional_height = optional_height.saturating_sub(queue_height);
    let help_height = usize::from(app.help_height()).min(optional_height);
    optional_height = optional_height.saturating_sub(help_height);
    let path_picker_height = usize::from(app.path_picker_height()).min(optional_height);
    let available_for_prompt = total_height
        .saturating_sub(status_height)
        .saturating_sub(permission_height)
        .saturating_sub(queue_height)
        .saturating_sub(help_height)
        .saturating_sub(path_picker_height);
    let content_height = usize::from(available_for_prompt > minimum_prompt_height);
    let preferred_prompt_height = usize::from(app.prompt_editor.preferred_height(area.width));
    let preferred_prompt_height = if app.density == Density::Compact {
        preferred_prompt_height.min(3)
    } else {
        preferred_prompt_height
    };
    let prompt_height =
        preferred_prompt_height.min(available_for_prompt.saturating_sub(content_height));
    let rows = Layout::vertical([
        Constraint::Min(0),
        Constraint::Length(status_height as u16),
        Constraint::Length(queue_height as u16),
        Constraint::Length(permission_height as u16),
        Constraint::Length(help_height as u16),
        Constraint::Length(path_picker_height as u16),
        Constraint::Length(prompt_height as u16),
    ])
    .split(area);
    let content_width = rows[0]
        .width
        .saturating_sub(2 + u16::from(app.show_scrollbar)) as usize;
    let content_height = usize::from(rows[0].height.saturating_sub(1));
    if app.follow_tail {
        app.follow_tail(rows[0].width as usize, content_height);
    }
    let visible = app
        .transcript
        .viewport(content_width, app.scroll_y, content_height, 0);
    render_transcript(frame.buffer_mut(), rows[0], visible, app, app.diff_split);
    if app.show_scrollbar {
        render_scrollbar(
            frame.buffer_mut(),
            rows[0],
            app.scroll_y,
            content_height,
            &mut app.transcript,
            content_width,
        );
    }

    let follow_label = if app.follow_tail {
        "FOLLOWING"
    } else {
        "SCROLLING"
    };
    let status_line = Line::from(vec![
        Span::styled(" ✈ ", Style::default().fg(Color::Cyan).bold()),
        Span::styled(
            app.active_agent.as_str(),
            Style::default()
                .fg(agent_color_for_name(app, &app.active_agent))
                .bold(),
        ),
        Span::styled("  ·  ", Style::default().fg(Color::Gray)),
        Span::styled(
            app.status.as_str(),
            Style::default().fg(status_color(&app.status)),
        ),
        Span::styled("  ·  ", Style::default().fg(Color::Gray)),
        Span::styled(
            follow_label,
            Style::default().fg(if app.follow_tail {
                Color::Green
            } else {
                Color::Yellow
            }),
        ),
        if app.roster_summary().is_empty() {
            Span::raw("")
        } else {
            Span::styled(
                format!("  ·  {}", app.roster_summary()),
                Style::default().fg(Color::Gray),
            )
        },
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
        .style(Style::default().bg(STATUS_BG))
        .render(
            Rect::new(rows[1].x, rows[1].y, rows[1].width, 1),
            frame.buffer_mut(),
        );
    if app.agent_names.len() > 1 && rows[1].height > 1 {
        let mut active_spans = vec![Span::styled(" active: ", Style::default().fg(Color::Gray))];
        for (index, (slot, _)) in app.agent_names.iter().enumerate() {
            let name = app.agent_name(*slot);
            let state = app
                .agent_states
                .get(slot)
                .map(String::as_str)
                .unwrap_or("starting");
            let marker = if state == "working" { "●" } else { "○" };
            let separator = if index == 0 { "" } else { "   " };
            active_spans.push(Span::styled(
                format!("{separator}{marker} {name} · {state}"),
                Style::default()
                    .fg(agent_color_for_slot(app, *slot, &name))
                    .bold(),
            ));
        }
        let active_line = Line::from(active_spans);
        Paragraph::new(active_line)
            .style(Style::default().bg(STATUS_BG))
            .render(
                Rect::new(rows[1].x, rows[1].y.saturating_add(1), rows[1].width, 1),
                frame.buffer_mut(),
            );
    }
    if app.queued_count() > 0 {
        render_queue(frame.buffer_mut(), rows[2], app);
    }
    if let Some(permission) = &app.permission {
        render_permission(frame.buffer_mut(), rows[3], permission);
    }
    if app.keyboard_help_visible() {
        render_keyboard_help(frame.buffer_mut(), rows[4]);
    }
    if app.path_picker_visible() {
        render_path_picker(frame.buffer_mut(), rows[5], app);
    }
    app.prompt_editor.render(frame, rows[6]);
}

fn render_path_picker(buffer: &mut Buffer, area: Rect, app: &App) {
    if area.width == 0 || area.height == 0 || !app.path_picker_visible() {
        return;
    }
    let visible = app.path_matches().iter().take(5);
    let mut lines = Vec::with_capacity(7);
    lines.push(Line::styled(
        format!(
            " files · {} matches · ↑/↓ choose · Enter insert",
            app.path_matches().len()
        ),
        Style::default().fg(Color::Gray),
    ));
    for (index, candidate) in visible.enumerate() {
        let selected = index == app.path_selection();
        let marker = if selected { "▶" } else { " " };
        let suffix = if candidate.directory { "/" } else { "" };
        let style = if selected {
            Style::default()
                .fg(Color::Black)
                .bg(Color::Cyan)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(Color::LightCyan)
        };
        lines.push(Line::from(vec![
            Span::styled(format!(" {marker} "), style),
            Span::styled(
                compact_label(
                    &format!("{}{}", candidate.path, suffix),
                    area.width.saturating_sub(6) as usize,
                ),
                style,
            ),
        ]));
    }
    Paragraph::new(lines)
        .style(Style::default().bg(PANEL_BG))
        .block(
            Block::default()
                .borders(Borders::TOP | Borders::BOTTOM)
                .border_style(Style::default().fg(Color::Cyan)),
        )
        .render(area, buffer);
}

/// Render a useful two- or three-row fallback in a very small pane. Keeping
/// this path separate avoids asking the multiline editor and auxiliary panels
/// to compete for space they cannot use.
fn render_compact(frame: &mut Frame, app: &mut App, area: Rect) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    // Keep the state users need to act on visible before the less important
    // agent label when a narrow pane clips the line.
    let status_width = usize::from(area.width).saturating_sub(5);
    let status_text = compact_label(&app.status, status_width);
    let roster = app.roster_summary();
    let agent_text = if roster.is_empty() {
        app.active_agent.clone()
    } else {
        roster
    };
    let label_width =
        usize::from(area.width).saturating_sub(status_text.chars().count().saturating_add(8));
    let status = Line::from(vec![
        Span::styled(" ✈ ", Style::default().fg(Color::Cyan).bold()),
        Span::styled(status_text, Style::default().fg(status_color(&app.status))),
        Span::styled(" · ", Style::default().fg(Color::Gray)),
        Span::styled(
            compact_label(&agent_text, label_width),
            Style::default()
                .fg(agent_color_for_name(app, &app.active_agent))
                .bold(),
        ),
    ]);
    Paragraph::new(status)
        .style(Style::default().bg(STATUS_BG))
        .render(Rect::new(area.x, area.y, area.width, 1), frame.buffer_mut());

    if area.height > 2 {
        let transcript_area = Rect::new(
            area.x,
            area.y.saturating_add(1),
            area.width,
            area.height.saturating_sub(2),
        );
        let width = transcript_area.width.saturating_sub(2) as usize;
        let height = usize::from(transcript_area.height.saturating_sub(1));
        if app.follow_tail {
            app.follow_tail(transcript_area.width as usize, height);
        }
        let visible = app.transcript.viewport(width, app.scroll_y, height, 0);
        render_transcript(
            frame.buffer_mut(),
            transcript_area,
            visible,
            app,
            app.diff_split,
        );
    }

    if area.height > 1 {
        let prompt_area = Rect::new(area.x, area.bottom().saturating_sub(1), area.width, 1);
        let prompt = compact_prompt(&app.prompt, area.width as usize);
        Paragraph::new(prompt)
            .style(Style::default().fg(Color::White).bg(PANEL_BG))
            .render(prompt_area, frame.buffer_mut());
    }
}

fn render_scrollbar(
    buffer: &mut Buffer,
    area: Rect,
    scroll_y: usize,
    viewport_height: usize,
    transcript: &mut Transcript,
    content_width: usize,
) {
    if area.width == 0 || viewport_height == 0 {
        return;
    }
    let track_height = area.height.saturating_sub(2) as usize;
    if track_height == 0 {
        return;
    }
    let total = transcript.row_count(content_width);
    let thumb_height = (track_height.saturating_mul(track_height) / total.max(1))
        .max(1)
        .min(track_height);
    let max_scroll = total.saturating_sub(viewport_height);
    let thumb_offset = if max_scroll == 0 {
        0
    } else {
        (track_height.saturating_sub(thumb_height))
            .saturating_mul(scroll_y)
            .checked_div(max_scroll)
            .unwrap_or(0)
    };
    let x = area.right().saturating_sub(1);
    for offset in 0..track_height {
        let symbol = if offset >= thumb_offset && offset < thumb_offset + thumb_height {
            "█"
        } else {
            "│"
        };
        let style = if symbol == "█" {
            Style::default().fg(Color::DarkGray).bg(TRANSCRIPT_BG)
        } else {
            Style::default()
                .fg(Color::Rgb(45, 55, 68))
                .bg(TRANSCRIPT_BG)
        };
        buffer[(x, area.y.saturating_add(1 + offset as u16))]
            .set_symbol(symbol)
            .set_style(style);
    }
}

fn compact_label(value: &str, width: usize) -> String {
    let budget = width.max(1);
    let mut chars = value.chars();
    let mut label = chars.by_ref().take(budget).collect::<String>();
    if chars.next().is_some() && budget > 1 {
        label.pop();
        label.push('…');
    }
    label
}

fn compact_prompt(value: &str, width: usize) -> String {
    let line = value.lines().last().unwrap_or_default();
    let budget = width.saturating_sub(2);
    if budget == 0 {
        return String::new();
    }
    let mut chars = line.chars();
    let mut prompt = chars.by_ref().take(budget).collect::<String>();
    if chars.next().is_some() && budget > 1 {
        prompt.pop();
        prompt.push('…');
    }
    format!("> {prompt}")
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
        Style::default().fg(Color::Gray),
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
            Style::default()
                .fg(Color::Yellow)
                .bg(Color::Rgb(50, 42, 22))
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(Color::White)
        };
        lines.push(Line::from(vec![
            Span::styled(format!(" {marker} {kind} → {target}: "), style),
            Span::styled(queued.prompt.as_str(), style),
        ]));
    }
    Paragraph::new(lines)
        .style(Style::default().bg(PANEL_BG))
        .block(
            Block::default()
                .borders(Borders::TOP | Borders::BOTTOM)
                .border_style(Style::default().fg(Color::Rgb(65, 75, 90))),
        )
        .render(area, buffer);
}

fn render_keyboard_help(buffer: &mut Buffer, area: Rect) {
    let lines = [
        " keys: ↑/↓ scroll · End follow tail · Tab details · Ctrl+K cancel queue · ? hide help",
        " commands: /help  /config  /agents  /add  /export  /diff  /mode  /collab  /reload  /drop  /promote  /swap  /cd",
        " /mode chat · /collab roster|manual|pair · /pause · /resume",
        " /clear clears the local transcript · /close exits the session",
        " Ctrl+Enter sends to the selected agent · Ctrl+C cancels active work",
        " Tab completes a slash command · Shift+Enter inserts a newline",
        "",
        " Press F1 or ? to hide this help",
    ];
    Paragraph::new(lines.into_iter().map(Line::raw).collect::<Vec<_>>())
        .style(Style::default().fg(Color::Gray).bg(PANEL_BG))
        .block(Block::default().borders(Borders::TOP | Borders::BOTTOM))
        .render(area, buffer);
}

fn render_config(frame: &mut Frame, app: &App, area: Rect) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let width = area.width.clamp(36, 76);
    let height = area.height.clamp(10, 24);
    let x = area.x + area.width.saturating_sub(width) / 2;
    let y = area.y + area.height.saturating_sub(height) / 2;
    let modal = Rect::new(x, y, width.min(area.width), height.min(area.height));
    frame.render_widget(Clear, modal);
    let compact = modal.width < 60;

    let rows = [
        (
            "Follow output",
            if app.follow_tail { "On" } else { "Off" },
            true,
        ),
        (
            "Collapse details",
            if app.collapse_details { "On" } else { "Off" },
            true,
        ),
        ("Notifications", app.notification_policy.label(), true),
        ("Mode", app.mode(), false),
        ("Collaboration", app.collaboration(), false),
        (
            "Diff view",
            if app.diff_split { "Split" } else { "Unified" },
            true,
        ),
        (
            "Thoughts",
            if app.show_thoughts {
                "Visible"
            } else {
                "Collapsed"
            },
            true,
        ),
        (
            "Tool details",
            match app.tool_expand_policy {
                ToolExpandPolicy::Fail => "On failure",
                ToolExpandPolicy::Always => "Always",
                ToolExpandPolicy::Never => "Never",
            },
            true,
        ),
        (
            "Density",
            match app.density {
                Density::Comfortable => "Comfortable",
                Density::Compact => "Compact",
            },
            true,
        ),
        (
            "Scrollbar",
            if app.show_scrollbar {
                "Normal"
            } else {
                "Hidden"
            },
            true,
        ),
        ("Sounds", if app.sounds { "On" } else { "Off" }, true),
        (
            "Blink title",
            if app.blink_title { "On" } else { "Off" },
            true,
        ),
        ("Renderer", "Inline · tmux safe", false),
        ("Roster", "Enter toggles agents", false),
    ];
    let total_rows = rows.len().saturating_add(app.config_agents.len());
    let mut lines = Vec::with_capacity(total_rows + 3);
    lines.push(Line::styled(
        "Configuration",
        Style::default()
            .fg(Color::White)
            .add_modifier(Modifier::BOLD),
    ));
    lines.push(Line::raw(""));
    let row_capacity = usize::from(modal.height.saturating_sub(4)).max(1);
    let start = app
        .config_selected
        .saturating_sub(row_capacity.saturating_sub(1))
        .min(total_rows.saturating_sub(1));
    let end = (start + row_capacity).min(total_rows);
    for index in start..end {
        if index >= rows.len() {
            let roster_index = index - rows.len();
            let Some(agent) = app.config_agents.get(roster_index) else {
                continue;
            };
            let selected = index == app.config_selected;
            let marker = if selected { "▶" } else { " " };
            let checked = if agent.selected { "☑" } else { "☐" };
            let line_style = if selected {
                Style::default()
                    .fg(Color::White)
                    .bg(Color::Rgb(38, 48, 65))
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(Color::White)
            };
            let availability = if agent.available { "ready" } else { "missing" };
            let name = if compact {
                compact_label(&agent.name, 18)
            } else {
                agent.name.clone()
            };
            lines.push(Line::from(vec![
                Span::styled(format!(" {marker} {checked} "), line_style),
                Span::styled(format!("{name:<20}"), line_style),
                Span::styled(
                    format!(" {availability} · {}", agent.adapter),
                    Style::default().fg(if agent.available {
                        Color::Green
                    } else {
                        Color::Yellow
                    }),
                ),
            ]));
            continue;
        }
        let (label, value, mutable) = rows[index];
        let selected = index == app.config_selected;
        let marker = if selected { "▶" } else { " " };
        let value_style = if mutable {
            Style::default().fg(if selected { Color::Green } else { Color::Cyan })
        } else {
            Style::default().fg(Color::Gray)
        };
        let line_style = if selected {
            Style::default()
                .fg(Color::White)
                .bg(Color::Rgb(38, 48, 65))
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(Color::White)
        };
        let label = if compact {
            match label {
                "Follow output" => "Follow",
                "Collapse details" => "Details",
                "Notifications" => "Notify",
                "Diff view" => "Diff",
                "Thoughts" => "Thoughts",
                "Tool details" => "Tools",
                "Density" => "Density",
                "Scrollbar" => "Scroll",
                "Sounds" => "Sound",
                "Blink title" => "Blink",
                "Collaboration" => "Collab",
                "Renderer" => "Render",
                "Roster" => "Roster",
                other => other,
            }
        } else {
            label
        };
        let label_width = if compact { 11 } else { 20 };
        let value_width =
            usize::from(modal.width).saturating_sub(label_width + if compact { 7 } else { 9 });
        lines.push(Line::from(vec![
            Span::styled(format!(" {marker} "), line_style),
            Span::styled(format!("{label:<label_width$}"), line_style),
            Span::styled(compact_label(value, value_width), value_style),
        ]));
    }
    lines.push(Line::raw(""));
    lines.push(Line::styled(
        format!(
            " ↑/↓ select · Enter change · Alt+↑/↓ reorder · Ctrl+S save · Esc close  ({}/{})",
            app.config_selected + 1,
            total_rows
        ),
        Style::default().fg(Color::Gray),
    ));
    Paragraph::new(lines)
        .style(Style::default().bg(PANEL_BG))
        .block(
            Block::default()
                .title(" CodeSwarm settings ")
                .title_style(Style::default().fg(Color::Cyan).bold())
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::Cyan)),
        )
        .render(modal, frame.buffer_mut());
}

fn render_store(frame: &mut Frame, app: &App, area: Rect) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let width = area.width.clamp(44, 88);
    let height = area.height.clamp(10, 22);
    let modal = Rect::new(
        area.x + area.width.saturating_sub(width) / 2,
        area.y + area.height.saturating_sub(height) / 2,
        width.min(area.width),
        height.min(area.height),
    );
    frame.render_widget(Clear, modal);
    let compact = modal.width < 60;
    let mut lines = vec![
        Line::styled(
            if compact {
                "Agents"
            } else {
                "Choose your agents"
            },
            Style::default()
                .fg(Color::White)
                .add_modifier(Modifier::BOLD),
        ),
        Line::styled(
            if compact {
                "Ctrl+D dir · Ctrl+S save · Enter run"
            } else {
                "Space select · Ctrl+D dir · Ctrl+S save · Enter launch · Esc quit"
            },
            Style::default().fg(Color::Gray),
        ),
        Line::styled(
            format!(
                " {}{}",
                if compact { "Dir: " } else { "Workspace: " },
                compact_label(app.store_directory(), if compact { 24 } else { 64 })
            ),
            Style::default().fg(Color::Gray),
        ),
    ];
    if !compact {
        lines.push(Line::raw(""));
    }
    if !app.store_status.is_empty() {
        lines.push(Line::styled(
            format!(" {}", app.store_status),
            Style::default().fg(Color::Green),
        ));
    }
    for (index, agent) in app.store_agents.iter().enumerate() {
        let marker = if index == app.store_selected {
            "▶"
        } else {
            " "
        };
        let checked = if agent.selected { "☑" } else { "☐" };
        let availability = if agent.available {
            "ready"
        } else {
            "not found"
        };
        let style = if index == app.store_selected {
            Style::default()
                .fg(Color::White)
                .bg(Color::Rgb(38, 48, 65))
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(Color::White)
        };
        if compact {
            lines.push(Line::from(vec![
                Span::styled(format!(" {marker} {checked} "), style),
                Span::styled(compact_label(&agent.name, 18), style),
                Span::styled(
                    if agent.available { "ready" } else { "missing" },
                    if agent.available {
                        Style::default().fg(Color::Green)
                    } else {
                        Style::default().fg(Color::Yellow)
                    },
                ),
            ]));
        } else {
            lines.push(Line::from(vec![
                Span::styled(format!(" {marker} {checked} "), style),
                Span::styled(format!("{:<20}", agent.name), style),
                Span::styled(
                    format!(" {:<9} {}", availability, agent.adapter),
                    if agent.available {
                        Style::default().fg(Color::Green)
                    } else {
                        Style::default().fg(Color::Yellow)
                    },
                ),
            ]));
        }
        if index == app.store_selected {
            lines.push(Line::styled(
                if compact {
                    format!(
                        "   {}",
                        compact_label(&agent.identity, usize::from(modal.width).saturating_sub(6))
                    )
                } else {
                    format!("     {} · {}", agent.identity, agent.command)
                },
                Style::default().fg(Color::Gray),
            ));
        }
    }
    Paragraph::new(lines)
        .style(Style::default().bg(PANEL_BG))
        .block(
            Block::default()
                .title(" CodeSwarm agent store ")
                .title_style(Style::default().fg(Color::Cyan).bold())
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::Cyan)),
        )
        .render(modal, frame.buffer_mut());
}

fn render_store_directory(frame: &mut Frame, app: &mut App, area: Rect) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let width = area.width.clamp(32, 80);
    let height = area.height.clamp(4, 8);
    let modal = Rect::new(
        area.x + area.width.saturating_sub(width) / 2,
        area.y + area.height.saturating_sub(height) / 2,
        width.min(area.width),
        height.min(area.height),
    );
    frame.render_widget(Clear, modal);
    let inner = Rect::new(
        modal.x.saturating_add(1),
        modal.y.saturating_add(1),
        modal.width.saturating_sub(2),
        modal.height.saturating_sub(2),
    );
    Paragraph::new(Line::styled(
        " Enter apply · Esc cancel",
        Style::default().fg(Color::Gray),
    ))
    .block(
        Block::default()
            .title(" Workspace directory ")
            .title_style(Style::default().fg(Color::Cyan).bold())
            .borders(Borders::ALL)
            .border_style(Style::default().fg(Color::Cyan)),
    )
    .render(modal, frame.buffer_mut());
    app.prompt_editor.render(frame, inner);
}

fn render_permission(buffer: &mut Buffer, area: Rect, request: &PermissionPrompt) {
    let mut lines = Vec::with_capacity(request.options.len().saturating_add(1));
    lines.push(Line::from(vec![
        Span::styled(
            " permission: ",
            Style::default()
                .fg(Color::Yellow)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(request.title.as_str(), Style::default().fg(Color::White)),
    ]));
    for (index, option) in request.options.iter().enumerate() {
        let marker = if index == request.selected {
            "▶"
        } else {
            " "
        };
        let style = if index == request.selected {
            Style::default()
                .fg(Color::Yellow)
                .bg(Color::Rgb(50, 42, 22))
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(Color::White)
        };
        lines.push(Line::from(vec![
            Span::styled(format!(" {marker} {}. ", index + 1), style),
            Span::styled(option.as_str(), style),
        ]));
    }
    if request.options.is_empty() {
        lines.push(Line::styled(
            " no options · Esc to cancel",
            Style::default().fg(Color::Gray),
        ));
    }
    Paragraph::new(lines)
        .style(Style::default().bg(PANEL_BG))
        .block(
            Block::default()
                .borders(Borders::TOP | Borders::BOTTOM)
                .border_style(Style::default().fg(Color::Yellow)),
        )
        .render(area, buffer);
}

fn render_transcript(
    buffer: &mut Buffer,
    area: Rect,
    rows: Vec<RenderRow>,
    app: &App,
    diff_split: bool,
) {
    // Keep the initial workspace quiet. The prompt and status ribbon already
    // explain how to begin; rendering an empty bordered panel only consumes
    // space and reads like a stale placeholder in small tmux panes.
    if rows.is_empty() {
        return;
    }
    if diff_split
        && rows
            .iter()
            .any(|row| row.kind == codeswarm_transcript::BlockKind::Diff)
    {
        render_split_diff(buffer, area, rows);
        return;
    }
    let lines = if rows.is_empty() {
        Vec::new()
    } else {
        let mut in_code = false;
        rows.into_iter()
            .map(|row| {
                if row.first_in_block {
                    in_code = false;
                }
                let marker = if row.first_in_block {
                    match row.kind {
                        codeswarm_transcript::BlockKind::Human => "› ",
                        codeswarm_transcript::BlockKind::Agent => "● ",
                        codeswarm_transcript::BlockKind::Thought => "… ",
                        codeswarm_transcript::BlockKind::Tool => "◆ ",
                        codeswarm_transcript::BlockKind::Diff => "± ",
                        codeswarm_transcript::BlockKind::Notice => "· ",
                    }
                } else {
                    "  "
                };
                if row.kind == codeswarm_transcript::BlockKind::Agent
                    && row.first_in_block
                    && let Some((speaker, body)) = row.text.split_once(": ")
                {
                    let color = agent_color_for_name(app, speaker);
                    let mut spans = vec![
                        Span::styled(marker, Style::default().fg(color).bold()),
                        Span::styled(format!("{speaker}:"), Style::default().fg(color).bold()),
                    ];
                    spans.extend(markdown_spans(row.kind, &format!(" {body}"), &mut in_code));
                    return Line::from(spans);
                }
                let style = row_style(row.kind, &row.text);
                let mut spans = vec![Span::styled(marker, style.add_modifier(Modifier::BOLD))];
                spans.extend(markdown_spans(row.kind, &row.text, &mut in_code));
                Line::from(spans)
            })
            .collect::<Vec<_>>()
    };
    Paragraph::new(lines)
        .style(Style::default().bg(TRANSCRIPT_BG))
        .block(
            Block::default()
                .title(" Conversation ")
                .title_style(Style::default().fg(Color::Rgb(120, 145, 165)).bold())
                .borders(Borders::LEFT | Borders::RIGHT)
                .border_style(Style::default().fg(Color::Rgb(75, 95, 110))),
        )
        .render(area, buffer);
}

fn render_split_diff(buffer: &mut Buffer, area: Rect, rows: Vec<RenderRow>) {
    let columns =
        Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)]).split(area);
    let mut left = Vec::new();
    let mut right = Vec::new();
    for row in rows {
        if row.kind != codeswarm_transcript::BlockKind::Diff {
            let line = Line::styled(row.text, markdown_style(row.kind, ""));
            left.push(line.clone());
            right.push(line);
            continue;
        }
        let text = row.text;
        let style = row_style(row.kind, &text);
        let line = Line::styled(text.clone(), style);
        if text.starts_with('-') && !text.starts_with("---") {
            left.push(line);
            right.push(Line::raw(""));
        } else if text.starts_with('+') && !text.starts_with("+++") {
            left.push(Line::raw(""));
            right.push(line);
        } else {
            left.push(line.clone());
            right.push(line);
        }
    }
    Paragraph::new(left)
        .style(Style::default().bg(TRANSCRIPT_BG))
        .block(
            Block::default()
                .title(" Diff · original ")
                .borders(Borders::LEFT | Borders::RIGHT)
                .border_style(Style::default().fg(Color::Red)),
        )
        .render(columns[0], buffer);
    Paragraph::new(right)
        .style(Style::default().bg(TRANSCRIPT_BG))
        .block(
            Block::default()
                .title(" Diff · updated ")
                .borders(Borders::LEFT | Borders::RIGHT)
                .border_style(Style::default().fg(Color::Green)),
        )
        .render(columns[1], buffer);
}

const AGENT_COLORS: [Color; 8] = [
    Color::LightBlue,
    Color::LightGreen,
    Color::LightYellow,
    Color::LightMagenta,
    Color::LightCyan,
    Color::Magenta,
    Color::LightRed,
    Color::Blue,
];

fn agent_slot_color(slot: usize) -> Color {
    AGENT_COLORS[slot % AGENT_COLORS.len()]
}

fn agent_header_color(name: &str) -> Color {
    const COLORS: [Color; 8] = [
        Color::LightBlue,
        Color::LightGreen,
        Color::LightYellow,
        Color::LightMagenta,
        Color::LightCyan,
        Color::LightRed,
        Color::Magenta,
        Color::Blue,
    ];
    let hash = name.bytes().fold(0usize, |hash, byte| {
        hash.wrapping_mul(31).wrapping_add(byte as usize)
    });
    COLORS[hash % COLORS.len()]
}

/// Resolve a transcript/footer identity to its roster slot whenever possible.
/// Slot-based colors keep similarly named agents (for example Codex and
/// Gemini) visually distinct; the hash fallback keeps standalone or replayed
/// rows deterministic when no roster metadata is available.
fn agent_color_for_name(app: &App, name: &str) -> Color {
    app.agent_names
        .keys()
        .find_map(|slot| (app.agent_name(*slot) == name).then_some(agent_slot_color(*slot)))
        .unwrap_or_else(|| agent_header_color(name))
}

fn agent_color_for_slot(app: &App, slot: usize, name: &str) -> Color {
    if app.agent_names.contains_key(&slot) {
        agent_slot_color(slot)
    } else {
        agent_header_color(name)
    }
}

fn block_style(kind: codeswarm_transcript::BlockKind) -> Style {
    match kind {
        codeswarm_transcript::BlockKind::Human => Style::default()
            .fg(Color::LightBlue)
            .add_modifier(Modifier::BOLD),
        codeswarm_transcript::BlockKind::Agent => Style::default().fg(Color::White),
        codeswarm_transcript::BlockKind::Thought => Style::default().fg(Color::Gray).italic(),
        codeswarm_transcript::BlockKind::Tool => Style::default().fg(Color::Yellow),
        codeswarm_transcript::BlockKind::Diff => Style::default().fg(Color::Magenta),
        codeswarm_transcript::BlockKind::Notice => Style::default().fg(Color::Cyan),
    }
}

fn row_style(kind: codeswarm_transcript::BlockKind, text: &str) -> Style {
    if kind != codeswarm_transcript::BlockKind::Diff {
        return block_style(kind);
    }
    if text.starts_with('+') && !text.starts_with("+++") {
        Style::default().fg(Color::Green)
    } else if text.starts_with('-') && !text.starts_with("---") {
        Style::default().fg(Color::Red)
    } else if text.starts_with("@@") {
        Style::default().fg(Color::Cyan)
    } else {
        block_style(kind)
    }
}

fn markdown_style(kind: codeswarm_transcript::BlockKind, text: &str) -> Style {
    let base = block_style(kind);
    let trimmed = text.trim();
    if trimmed.starts_with('#') {
        base.fg(Color::Cyan).add_modifier(Modifier::BOLD)
    } else if trimmed.starts_with("```") {
        base.fg(Color::Gray)
    } else if matches!(trimmed, "---" | "___" | "***") {
        base.fg(Color::DarkGray)
    } else if trimmed.contains('|') && trimmed.split('|').count() >= 3 {
        base.fg(Color::LightBlue)
    } else {
        base
    }
}

fn markdown_spans(
    kind: codeswarm_transcript::BlockKind,
    text: &str,
    in_code: &mut bool,
) -> Vec<Span<'static>> {
    if text.trim_start().starts_with("```") {
        *in_code = !*in_code;
        return vec![Span::styled(
            text.to_owned(),
            Style::default().fg(Color::Gray),
        )];
    }
    if *in_code {
        return vec![Span::styled(
            text.to_owned(),
            Style::default().fg(Color::LightCyan),
        )];
    }
    let base = markdown_style(kind, text);
    let mut spans = Vec::new();
    let mut remaining = text;
    let leading = text.len().saturating_sub(text.trim_start().len());
    let trimmed = &text[leading..];
    let marker_len =
        if trimmed.starts_with("> ") || trimmed.starts_with("- ") || trimmed.starts_with("* ") {
            Some(2)
        } else if trimmed
            .char_indices()
            .find(|(_, character)| *character == '.')
            .is_some_and(|(index, _)| {
                index > 0
                    && trimmed[..index]
                        .chars()
                        .all(|character| character.is_ascii_digit())
                    && trimmed.as_bytes().get(index + 1) == Some(&b' ')
            })
        {
            trimmed.find(' ').map(|index| index + 1)
        } else {
            None
        };
    if let Some(marker_len) = marker_len {
        if leading > 0 {
            spans.push(Span::styled(text[..leading].to_owned(), base));
        }
        let marker_end = leading + marker_len;
        let marker_style = if trimmed.starts_with("> ") {
            base.fg(Color::Cyan).add_modifier(Modifier::BOLD)
        } else {
            base.fg(Color::LightYellow).add_modifier(Modifier::BOLD)
        };
        spans.push(Span::styled(
            text[leading..marker_end].to_owned(),
            marker_style,
        ));
        remaining = &text[marker_end..];
    }
    while !remaining.is_empty() {
        let next = ["**", "*", "`"]
            .iter()
            .filter_map(|delimiter| remaining.find(delimiter).map(|index| (index, *delimiter)))
            .min_by_key(|(index, _)| *index);
        let Some((start, delimiter)) = next else {
            spans.extend(inline_text_spans(base, remaining));
            break;
        };
        if start > 0 {
            spans.extend(inline_text_spans(base, &remaining[..start]));
        }
        let content_start = start + delimiter.len();
        let Some(end_relative) = remaining[content_start..].find(delimiter) else {
            spans.extend(inline_text_spans(base, &remaining[start..]));
            break;
        };
        let end = content_start + end_relative;
        let mut style = base;
        style = match delimiter {
            "**" => style.add_modifier(Modifier::BOLD),
            "*" => style.add_modifier(Modifier::ITALIC),
            "`" => style.fg(Color::LightYellow),
            _ => style,
        };
        spans.push(Span::styled(
            remaining[content_start..end].to_owned(),
            style,
        ));
        remaining = &remaining[end + delimiter.len()..];
    }
    spans
}

/// Style Markdown links and source references without hiding their literal
/// target. Keeping the complete token visible is useful in a terminal, while
/// the accent makes it obvious that the text is actionable/reference-like.
fn inline_text_spans(base: Style, text: &str) -> Vec<Span<'static>> {
    let link_style = base.fg(Color::LightCyan).add_modifier(Modifier::UNDERLINED);
    let mut spans = Vec::new();
    let mut cursor = 0;
    while let Some(open_relative) = text[cursor..].find('[') {
        let open = cursor + open_relative;
        let Some(close_relative) = text[open + 1..].find("](") else {
            break;
        };
        let close = open + 1 + close_relative;
        let Some(end_relative) = text[close + 2..].find(')') else {
            break;
        };
        let end = close + 2 + end_relative + 1;
        if open > cursor {
            spans.extend(file_reference_spans(base, &text[cursor..open]));
        }
        spans.push(Span::styled(text[open..end].to_owned(), link_style));
        cursor = end;
    }
    if cursor < text.len() {
        spans.extend(file_reference_spans(base, &text[cursor..]));
    }
    if spans.is_empty() {
        file_reference_spans(base, text)
    } else {
        spans
    }
}

const FILE_REFERENCE_EXTENSIONS: &[&str] = &[
    "bash", "c", "cc", "cpp", "css", "go", "h", "hpp", "html", "ini", "java", "js", "json", "jsx",
    "kotlin", "kt", "md", "php", "py", "pyi", "rb", "rs", "sh", "sql", "swift", "toml", "ts",
    "tsx", "xml", "yaml", "yml",
];

/// Split prose into ordinary and source-file spans without a regex or a
/// heavyweight Markdown parser.  The Python client highlights the same
/// family of source references, including optional `:line` / `:line-line`
/// suffixes.  This stays allocation-light and runs only for the visible
/// transcript rows, never for the complete retained conversation.
fn file_reference_spans(base: Style, text: &str) -> Vec<Span<'static>> {
    let mut ranges = Vec::new();
    let mut token_start = None;
    for (index, character) in text.char_indices() {
        if is_file_token_character(character) {
            token_start.get_or_insert(index);
        } else if let Some(start) = token_start.take() {
            add_file_reference_range(text, start, index, &mut ranges);
        }
    }
    if let Some(start) = token_start {
        add_file_reference_range(text, start, text.len(), &mut ranges);
    }

    if ranges.is_empty() {
        return vec![Span::styled(text.to_owned(), base)];
    }
    let reference_style = base.fg(Color::LightCyan).add_modifier(Modifier::UNDERLINED);
    let mut spans = Vec::with_capacity(ranges.len() * 2 + 1);
    let mut cursor = 0;
    for (start, end) in ranges {
        if start > cursor {
            spans.push(Span::styled(text[cursor..start].to_owned(), base));
        }
        spans.push(Span::styled(text[start..end].to_owned(), reference_style));
        cursor = end;
    }
    if cursor < text.len() {
        spans.push(Span::styled(text[cursor..].to_owned(), base));
    }
    spans
}

fn is_file_token_character(character: char) -> bool {
    character.is_ascii_alphanumeric() || matches!(character, '/' | '.' | '_' | '-' | ':' | '~')
}

fn add_file_reference_range(
    text: &str,
    start: usize,
    end: usize,
    ranges: &mut Vec<(usize, usize)>,
) {
    let token = &text[start..end];
    let path_end = token
        .rfind(':')
        .filter(|colon| {
            let suffix = &token[colon + 1..];
            !suffix.is_empty()
                && suffix.chars().any(|character| character.is_ascii_digit())
                && suffix
                    .chars()
                    .all(|character| character.is_ascii_digit() || character == '-')
        })
        .unwrap_or(token.len());
    let path = &token[..path_end];
    let filename = path.rsplit('/').next().unwrap_or(path);
    let special_name = matches!(
        filename,
        "Dockerfile" | "Makefile" | "Justfile" | "Procfile"
    ) || filename == ".env"
        || filename.starts_with(".env.");
    let extension = filename.rsplit_once('.').map(|(_, extension)| extension);
    if (special_name
        || extension.is_some_and(|extension| {
            FILE_REFERENCE_EXTENSIONS.contains(&extension.to_ascii_lowercase().as_str())
        }))
        && !path.is_empty()
    {
        // Keep the optional source location attached to the reference. The
        // Python renderer treats `src/main.rs:42` as one destination span,
        // which makes line-addressed references easy to spot at a glance.
        ranges.push((start, end));
    }
}

fn looks_like_unified_diff(text: &str) -> bool {
    let mut has_hunk = false;
    let mut has_file_header = false;
    for line in text.lines() {
        has_hunk |= line.starts_with("@@");
        has_file_header |= line.starts_with("--- ") || line.starts_with("+++ ");
    }
    has_hunk && has_file_header
}

#[cfg(test)]
mod tests {
    use codeswarm_transcript::BlockKind;
    use ratatui::{
        Terminal,
        backend::TestBackend,
        layout::Rect,
        style::{Color, Modifier, Style},
    };
    use tui_textarea::{Input, Key};

    use super::{
        App, ConfigAction, ConfigKey, LocalCommand, PathPickerAction, PermissionAction,
        PermissionKey, PromptAction, PromptEditor, StoreAction, StoreAgent, StoreKey,
        agent_header_color, agent_slot_color, file_reference_spans, markdown_spans, markdown_style,
        render, row_style,
    };

    fn key(key: Key) -> Input {
        Input {
            key,
            ctrl: false,
            alt: false,
            shift: false,
        }
    }

    #[test]
    fn prompt_editor_supports_multiline_unicode_cursor_editing() {
        let mut editor = PromptEditor::default();
        for character in "héllo".chars() {
            assert_eq!(
                editor.handle_input(key(Key::Char(character))),
                PromptAction::Changed
            );
        }
        assert_eq!(editor.cursor(), (0, 5));
        assert_eq!(
            editor.handle_input(Input {
                key: Key::Enter,
                ctrl: false,
                alt: false,
                shift: true,
            }),
            PromptAction::Changed
        );
        for character in "世界".chars() {
            editor.handle_input(key(Key::Char(character)));
        }
        assert_eq!(editor.handle_input(key(Key::Left)), PromptAction::Changed);
        editor.handle_input(key(Key::Char('!')));
        assert_eq!(editor.text(), "héllo\n世!界");
        assert_eq!(editor.cursor(), (1, 2));
    }

    #[test]
    fn prompt_editor_submits_and_bounds_deduplicated_history() {
        let mut editor = PromptEditor::from_text("first\nsecond");
        assert_eq!(
            editor.handle_input(key(Key::Enter)),
            PromptAction::Submit("first\nsecond".into())
        );
        editor.remember("first\nsecond");
        assert_eq!(editor.history().len(), 1);
        for index in 0..55 {
            editor.remember(format!("prompt-{index}"));
        }
        assert_eq!(editor.history().len(), 50);
        assert_eq!(
            editor.history().front().map(String::as_str),
            Some("prompt-5")
        );
        assert!(editor.history_previous());
        assert_eq!(editor.text(), "prompt-54");
        assert!(editor.history_next());
        assert_eq!(editor.text(), "");
    }

    #[test]
    fn prompt_editor_cycles_slash_command_completions() {
        let mut editor = PromptEditor::from_text("/h");
        editor.set_completion_candidates(["/help", "/history", "/quit"]);
        assert!(editor.completion_matches().is_empty());
        assert_eq!(
            editor.handle_input(key(Key::Tab)),
            PromptAction::Completion {
                value: "/help".into(),
                index: 0,
                total: 2,
            }
        );
        assert_eq!(editor.text(), "/help");
        assert_eq!(editor.completion_matches(), &["/help", "/history"]);
        assert_eq!(
            editor.handle_input(key(Key::Tab)),
            PromptAction::Completion {
                value: "/history".into(),
                index: 1,
                total: 2,
            }
        );
        assert_eq!(editor.text(), "/history");
    }

    #[test]
    fn prompt_editor_renders_bounded_multiline_widget() {
        let backend = TestBackend::new(48, 8);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let editor = PromptEditor::from_text("review\nthese changes");
        terminal
            .draw(|frame| editor.render(frame, Rect::new(0, 0, 48, 8)))
            .expect("draw prompt editor");
        let rendered = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(rendered.contains("Prompt"), "rendered={rendered:?}");
        assert!(rendered.contains("review"));
        assert!(rendered.contains("these changes"));
    }

    #[test]
    fn app_routes_prompt_keys_through_editor_and_keeps_compatibility_text() {
        let mut app = App::default();
        for character in "first".chars() {
            assert_eq!(
                app.handle_prompt_input(key(Key::Char(character))),
                PromptAction::Changed
            );
        }
        assert_eq!(app.prompt, "first");
        assert_eq!(
            app.handle_prompt_input(Input {
                key: Key::Enter,
                shift: true,
                ..Input::default()
            }),
            PromptAction::Changed
        );
        app.handle_prompt_input(key(Key::Char('n')));
        assert_eq!(app.prompt, "first\nn");
        assert_eq!(
            app.handle_prompt_input(key(Key::Enter)),
            PromptAction::Submit("first\nn".into())
        );
        assert!(app.prompt.is_empty());
    }

    #[test]
    fn app_prompt_tab_completion_updates_compatibility_text() {
        let mut app = App::default();
        app.set_prompt_completions(["/help", "/history"]);
        app.handle_prompt_input(key(Key::Char('/')));
        app.handle_prompt_input(key(Key::Char('h')));
        assert!(matches!(
            app.handle_prompt_input(key(Key::Tab)),
            PromptAction::Completion { value, .. } if value == "/help"
        ));
        assert_eq!(app.prompt, "/help");
    }

    #[test]
    fn prompt_tab_completion_supports_workspace_at_paths() {
        let mut app = App::default();
        app.set_prompt_completions(["@src/main.rs", "@src/lib.rs"]);
        app.handle_prompt_input(key(Key::Char('@')));
        app.handle_prompt_input(key(Key::Char('s')));
        app.handle_prompt_input(key(Key::Char('r')));
        app.handle_prompt_input(key(Key::Char('c')));
        app.handle_prompt_input(key(Key::Char('m')));
        assert!(matches!(
            app.handle_prompt_input(key(Key::Tab)),
            PromptAction::Completion { value, .. } if value == "@src/main.rs"
        ));
        let mut editor = PromptEditor::default();
        editor.set_completion_candidates(["@src/main.rs", "@README.md"]);
        editor.handle_input(key(Key::Char('@')));
        editor.handle_input(key(Key::Char('m')));
        editor.handle_input(key(Key::Char('a')));
        editor.handle_input(key(Key::Char('i')));
        assert!(matches!(
            editor.handle_input(key(Key::Tab)),
            PromptAction::Completion { value, .. } if value == "@src/main.rs"
        ));
    }

    #[test]
    fn narrow_tmux_pane_clips_optional_regions_without_panicking() {
        let backend = TestBackend::new(18, 6);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App {
            prompt: "a deliberately long prompt that must remain editable".into(),
            ..App::default()
        };
        app.queue_prompt("a queued request", Some(1), false);
        app.apply_event(&codeswarm_core::AgentEvent::Permission {
            slot: 0,
            request: codeswarm_core::PermissionRequest {
                id: "narrow-permission".into(),
                title: "Allow this operation?".into(),
                options: vec!["Allow".into(), "Deny".into(), "Always".into()],
                option_ids: Vec::new(),
            },
        });
        app.toggle_keyboard_help();
        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("narrow pane draw");
        let rendered = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(rendered.contains(">"), "rendered={rendered:?}");
    }

    #[test]
    fn constrained_full_layout_keeps_permission_and_prompt_regions() {
        let backend = TestBackend::new(40, 7);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App {
            prompt: "review this".into(),
            ..App::default()
        };
        app.queue_prompt("queued request", Some(1), false);
        app.apply_event(&codeswarm_core::AgentEvent::Permission {
            slot: 0,
            request: codeswarm_core::PermissionRequest {
                id: "permission".into(),
                title: "Allow operation?".into(),
                options: vec!["Allow".into(), "Deny".into()],
                option_ids: Vec::new(),
            },
        });
        app.toggle_keyboard_help();
        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("constrained draw");
        let rendered = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(rendered.contains("permission:"), "rendered={rendered:?}");
        assert!(rendered.contains("Prompt"), "rendered={rendered:?}");
    }

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
    fn empty_transcript_stays_quiet() {
        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App::default();
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
        assert!(!rendered.contains("No messages yet"));
        assert!(!rendered.contains("Type a prompt below"));
        assert!(!rendered.contains("Conversation"));
    }

    #[test]
    fn local_shell_output_is_visible_in_the_transcript() {
        let backend = TestBackend::new(80, 12);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App::default();
        app.apply_event(&codeswarm_core::AgentEvent::Terminal {
            slot: 0,
            event: codeswarm_core::TerminalEvent::Output {
                id: "local-shell".into(),
                text: "shell-ok".into(),
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
        assert!(rendered.contains("shell-ok"), "rendered={rendered:?}");
    }

    #[test]
    fn unified_diff_lines_use_add_delete_and_hunk_colors() {
        assert_eq!(row_style(BlockKind::Diff, "+added").fg, Some(Color::Green));
        assert_eq!(row_style(BlockKind::Diff, "-removed").fg, Some(Color::Red));
        assert_eq!(
            row_style(BlockKind::Diff, "@@ -1 +1 @@").fg,
            Some(Color::Cyan)
        );
        assert_eq!(
            row_style(BlockKind::Diff, "+++ file").fg,
            Some(Color::Magenta)
        );
    }

    #[test]
    fn markdown_headings_keep_lightweight_visual_hierarchy() {
        assert_eq!(
            markdown_style(BlockKind::Agent, "## Summary").fg,
            Some(Color::Cyan)
        );
        assert_eq!(
            markdown_style(BlockKind::Agent, "normal text").fg,
            Some(Color::White)
        );
    }

    #[test]
    fn markdown_inline_emphasis_and_fenced_code_are_styled_without_reflow() {
        let mut in_code = false;
        let spans = markdown_spans(BlockKind::Agent, "**bold** and `code`", &mut in_code);
        assert!(
            spans.iter().any(|span| span.content == "bold"
                && span.style.add_modifier(Modifier::BOLD) == span.style)
        );
        assert!(
            spans
                .iter()
                .any(|span| span.content == "code" && span.style.fg == Some(Color::LightYellow))
        );
        let _ = markdown_spans(BlockKind::Agent, "```rust", &mut in_code);
        let code = markdown_spans(BlockKind::Agent, "let answer = 42;", &mut in_code);
        assert_eq!(code[0].style.fg, Some(Color::LightCyan));
        let mut in_code = false;
        let list = markdown_spans(BlockKind::Agent, "- item", &mut in_code);
        assert_eq!(list[0].content, "- ");
        assert_eq!(list[0].style.fg, Some(Color::LightYellow));
        let quote = markdown_spans(BlockKind::Agent, "> note", &mut in_code);
        assert_eq!(quote[0].style.fg, Some(Color::Cyan));
        let numbered = markdown_spans(BlockKind::Agent, "1. step", &mut in_code);
        assert_eq!(numbered[0].content, "1. ");
    }

    #[test]
    fn markdown_links_tables_and_rules_have_lightweight_styles() {
        let mut in_code = false;
        let links = markdown_spans(
            BlockKind::Agent,
            "see [the guide](https://example.test/docs)",
            &mut in_code,
        );
        assert!(links.iter().any(|span| {
            span.content == "[the guide](https://example.test/docs)"
                && span.style.add_modifier.contains(Modifier::UNDERLINED)
        }));
        assert_eq!(
            markdown_style(BlockKind::Agent, "| A | B |").fg,
            Some(Color::LightBlue)
        );
        assert_eq!(
            markdown_style(BlockKind::Agent, "---").fg,
            Some(Color::DarkGray)
        );
    }

    #[test]
    fn path_completion_waits_for_a_meaningful_query() {
        let mut editor = PromptEditor::default();
        editor.set_completion_candidates(["@src/main.rs", "@src/lib.rs"]);
        editor.handle_input(key(Key::Char('@')));
        assert!(matches!(
            editor.handle_input(key(Key::Tab)),
            PromptAction::Ignored
        ));

        editor.handle_input(key(Key::Char('s')));
        editor.handle_input(key(Key::Char('r')));
        editor.handle_input(key(Key::Char('c')));
        assert!(matches!(
            editor.handle_input(key(Key::Tab)),
            PromptAction::Completion { .. }
        ));
    }

    #[test]
    fn async_path_picker_ranks_and_inserts_a_workspace_file() {
        let root = std::env::temp_dir().join(format!(
            "codeswarm-tui-picker-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        std::fs::create_dir_all(root.join("src")).expect("workspace");
        std::fs::write(root.join("src/main.rs"), "fn main() {}").expect("file");

        let mut app = App::default();
        app.set_workspace_root(root.clone());
        for character in "@src/m".chars() {
            app.handle_prompt_input(key(Key::Char(character)));
        }
        for _ in 0..100 {
            app.poll_path_index();
            if app.path_picker_visible() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(2));
        }
        assert!(app.path_picker_visible());
        assert_eq!(app.path_matches()[app.path_selection()].path, "src/main.rs");
        assert!(matches!(
            app.handle_path_picker_key(Key::Enter),
            PathPickerAction::Insert(value) if value == "@src/main.rs "
        ));
        assert_eq!(app.prompt, "@src/main.rs ");
        assert!(!app.path_picker_visible());
        std::fs::remove_dir_all(root).expect("cleanup");
    }

    #[test]
    fn quoted_path_picker_keeps_spaces_inside_the_current_token() {
        let root = std::env::temp_dir().join(format!(
            "codeswarm-tui-quoted-picker-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        std::fs::create_dir_all(root.join("notes")).expect("workspace");
        std::fs::write(root.join("notes/project plan.md"), "plan").expect("file");
        let mut app = App::default();
        app.set_workspace_root(root.clone());
        for character in "@\"notes/pro".chars() {
            app.handle_prompt_input(key(Key::Char(character)));
        }
        for _ in 0..100 {
            app.poll_path_index();
            if app.path_picker_visible() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(2));
        }
        assert!(app.path_picker_visible());
        assert!(
            app.path_matches()
                .iter()
                .any(|candidate| { candidate.path == "notes/project plan.md" })
        );
        assert!(matches!(
            app.handle_path_picker_key(Key::Enter),
            PathPickerAction::Insert(value) if value == "@\"notes/project plan.md\" "
        ));
        assert_eq!(app.prompt, "@\"notes/project plan.md\" ");
        std::fs::remove_dir_all(root).expect("cleanup");
    }

    #[test]
    fn file_references_are_accented_with_line_suffix_attached() {
        let spans = file_reference_spans(
            Style::default().fg(Color::White),
            "see src/main.rs:42 and README",
        );
        assert_eq!(spans.len(), 3);
        assert_eq!(spans[0].content, "see ");
        assert_eq!(spans[1].content, "src/main.rs:42");
        assert!(spans[1].style.add_modifier == Modifier::UNDERLINED);
        assert_eq!(spans[2].content, " and README");
    }

    #[test]
    fn file_references_inside_inline_code_are_not_restyled() {
        let mut in_code = false;
        let spans = markdown_spans(BlockKind::Agent, "`src/main.rs`", &mut in_code);
        assert_eq!(spans.len(), 1);
        assert_ne!(spans[0].style.add_modifier, Modifier::UNDERLINED);
    }

    #[test]
    fn appended_detail_remains_visible_at_the_tail_of_a_long_transcript() {
        let backend = TestBackend::new(80, 12);
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
        app.transcript.append(BlockKind::Tool, "shell-ok", false);
        app.follow_tail(80, 8);
        let tail = app.transcript.viewport(78, app.scroll_y, 8, 0);
        assert!(
            tail.iter().any(|row| row.text.contains("shell-ok")),
            "tail={tail:?}, scroll={}",
            app.scroll_y
        );
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
        assert!(rendered.contains("shell-ok"), "rendered={rendered:?}");
    }

    #[test]
    fn narrow_terminal_keeps_status_transcript_and_prompt_visible() {
        let backend = TestBackend::new(30, 6);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App::default();
        app.set_header("Very Long Agent Name", "streaming");
        app.prompt = "check status".into();
        app.transcript
            .append(BlockKind::Agent, "response is visible", false);
        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("draw compact");
        let rendered = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(rendered.contains("streaming"));
        assert!(rendered.contains("response"));
        assert!(rendered.contains("> check status"));
    }

    #[test]
    fn failure_status_is_visible_and_uses_error_color() {
        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App::default();
        app.apply_event(&codeswarm_core::AgentEvent::Failed {
            slot: 0,
            started: true,
            detail: "connection lost".into(),
        });
        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("draw error");
        let content = terminal.backend().buffer().content();
        assert!(content.iter().any(|cell| cell.symbol() == "c"));
        assert!(
            content
                .iter()
                .any(|cell| cell.fg == ratatui::style::Color::Red)
        );
        assert!(app.status.contains("/reload"));
    }

    #[test]
    fn ready_event_preserves_human_readable_agent_name() {
        let mut app = App::default();
        app.set_agent_name(0, "Codex CLI");
        app.apply_event(&codeswarm_core::AgentEvent::Ready {
            slot: 0,
            capabilities: codeswarm_core::AgentCapabilities::default(),
        });
        assert_eq!(app.active_agent, "Codex CLI");
    }

    #[test]
    fn loaded_roster_names_are_visible_before_the_first_response() {
        let backend = TestBackend::new(96, 12);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App::default();
        app.set_agent_name(0, "Claude Code");
        app.set_agent_name(1, "Codex CLI");
        app.set_header("CodeSwarm roster", "starting");
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
        assert!(rendered.contains("Claude Code"), "rendered={rendered:?}");
        assert!(rendered.contains("Codex CLI"), "rendered={rendered:?}");
    }

    #[test]
    fn agent_headers_use_deterministic_distinct_identity_colors() {
        assert_eq!(
            agent_header_color("Claude Code"),
            agent_header_color("Claude Code")
        );
        assert_ne!(agent_header_color("a"), agent_header_color("b"));
    }

    #[test]
    fn duplicate_agent_names_are_numbered_and_keep_distinct_colors() {
        let mut app = App::default();
        app.set_agent_name(0, "Claude Code");
        app.set_agent_name(1, "Claude Code");

        assert_eq!(app.agent_name(0), "Claude Code #1");
        assert_eq!(app.agent_name(1), "Claude Code #2");
        assert_ne!(
            agent_header_color(&app.agent_name(0)),
            agent_header_color(&app.agent_name(1))
        );
        assert!(app.roster_summary().contains("Claude Code #1"));
        assert!(app.roster_summary().contains("Claude Code #2"));
    }

    #[test]
    fn footer_and_transcript_use_the_same_slot_color_for_each_agent() {
        let backend = TestBackend::new(96, 14);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App::default();
        app.set_agent_name(0, "Codex CLI");
        app.set_agent_name(1, "Gemini CLI");
        app.apply_event(&codeswarm_core::AgentEvent::Text {
            slot: 1,
            text: "review".into(),
        });
        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("draw colored roster");
        let cells = terminal.backend().buffer().content();
        let footer_color = cells
            .iter()
            .find(|cell| cell.symbol() == "G")
            .map(|cell| cell.fg)
            .expect("Gemini footer/header cell");
        assert_eq!(footer_color, agent_slot_color(1));
        assert_ne!(footer_color, agent_slot_color(0));
    }

    #[test]
    fn promoting_a_live_agent_moves_its_identity_and_marks_old_owner_dropped() {
        let mut app = App::default();
        app.set_agent_name(0, "Claude Code");
        app.set_agent_name(1, "Codex CLI");
        app.apply_event(&codeswarm_core::AgentEvent::Ready {
            slot: 0,
            capabilities: codeswarm_core::AgentCapabilities::default(),
        });
        app.apply_event(&codeswarm_core::AgentEvent::Ready {
            slot: 1,
            capabilities: codeswarm_core::AgentCapabilities::default(),
        });

        assert!(app.promote_agent(1));
        assert_eq!(app.agent_name(0), "Codex CLI");
        assert_eq!(app.agent_name(1), "Claude Code");
        assert_eq!(
            app.agent_states.get(&1).map(String::as_str),
            Some("dropped")
        );
        assert_eq!(app.active_agent, "Codex CLI");
        assert!(!app.promote_agent(1));
    }

    #[test]
    fn swapping_live_agents_moves_their_visible_state_without_touching_dropped_slots() {
        let mut app = App::default();
        app.set_agent_name(0, "Claude Code");
        app.set_agent_name(1, "Codex CLI");
        app.set_agent_name(2, "Gemini CLI");
        app.mark_agent_dropped(2);
        assert!(app.swap_agents(0, 1));
        assert_eq!(app.agent_name(0), "Codex CLI");
        assert_eq!(app.agent_name(1), "Claude Code");
        assert!(!app.swap_agents(0, 2));
        assert_eq!(app.agent_name(2), "Gemini CLI");
    }

    #[test]
    fn local_commands_do_not_become_agent_prompts() {
        let mut app = App::default();
        assert_eq!(
            app.handle_local_command("/config"),
            Some(LocalCommand::Handled)
        );
        assert_eq!(app.status, "configuration");
        assert!(app.config_visible());
        assert_eq!(
            app.handle_config_key(ConfigKey::Confirm),
            ConfigAction::Changed
        );
        assert!(!app.follow_tail);
        assert_eq!(
            app.handle_config_key(ConfigKey::Cancel),
            ConfigAction::Close
        );
        assert_eq!(
            app.handle_local_command("/close"),
            Some(LocalCommand::Close)
        );
        assert_eq!(app.handle_local_command("ordinary text"), None);
    }

    #[test]
    fn commands_update_mode_and_collaboration_without_agent_dispatch() {
        let mut app = App::default();
        assert_eq!(
            app.handle_local_command("/mode chat"),
            Some(LocalCommand::Mode)
        );
        assert_eq!(app.mode(), "Chat");
        assert_eq!(
            app.handle_local_command("/collab manual"),
            Some(LocalCommand::Collaboration)
        );
        assert_eq!(app.collaboration(), "Manual routing");
        assert_eq!(
            app.handle_local_command("/collab invalid"),
            Some(LocalCommand::Handled)
        );
        assert_eq!(
            app.handle_local_command("/agents"),
            Some(LocalCommand::Agents)
        );
        assert_eq!(
            app.handle_local_command("/add acp:reviewer --acp"),
            Some(LocalCommand::Add("acp:reviewer --acp".into()))
        );
        assert_eq!(app.handle_local_command("/drop"), Some(LocalCommand::Drop));
        assert_eq!(
            app.handle_local_command("/drop 2"),
            Some(LocalCommand::DropSlot(2))
        );
        assert_eq!(
            app.handle_local_command("/promote 2"),
            Some(LocalCommand::Promote(2))
        );
        assert_eq!(
            app.handle_local_command("/swap 0 2"),
            Some(LocalCommand::Swap(0, 2))
        );
        assert_eq!(
            app.handle_local_command("/cd /tmp"),
            Some(LocalCommand::Directory("/tmp".into()))
        );
        assert_eq!(
            app.handle_local_command("/diff split"),
            Some(LocalCommand::Diff)
        );
        assert!(app.diff_split());
    }

    #[test]
    fn advertised_mode_catalog_drives_the_config_mode_cycle() {
        let mut app = App::default();
        app.apply_event(&codeswarm_core::AgentEvent::ModesReplaced {
            slot: 0,
            modes: vec![
                codeswarm_core::Mode {
                    id: "plan".into(),
                    label: "Plan".into(),
                },
                codeswarm_core::Mode {
                    id: "full-access".into(),
                    label: "Auto pilot".into(),
                },
            ],
            current_mode: Some("full-access".into()),
        });
        app.handle_local_command("/config");
        app.handle_config_key(ConfigKey::Down);
        app.handle_config_key(ConfigKey::Down);
        app.handle_config_key(ConfigKey::Down);
        assert_eq!(
            app.handle_config_key(ConfigKey::Confirm),
            ConfigAction::Changed
        );
        assert_eq!(app.mode(), "Plan");
        assert_eq!(
            app.take_requested_mode(),
            Some("codeswarm:mode:plan".into())
        );
    }

    #[test]
    fn config_roster_rows_toggle_and_reorder_without_losing_selection() {
        let mut app = App::default();
        app.set_config_agents(vec![
            StoreAgent {
                identity: "one.example".into(),
                name: "One".into(),
                adapter: "ACP".into(),
                command: "one --acp".into(),
                available: true,
                selected: true,
            },
            StoreAgent {
                identity: "two.example".into(),
                name: "Two".into(),
                adapter: "native".into(),
                command: "two".into(),
                available: true,
                selected: false,
            },
        ]);
        app.handle_local_command("/config");
        for _ in 0..15 {
            app.handle_config_key(ConfigKey::Down);
        }
        assert_eq!(
            app.handle_config_key(ConfigKey::Confirm),
            ConfigAction::Changed
        );
        assert_eq!(
            app.config_roster_identities(),
            ["one.example", "two.example"]
        );
        assert!(app.config_roster_dirty());
        assert_eq!(
            app.handle_config_key(ConfigKey::MoveUp),
            ConfigAction::Changed
        );
        assert_eq!(
            app.config_roster_identities(),
            ["two.example", "one.example"]
        );
        assert_eq!(app.handle_config_key(ConfigKey::Save), ConfigAction::Close);
        assert!(!app.config_visible());
    }

    #[test]
    fn workspace_path_picker_indexes_off_thread_and_inserts_selected_path() {
        let root = std::env::temp_dir().join(format!(
            "codeswarm-tui-path-picker-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        std::fs::create_dir_all(root.join("src")).expect("workspace");
        std::fs::write(root.join("src/main.rs"), "fn main() {}\n").expect("source");

        let mut app = App::default();
        app.set_workspace_root(&root);
        for input_key in [
            Key::Char('@'),
            Key::Char('s'),
            Key::Char('r'),
            Key::Char('c'),
        ] {
            app.handle_prompt_input(key(input_key));
        }
        for _ in 0..100 {
            app.poll_path_index();
            if app.path_picker_visible() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
        assert!(app.path_picker_visible());
        assert!(
            app.path_matches()
                .iter()
                .any(|candidate| candidate.path == "src")
        );
        assert!(matches!(
            app.handle_path_picker_key(Key::Down),
            PathPickerAction::Changed | PathPickerAction::Ignored
        ));
        let _ = app.handle_path_picker_key(Key::Enter);
        assert!(app.prompt.contains("@src"));
        std::fs::remove_dir_all(root).expect("cleanup workspace");
    }

    #[test]
    fn notification_preference_is_a_persistent_config_value() {
        let mut app = App::default();
        app.handle_local_command("/config");
        app.handle_config_key(ConfigKey::Down);
        app.handle_config_key(ConfigKey::Down);
        assert!(app.notifications_enabled());
        assert_eq!(app.notification_policy().as_str(), "blur");
        assert_eq!(
            app.handle_config_key(ConfigKey::Confirm),
            ConfigAction::Changed
        );
        assert!(app.notifications_enabled());
        assert_eq!(app.notification_policy().as_str(), "always");
    }

    #[test]
    fn system_notifications_are_disabled_while_terminal_is_focused() {
        let mut app = App::default();
        assert!(app.terminal_focused());
        app.set_terminal_focused(false);
        assert!(!app.terminal_focused());
        app.set_terminal_focused(true);
        assert!(app.terminal_focused());
    }

    #[test]
    fn notification_policy_preserves_python_blur_always_never_semantics() {
        let mut app = App::default();
        assert_eq!(app.notification_policy().as_str(), "blur");
        assert!(!app.should_notify_system());

        app.set_notification_policy("blur");
        assert!(!app.should_notify_system());
        app.set_terminal_focused(false);
        assert!(app.should_notify_system());

        app.set_notification_policy("always");
        app.set_terminal_focused(true);
        assert!(app.should_notify_system());

        app.set_notification_policy("invalid");
        assert!(!app.should_notify_system());
    }

    #[test]
    fn terminal_title_alerts_are_reference_counted_and_sanitized() {
        let mut app = App::default();
        app.set_header("agent\nwith\tescape", "idle");
        assert_eq!(app.terminal_title(), "✈ agentwithescape · CodeSwarm");
        assert!(!app.terminal_alert_active());

        app.terminal_alert(true);
        app.terminal_alert(true);
        assert!(app.terminal_alert_active());
        app.toggle_terminal_title_blink();
        assert_eq!(app.terminal_title(), "👉 agentwithescape · CodeSwarm");
        app.terminal_alert(false);
        assert!(app.terminal_alert_active());
        app.terminal_alert(false);
        assert!(!app.terminal_alert_active());
        assert!(!app.terminal_title_blink());
        assert_eq!(app.terminal_title(), "✈ agentwithescape · CodeSwarm");
    }

    #[test]
    fn blink_title_is_a_persistent_config_toggle() {
        let mut app = App::default();
        assert!(app.blink_title_enabled());
        app.handle_local_command("/config");
        for _ in 0..11 {
            app.handle_config_key(ConfigKey::Down);
        }
        assert_eq!(
            app.handle_config_key(ConfigKey::Confirm),
            ConfigAction::Changed
        );
        assert!(!app.blink_title_enabled());
        app.terminal_alert(true);
        app.toggle_terminal_title_blink();
        assert!(!app.terminal_title_blink());
    }

    #[test]
    fn detail_preferences_control_thought_and_tool_initial_visibility() {
        let mut app = App::default();
        app.handle_local_command("/config");
        for _ in 0..6 {
            app.handle_config_key(ConfigKey::Down);
        }
        assert_eq!(
            app.handle_config_key(ConfigKey::Confirm),
            ConfigAction::Changed
        );
        app.handle_config_key(ConfigKey::Down);
        assert_eq!(
            app.handle_config_key(ConfigKey::Confirm),
            ConfigAction::Changed
        );
        assert!(app.thoughts_enabled());
        assert!(app.tools_expanded());
        app.apply_event(&codeswarm_core::AgentEvent::Thought {
            slot: 0,
            text: "thinking".into(),
        });
        assert!(app.transcript.row_count(80) > 0);
        app.apply_event(&codeswarm_core::AgentEvent::Tool {
            slot: 0,
            update: codeswarm_core::ToolUpdate {
                id: "tool".into(),
                title: "Run".into(),
                status: codeswarm_core::ToolStatus::Completed,
                detail: Some("detail".into()),
            },
        });
        assert!(app.transcript.row_count(80) > 1);
    }

    #[test]
    fn scrollbar_preference_toggles_the_cached_indicator() {
        let mut app = App::default();
        app.handle_local_command("/config");
        for _ in 0..9 {
            app.handle_config_key(ConfigKey::Down);
        }
        assert!(app.scrollbar_visible());
        assert_eq!(
            app.handle_config_key(ConfigKey::Confirm),
            ConfigAction::Changed
        );
        assert!(!app.scrollbar_visible());
    }

    #[test]
    fn sound_preference_is_separate_from_completion_notifications() {
        let mut app = App::default();
        assert!(app.sounds_enabled());
        assert!(app.notifications_enabled());
        app.handle_local_command("/config");
        for _ in 0..10 {
            app.handle_config_key(ConfigKey::Down);
        }
        assert_eq!(
            app.handle_config_key(ConfigKey::Confirm),
            ConfigAction::Changed
        );
        assert!(!app.sounds_enabled());
        assert!(app.notifications_enabled());
    }

    #[test]
    fn tool_expand_policy_matches_python_fail_always_never_choices() {
        let tool = |status| codeswarm_core::AgentEvent::Tool {
            slot: 0,
            update: codeswarm_core::ToolUpdate {
                id: "tool".into(),
                title: "Run".into(),
                status,
                detail: Some("line one\nline two".into()),
            },
        };

        // The default `fail` policy keeps successful details collapsed but
        // surfaces a failed tool for immediate diagnosis.
        let mut app = App::default();
        app.apply_event(&tool(codeswarm_core::ToolStatus::Completed));
        assert_eq!(app.transcript.row_count(80), 1);
        let mut failed = App::default();
        failed.apply_event(&tool(codeswarm_core::ToolStatus::Failed));
        assert!(failed.transcript.row_count(80) > 1);

        let mut always = App::default();
        always.set_tool_expand_policy("always");
        always.apply_event(&tool(codeswarm_core::ToolStatus::Completed));
        assert!(always.transcript.row_count(80) > 1);

        let mut never = App::default();
        never.set_tool_expand_policy("never");
        never.apply_event(&tool(codeswarm_core::ToolStatus::Failed));
        assert_eq!(never.transcript.row_count(80), 1);
    }

    #[test]
    fn density_is_a_stable_setting_and_compact_limits_prompt_height() {
        let mut app = App::default();
        app.prompt_editor.set_text("one\ntwo\nthree\nfour");
        let comfortable_height = app.content_height(24);
        assert_eq!(app.density(), "comfortable");
        app.set_density("compact");
        assert_eq!(app.density(), "compact");
        assert!(app.content_height(24) > comfortable_height);
        app.set_density("unknown-value");
        assert_eq!(app.density(), "comfortable");
    }

    #[test]
    fn clear_command_resets_streaming_detail_state() {
        let mut app = App::default();
        app.apply_event(&codeswarm_core::AgentEvent::Text {
            slot: 0,
            text: "in progress".into(),
        });
        assert_eq!(app.transcript.len(), 1);
        assert_eq!(
            app.handle_local_command("/clear"),
            Some(LocalCommand::Handled)
        );
        assert!(app.transcript.is_empty());
        app.apply_event(&codeswarm_core::AgentEvent::Text {
            slot: 0,
            text: "fresh".into(),
        });
        assert_eq!(app.transcript.len(), 1);
        assert_eq!(
            app.transcript.viewport(80, 0, 10, 0)[0].text,
            "Agent 0: fresh"
        );
    }

    #[test]
    fn config_panel_is_readable_and_does_not_render_the_transcript() {
        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App::default();
        app.set_config_agents(vec![StoreAgent {
            identity: "codex.example".into(),
            name: "Codex CLI".into(),
            adapter: "ACP".into(),
            command: "codex --acp".into(),
            available: true,
            selected: true,
        }]);
        app.handle_local_command("/config");
        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("draw config");
        let rendered = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(rendered.contains("Configuration"), "rendered={rendered:?}");
        assert!(rendered.contains("Follow output"), "rendered={rendered:?}");
        assert!(
            rendered.contains("Collapse details"),
            "rendered={rendered:?}"
        );
        assert!(rendered.contains("Renderer"), "rendered={rendered:?}");
        assert!(rendered.contains("Codex CLI"), "rendered={rendered:?}");
        assert!(
            !rendered.contains("No messages yet"),
            "rendered={rendered:?}"
        );
    }

    #[test]
    fn help_panel_lists_local_commands() {
        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App::default();
        assert_eq!(
            app.handle_local_command("/help"),
            Some(LocalCommand::Handled)
        );
        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("draw help");
        let rendered = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(rendered.contains("/export"), "rendered={rendered:?}");
        assert!(rendered.contains("/collab"), "rendered={rendered:?}");
        assert!(rendered.contains("/mode"), "rendered={rendered:?}");
    }

    #[test]
    fn agent_store_selects_reorders_and_launches_a_roster() {
        let mut app = App::default();
        app.show_store(vec![
            StoreAgent {
                identity: "one.example".into(),
                name: "One".into(),
                adapter: "ACP".into(),
                command: "one-acp".into(),
                available: true,
                selected: false,
            },
            StoreAgent {
                identity: "two.example".into(),
                name: "Two".into(),
                adapter: "native".into(),
                command: "two".into(),
                available: false,
                selected: false,
            },
        ]);
        assert_eq!(app.handle_store_key(StoreKey::Toggle), StoreAction::Changed);
        assert_eq!(
            app.handle_store_key(StoreKey::Save),
            StoreAction::Save(vec![0])
        );
        assert_eq!(app.handle_store_key(StoreKey::Down), StoreAction::Changed);
        assert_eq!(app.handle_store_key(StoreKey::Toggle), StoreAction::Changed);
        assert_eq!(app.handle_store_key(StoreKey::MoveUp), StoreAction::Changed);
        assert_eq!(
            app.handle_store_key(StoreKey::Confirm),
            StoreAction::Launch(vec![0, 1])
        );
        assert!(!app.store_visible());
        assert_eq!(app.store_agents()[0].identity, "two.example");
    }

    #[test]
    fn agent_store_renders_availability_and_command_details() {
        let backend = TestBackend::new(72, 18);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App::default();
        app.show_store(vec![StoreAgent {
            identity: "custom.example".into(),
            name: "Custom Agent".into(),
            adapter: "ACP".into(),
            command: "custom-agent --acp".into(),
            available: false,
            selected: false,
        }]);
        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("draw store");
        let rendered = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(
            rendered.contains("Choose your agents"),
            "rendered={rendered:?}"
        );
        assert!(rendered.contains("Custom Agent"), "rendered={rendered:?}");
        assert!(rendered.contains("not found"), "rendered={rendered:?}");
        assert!(
            rendered.contains("custom-agent --acp"),
            "rendered={rendered:?}"
        );
    }

    #[test]
    fn store_directory_editor_accepts_a_new_workspace() {
        let mut app = App::default();
        app.show_store(vec![StoreAgent {
            identity: "agent.example".into(),
            name: "Agent".into(),
            adapter: "ACP".into(),
            command: "agent".into(),
            available: true,
            selected: false,
        }]);
        app.set_store_directory("");
        app.begin_store_directory_edit();
        app.handle_store_directory_input(key(Key::Char('/')));
        for character in "tmp".chars() {
            app.handle_store_directory_input(key(Key::Char(character)));
        }
        assert_eq!(
            app.handle_store_directory_input(key(Key::Enter)),
            StoreAction::Directory("/tmp".into())
        );
        assert_eq!(app.store_directory(), "/tmp");
        assert!(!app.store_editing_directory());
    }

    #[test]
    fn compact_config_and_store_surfaces_fit_a_mobile_sized_pane() {
        let backend = TestBackend::new(32, 8);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut app = App::default();
        app.handle_local_command("/config");
        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("draw compact config");
        let config = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(config.contains("Follow"), "rendered={config:?}");
        for _ in 0..12 {
            app.handle_config_key(ConfigKey::Down);
        }
        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("draw scrolled compact config");
        let scrolled_config = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(
            scrolled_config.contains("Sound"),
            "rendered={scrolled_config:?}"
        );
        app.handle_config_key(ConfigKey::Cancel);
        app.show_store(vec![StoreAgent {
            identity: "mobile.example".into(),
            name: "Mobile Agent".into(),
            adapter: "ACP".into(),
            command: "mobile-agent".into(),
            available: true,
            selected: false,
        }]);
        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("draw compact store");
        let store = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(store.contains("Mobile Agent"), "rendered={store:?}");
        assert!(store.contains("save"), "rendered={store:?}");
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
            "Agent 0: first second"
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
    fn tool_diff_payload_is_retained_and_classified_lazily() {
        let mut app = App::default();
        app.apply_event(&codeswarm_core::AgentEvent::Tool {
            slot: 0,
            update: codeswarm_core::ToolUpdate {
                id: "patch".into(),
                title: "Apply patch".into(),
                status: codeswarm_core::ToolStatus::Completed,
                detail: Some("--- a/file.rs\n+++ b/file.rs\n@@ -1 +1 @@\n-old\n+new".into()),
            },
        });
        let rows = app.transcript.viewport(80, 0, 4, 0);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].kind, BlockKind::Diff);
        assert!(rows[0].text.contains("Diff"));
        assert_eq!(app.toggle_focused_detail(), Some(false));
        assert!(app.transcript.row_count(80) > 1);
        app.set_diff_split(true);
        let backend = TestBackend::new(80, 12);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("draw split diff");
        let rendered = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(rendered.contains("original"), "rendered={rendered:?}");
        assert!(rendered.contains("updated"), "rendered={rendered:?}");
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
                option_ids: vec!["allow-once".into(), "always".into(), "deny".into()],
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
                option_id: "deny".into(),
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
                option_ids: Vec::new(),
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
                option_ids: Vec::new(),
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
                option_ids: Vec::new(),
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
                option_ids: Vec::new(),
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
