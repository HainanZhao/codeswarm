# CodeSwarm User Manual

CodeSwarm is a Rust terminal workspace for collaborating with coding agents.
The default renderer is inline and deliberately optimized for tmux, SSH, and
slow terminal links.

## Quick start

```bash
cargo build --release -p codeswarm-cli
./target/release/codeswarm
```

Select agents with the arrow keys and `Space`, then press `Enter`. The selected
roster is saved and restored by the next bare launch. Use `--demo` to exercise
the UI without an external agent.

Use `--project-dir PATH` to start the session in a different workspace.

The store reads custom agents from
`$XDG_CONFIG_HOME/codeswarm/codeswarm.json` (default:
`~/.config/codeswarm/codeswarm.json`). Add an `agents` array or object with
`identity`, `name`, `short_name`, `adapter` (`native` or `acp`), and `command`.
Entries may override a built-in identity or add a new one. In the store,
`Space` toggles membership, `Alt+Up`/`Alt+Down` changes order, `Ctrl+S` saves
without launching, and `Enter` saves and launches the selected roster (or the
highlighted agent when none is selected).

## Agent adapters

CodeSwarm supports both native adapters and ACP adapters. They share the same
normalized event model, but a custom adapter does not need to implement ACP.

```bash
codeswarm --agy "summarize the repository"
codeswarm --acp "codex-acp" "review the patch"
codeswarm --roster "agy:agy" --roster "acp:codex-acp" "review the patch"
```

Agents in a roster run sequentially. Each agent receives public human and
agent messages it has not seen since its previous turn. Tool calls, thoughts,
terminal output, and UI history stay local to the producing agent.

## Prompt and keyboard controls

- `Enter` submits a prompt; `Shift+Enter` inserts a newline.
- `Up`/`Down` scroll the transcript; `End` follows the live tail.
- `Tab` completes a slash command; `F1` or `?` toggles help.
- `Ctrl+Enter` sends to the selected roster agent.
- `Ctrl+C` cancels active work; while idle it exits.
- `Esc` closes the config panel or exits the terminal surface.

## Slash commands

- `/help` — show the complete keyboard and command guide.
- `/config` — open the lightweight inline settings panel.
- `/agents` — return to the agent store and edit the saved roster.
- `/export` — write the retained transcript to a timestamped Markdown file.
- `/mode` — focus mode settings; `/mode chat` selects chat mode.
- `/collab` — focus collaboration settings;
  `/collab roster`, `/collab manual`, and `/collab pair` select a strategy.
- `/pause` and `/resume` — pause or resume a multi-agent relay.
- `/clear` — clear the local transcript.
- `/cancel` — cancel the active turn when an adapter supports cancellation.
- `/close`, `/quit`, and `/exit` — leave the session.

Unknown slash commands are reported locally and are never sent to an agent.

## Performance model

Transcript blocks are retained as source text and rendered into a cached row
viewport. Streamed chunks extend one logical block, and tool/thought details
start collapsed. Scrolling therefore touches only the visible slice rather than
reparsing the full conversation.

Run the real-pane checks with:

```bash
bash tests/tmux/smoke.sh
bash tests/tmux/config.sh
bash tests/tmux/performance.sh
```

## Development and verification

Cargo is the canonical build and test tool:

```bash
cargo fmt --all -- --check
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
make verify
```

The tmux tests use the release binary for the config path and enforce bounded
input, capture, scroll, and resize latency.

## Privacy

CodeSwarm collects no telemetry. Prompts, responses, tool calls, and terminal
activity remain subject to the policies of the agent and provider you choose.
