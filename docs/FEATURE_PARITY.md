# Python-to-Rust feature parity matrix

The deleted Python client at `4e66ca9^` is the comparison baseline. This
matrix records behavior, not widget-level implementation details.

| Capability | Rust status | Evidence / next work |
| --- | --- | --- |
| Native and ACP adapters | Implemented | Shared `AgentAdapter`; native Agy and ACP adapters. |
| ACP workspace file mediation | Implemented | `fs/read_text_file` and `fs/write_text_file` requests are root-bound, symlink-safe, and capped at 4 MiB. |
| Custom adapter commands | Implemented | JSON catalog plus shell-free quoted argv parsing. |
| Legacy CLI entry points | Implemented | `run`/`acp` aliases, `-h`/`--help`, `-v`/`--version`, and optional standalone prompts are accepted. |
| Named CLI agent selection | Implemented | Repeated `-a`/`--agent` options resolve catalog identities, aliases, and short names with one-based `--first-agent`. |
| Bare launch and saved roster | Implemented | Rust catalog/store and atomic `launcher.roster` persistence. |
| Agent store selection/order | Implemented | Ratatui store; Space, Ctrl+S, Alt+Up/Down, Enter. |
| Prompt editing/history/completion | Implemented | `tui-textarea`, bounded history, slash completion. |
| Cached transcript/long output | Implemented | Logical blocks, lazy details, viewport cache, tmux benchmark. |
| Tool/terminal/thought lifecycle | Implemented | Normalized events, collapsed detail blocks, root-bound ACP filesystem mediation, and client-mediated terminal create/output/wait/kill/release. |
| Permission prompts | Implemented | Keyboard focus, readable option labels, stable ACP `optionId` routing, and cancellation. |
| Relay roster mode | Implemented | Sequential automatic ring with max-round safety. |
| Relay manual mode | Implemented | Explicitly targeted follow-ups; no implicit handoff. |
| Relay pair mode | Implemented | Owner/reviewer alternation. |
| Reviewer stop token | Implemented | Prompt guidance, filtering, and batch termination tests. |
| Pause/resume/cancel/queue | Implemented | CLI controls and relay cancellation. |
| Mode policy synchronization | Implemented | Advertised catalogs drive the config picker, Auto pilot synchronizes once per loaded slot, and semantic selections translate through adapter-native IDs. |
| First-turn roster guidance | Implemented | Each relay agent receives a one-time identity/collaborator introduction; reloads receive it again. |
| Adapter crash attribution | Implemented | Native result failures, ACP transport errors, and relay EOFs tombstone their slot and emit a reloadable failure event; Unix children run in isolated process groups for descendant cleanup. |
| Crash tombstone/reload UX | Implemented | Core tombstones failed slots and exposes `/reload` and `/drop` recovery actions. |
| Project-directory selection | Implemented | Rust supports `--project-dir PATH`, positional paths, `/cd PATH`, and `Ctrl+D` in the agent store. |
| Prompt path/resource completion | Partial | Rust has a bounded, root-safe asynchronous workspace index, `.gitignore` filtering, fuzzy `@path` popup with directory/quoted insertion, keyboard/mouse dismissal, ACP text/binary attachment expansion, and stale-generation protection; the full Python picker surface remains unported. |
| Live roster reconfiguration | Partial | Rust supports a catalog-backed `/config` editor with idle-session add/drop/promote/swap reconciliation (including starting and promoting a newly selected owner), next-launch persistence, and live `/add`, `/drop SLOT`, `/promote SLOT`, `/swap A B`; active-turn edits are deferred and full Python session metadata persistence remains. |
| Local `!command` shell execution | Implemented | Runs asynchronously in the workspace with bounded output and local transcript rendering. |
| Persistent prompt/settings preferences | Partial | Roster, custom agents, Python-compatible prompt history, follow-tail, collapsed-details, notification policy, thoughts, tool-expansion, density, scrollbar, diff view, and title-blink preference persist; theme/focus preferences do not. |
| Rich Markdown/diff views | Partial | Tool payloads are retained for lazy expansion/export, unified patches support inline or side-by-side views with line colors, and headings, fences, emphasis, lists, and quotes are styled lazily; full Markdown layout remains intentionally unported. |
| OS notifications/sounds | Partial | Rust preserves Python's `blur`/`always`/`never` policy (plus legacy boolean settings), emits completion and permission-request notifications through `notify-send`/`osascript`, supports a sanitized OSC terminal title with reference-counted blinking alerts, and persists the blink toggle; named sound assets are not yet ported. |
| Session history browser | Not applicable | The Python baseline persisted session rows for adapter resume, but exposed no session browser, picker, CLI flag, or store action: `session_get_recent` is only exercised by persistence tests. Rust preserves the exposed behavior with event replay, adapter session loading, and bounded prompt history. |

The Rust client is therefore functionally complete for the tmux-first relay
path, but not yet a claim of 100% parity until the remaining partial/missing
rows are implemented or explicitly removed from the product contract.
