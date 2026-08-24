# Wingmen agent-development notes

## Project identity

- Wingmen is the current project name and the only supported package identity.
- The published Python distribution, import package, and executable are all
  `wingmen`. There is no compatibility package or executable.
- User-facing branding uses Wingmen and the `✈` symbol.
- No telemetry is collected. The upstream sponsor tile and testimonial/about
  UI were removed; `©` attribution to Will McGugan remains in the license.

## ACP relay behavior

- Every session defaults to Wingmen's **Fully Auto** permission policy. After
  all active agents advertise their mode catalogs, Wingmen translates the
  policy to each native mode ID and synchronizes the complete roster. A later
  user selection becomes the new desired roster-wide policy; `Mixed` is not a
  user-facing mode.
- An unlimited-size roster of ACP agents relay turns sequentially in a ring
  (`src/wingmen/acp/relay.py`, `RelayConversation`), never concurrently — a relay
  has a causal dependency on the previous response. Solo sessions (roster size
  1) never construct a relay; `Conversation._relay_active` gates every relay
  code path so the common single-agent case is untouched.
- Roster index 0 is the session owner: it holds the session's DB row and
  title, and cannot be dropped (`/close` instead).
- Each agent receives the ordered public human and agent-message updates it has
  not seen since its previous turn. Only streamed message text enters this
  journal; tool calls, thoughts, terminal output, and UI history stay local.
- Untagged human messages submitted while an agent is working are queued back
  to that same agent, in FIFO order, before the relay advances. The next agent
  receives the active agent's latest response as context.
- Clicking an agent beside the prompt selects it as the first recipient for
  the next normal relay message. Duplicate names display their roster number.
- `[WINGMEN:STOP]` is the safe word, but only an agent reviewing a different
  agent's response may use it. The first responder after any human message and
  direct/private turns cannot stop peer review. An eligible reviewer with
  nothing meaningful to add may send an emoji followed by the token; a
  token-only response is displayed as `👍`. Wingmen always hides the token.
- While work is active, the first `Ctrl+C` requests cancellation and a second
  press within three seconds quits; while idle, `Ctrl+C` quits immediately.
  Pause/resume is available for relays through `Ctrl+Shift+P` and `/pause`;
  queued messages remain buffered while paused.
- The relay defaults to 100 automated turns and can be adjusted with
  `--max-rounds N`. This is a runaway-safety limit, not a per-agent budget —
  it does not scale with roster size.
- `/agent list|add <agent>|drop <n>` changes the roster inside a running
  session. `drop` tombstones an entry (`active = False`) rather than removing
  it, so roster indices stay valid.

## Launch flow

- Bare `wingmen` restores the last-used roster (`launcher.roster` setting,
  written on every roster mutation). If no saved roster resolves, it opens the
  agent store instead of auto-starting anything — detection
  (`agents.detect_preferred_agents`) only pre-selects candidates on that
  screen, it never starts a session by itself.
- In the store, `space` toggles an agent's membership in the roster being
  built; `enter` launches that roster, or the highlighted agent solo if
  nothing is selected. There is no quick-launch row.

## Verification

Run the repository quality gate before release:

```bash
make verify
```

For Textual, ACP, and CLI changes, add a regression test at the integration
boundary that failed: use `WingmenApp.run_test` for reactive UI flows and
Click's `CliRunner` for entry points. Test invalid and replacement external
state as well as the nominal state; adapters may omit, reorder, or replace
values between messages.

`tests/test_relay.py` is the regression gate for the relay ring. Preserve its
two-agent alternation behavior except when intentionally changing a documented
relay contract, such as reviewer-only stopping. `RelayConversationRosterTests`
covers N>2 semantics specifically.
