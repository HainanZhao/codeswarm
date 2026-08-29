#!/usr/bin/env bash
set -euo pipefail

# Black-box coverage for the command/config boundary. This deliberately runs
# the release binary that users reach through the codeswarm symlink.
project_root="$(git rev-parse --show-toplevel)"
socket_name="codeswarm-config-$$"
session_name="codeswarm-config"
pane_target="${session_name}:0.0"
binary="$project_root/target/release/codeswarm"

cleanup() {
  tmux -L "$socket_name" kill-server 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [[ ! -x "$binary" ]]; then
  echo "missing release binary: $binary (run cargo build --release -p codeswarm-cli)" >&2
  exit 1
fi

tmux -L "$socket_name" new-session -d -x 96 -y 24 -s "$session_name" \
  "cd '$project_root' && TERM=xterm-256color '$binary' --demo"

capture() {
  tmux -L "$socket_name" capture-pane -p -J -t "$pane_target"
}

wait_for() {
  local expected="$1"
  for _ in $(seq 1 100); do
    if capture | grep -Fq "$expected"; then
      return 0
    fi
    sleep 0.05
  done
  echo "timed out waiting for: $expected" >&2
  capture >&2 || true
  exit 1
}

wait_for "CodeSwarm preview"
tmux -L "$socket_name" send-keys -t "$pane_target" -l "/config"
tmux -L "$socket_name" send-keys -t "$pane_target" Enter
wait_for "Configuration"
wait_for "Follow output"
wait_for "Collapse details"
wait_for "Renderer"

# Navigate and toggle a setting without leaving the modal.
tmux -L "$socket_name" send-keys -t "$pane_target" Down Enter
wait_for "Collapse details"

# Esc closes the modal and returns to the normal conversation surface.
tmux -L "$socket_name" send-keys -t "$pane_target" Escape
wait_for "Conversation"
wait_for "Prompt"

# Verify export is a local command and creates Markdown rather than reaching
# an adapter. Remove only this uniquely named test artifact on exit.
tmux -L "$socket_name" send-keys -t "$pane_target" -l "/export"
tmux -L "$socket_name" send-keys -t "$pane_target" Enter
wait_for "conversation exported to"
export_path="$(capture | sed -n 's/.*conversation exported to \([^ ]*\.md\).*/\1/p' | tail -n 1)"
if [[ -z "$export_path" || ! -f "$project_root/$export_path" ]]; then
  echo "export file was not created: ${export_path:-<missing>}" >&2
  capture >&2 || true
  exit 1
fi
rm -f -- "$project_root/$export_path"

tmux -L "$socket_name" send-keys -t "$pane_target" Escape
for _ in $(seq 1 50); do
  if ! tmux -L "$socket_name" has-session -t "$session_name" 2>/dev/null; then
    echo "config command harness passed"
    exit 0
  fi
  sleep 0.05
done
echo "Rust terminal did not exit after Escape" >&2
exit 1
