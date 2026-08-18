# Goal: two-agent ACP conversations

Refactor Taiji so one conversation can run two ACP agents at the same time (for
example Claude Code and Codex).

## User experience

- Start with `taiji run -a claude --agent2 codex PATH`.
- Use `--first-agent 2` to send the initial prompt to the second agent.
- Gemini CLI is available as `gemini`, for example:
  `taiji run -a gemini --agent2 codex --first-agent 1 PATH`.
- The user's first prompt goes to the primary agent.
- Once that turn completes, the agents alternate automatically. Each agent
  receives the previous agent's response and the original task context.
- Untagged human messages entered while an agent is working are queued for the
  next agent in sequence.
- Tagged messages such as `@claude: inspect this` bypass turn alternation and
  target only the named agent.
- The conversation stops when an agent includes `[TAIJI:STOP]`, when an ACP
  agent fails, when the user cancels, or after a safe maximum number of rounds.
- The first prompt explicitly teaches both agents that `[TAIJI:STOP]` is the
  safe word. A response containing it is terminal and is never relayed.
- Existing single-agent invocations and behavior remain unchanged.

## Implementation requirements

- Use the existing ACP transport and session lifecycle for both agents.
- Keep relay orchestration separate from Textual rendering so it is testable.
- Capture streamed ACP text for the relay without changing rendered output.
- Relay only bounded agent message text; never forward tool-call, thought,
  terminal, or UI history.
- Clearly label automated turns in the TUI.
- Stop both child agents when the conversation unmounts.
- Add focused tests for turn order, stop-token handling, and round limits.

## Verification

- Run syntax/type-appropriate checks available in the repository.
- Run the focused tests.
- Verify the CLI help exposes the second-agent option.
