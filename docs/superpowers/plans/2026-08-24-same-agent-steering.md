# Same-Agent Steering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every untagged message submitted during a relay turn back to that same active agent before the ring advances.

**Architecture:** Replace the relay's anonymous next-agent human queue with a bounded FIFO of `(stable_roster_index, prompt)` steering entries. The relay dispatches steering as normal, context-producing turns, while explicit tagged turns remain private and higher priority. Conversation code continues to own transcript rendering and changes only its queue confirmation copy.

**Tech Stack:** Python 3.14, asyncio, Textual, `unittest`

**Spec:** `docs/superpowers/specs/2026-08-24-same-agent-steering-design.md`

## Global Constraints

- Do not overlap `session/prompt` requests for one ACP agent.
- Preserve the existing `MAX_QUEUED_PROMPTS = 100` shared bound.
- Keep stable roster indices and tombstone semantics.
- Preserve direct tagged-message privacy and priority.
- Every steering dispatch counts toward `--max-rounds`.
- Do not add transcript notes, loading widgets, or adapter-specific extensions.

---

### Task 1: Relay-owned same-agent steering queue

**Files:**
- Modify: `src/wingmen/acp/relay.py`
- Test: `tests/test_relay.py`

**Interfaces:**
- Consumes: `RelayConversation.last_active_index`, stable roster indices, `MAX_QUEUED_PROMPTS`
- Produces: `RelayConversation.enqueue_human(prompt: str) -> bool` with same-agent ownership; `_steering_queue: deque[tuple[int, str]]`

- [ ] **Step 1: Replace the old next-agent expectations with failing steering tests**

Add tests using the existing `FakeAgent` and `asyncio.Event` pattern:

```python
def test_human_follow_up_returns_to_current_agent_before_handoff(self) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class WaitingAgent(FakeAgent):
            async def send_prompt(self, prompt: str) -> str:
                self.prompts.append(prompt)
                if len(self.prompts) == 1:
                    started.set()
                    await release.wait()
                    self.last_response = "initial output"
                else:
                    self.last_response = "revised output"
                return "end_turn"

        claude = WaitingAgent("Claude", [])
        codex = FakeAgent("Codex", [("end_turn", "[WINGMEN:STOP]")])
        relay = RelayConversation((claude, codex), max_rounds=4)
        task = asyncio.create_task(relay.run("build it"))
        await started.wait()
        assert relay.enqueue_human("use the existing helper")
        release.set()
        await task

        self.assertEqual(len(claude.prompts), 2)
        self.assertIn("use the existing helper", claude.prompts[1])
        self.assertIn("revised output", codex.prompts[0])

    asyncio.run(scenario())
```

Add independent tests proving multiple steering prompts are FIFO, pending steering overrides a stop token, dropping an agent discards its steering, and queue counts include direct plus steering entries.

- [ ] **Step 2: Run the focused relay tests and verify the expected failures**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_relay -v
```

Expected: the new same-agent assertions fail because `_human_queue` currently routes to `next_agent_index`.

- [ ] **Step 3: Implement the bounded targeted steering queue**

In `RelayConversation`:

```python
self._steering_queue: deque[tuple[int, str]] = deque()

def enqueue_human(self, prompt: str) -> bool:
    if not prompt.strip() or self.queued_prompt_count >= MAX_QUEUED_PROMPTS:
        return False
    self._steering_queue.append((self.last_active_index, prompt))
    return True
```

Update `queued_prompt_count`, `drop_agent`, and `drain_for_solo_agent` to use `_steering_queue`. In `run`, dispatch `_direct_queue` first and then the oldest steering entry. Steering turns are not marked `direct_turn`, so their final response becomes relay context. If steering remains after a response—even one ending in the stop token—continue the loop before normal ring advancement.

- [ ] **Step 4: Run relay tests and verify all relay behavior passes**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_relay -v
```

Expected: all relay tests pass, including unchanged no-steering two-agent and N-agent cases.

- [ ] **Step 5: Commit the relay behavior**

```bash
git add src/wingmen/acp/relay.py tests/test_relay.py
git commit -m "feat: route busy follow-ups to active agent"
```

### Task 2: Conversation routing feedback

**Files:**
- Modify: `src/wingmen/widgets/conversation.py`
- Test: `tests/test_conversation_acp.py`

**Interfaces:**
- Consumes: `SessionCoordinator.enqueue_human(prompt: str) -> bool`, `Conversation._active_relay_agent`
- Produces: a compact flash reading `Queued for <display name>` when busy-relay steering is accepted

- [ ] **Step 1: Write a failing conversation test for active-agent feedback**

Use a mounted `Conversation` with a two-agent roster and built relay. Set `turn = "agent"` and `_active_relay_agent` to Gemini, submit an untagged message, and capture `flash`:

```python
with patch.object(conversation, "flash") as flash:
    await conversation.on_user_input_submitted(
        messages.UserInputSubmitted("use the existing parser")
    )

flash.assert_called_once_with("Queued for Gemini", style="success")
```

- [ ] **Step 2: Run the focused test and verify it fails on old copy**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_conversation_acp.ConversationACPDispatchTests.test_busy_follow_up_names_the_active_agent -v
```

Expected: FAIL because the current message is `Queued for the next agent`.

- [ ] **Step 3: Change only the busy-relay confirmation**

Resolve the active name with `_agent_display_name` and pass:

```python
active_name = (
    self._agent_display_name(self._active_relay_agent)
    if self._active_relay_agent is not None
    else "the active agent"
)
self._queue_relay_prompt(
    self.session.enqueue_human(text),
    f"Queued for {active_name}",
)
```

Do not alter tagged, paused, shell, slash-command, or solo routing branches.

- [ ] **Step 4: Run focused conversation routing tests**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_conversation_acp.ConversationACPDispatchTests.test_busy_follow_up_names_the_active_agent \
  tests.test_conversation_acp.ConversationACPDispatchTests.test_solo_follow_up_is_queued_until_the_current_turn_finishes -v
```

Expected: PASS.

- [ ] **Step 5: Commit the UI feedback**

```bash
git add src/wingmen/widgets/conversation.py tests/test_conversation_acp.py
git commit -m "fix: describe active-agent steering in prompt status"
```

### Task 3: Documentation and complete verification

**Files:**
- Modify: `AGENTS.md`
- Modify: `docs/USER_MANUAL.md`
- Test: full repository verification

**Interfaces:**
- Consumes: completed relay semantics from Tasks 1 and 2
- Produces: accurate developer and user documentation

- [ ] **Step 1: Update behavior documentation**

Replace statements saying untagged busy messages go to the next agent with:

```markdown
Untagged messages submitted while an agent is working are queued back to that
same agent. The relay advances only after the agent has handled those steering
messages, and the next agent receives the latest response as context.
```

Document FIFO ordering, the shared 100-message bound, and that explicit tags remain private targeted turns.

- [ ] **Step 2: Run the complete verification gate**

Run:

```bash
make verify
git diff --check
```

Expected: all unit tests, compile checks, lock validation, mypy checks, and whitespace checks pass.

- [ ] **Step 3: Commit documentation**

```bash
git add AGENTS.md docs/USER_MANUAL.md
git commit -m "docs: explain same-agent steering"
```
