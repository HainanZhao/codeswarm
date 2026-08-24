# Wingmen

Wingmen is a focused terminal workspace for collaborating with one or more
[Agent Client Protocol](https://agentclientprotocol.com/) (ACP) coding agents.
It keeps the conversation, tool activity, and relay hand-offs in one terminal
instead of trying to be an IDE, file browser, or agent marketplace.

## Install

```bash
uv tool install wingmen
# or: pip install wingmen
```

Wingmen requires Python 3.14 or later and runs on macOS and Linux.

## Start a workspace

```bash
wingmen
```

On first launch, choose agents in the roster screen:

- Click an agent, or press `space`, to add or remove it.
- `enter` launches the selected roster, or the highlighted agent solo.

Later launches restore the last usable roster. To choose a project directory,
pass it to the command:

```bash
wingmen ~/projects/example
```

You can also launch a known roster directly:

```bash
wingmen run -a claude -a codex -a gemini ~/projects/example
```

Agents take turns sequentially. Before each turn, an agent receives the shared
task plus every public human question and agent response it has not seen since
its previous turn. Tool calls, reasoning, terminals, and UI history remain
local to the agent that produced them. Use `--first-agent N` to choose the
first speaker and `--max-rounds N` to set the relay safety limit.

## Working with agents

Send a normal message to continue the relay. To address a specific agent
without relaying its response, use a tag:

```text
@claude: inspect the failing test
```

If names repeat, use the displayed suffix such as `@claude-2:`.

The roster is shown beside the prompt. A filled marker identifies the current
speaker; the colored name above each response identifies its agent.

Core controls:

| Control | Action |
| --- | --- |
| `Ctrl+C` | Cancel active work; press again within 3 seconds to quit |
| `Ctrl+Shift+P` | Pause or resume a multi-agent relay |
| `/` | Show available local and agent commands |

Useful local commands:

```text
/about
/agent list
/agent add codex
/agent drop 2
/pause
/clear
/close
```

See the [user manual](docs/USER_MANUAL.md) for every command, keyboard action,
mode, setting, and multi-agent workflow.

When the agents have completed the task, Wingmen ends the relay automatically.
Its internal relay-control output is never shown to you or forwarded to another
agent.

## ACP agents

Wingmen launches ACP adapters over stdio. Its bundled catalog deliberately
contains only Claude Code, Codex, and Gemini CLI. Detection checks the
platform-specific executable required by each agent; for npx-backed adapters,
that means the underlying CLI rather than Node alone.

For a CLI without native ACP, use a local bridge:

```bash
wingmen acp "node /path/to/agent-acp.js" ~/projects/example
```

Google Antigravity is available through its ACP Registry server. Install it
from the ACP Registry, then point Wingmen at the downloaded server:

```bash
wingmen acp "/path/to/agy_acp_server.par" ~/projects/example
```

The official registry publishes platform binaries for that server; the plain
`agy` CLI is not itself an ACP server.

## Development

Run the release-quality gate before shipping a change:

```bash
make verify
```

## License

Wingmen is licensed under the [AGPL-3.0](./LICENSE). See
[COMMERCIAL_LICENSE.md](./COMMERCIAL_LICENSE.md) for commercial licensing.
