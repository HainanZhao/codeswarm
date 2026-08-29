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
```

Launch a mixed roster with repeated `--roster` arguments:

```bash
codeswarm --roster "acp:codex-acp" --roster "agy:agy" "review the patch"
```

Adapters are intentionally not forced through ACP. Native adapters and custom
ACP commands can coexist in one roster.

## Commands

Inside the conversation prompt:

- `/help` shows keyboard and command help.
- `/config` opens the lightweight inline settings panel.
- `/export` writes the retained conversation to Markdown.
- `/mode` and `/mode chat` select or show the current mode state.
- `/collab roster|manual|pair` selects collaboration routing.
- `/pause` and `/resume` control a relay.
- `/clear` clears the local transcript; `/close` exits the session.

The interface keeps streamed output coalesced and transcript rows cached, so a
5,000-word response remains interactive in tmux. Run the performance harness
with `bash tests/tmux/performance.sh`.

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
