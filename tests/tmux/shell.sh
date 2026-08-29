#!/usr/bin/env bash
set -euo pipefail

project_root="$(git rev-parse --show-toplevel)"
socket_name="codeswarm-shell-$$"
session_name="codeswarm-shell"
pane_target="${session_name}:0.0"
binary="$project_root/target/release/codeswarm"

cleanup() { tmux -L "$socket_name" kill-server 2>/dev/null || true; }
trap cleanup EXIT INT TERM

if [[ "${CODESWARM_TMUX_SKIP_BUILD:-0}" != "1" ]]; then
  cargo build -q --release -p codeswarm-cli
fi
if [[ ! -x "$binary" ]]; then
  echo "missing release binary: $binary" >&2
  exit 1
fi

tmux -L "$socket_name" new-session -d -x 96 -y 24 -s "$session_name" \
  "cd '$project_root' && TERM=xterm-256color '$binary' --demo"

capture() { tmux -L "$socket_name" capture-pane -p -J -t "$pane_target"; }
for _ in $(seq 1 100); do
  if capture | grep -Fq "CodeSwarm preview"; then break; fi
  sleep 0.05
done
tmux -L "$socket_name" send-keys -t "$pane_target" -l '!echo shell-ok'
for _ in $(seq 1 200); do
  if capture | grep -Fq '!echo shell-ok'; then break; fi
  sleep 0.05
done
tmux -L "$socket_name" send-keys -t "$pane_target" Enter
for _ in $(seq 1 100); do
  if capture | grep -Fq "shell-ok"; then
    echo "local shell command harness passed"
    exit 0
  fi
  sleep 0.05
done
echo "local shell output did not reach the transcript" >&2
capture >&2 || true
exit 1
