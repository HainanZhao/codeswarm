# ✈ CodeSwarm

CodeSwarm is a focused terminal workspace for collaborating with one or more
[Agent Client Protocol](https://agentclientprotocol.com/) (ACP) coding agents.
Build a roster of Claude Code, Codex, Gemini CLI, or another ACP-compatible
agent and let them work through a task sequentially in one shared conversation.

## Highlights

- Run one coding agent or an unlimited roster from the same terminal UI.
- Relay multi-agent turns in order, so every response can build on the last.
- Select the first recipient for the next relay message directly from the
  roster beside the prompt.
- Queue follow-up messages safely while an agent is working.
- Use a compact fighter-HUD interface designed for terminal workflows.
- Keep work local: CodeSwarm collects no telemetry.

## Install

```bash
uv tool install codeswarm
# or: pip install codeswarm
```

CodeSwarm requires Python 3.14 or later on macOS or Linux.

## Run

```bash
codeswarm
```

On first launch, choose the agents for your roster. Later launches restore that
roster. To select a project directory, pass it to the command:

```bash
codeswarm ~/projects/example
```

Launch a specific roster directly when needed:

```bash
codeswarm run -a claude -a codex -a gemini ~/projects/example
```

Agents take turns sequentially. Click an agent beside the prompt to choose who
receives the next message. Press `Ctrl+C` to cancel work or quit; use
`Ctrl+Shift+P` to pause or resume a multi-agent relay.

New sessions default to **Auto pilot**, which allows agent tool requests
without asking for confirmation. CodeSwarm translates that policy to each
agent's native permission mode and keeps the roster synchronized. You can
change the policy from the mode selector beside the prompt.

## Essential controls

| Action | Control |
| --- | --- |
| Attach a project file | Type `@` followed by its path. `@` does not tag agents. |
| Choose the next recipient | Click an agent beside the prompt. |
| Pause or resume a relay | `Ctrl+Shift+P` or `/pause` |
| Cancel active work | Press `Ctrl+C` once. |
| Quit | Press `Ctrl+C` while idle, or twice within three seconds while work is active. |
| Set the relay safety limit | Start with `--max-rounds N` (default: 100 automated turns). |

Messages submitted while an agent is working wait in a bounded holding area
and are delivered in order before the relay advances. See the
[user manual](https://github.com/HainanZhao/codeswarm/blob/main/docs/USER_MANUAL.md)
for the complete launch flow, commands,
permissions, and troubleshooting guide.

## ACP agents

CodeSwarm bundles catalog entries for Claude Code, Codex, and Gemini CLI. To use
another ACP-compatible command:

```bash
codeswarm acp "node /path/to/agent-acp.js" ~/projects/example
```

The external agent and its ACP adapter must already be installed and
authenticated. CodeSwarm starts the adapter but does not bundle provider CLIs or
manage their accounts.

## Privacy

CodeSwarm does not collect telemetry. Agent prompts, responses, tool calls, and
terminal activity remain subject to the policies of the agent and provider you
choose.

## Development

```bash
make verify
```

## License

CodeSwarm is licensed under
[AGPL-3.0](https://github.com/HainanZhao/codeswarm/blob/main/LICENSE). See the
[commercial license notice](https://github.com/HainanZhao/codeswarm/blob/main/COMMERCIAL_LICENSE.md)
for commercial licensing.
