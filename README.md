# Wingmen

Wingmen is a focused terminal workspace for collaborating with one or more
[Agent Client Protocol](https://agentclientprotocol.com/) (ACP) coding agents.

## Install

```bash
uv tool install wingmen
# or: pip install wingmen
```

Wingmen requires Python 3.14 or later on macOS or Linux.

## Run

```bash
wingmen
# `wingwomen` is an equivalent alias.
```

On first launch, choose the agents for your roster. Later launches restore that
roster. To select a project directory, pass it to the command:

```bash
wingmen ~/projects/example
```

Launch a specific roster directly when needed:

```bash
wingmen run -a claude -a codex -a gemini ~/projects/example
```

Agents take turns sequentially. Click an agent beside the prompt to choose who
receives the next message. Press `Ctrl+C` to cancel work or quit; use
`Ctrl+Shift+P` to pause or resume a multi-agent relay.

See the [user manual](docs/USER_MANUAL.md) for all commands and controls.

## ACP agents

Wingmen bundles catalog entries for Claude Code, Codex, and Gemini CLI. To use
another ACP-compatible command:

```bash
wingmen acp "node /path/to/agent-acp.js" ~/projects/example
```

## Development

```bash
make verify
```

## License

Wingmen is licensed under [AGPL-3.0](./LICENSE). See
[COMMERCIAL_LICENSE.md](./COMMERCIAL_LICENSE.md) for commercial licensing.
