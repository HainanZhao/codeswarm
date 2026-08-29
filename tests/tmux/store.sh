#!/usr/bin/env bash
set -euo pipefail

# Verify a fresh bare launch is actionable: the store must expose the catalog
# and keyboard selection instead of dead-ending at a notice.
project_root="$(git rev-parse --show-toplevel)"
socket_name="codeswarm-store-$$"
session_name="codeswarm-store"
pane_target="${session_name}:0.0"
config_dir="$(mktemp -d "${TMPDIR:-/tmp}/codeswarm-store.XXXXXX")"
binary="$project_root/target/release/codeswarm"

mkdir -p "$config_dir/codeswarm"
printf '%s\n' '{"agents":[{"identity":"custom.example","name":"Custom Agent","short_name":"custom","adapter":"acp","command":"codeswarm-test-agent --acp"}]}' > "$config_dir/codeswarm/codeswarm.json"

cleanup() {
  tmux -L "$socket_name" kill-server 2>/dev/null || true
  rmdir "$config_dir" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [[ "${CODESWARM_TMUX_SKIP_BUILD:-0}" != "1" ]]; then
  cargo build -q --release -p codeswarm-cli
fi
if [[ ! -x "$binary" ]]; then
  echo "missing release binary: $binary" >&2
  exit 1
fi

tmux -L "$socket_name" new-session -d -x 96 -y 24 -s "$session_name" \
  "cd '$project_root' && XDG_CONFIG_HOME='$config_dir' TERM=xterm-256color '$binary'"

capture() {
  tmux -L "$socket_name" capture-pane -p -J -t "$pane_target"
}

wait_for() {
  local expected="$1"
  for _ in $(seq 1 100); do
    if capture | grep -Fq "$expected"; then return 0; fi
    sleep 0.05
  done
  echo "timed out waiting for: $expected" >&2
  capture >&2 || true
  exit 1
}

wait_for "Choose your agents"
wait_for "Custom Agent"
wait_for "Space select"
tmux -L "$socket_name" send-keys -t "$pane_target" -N 6 Down
wait_for "▶ ☐ Custom Agent"
tmux -L "$socket_name" send-keys -t "$pane_target" Space
wait_for "▶ ☑ Custom Agent"
tmux -L "$socket_name" send-keys -t "$pane_target" C-s
for _ in $(seq 1 100); do
  if grep -Fq '"roster": "custom.example"' "$config_dir/codeswarm/codeswarm.json"; then
    break
  fi
  sleep 0.05
done
if ! grep -Fq '"roster": "custom.example"' "$config_dir/codeswarm/codeswarm.json"; then
  echo "Ctrl+S did not persist the selected roster" >&2
  capture >&2 || true
  exit 1
fi
tmux -L "$socket_name" send-keys -t "$pane_target" Enter
tmux -L "$socket_name" send-keys -t "$pane_target" Escape
for _ in $(seq 1 50); do
  if ! tmux -L "$socket_name" has-session -t "$session_name" 2>/dev/null; then
    echo "store command harness passed"
    exit 0
  fi
  sleep 0.05
done
echo "Rust terminal did not exit after q" >&2
exit 1
