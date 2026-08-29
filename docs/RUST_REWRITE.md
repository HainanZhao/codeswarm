# Rust terminal preview

The Rust client is an opt-in preview on the rewrite/rust-ratatui-architecture
branch. The Python/Textual client remains the default codeswarm executable
until real-agent dogfooding and a staged release pass.

## Run the preview

From a project checkout:

    cargo run -p codeswarm-cli -- --demo

Run a native adapter:

    cargo run -p codeswarm-cli -- --agy "describe the repository"

Run an ACP adapter:

    cargo run -p codeswarm-cli -- --acp "codex-acp" "describe the repository"

Run a mixed roster. Quote each command so its arguments remain part of the
adapter specification:

    cargo run -p codeswarm-cli -- \
      --roster "acp:gemini --experimental-acp" \
      --roster "agy:agy" \
      "review the current changes" \
      --first 0 --max-rounds 4

Use --alt-screen only when a modal/fullscreen surface is needed. Inline mode
is the tmux/SSH default.

## Roll back

The preview does not replace the Python package or its settings. Stop the Rust
process and run the existing executable:

    .venv/bin/codeswarm

Rust event logs are stored under XDG_STATE_HOME/codeswarm (or
~/.local/state/codeswarm) and are independent of Python session records.

## Verification

    cargo fmt --all -- --check
    cargo test --workspace
    cargo clippy --workspace --all-targets -- -D warnings
    bash tests/tmux/performance.sh
    make verify

The real-agent gate remains environment-dependent: an installed adapter must
have valid credentials and a supported provider tier.
