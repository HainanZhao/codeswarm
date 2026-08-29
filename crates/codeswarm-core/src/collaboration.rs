//! Bounded public context shared between sequential relay participants.

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PublicEvent {
    pub speaker: String,
    pub text: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CollaborationContext {
    events: Vec<PublicEvent>,
    seen: Vec<usize>,
    truncated: Vec<bool>,
}

impl CollaborationContext {
    pub fn new(agent_count: usize) -> Self {
        Self {
            events: Vec::new(),
            seen: vec![0; agent_count],
            truncated: vec![false; agent_count],
        }
    }

    pub fn record(&mut self, speaker: impl Into<String>, text: impl Into<String>, active: &[bool]) {
        self.prune(active);
        self.events.push(PublicEvent {
            speaker: speaker.into(),
            text: compact(text.into()),
        });
    }

    pub fn mark_seen(&mut self, slot: usize) {
        if let Some(seen) = self.seen.get_mut(slot) {
            *seen = self.events.len();
        }
    }

    pub fn unseen(&mut self, slot: usize) -> String {
        let start = self.seen.get(slot).copied().unwrap_or(self.events.len());
        let mut updates = self.events[start..]
            .iter()
            .filter(|event| !event.text.is_empty())
            .map(|event| format!("{}:\n{}", event.speaker, event.text))
            .collect::<Vec<_>>();
        if self.truncated.get(slot).copied().unwrap_or(false) {
            updates.insert(
                0,
                "[CodeSwarm omitted older unseen updates to protect context.]".into(),
            );
            self.truncated[slot] = false;
        }
        limit(updates, 24_000)
    }

    fn prune(&mut self, active: &[bool]) {
        let consumed = active
            .iter()
            .enumerate()
            .filter_map(|(slot, enabled)| {
                enabled.then_some(self.seen.get(slot).copied().unwrap_or(0))
            })
            .min()
            .unwrap_or(0);
        if consumed > 0 {
            self.events.drain(..consumed);
            for seen in &mut self.seen {
                *seen = seen.saturating_sub(consumed);
            }
        }
        while self.events.len() >= 200
            || self
                .events
                .iter()
                .map(|event| event.text.len())
                .sum::<usize>()
                >= 48_000
        {
            self.events.remove(0);
            for (slot, seen) in self.seen.iter_mut().enumerate() {
                if *seen > 0 {
                    *seen -= 1;
                } else if active.get(slot).copied().unwrap_or(false) {
                    self.truncated[slot] = true;
                }
            }
        }
    }
}

fn compact(text: String) -> String {
    const LIMIT: usize = 12_000;
    if text.len() <= LIMIT {
        return text;
    }
    let head = LIMIT / 2;
    format!(
        "{}\n\n[CodeSwarm omitted the middle of this response to protect context.]\n\n{}",
        &text[..head],
        &text[text.len() - (LIMIT - head)..],
    )
}

fn limit(mut updates: Vec<String>, limit: usize) -> String {
    let rendered = updates.join("\n\n");
    if rendered.len() <= limit {
        return rendered;
    }
    let marker = "[CodeSwarm omitted older unseen updates to protect context.]";
    let mut selected = Vec::new();
    let mut used = 0;
    while let Some(update) = updates.pop() {
        let added = update.len() + 2;
        if used + added > limit - marker.len() - 2 {
            break;
        }
        used += added;
        selected.push(update);
    }
    selected.reverse();
    format!("{marker}\n\n{}", selected.join("\n\n"))
}

#[cfg(test)]
mod tests {
    use super::CollaborationContext;

    #[test]
    fn only_unseen_public_text_is_sent_to_each_agent() {
        let mut context = CollaborationContext::new(2);
        context.record("Human", "task", &[true, true]);
        context.mark_seen(0);
        assert_eq!(context.unseen(0), "");
        assert_eq!(context.unseen(1), "Human:\ntask");
    }

    #[test]
    fn long_history_is_bounded_without_losing_recent_updates() {
        let mut context = CollaborationContext::new(1);
        for index in 0..250 {
            context.record("Agent", format!("reply {index}"), &[true]);
        }
        let unseen = context.unseen(0);
        assert!(unseen.contains("reply 249"));
        assert!(unseen.len() <= 24_000);
    }
}
