#!/usr/bin/env bash
set -euo pipefail

# Exercise the preview through a real tmux pane. The Ratatui TestBackend is
# useful for unit tests, but cannot detect terminal mode or pane interaction
# regressions.
project_root="$(git rev-parse --show-toplevel)"
socket_name="codeswarm-smoke-$$"
pane_target="codeswarm-smoke:0.0"

cleanup() {
  tmux -L "$socket_name" kill-server 2>/dev/null || true
}
trap cleanup EXIT

tmux -L "$socket_name" new-session -d -x 80 -y 24 -s codeswarm-smoke \
  "cd '$project_root' && TERM=xterm-256color cargo run -q -p codeswarm-cli -- --demo"

for _ in $(seq 1 50); do
  if tmux -L "$socket_name" capture-pane -p -t "$pane_target" | grep -q "CodeSwarm preview"; then
    break
  fi
  sleep 0.1
done

snapshot="$(tmux -L "$socket_name" capture-pane -p -t "$pane_target")"
grep -q "CodeSwarm preview" <<<"$snapshot"
tmux -L "$socket_name" send-keys -t "$pane_target" q

for _ in $(seq 1 50); do
  if ! tmux -L "$socket_name" has-session -t codeswarm-smoke 2>/dev/null; then
    exit 0
  fi
  sleep 0.1
done

echo "CodeSwarm preview did not exit after q" >&2
exit 1
