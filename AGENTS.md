# Taiji agent-development notes

## Project identity

- The published Python distribution is `taiji-cli`.
- The primary executable is `taiji`; `toad` remains a compatibility alias for
  the internal module layout and existing installations.
- User-facing branding uses Taiji and the `☯` symbol.

## ACP relay behavior

- Two ACP agents run sequentially, never concurrently for a causal relay.
- An agent's streamed message text is the only content forwarded to the next
  agent. Tool calls, thoughts, terminal output, and UI history stay local.
- Untagged human messages wait for the current agent to finish, then go to the
  next agent in sequence.
- Tagged messages such as `@claude: inspect this` bypass alternation and target
  only that agent. Duplicate names use `-yin` and `-yang` suffixes.
- `[TAIJI:STOP]` is the safe word. A response containing it ends the relay and
  is never forwarded.
- Pause/resume is available through `Ctrl+C`, `Ctrl+Shift+P`, and
  `/taiji:pause`. Queued messages remain buffered while paused.
- The relay defaults to 100 automated turns and can be adjusted with
  `--max-rounds N`.

## Verification

Run the focused tests and syntax checks before release:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```
