//! Immutable, viewport-oriented transcript data.
//!
//! This crate deliberately has no terminal or async dependencies. Rendering a
//! scroll position is a lookup over cached rows; it never reparses the full
//! transcript, talks to an adapter, or waits for persistence.

use std::collections::BTreeMap;

/// A logical transcript item. The source is retained for copy/export even
/// when its rendered detail is collapsed.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TranscriptBlock {
    pub id: u64,
    pub kind: BlockKind,
    pub source: String,
    pub collapsed: bool,
}

/// The renderer's stable, presentation-neutral vocabulary.
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum BlockKind {
    Human,
    Agent,
    Thought,
    Tool,
    Diff,
    Notice,
}

/// A rendered terminal row. Rows borrow nothing so a terminal renderer can
/// retain a frame independently of the transcript mutation lock.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RenderRow {
    pub block_id: u64,
    pub text: String,
}

/// Maps blocks to the terminal rows produced at a particular width.
#[derive(Clone, Debug, Default)]
pub struct Transcript {
    blocks: Vec<TranscriptBlock>,
    next_id: u64,
    cached_width: Option<usize>,
    rows: Vec<RenderRow>,
    block_starts: BTreeMap<u64, usize>,
}

impl Transcript {
    /// Append a completed logical block. Row materialization is deferred until
    /// a viewport requests it at a concrete width.
    pub fn append(&mut self, kind: BlockKind, source: impl Into<String>, collapsed: bool) -> u64 {
        let id = self.next_id;
        self.next_id = self.next_id.saturating_add(1);
        self.blocks.push(TranscriptBlock {
            id,
            kind,
            source: source.into(),
            collapsed,
        });
        self.invalidate_rows();
        id
    }

    /// Number of durable logical blocks, not currently visible terminal rows.
    pub fn len(&self) -> usize {
        self.blocks.len()
    }

    pub fn is_empty(&self) -> bool {
        self.blocks.is_empty()
    }

    /// Toggle one block's detail. No other source is reparsed until a caller
    /// asks for a viewport.
    pub fn set_collapsed(&mut self, id: u64, collapsed: bool) -> bool {
        let Some(block) = self.blocks.iter_mut().find(|block| block.id == id) else {
            return false;
        };
        if block.collapsed != collapsed {
            block.collapsed = collapsed;
            self.invalidate_rows();
        }
        true
    }

    /// Render height rows beginning at scroll_y, including a bounded overscan
    /// margin. Steady-state scrolling clones an indexed slice.
    pub fn viewport(
        &mut self,
        width: usize,
        scroll_y: usize,
        height: usize,
        overscan: usize,
    ) -> Vec<RenderRow> {
        self.ensure_rows(width);
        let start = scroll_y.saturating_sub(overscan).min(self.rows.len());
        let end = scroll_y
            .saturating_add(height)
            .saturating_add(overscan)
            .min(self.rows.len());
        self.rows[start..end].to_vec()
    }

    /// Total cached rows at width. Calling this after a resize performs one
    /// rewrap, never one rewrap per scroll tick.
    pub fn row_count(&mut self, width: usize) -> usize {
        self.ensure_rows(width);
        self.rows.len()
    }

    /// First cached row of a logical block, for jump-to-message.
    pub fn block_row(&mut self, width: usize, id: u64) -> Option<usize> {
        self.ensure_rows(width);
        self.block_starts.get(&id).copied()
    }

    fn invalidate_rows(&mut self) {
        self.cached_width = None;
        self.rows.clear();
        self.block_starts.clear();
    }

    fn ensure_rows(&mut self, width: usize) {
        let width = width.max(1);
        if self.cached_width == Some(width) {
            return;
        }

        self.rows.clear();
        self.block_starts.clear();
        for block in &self.blocks {
            self.block_starts.insert(block.id, self.rows.len());
            let source = display_source(block);
            for line in wrap(&source, width) {
                self.rows.push(RenderRow {
                    block_id: block.id,
                    text: line,
                });
            }
        }
        self.cached_width = Some(width);
    }
}

fn display_source(block: &TranscriptBlock) -> String {
    if !block.collapsed {
        return block.source.clone();
    }
    let words = block.source.split_whitespace().count();
    format!(
        "{} · {} words · press Enter to expand",
        label(block.kind),
        words
    )
}

fn label(kind: BlockKind) -> &'static str {
    match kind {
        BlockKind::Human => "You",
        BlockKind::Agent => "Agent response",
        BlockKind::Thought => "Thought",
        BlockKind::Tool => "Tool",
        BlockKind::Diff => "Diff",
        BlockKind::Notice => "CodeSwarm",
    }
}

fn wrap(source: &str, width: usize) -> Vec<String> {
    let mut rows = Vec::new();
    for original_line in source.lines() {
        if original_line.is_empty() {
            rows.push(String::new());
            continue;
        }
        let mut row = String::new();
        for word in original_line.split_whitespace() {
            let separator = usize::from(!row.is_empty());
            if !row.is_empty() && row.chars().count() + separator + word.chars().count() > width {
                rows.push(std::mem::take(&mut row));
            }
            if word.chars().count() > width && row.is_empty() {
                let mut fragment = String::new();
                for character in word.chars() {
                    fragment.push(character);
                    if fragment.chars().count() == width {
                        rows.push(std::mem::take(&mut fragment));
                    }
                }
                row = fragment;
            } else {
                if !row.is_empty() {
                    row.push(' ');
                }
                row.push_str(word);
            }
        }
        if !row.is_empty() {
            rows.push(row);
        }
    }
    if source.is_empty() || source.ends_with('\n') {
        rows.push(String::new());
    }
    rows
}

/// Deterministic fixtures used by unit and tmux performance harnesses.
pub mod fixtures {
    use super::{BlockKind, Transcript};

    pub fn five_thousand_word_reply() -> String {
        (0..5_000)
            .map(|index| format!("word{index}"))
            .collect::<Vec<_>>()
            .join(" ")
    }

    pub fn hundred_turn_transcript() -> Transcript {
        let mut transcript = Transcript::default();
        let message = (0..300)
            .map(|index| format!("word{index}"))
            .collect::<Vec<_>>()
            .join(" ");
        for index in 0..100 {
            transcript.append(BlockKind::Human, format!("human {index} {message}"), false);
            transcript.append(BlockKind::Agent, format!("agent {index} {message}"), false);
        }
        transcript
    }
}

#[cfg(test)]
mod tests {
    use super::{BlockKind, Transcript, fixtures};

    #[test]
    fn single_long_reply_is_viewport_bounded() {
        let mut transcript = Transcript::default();
        transcript.append(
            BlockKind::Agent,
            fixtures::five_thousand_word_reply(),
            false,
        );

        let total_rows = transcript.row_count(80);
        assert!(total_rows > 100);
        let rows = transcript.viewport(80, total_rows / 2, 24, 8);
        assert!(rows.len() <= 40);
        assert!(rows.iter().all(|row| row.block_id == 0));
    }

    #[test]
    fn collapsed_detail_does_not_materialize_source_rows() {
        let mut transcript = Transcript::default();
        let id = transcript.append(BlockKind::Tool, fixtures::five_thousand_word_reply(), true);
        assert_eq!(transcript.row_count(80), 1);
        assert!(transcript.set_collapsed(id, false));
        assert!(transcript.row_count(80) > 100);
    }

    #[test]
    fn repeated_scrolls_at_same_width_are_stable() {
        let mut transcript = fixtures::hundred_turn_transcript();
        let total = transcript.row_count(80);
        let first = transcript.viewport(80, total / 3, 24, 8);
        let second = transcript.viewport(80, total / 3 + 1, 24, 8);
        assert!(first.len() <= 40);
        assert!(second.len() <= 40);
    }
}
