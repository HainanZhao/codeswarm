# ✈ CodeSwarm

CodeSwarm is a fast terminal workspace for one or more coding agents. It is a
Rust application built around Ratatui, with ACP and native adapter support,
sequential relay turns, lazy transcript details, and a tmux/SSH-first inline
interface. It collects no telemetry.

## Install

Build the release binary with Cargo:

```bash
cargo build --release -p codeswarm-cli
install -Dm755 target/release/codeswarm ~/.local/bin/codeswarm
```

CodeSwarm supports macOS and Linux with a recent stable Rust toolchain.

## Run

```bash
codeswarm
```

The first launch opens agent selection. A saved roster is restored on later
launches. For a deterministic preview or smoke test:

```bash
codeswarm --demo
codeswarm --agy "describe the repository"
codeswarm --acp "codex-acp" "review the current changes"
codeswarm --project-dir ~/projects/example
# A directory may also be supplied positionally.
codeswarm ~/projects/example
```

Launch a mixed roster with repeated `--roster` arguments:

```bash
codeswarm --roster "acp:codex-acp" --roster "agy:agy" "review the patch"
```

Adapters are intentionally not forced through ACP. Native adapters and custom
ACP commands can coexist in one roster.

### Configure custom agents

The agent store reads `~/.config/codeswarm/codeswarm.json` (or
`$XDG_CONFIG_HOME/codeswarm/codeswarm.json`). Add an `agents` array or object;
entries replace built-ins with the same identity or add a new agent:

```json
{
  "agents": {
    "reviewer.local": {
      "name": "Local Reviewer",
      "short_name": "reviewer",
      "adapter": "acp",
      "command": "my-reviewer --acp",
      "active": true
    }
  }
}
```

Use `adapter: "native"` for a native command. Bare `codeswarm` displays these
entries in the store; `Space` selects them, `Alt+↑/↓` changes roster order,
`Ctrl+S` saves without launching, and `Enter` saves and launches the selection.
The store writes the selected identities back
to `launcher.roster` without overwriting other settings.

## Commands

Inside the conversation prompt:

- `/help` shows keyboard and command help.
- `/config` opens the lightweight inline settings panel.
- `/agents` returns to the agent store to edit the saved roster.
- `/export` writes the retained conversation to Markdown.
- `/diff split|unified` switches the lazy diff view.
- `/mode` and `/mode chat` select or show the current mode state.
- `/collab roster|manual|pair` selects collaboration routing.
- `/pause` and `/resume` control a relay.
- `/reload` retries the most recently crashed agent in its roster slot.
- `/drop` removes a crashed peer from the active relay.
- `/cd PATH` changes the workspace for subsequent local commands and launches.
- `/clear` clears the local transcript; `/close` exits the session.
- `!command` runs a bounded local shell command in the selected workspace.

The interface keeps streamed output coalesced and transcript rows cached, so a
5,000-word response remains interactive in tmux. Run the performance harness
with `bash tests/tmux/performance.sh`.

Prompt history is persisted locally and capped at the last 50 entries.

## Development

Cargo is the canonical build and test tool:

```bash
make verify
```

This runs formatting, workspace tests, Clippy, and the tmux smoke/config/
performance harnesses.

## License

CodeSwarm is licensed under
[AGPL-3.0](https://github.com/HainanZhao/codeswarm/blob/main/LICENSE). See the
[commercial license notice](https://github.com/HainanZhao/codeswarm/blob/main/COMMERCIAL_LICENSE.md)
for commercial licensing.
