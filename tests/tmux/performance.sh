#!/usr/bin/env bash
set -euo pipefail

# Exercise the long-message path through a real tmux pane. The deterministic
# transcript benchmark supplies the 5k-word scroll budget; this black-box
# half verifies that the inline terminal remains interactive while scrolling,
# that pane captures stay bounded, and that a resize storm settles on the last
# geometry. Keep this separate from smoke.sh so a slow or missing tmux server
# cannot hide a deterministic model regression.

project_root="$(git rev-parse --show-toplevel)"
socket_name="codeswarm-performance-$$"
session_name="codeswarm-performance"
pane_target="${session_name}:0.0"
frame_budget_ms="${CODESWARM_TMUX_FRAME_BUDGET_MS:-100}"
resize_budget_ms="${CODESWARM_TMUX_RESIZE_BUDGET_MS:-500}"
scroll_key_count=20

cleanup() {
  tmux -L "$socket_name" kill-server 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is required for the terminal performance harness" >&2
  exit 1
fi

if [[ "${CODESWARM_TMUX_SKIP_BUILD:-0}" == "1" ]]; then
  echo "using existing Rust terminal client build"
else
  echo "building Rust terminal client"
  cargo build -q -p codeswarm-cli
fi

benchmark_output="$(cargo run -q -p codeswarm-transcript --bin codeswarm-transcript-bench)"
scroll_ms="$(sed -n 's/.*scenario=single_5k.*scroll_ms=\([0-9][0-9]*\).*/\1/p' <<<"$benchmark_output")"
scroll_rows="$(sed -n 's/.*scenario=single_5k.*rows=\([0-9][0-9]*\).*/\1/p' <<<"$benchmark_output")"
if [[ -z "$scroll_ms" || -z "$scroll_rows" ]]; then
  echo "benchmark did not report the single_5k scenario" >&2
  echo "$benchmark_output" >&2
  exit 1
fi
if (( scroll_ms >= 100 )); then
  echo "5k-word cached scroll exceeded 100ms: ${scroll_ms}ms (${scroll_rows} rows)" >&2
  exit 1
fi
echo "5k-word cached scroll: ${scroll_ms}ms (${scroll_rows} rows; budget <100ms)"

binary="$project_root/target/debug/codeswarm"
command="cd $(printf '%q' "$project_root") && TERM=xterm-256color $(printf '%q' "$binary") --demo"
tmux -L "$socket_name" new-session -d -x 80 -y 24 -s "$session_name" "$command"

capture() {
  tmux -L "$socket_name" capture-pane -p -J -t "$pane_target"
}

wait_for_text() {
  local expected="$1"
  local attempts="${2:-100}"
  local snapshot
  for _ in $(seq 1 "$attempts"); do
    if snapshot="$(capture 2>/dev/null)" && grep -Fq "$expected" <<<"$snapshot"; then
      return 0
    fi
    sleep 0.05
  done
  echo "timed out waiting for pane text: $expected" >&2
  capture >&2 || true
  return 1
}

wait_for_text "CodeSwarm preview"
wait_for_text "word4900"

# Exercise a burst of scrolling against the long cached transcript, then
# return to the tail before measuring prompt input. Keeping the burst and the
# latency probe separate avoids measuring tmux's key-repeat queue instead of
# the renderer's response to a fresh input event.
tmux -L "$socket_name" send-keys -t "$pane_target" -N "$scroll_key_count" Up
wait_for_text "scrolling" 40
tmux -L "$socket_name" send-keys -t "$pane_target" End
wait_for_text "following" 80
frame_start="$(date +%s%N)"
tmux -L "$socket_name" send-keys -t "$pane_target" x
wait_for_text "> x" 40
frame_end="$(date +%s%N)"
input_latency_ms=$(( (frame_end - frame_start) / 1000000 ))
if (( input_latency_ms > frame_budget_ms )); then
  echo "scroll/input frame exceeded ${frame_budget_ms}ms: ${input_latency_ms}ms" >&2
  exit 1
fi
echo "scroll/input frame: ${input_latency_ms}ms (budget <=${frame_budget_ms}ms)"

# Capture a warm pane repeatedly. This is intentionally a pane-level proxy,
# not a claim that tmux capture time equals renderer time; it catches accidental
# history-wide output and gives a stable bounded-output signal in CI.
capture_times=()
for _ in $(seq 1 20); do
  capture_start="$(date +%s%N)"
  snapshot="$(capture)"
  capture_end="$(date +%s%N)"
  capture_times+=( $(( (capture_end - capture_start) / 1000000 )) )
  grep -Fq "CodeSwarm preview" <<<"$snapshot"
done
capture_p99="$(printf '%s\n' "${capture_times[@]}" | sort -n | awk '{ values[NR] = $1 } END { p99 = int(NR * 0.99); if (p99 < 1) p99 = 1; print values[p99] }')"
if (( capture_p99 > frame_budget_ms )); then
  echo "warm pane capture p99 exceeded ${frame_budget_ms}ms: ${capture_p99}ms" >&2
  exit 1
fi
echo "warm pane capture p99: ${capture_p99}ms (budget <=${frame_budget_ms}ms)"

# Resize repeatedly without allowing intermediate snapshots to become the
# final state. The last geometry is the only one accepted by this harness.
resize_start="$(date +%s%N)"
for geometry in "100 30" "120 40" "90 26" "112 32" "96 28"; do
  read -r width height <<<"$geometry"
  tmux -L "$socket_name" resize-window -t "$session_name" -x "$width" -y "$height"
done
final_geometry=""
for _ in $(seq 1 40); do
  final_geometry="$(tmux -L "$socket_name" display-message -p -t "$pane_target" '#{pane_width}x#{pane_height}')"
  if [[ "$final_geometry" == "96x28" ]]; then
    break
  fi
  sleep 0.025
done
resize_end="$(date +%s%N)"
resize_elapsed_ms=$(( (resize_end - resize_start) / 1000000 ))
if [[ "$final_geometry" != "96x28" ]]; then
  echo "resize storm settled on ${final_geometry}, expected 96x28" >&2
  exit 1
fi
if (( resize_elapsed_ms > resize_budget_ms )); then
  echo "resize storm exceeded ${resize_budget_ms}ms: ${resize_elapsed_ms}ms" >&2
  exit 1
fi
sleep 0.15
snapshot="$(capture)"
grep -Fq "CodeSwarm preview" <<<"$snapshot"
echo "resize storm: ${resize_elapsed_ms}ms (budget <=${resize_budget_ms}ms; final=${final_geometry})"

tmux -L "$socket_name" send-keys -t "$pane_target" q
for _ in $(seq 1 50); do
  if ! tmux -L "$socket_name" has-session -t "$session_name" 2>/dev/null; then
    echo "tmux performance harness passed"
    exit 0
  fi
  sleep 0.05
done
echo "Rust terminal did not exit after q" >&2
exit 1
