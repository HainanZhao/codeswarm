---
title: "CodeSwarm: A Terminal Relay for Collaborative Coding Agents"
published: true
description: "CodeSwarm is a terminal workspace that lets ACP and native coding agents (Claude Code, Codex, Gemini CLI, Antigravity CLI) collaborate through sequential shared conversations."
tags: ["ai", "python", "opensource", "productivity"]
canonical_url: "https://dev.to/hainanzhao/codeswarm-a-terminal-relay-for-collaborative-coding-agents-4o1n"
---

If you use coding agents, you’ve probably felt the handoff problem: one agent has useful context, another has a better critique or specialization, and the human ends up copying conclusions back and forth between terminal windows.

CodeSwarm is a terminal workspace built for collaborative agentic engineering. It connects directly to coding agents via the [Agent Client Protocol (ACP)](https://agentclientprotocol.com/) and native CLI stream interfaces, letting you assemble a roster of agents that work through tasks sequentially in one shared conversation.

The interesting part is not "more chat windows." It’s the structured relay.

---

## A roster, not a pile of tabs

CodeSwarm connects to major terminal coding agents out of the box:
- **Anthropic Claude Code** (via ACP)
- **OpenAI Codex CLI** (via ACP)
- **Google Gemini CLI** (via ACP)
- **Google Antigravity CLI (`agy`)** (via native stream-JSON)
- **Any custom ACP agent** via `codeswarm acp`

You choose a roster, send a prompt once, and the agents take turns sequentially.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Human: "Review the authentication flow and propose a test suite"     │
├──────────────────────────────────────────────────────────────────────┤
│ Claude Code (Speaker 1) — maps architecture, flags boundary risks    │
│   └── Tool activity collapsed: [read_file, grep_search]              │
├──────────────────────────────────────────────────────────────────────┤
│ Antigravity CLI (Speaker 2) — critiques assumptions, drafts tests    │
│   └── Tool activity collapsed: [view_file, run_command]              │
├──────────────────────────────────────────────────────────────────────┤
│ Codex CLI (Speaker 3) — implements fixtures, verifies edge cases     │
└──────────────────────────────────────────────────────────────────────┘
```

That sequential ordering matters. A later agent receives the public human prompts and agent responses produced since its previous turn. Tool calls, internal thoughts, terminal output, and private UI history stay local to the agent that produced them.

The result is a compact, high-signal shared journal instead of an increasingly noisy transcript of every raw tool execution.

The first agent in the roster is the session owner: it owns the session record and title. The others are collaborators in the relay. That gives the conversation a stable home while still letting the roster grow.

Relays are deliberately sequential. One agent's response is the next agent's input, so running the round concurrently would throw away the causal dependency that makes review worth anything.

Two rules keep the review honest rather than decorative. The first agent to answer a human message cannot end the relay — a second agent always gets a turn on it. And the shared journal is explicitly bounded: each relayed response is capped, and the journal retains a rolling window of recent public events, so a long session degrades by dropping the oldest context rather than by silently overflowing.

You can also aim a message. Click an agent beside the prompt and the `→` marker moves to it, so your next message starts the relay there instead of at the top of the roster.

---

## Collaboration Patterns: Roster, Pair, and Manual

Multi-agent collaboration is not one-size-fits-all. CodeSwarm provides three distinct routing patterns that you can toggle from the prompt bar or via slash commands:

1. **Roster Mode (`/collab roster`)** — The default round-robin review relay. Agents take turns in sequence (`A → B → C → A...`). If a reviewing agent agrees completely and has nothing to add, it can acknowledge with a thumbs-up (`👍`) or an internal stop signal, which CodeSwarm cleanly handles without polluting the context.
2. **Pair Mode (`/collab pair`)** — Built for the classic **Doer → Verifier** workflow. Every new user prompt starts with the first roster agent (the doer), runs through the relay to the reviewer/verifier, and then resets back to the first agent for the next user batch.
3. **Manual Mode (`/collab manual`)** — Pins a specific specialist agent. Prompts are routed exclusively to that pinned agent until you click a different collaborator in the roster bar.

You can switch modes instantly by clicking the collaboration pill (`Roster`, `Pair`, or `Manual`) beside the prompt footer.

---

## Install and launch

CodeSwarm requires Python 3.14 or later on macOS or Linux:

```bash
uv tool install codeswarm
# or: pip install codeswarm
```

Start it in a project directory:

```bash
codeswarm ~/projects/example
```

On first launch, an interactive catalog lets you select and order your active agents. Later launches automatically restore your last-used roster.

You can also launch a specific roster directly from your shell:

```bash
# Launch with Claude, Antigravity, and Codex
codeswarm run -a claude -a agy -a codex ~/projects/example

# Start the relay at the second agent instead of the first
codeswarm run -a claude -a agy --first-agent 2 ~/projects/example
```

Or connect any external ACP-compatible server directly:

```bash
codeswarm acp "node /path/to/custom-acp-agent.js" ~/projects/example
```

Provider CLIs and authentication remain local to your machine. CodeSwarm orchestrates the interaction without bundling proprietary accounts or collecting telemetry.

---

## Steering work without losing the turn

In typical terminal chats, typing while an agent is thinking either gets dropped or disrupts execution. CodeSwarm introduces deterministic turn management:

- **FIFO Follow-up Queue**: A follow-up typed while an agent is working joins a bounded holding queue instead of interrupting the request in flight. It goes back to the working agent by default, or to a different agent if you have selected one beside the prompt — the footer names the recipient, and CodeSwarm warns when the queue is full. Queued messages are delivered in order before the relay advances, and you can cancel one while it waits.
- **Safe Interruption**: `Ctrl+C` once cancels the executing agent turn and immediately dispatches any queued follow-ups so they are never stranded. Press it again within three seconds to quit.
- **Pause & Resume**: `Ctrl+Shift+P` (or `/pause`, available once at least two agents are active) cancels current agent work but preserves the queue. Resuming asks the agents to continue from the workspace as it now stands.
- **Turn Limits**: Automated relays default to a 100-turn safety ceiling, customizable at launch (`--max-rounds N`). The limit applies to the relay as a whole, not separately to each agent.

---

## Unified permissions across heterogeneous agents

Different agents have different permission models. CodeSwarm normalizes them into one shared vocabulary, ordered from least to most automation:

- **Chat** — instructs every agent to discuss instead of inspecting or changing the workspace, and CodeSwarm blocks ACP terminal creation. Treat it as a convention for architecture and brainstorming, *not* a security boundary: the connected CLI still owns its own native tools.
- **Plan** — read-only planning with no tool execution.
- **Manual** — asks before any operation that requires permission.
- **Accept Edits** — automatically approves file edits while keeping other safeguards.
- **Auto pilot** *(the default for new sessions)* — approves all tools and bypasses permission prompts.

Press `Ctrl+O` or click the mode name beside the prompt to switch. One selection applies to the entire roster, and CodeSwarm translates it into each adapter's native mode: *Accept Edits* becomes Claude's `acceptEdits` and Gemini's `autoEdit`, while Antigravity — which exposes no in-session full-access mode — gets *Auto pilot* by being relaunched with its skip-permissions flag.

The picker only offers modes that **every** active agent can honor. An agent-specific mode with no honest equivalent across the roster is omitted rather than approximated, so the label above the prompt means the same thing for every agent in the relay.

One boundary is worth stating plainly: CodeSwarm orchestrates the roster, but each underlying agent still operates under its provider's capabilities and policies. Check the mode before pointing a roster at a project with sensitive files or external side effects.

---

## Developer ergonomics built for the terminal

CodeSwarm is built with Textual and designed to feel like a high-performance terminal workspace:

- **Inline Shell Execution (`!command`)**: Run local commands directly in your workspace by prefixing with `!`. For example, `!git diff` or `!pytest` executes in your local shell and displays output in the conversation stream without forwarding tokens to the agents.
- **File Autocompletion (`@path`)**: Type `@` followed by a file or directory name to fuzzy-search workspace paths and insert a reference, with spaces quoted for you. It inserts a path reference only — it does not open the file or tag an agent.
- **Transcript Navigation & History**: Each turn ends with a one-line, collapsed tool-activity preview so long tool output never crowds out the answer. `Alt+Up` / `Alt+Down` moves between transcript blocks and `Space` expands the selected one; click a tool line to focus that turn's history, then `Up` / `Down` to browse it and `Enter` to open an individual call. `c` copies the selected block, `p` copies it into the prompt.
- **Markdown Export (`/export`)**: Writes the retained conversation to `codeswarm-conversation-<timestamp>.md` in the workspace directory.
- **Left-Rail Visual Hierarchy**: Each agent owns a hue on its message left rail, matched to its name in the roster beside the prompt, so a reply can be traced to a speaker at a glance. The rails stay distinguishable in greyscale, and identity is carried by the name and rail rather than by colour alone.

---

## Why build this as a terminal workspace?

A terminal is where coding agents already do their work: they inspect files, run tests, edit code, and report results. Keeping the collaboration layer there makes the control loop visible and local.

CodeSwarm also intentionally keeps its data model small:

- the **roster** defines who participates;
- the **journal** defines what collaborators have publicly said;
- the **relay** — roster, pair, or manual — defines whose turn is next;
- the **human** remains able to select, steer, pause, or cancel.

That separation makes multi-agent collaboration feel more like a review session than a swarm of autonomous processes.

---

## Try it on a real task

A great first experiment is a multi-agent architectural review or test-driven implementation:

```text
Review this repository's authentication flow. Start by identifying the trust boundaries and missing edge-case tests. Let each collaborator critique the previous response. Do not modify files until we agree on a plan.
```

Set the mode to **Plan** first, so "do not modify files" is enforced by the adapters rather than merely requested in prose.

Once the relay produces an aligned plan, switch to **Pair Mode** (`/collab pair`) to let your primary agent implement the code while the secondary agent verifies test coverage and edge cases on each turn.

CodeSwarm is open source under **AGPL-3.0**, with a separate commercial license available, and is on GitHub:

👉 [github.com/HainanZhao/codeswarm](https://github.com/HainanZhao/codeswarm)

If your current workflow is juggling multiple agent tabs and copying snippets across terminals, CodeSwarm turns that friction into an orderly, automated relay.
