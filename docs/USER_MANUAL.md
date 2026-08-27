# CodeSwarm User Manual

CodeSwarm is a terminal workspace for one or more coding agents. It keeps their
conversation, tool activity, approvals, and sequential hand-offs in one place.

## Contents

- [Quick Start](#quick-start)
- [How the Relay Works](#how-a-multi-agent-conversation-works)
- [Start and Choose Agents](#starting-codeswarm)
- [Send Messages](#sending-messages)
- [Control the Relay](#control-the-relay)
- [Modes and Permissions](#modes)
- [Commands](#codeswarm-slash-commands)
- [Navigate the Transcript](#navigate-the-transcript)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)

## Quick Start

Install CodeSwarm and open the current directory:

```bash
uv tool install codeswarm
codeswarm
```

On the agent selection screen, use the arrow keys to highlight an agent,
`Space` to add or remove it from the roster, and `Enter` to start. Selected
agents run in the order shown in the roster strip.

Type a request and press `Enter`. With multiple agents, CodeSwarm sends each
response to the next agent in sequence. The roster beside the prompt shows
connection and speaker state:

- `→` — receives the next message
- `⌖` — pinned target in Manual mode
- `●` — currently working
- `○` — connected and waiting
- `…` — connecting

## How a Multi-Agent Conversation Works

- Agents work sequentially, never concurrently. Before each turn, an agent
  receives every public human question and agent response it has not seen since
  its previous turn, in order.
- Only human and agent message text enters this shared history. Tool calls,
  reasoning, terminal output, and interface history stay with the agent that
  produced them.
- A normal follow-up entered while an agent is working is queued back to that
  agent. After it handles all queued follow-ups, the relay advances and gives
  the next agent the latest response as context.
- Click an agent beside the prompt to choose who receives the next normal
  message. Duplicate names include their roster number, such as `Claude (1)`
  and `Claude (2)`.
- The first agent answering a human message cannot stop the relay; another agent
  always gets the chance to review it. If that reviewer has nothing meaningful
  to correct or add, it may acknowledge with an emoji and CodeSwarm's internal
  stop signal. CodeSwarm hides the signal and displays `👍` when no emoji was
  supplied.
- Agents leave the relay running when meaningful uncertainty, unfinished work,
  or useful independent review remains.
- The automated-turn safety limit defaults to 100. Change it at launch with
  `--max-rounds`.

## Starting CodeSwarm

### Standard Launch

```bash
codeswarm [PATH]
```

`PATH` is the project directory and defaults to the current directory. A
standard launch restores the last usable roster. If no roster is saved,
CodeSwarm opens agent selection.

### Choose Agents from the Command Line

Repeat `--agent` (`-a`) in relay order:

```bash
codeswarm run -a claude -a codex -a gemini ~/projects/example
```

Launch options:

| Option | Action |
| --- | --- |
| `-a`, `--agent AGENT` | Add an agent by short name or identity; repeat in relay order. |
| `--first-agent N` | Start with roster member `N` (1-based). |
| `--max-rounds N` | Stop after `N` automated relay turns. |
| `-v`, `--version` | Print the CodeSwarm version. |
| `-h`, `--help` | Show command-line help. |

### Launch a Custom ACP Agent

```bash
codeswarm acp "COMMAND" [PATH]
```

| Option | Action |
| --- | --- |
| `-t`, `--title TITLE` | Set the agent name shown in CodeSwarm. |
| `-d`, `--project-dir PATH` | Set the workspace directory. This overrides the positional path. |

The command must start an Agent Client Protocol server over standard input and
output.

## Agent Selection

| Action | Keyboard | Mouse |
| --- | --- | --- |
| Move between agents | Arrow keys | Hover or click an agent |
| Add or remove an agent | `Space` | Click the agent card |
| Start the selected roster | `Enter` | — |
| Start the highlighted agent alone when none are selected | `Enter` | — |
| Move between interface sections | `Tab` / `Shift+Tab` | Click a control |
| Change the project directory | `Ctrl+D` | Click the displayed directory |
| Filter the catalog | Focus the filter and type | Click the filter and type |
| Quit | `Ctrl+C` | — |

Detected agents appear first and are initially selected. Press `Space` to
remove any you do not want before starting. CodeSwarm will not launch an
unavailable agent; install its CLI and reopen CodeSwarm so it can be detected
again. Starting another workspace stops and replaces the current workspace.

## Sending Messages

| Action | Control |
| --- | --- |
| Send the prompt | `Enter` |
| Insert a newline | `Shift+Enter` or `Ctrl+J` |
| Recall older or newer prompts | `Up` / `Down` at the first or last prompt line |
| Complete a suggested path | `Tab` |
| Open slash-command search | Type `/` at the start of the prompt |
| Select a command from search | `Up` / `Down`, then `Enter` |
| Close command or path search | `Esc` |
| Search workspace paths | Type `@` after some prompt text or a space |
| Insert a selected path | `Up` / `Down`, then `Enter` |

Selecting a path inserts a reference into the prompt; it does not open or show
the file. Paths containing spaces are quoted automatically. Slash commands
owned by CodeSwarm run locally. Commands advertised by an agent are sent to that
agent; an unknown slash command shows an error instead of becoming a prompt.

### Select the Next Agent

Click an agent beside the prompt. The `→` marker moves to the selected agent;
your next normal message starts the relay with that agent. While an agent is
working, ordinary follow-ups still queue for that active agent.

### Choose Collaboration Routing

Use `/collab roster` for the default sequential review relay. Use
`/collab manual` for manual routing: the selected agent stays pinned, and a
message is sent to a different agent only after you click that agent beside
the prompt. Prompts already queued stay with the agent selected when they were
submitted.

Use `/collab pair` for the doer→verifier pattern. Each new user batch starts
with the first roster agent as the doer, then follows the normal relay to the
verifier; the next batch starts over with that first agent.

## Control the Relay

| Action | Control |
| --- | --- |
| Cancel active work | Press `Ctrl+C` once |
| Quit while cancellation is pending | Press `Ctrl+C` again within 3 seconds |
| Quit while idle | `Ctrl+C` |
| Pause or resume a multi-agent relay | `Ctrl+Shift+P` or `/pause` |
| Open the mode picker | `Ctrl+O` or click the mode name |
| Close the workspace and return to agent selection | `F4` or `/close` |

Pausing cancels current agent work but preserves queued prompts. Resuming asks
the agents to continue from the shared workspace state. Pause is available
only when at least 2 agents are active.

Follow-ups entered during active work are queued for that same agent instead
of overlapping its current request. CodeSwarm names the target agent when a
prompt is queued and warns if the bounded queue is full. Multiple follow-ups
are handled in the order entered before the relay advances.

## Modes

Open the mode picker with `Ctrl+O` or click the mode at the lower right. The
options are ordered from least to most automation:

- **Chat** instructs every agent not to inspect or change the workspace.
  CodeSwarm blocks ACP terminal creation, but the connected CLI ultimately
  controls its own native tools. Use it for architecture, brainstorming, or
  general questions—not as a security boundary.
- **Plan** is read-only planning with no tool execution.
- **Manual** asks before operations that require permission.
- **Accept Edits** automatically approves file edits while retaining other
  safeguards.
- **Auto pilot** automatically approves all tools and bypasses permission
  prompts.
- New sessions default to Auto pilot. CodeSwarm translates and applies that
  policy to every agent as soon as the roster connects.
- One selection applies to every active agent. CodeSwarm translates these shared
  names to each adapter's native mode—for example, Accept Edits maps to
  Claude's `acceptEdits` and Gemini's `autoEdit`.
- Only modes supported by every active agent are shown. Agent-specific modes
  without an honest equivalent are omitted.
- CodeSwarm keeps every agent synchronized to the selected shared mode; adapter
  defaults are never exposed as a mixed roster mode.
- Choosing any permission mode exits Chat.
- The check mark identifies the active mode.
- `Esc` closes the mode picker without changing anything.

`/mode` opens the same picker. `/mode chat` is a shortcut for Chat;
choose any shared permission mode to leave it.

## CodeSwarm Slash Commands

Prefix a line with `!` to run it directly in the current workspace shell. For
example, `!git status` runs locally and displays its output in the conversation;
it is never sent to an agent. Press `Ctrl+C` to stop a running command.

| Command | Action |
| --- | --- |
| `/help` | Show the concise command and control reference in the conversation. |
| `/config` | Open CodeSwarm settings and the roster for the next workspace. |
| `/export` | Export the conversation as a Markdown file in the workspace. |
| `/pause` | Pause or resume the multi-agent relay. |
| `/mode` | Open the mode picker. |
| `/mode chat` | Enter Chat mode directly. |
| `/collab roster` | Use the sequential review relay. |
| `/collab manual` | Manually route each turn to the selected pinned agent. |
| `/collab pair` | Start each doer→verifier batch with the first roster agent. |
| `/close` | Close this workspace and return to agent selection. |

Agents may advertise additional slash commands. They appear in command search
and are forwarded to the active agent. A CodeSwarm command with the same name
always runs locally.

## Navigate the Transcript

Each agent turn ends with a one-line tool-history preview after the reply. It
stays collapsed by default so long tool output does not crowd out the answer;
focus it to browse calls and expand only the details you need.

| Action | Control |
| --- | --- |
| Return focus to the prompt | `End` or start typing |
| Select previous or next transcript block | `Alt+Up` / `Alt+Down` |
| Expand or collapse the selected block | `Space` |
| Focus an agent's compact tool activity | Click the tool line |
| Browse that turn's tool history | `Up` / `Down` while the tool line is focused |
| Expand or collapse the selected tool | `Enter` or click its header |
| Copy the selected block | `c` |
| Copy the selected block into the prompt | `p` |

Arrow keys, `Page Up`, `Page Down`, `Home`, and `End` scroll focused long
content. Horizontal content also supports `Left`, `Right`, `Ctrl+Page Up`, and
`Ctrl+Page Down` when applicable.

## Configuration

Run `/config`, move between controls with `Tab` and `Shift+Tab`, and then use:

| Action | Control |
| --- | --- |
| Toggle a roster agent or Boolean setting | `Space` |
| Move the focused roster agent | `Alt+Up` / `Alt+Down` or **Move Up/Down** |
| Change a menu value | Focus it and choose an option |
| Save | `Ctrl+S` or **Save** |
| Discard changes | `Esc` or **Cancel** |

Checked roster agents are used for the next workspace in numbered,
top-to-bottom order. At least 1 agent must be selected. “Not detected” means
the CLI must be installed and CodeSwarm reopened before that roster can launch.
Roster changes apply to the next workspace; the current session roster cannot
be edited from the conversation.

Available preferences:

| Group | Setting | Effect |
| --- | --- | --- |
| UI | Theme | Changes the terminal color theme. |
| UI | Prompt Message | Changes the empty prompt message. |
| UI | Density | Uses comfortable or compact spacing. |
| UI | Scrollbar | Uses normal or hidden scrollbars. |
| UI | Flash Duration | Sets how long temporary status messages remain visible. |
| Notifications | System | Shows system notifications when unfocused, always, or never. |
| Notifications | Blink Title | Marks activity in the terminal title. |
| Notifications | Enable Sounds | Enables sounds for agent questions and permission requests. Completion is silent. |
| Notifications | Turn Over | Enables completion system notifications. |
| Agent | Thoughts | Shows agent reasoning when the adapter provides it. |
| Tools | Expand | Expands tool details on failure, always, or never. |
| Diff | View | Uses automatic, unified, or split diffs. |
| Diff | Wrap | Wraps or does not wrap long diff lines. |

Advanced settings:

| Setting | Effect |
| --- | --- |
| Prune Low Mark / Prune Excess | Bounds rendered transcript content in very long sessions. |
| Hide Low Severity | Hides low-priority notifications. |
| Diff Annotations | Shows annotations supplied with diffs. |

## Questions and Permission Requests

When an agent asks a question, use `Up` / `Down` and `Enter`. Clicking an
answer selects it; press `Enter` to confirm. Permission requests also support
these immediate shortcuts when the matching choice is available:

Permission requests start with no option selected, so `Enter` alone cannot
accept the first adapter-provided choice.

| Control | Action |
| --- | --- |
| `a` | Allow once |
| `Shift+A` | Always allow this kind of action |
| `r` | Reject once |
| `Shift+R` | Always reject this kind of action |
| `j` / `k` | Move between changed files in a multi-file approval |
| `Tab` / `Shift+Tab` | Move between approval controls |

In a multi-file approval, use the diff selector to switch between Automatic,
Unified, and Split views.

Always review the requested action or diff before granting persistent
permission. Permission choices are supplied by the active agent and may vary.

## Troubleshooting

### An Agent Is Not Detected

Confirm that the underlying CLI is installed and available on `PATH`, then
restart CodeSwarm. The bundled catalog supports Claude Code, Codex CLI, and
Gemini CLI. Some agents also require an ACP adapter.

### Only One Agent Responds

Confirm that at least 2 roster members are shown beside the prompt. Click an
agent there to choose who receives the next normal message.

### An Agent Keeps Working on the Wrong Task

Press `Ctrl+C` once to request cancellation. Use `/pause` to stop the entire
relay while preserving queued prompts. When ready, run `/pause` again to
resume.

### The Relay Runs Too Long

Press `Ctrl+C` to interrupt current work or `/pause` to pause the relay. On the
next launch, lower the safety limit with `--max-rounds N`.

### The `codeswarm` Launcher Says `No module named 'codeswarm'`

The launcher may be an editable uv installation pointing to an old checkout.
Reinstall it from the current CodeSwarm checkout and verify the executable:

```bash
uv tool install --editable . --force
codeswarm --version
codeswarm --help
```
