# Python-to-Rust feature parity matrix

The deleted Python client at `4e66ca9^` is the comparison baseline. This
matrix records behavior, not widget-level implementation details.

| Capability | Rust status | Evidence / next work |
| --- | --- | --- |
| Native and ACP adapters | Implemented | Shared `AgentAdapter`; native Agy and ACP adapters. |
| Custom adapter commands | Implemented | JSON catalog plus shell-free quoted argv parsing. |
| Bare launch and saved roster | Implemented | Rust catalog/store and atomic `launcher.roster` persistence. |
| Agent store selection/order | Implemented | Ratatui store; Space, Ctrl+S, Alt+Up/Down, Enter. |
| Prompt editing/history/completion | Implemented | `tui-textarea`, bounded history, slash completion. |
| Cached transcript/long output | Implemented | Logical blocks, lazy details, viewport cache, tmux benchmark. |
| Tool/terminal/thought lifecycle | Implemented | Normalized events and collapsed detail blocks. |
| Permission prompts | Implemented | Keyboard focus and ACP response routing. |
| Relay roster mode | Implemented | Sequential automatic ring with max-round safety. |
| Relay manual mode | Implemented | Explicitly targeted follow-ups; no implicit handoff. |
| Relay pair mode | Implemented | Owner/reviewer alternation. |
| Reviewer stop token | Implemented | Prompt guidance, filtering, and batch termination tests. |
| Pause/resume/cancel/queue | Implemented | CLI controls and relay cancellation. |
| Mode policy synchronization | Partial | Semantic local mode and adapter forwarding exist; shared native catalog negotiation still needs completion. |
| Crash tombstone/reload UX | Partial | Core tombstones and `/reload` retries a failed slot; an explicit reload/drop prompt is still needed for unattended failures. |
| Project-directory selection | Partial | Rust supports `--project-dir PATH`; an in-store directory editor remains to be added. |
| Local `!command` shell execution | Missing | The Rust CLI reports this as unsupported; add an async workspace-local shell runner before claiming parity. |
| Persistent prompt/settings preferences | Partial | Roster, custom agents, follow-tail, and collapsed-details persist; density/theme/notification/tool/diff preferences do not. |
| Rich Markdown/diff views | Deliberately compact | Rust keeps rich details lazy; a full diff renderer is not yet ported. |
| OS notifications/sounds | Missing | No Rust notification or sound backend yet. |
| Session history browser | Missing | Event replay exists; interactive history/session picker is not ported. |

The Rust client is therefore functionally complete for the tmux-first relay
path, but not yet a claim of 100% parity until the remaining partial/missing
rows are implemented or explicitly removed from the product contract.
