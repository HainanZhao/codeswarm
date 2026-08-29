# Remote Terminal Rendering Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep CodeSwarm responsive in slow SSH/tmux and phone terminals with 100-turn conversations whose user and agent bubbles average 300 words.

**Architecture:** Retain Textual as the application shell, but replace its FIFO terminal-output backlog with a latest-state scheduler: one write may be in flight, stale dependent diffs are discarded, and the next accepted frame is a complete repaint. Coalesce resize storms until their final geometry and make transcript virtualization clean up append-time tail growth without reintroducing remounts during small scrolls.

**Tech Stack:** Python 3.14, Textual 8.2.7, `threading`, `unittest`, and `CodeSwarmApp.run_test`.

**Spec:** The approved architecture diagnosis in the preceding conversation; this first deployable slice addresses terminal backpressure, resize storms, and unbounded live-widget growth. A line-oriented transcript renderer is a later phase only if these changes miss the acceptance targets below.

## Global Constraints

- Preserve CodeSwarm's native Textual UI, ACP behavior, and existing public interfaces.
- Never block Textual's asyncio event loop on terminal output.
- ANSI compositor diffs are state-dependent: after any dropped diff, accept only a complete repaint before resuming incremental output.
- Keep no more than one physical write and one full-repaint candidate in the terminal pipeline.
- Coalesce continuous resize events for 100 milliseconds while keeping the first startup size immediate.
- Keep transcript mounting independent of total history after the virtual window is active.
- Do not modify or include the user's existing `README.md` and `docs/USER_MANUAL.md` changes.
- Run `make verify` before reporting completion.

---

### Task 1: Latest-state terminal writer

**Files:**
- Modify: `src/codeswarm/textual_driver.py`
- Modify: `src/codeswarm/app.py`
- Test: `tests/test_textual_driver.py`

**Interfaces:**
- Consumes: Textual's `WriterThread` file-like contract (`write`, `flush`, `fileno`, `stop`).
- Produces: `NonBlockingWriterThread.write_snapshot(text: str) -> None`, the existing `on_overflow: Callable[[], None]` callback with latest-state semantics, and `ResponsiveLinuxDriver.prepare_full_refresh() -> None`.

- [x] **Step 1: Write a failing blocked-terminal test**

Extend `_BlockedFile` to record completed writes, then assert that a blocked first frame followed by 12 incremental frames writes none of those stale frames, requests one repaint, and writes a later explicit snapshot immediately after the first frame:

```python
def test_slow_terminal_keeps_only_the_latest_full_repaint(self) -> None:
    output = _BlockedFile()
    repaint_requested = threading.Event()
    writer = NonBlockingWriterThread(output, on_overflow=repaint_requested.set)
    writer.start()
    try:
        writer.write("first")
        self.assertTrue(output.started.wait(1))
        for index in range(12):
            writer.write(f"stale-{index}")
        self.assertTrue(repaint_requested.wait(1))
        output.release.set()
        writer.stop()
        self.assertEqual(output.writes, ["first"])
    finally:
        output.release.set()
        writer.stop()
```

- [x] **Step 2: Verify the test fails for the FIFO writer**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_textual_driver.TextualDriverTests.test_slow_terminal_discards_incremental_frames_while_write_is_blocked -v`

Expected: FAIL because the current writer does not request a repaint until its 30-frame queue overflows and retains all 12 frames.

- [x] **Step 3: Implement the one-in-flight scheduler**

Use a `Condition`-protected pending item and explicit snapshot flag instead of `Queue(30)`. `write()` must return immediately; when a write is already active or pending it sets the resync state, drops the incremental frame, and invokes `on_overflow` once per required repaint. `write_snapshot()` replaces any pending candidate with a complete repaint. The worker loop performs file writes outside the condition lock and wakes shutdown safely. Combine Textual's `SYNC_START`, frame body, and `SYNC_END` writes into one scheduled frame, while preserving startup and shutdown mode-control sequences through `write_control()`.

Update `ResponsiveLinuxDriver` so `prepare_full_refresh()` marks the next driver frame as a snapshot. In `CodeSwarmApp._request_full_terminal_refresh`, call that method before dirtying the entire compositor region.

- [x] **Step 4: Run the writer tests**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_textual_driver -v`

Expected: PASS; the enqueue call remains under 100 milliseconds and only the first frame plus latest snapshot reach the blocked file.

- [x] **Step 5: Consolidate the verified rendering changes into one commit**

```bash
git add src/codeswarm/textual_driver.py src/codeswarm/app.py tests/test_textual_driver.py
git commit -m "perf: keep remote terminal sessions responsive"
```

---

### Task 2: Settled resize delivery

**Files:**
- Modify: `src/codeswarm/app.py`
- Test: `tests/test_lifecycle.py`

**Interfaces:**
- Consumes: Textual `events.Resize`, `App._resize_event`, `App._resize_timer`, and `App._check_resize()`.
- Produces: `RESIZE_SETTLE_SECONDS = 0.1` and `CodeSwarmApp._on_resize(event: events.Resize) -> None` with trailing-edge coalescing.

- [x] **Step 1: Write a failing resize-storm integration test**

Run `CodeSwarmApp` with `run_test`, replace `_check_resize` with a counter after startup, deliver ten distinct resize events 20 milliseconds apart, and assert that no resize is forwarded mid-burst and exactly one callback runs after 120 milliseconds of quiet.

```python
for index in range(10):
    await app._on_resize(events.Resize(Size(70 + index, 20), Size(70 + index, 20)))
    await pilot.pause(0.02)
self.assertEqual(resize_calls, 0)
await pilot.pause(0.12)
self.assertEqual(resize_calls, 1)
```

- [x] **Step 2: Verify the test fails against Textual's 1/120-second timer**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_lifecycle.AgentLifecycleTests.test_resize_storm_delivers_only_final_geometry -v`

Expected: FAIL because the current timer fires repeatedly during the 200-millisecond burst.

- [x] **Step 3: Implement trailing-edge resize coalescing**

Override `_on_resize` in `CodeSwarmApp`. Preserve immediate delivery when `_size is None`; otherwise stop the previous timer and set a new 100-millisecond timer for the latest event. Ignore duplicate sizes exactly as Textual does.

- [x] **Step 4: Run lifecycle and driver tests**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_lifecycle tests.test_textual_driver -v`

Expected: PASS with one resize callback after the storm.

- [x] **Step 5: Included in the consolidated rendering commit**

```bash
git add src/codeswarm/app.py tests/test_lifecycle.py
git commit -m "perf: keep remote terminal sessions responsive"
```

---

### Task 3: Bound append-time transcript mounting

**Files:**
- Modify: `src/codeswarm/widgets/conversation.py`
- Test: `tests/test_conversation_acp.py`

**Interfaces:**
- Consumes: `Contents.append_block`, `Contents.virtualize`, the current buffer/overscan constants, and `Conversation.check_virtual_window`.
- Produces: an append-generation watermark that forces one cleanup after tail growth while retaining the existing guard-window fast path for scroll-only updates.

- [x] **Step 1: Write a failing realistic transcript test**

Build 100 alternating user/agent-style transcript blocks at 300 words per block in an 80×24 `CodeSwarmApp.run_test` session. Allow one settled refresh, then assert the transcript retains all 100 logical blocks while fewer than 25 top-level blocks remain mounted. Scroll through 20 adjacent row positions and assert `remove_children` is not called while the positions remain inside the current overscan guard.

- [x] **Step 2: Verify append accumulation fails**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_conversation_acp.ConversationACPDispatchTests.test_hundred_turn_transcript_keeps_a_bounded_live_window -v`

Expected: FAIL because append operations can remain in `_mounted_indices` until a later guard miss.

- [x] **Step 3: Add an append-generation cleanup condition**

Track the transcript block count at the last virtual-tree rebuild. A virtualize call may use the cheap guard return only when the mounted window covers the guard and no appended blocks older than the current pinned tail remain mounted. After rebuilding, advance the watermark. Do not restore a raw maximum-row or maximum-widget guard, since tall messages previously caused every small scroll to remount.

- [x] **Step 4: Run all transcript virtualization regressions**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_conversation_acp.ConversationACPDispatchTests.test_hundred_turn_transcript_keeps_a_bounded_live_window tests.test_conversation_acp.ConversationACPDispatchTests.test_long_transcript_mounts_only_the_visible_window_and_live_tail tests.test_conversation_acp.ConversationACPDispatchTests.test_rapid_scroll_requests_rebuild_the_virtual_window_at_a_bounded_rate tests.test_conversation_acp.ConversationACPDispatchTests.test_small_scrolls_do_not_remount_a_tall_virtual_window tests.test_conversation_acp.ConversationACPDispatchTests.test_scrolling_keeps_detached_agent_message_content_rendered -v`

Expected: PASS; content remains present, the live tail stays mounted, and adjacent scrolling performs no tree rebuild.

- [x] **Step 5: Included in the consolidated rendering commit**

```bash
git add src/codeswarm/widgets/conversation.py tests/test_conversation_acp.py
git commit -m "perf: keep remote terminal sessions responsive"
```

---

### Task 4: End-to-end slow-terminal verification

**Files:**
- Modify: `tests/test_textual_driver.py`
- Modify: `tests/test_conversation_acp.py`

**Interfaces:**
- Consumes: the latest-state writer and realistic transcript fixture from Tasks 1 and 3.
- Produces: regression coverage for constant-time enqueue, a maximum one-frame backlog, retained message content, and bounded mounting at 100 × 300 words.

- [x] **Step 1: Add the sustained-backpressure edge case**

While the first write remains blocked, enqueue an explicit snapshot, then more incremental output. Assert that the first snapshot is not followed by dependent diffs and a second repaint request is raised for the newer state.

- [x] **Step 2: Run the focused performance regressions**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_textual_driver tests.test_conversation_acp -v`

Expected: PASS with no blocked event-loop writes, no stale FIFO replay, and no lost transcript content.

- [x] **Step 3: Run the repository quality gate**

Run: `make verify`

Expected: PASS for package identity, 300+ unit/integration tests, compilation, lockfile validation, type checking, and diff hygiene.

- [x] **Step 4: Inspect the final patch without touching unrelated files**

Run: `git status --short && git diff --check HEAD && git diff --stat HEAD`

Expected: only `plan.md`, the rendering implementation, and its tests are part of this work; the pre-existing README/manual edits remain unstaged and unchanged.

## Acceptance Targets

- A blocked terminal never makes `write()` wait and never accumulates a FIFO of stale screen diffs.
- The terminal pipeline contains at most one in-flight write and one complete repaint candidate.
- Ten resize events over 200 milliseconds result in one final geometry delivery after 100 milliseconds of quiet.
- A 100-message transcript at 300 words per bubble retains every logical message while keeping fewer than 25 top-level transcript blocks mounted after settling.
- Twenty adjacent small scroll positions inside one guard window cause zero virtual-tree remounts.
- Timers and input remain runnable while the simulated terminal file is blocked.
- `make verify` passes.
