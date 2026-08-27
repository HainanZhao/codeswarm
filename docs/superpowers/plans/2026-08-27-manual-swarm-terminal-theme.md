# Manual Swarm Mode and Vivid Terminal Theme Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Add a manually pinned \`Swarm\` collaboration mode while preserving the existing sequential \`Roster\` relay, brighten conversation bubbles, and publish a verified new package release.

**Architecture:** Keep \`RelayConversation\` as the unchanged-default ring implementation and add \`PinnedConversation\` for one selected agent per user turn. Share bounded public-journal context between the two implementations. \`SessionCoordinator\` selects the collaboration implementation; \`Conversation\` owns commands, click routing, queues, and footer state. Release verification builds and installs a clean wheel before publishing.

**Tech Stack:** Python 3.14, Textual 8.2.7, Click 8.4.0, \`unittest\`, Textual \`CodeSwarmApp.run_test\`, uv, PyPI.

**Spec:** \`docs/superpowers/specs/2026-08-27-manual-swarm-terminal-theme-design.md\`

## Global Constraints

- \`Roster\` remains the default and preserves automatic sequential handoff, one-shot first-recipient selection, reviewer-only stopping, and queue ordering.
- \`Swarm\` sends one user turn to one persistent pinned active agent and changes agents only after user selection.
- Collaboration mode is separate from ACP permission modes (\`Manual\`, \`Plan\`, and \`Auto pilot\`).
- Only public human and agent text enters shared context; tool calls, thoughts, terminal output, and UI history stay local.
- No concurrent agent execution or ACP provider changes.
- Retain the black/teal foundation, teal input and agent headers, and use vivid high-contrast message cards.
- Run focused tests after each task, then \`make verify\` and clean-wheel smoke tests before publishing.
- Do not add a runtime \`PYTHONPATH\` fallback; the launcher issue was a stale local uv editable install and is repaired by reinstalling from this checkout.

## File Map

- Create \`src/codeswarm/acp/collaboration.py\` for shared public-journal and prompt context.
- Create \`src/codeswarm/acp/pinned.py\` for persistent one-agent-per-turn orchestration.
- Modify \`src/codeswarm/acp/relay.py\` only to consume shared context without changing the ring contract.
- Modify \`src/codeswarm/session.py\` for collaboration mode selection and persistent pinned selection.
- Modify \`src/codeswarm/widgets/conversation.py\` and, if needed, \`src/codeswarm/widgets/prompt.py\` for commands, routing, queues, and footer markers.
- Modify \`src/codeswarm/app.py\`, \`src/codeswarm/screens/main.tcss\`, and message widgets for vivid card styling.
- Modify \`tests/test_relay.py\`, \`tests/test_session.py\`, and \`tests/test_conversation_acp.py\`; create \`tests/test_pinned.py\`.
- Modify \`docs/USER_MANUAL.md\`, \`pyproject.toml\`, \`uv.lock\`, and \`CHANGELOG.md\` for usage, release version, lock state, and release notes.

---

### Task 1: Extract Shared Collaboration Context Without Changing the Ring

**Files:**
- Create: \`src/codeswarm/acp/collaboration.py\`
- Modify: \`src/codeswarm/acp/relay.py\`
- Test: \`tests/test_relay.py\`

**Interfaces:**
- Produce \`CollaborationContext\`, \`RelayEvent\`, bounded event history, response compaction, and turn-prompt helpers for both coordinators.
- Preserve \`RelayConversation\`'s current constructor, callbacks, queue behavior, stop-token rules, and public tests.

- [ ] **Step 1: Add failing context-preservation tests.**

Assert that an agent receives prior public responses when a second relay uses the same context, while private direct prompts and trailing stop tokens never enter the context. Keep all current two-agent and N-agent tests unchanged.

- [ ] **Step 2: Run the baseline relay suite.**

~~~text
PYTHONPATH=src .venv/bin/python -m unittest tests.test_relay -v
~~~

Expected: current relay tests pass before the refactor.

- [ ] **Step 3: Implement \`CollaborationContext\`.**

Use these fields and operations:

~~~python
class CollaborationContext:
    shared_task: str | None
    public_events: list[RelayEvent]
    seen_event_count: list[int]
    history_truncated: list[bool]
    turn_instructions: str

    def record_event(self, speaker: str, text: str) -> int: ...
    def unseen_updates(self, agent_index: int, excluding: int | None) -> str: ...
    def build_turn_prompt(self, task: str, context: str, *, previous_agent: AgentLike | None, unseen_updates: str = "", can_stop: bool = False) -> str: ...
    def mark_seen(self, agent_index: int) -> None: ...
    def add_agent(self) -> None: ...
~~~

Move the existing limits and prompt wording without changing their output. Preserve stable per-agent indices when agents are tombstoned.

- [ ] **Step 4: Delegate \`RelayConversation\` journal operations to the context.**

Keep \`_direct_queue\`, \`_steering_queue\`, \`_advance\`, \`run\`, callback order, and reviewer-only stop-token gating structurally unchanged. Accept an optional keyword-only context for mode transitions while creating a fresh context by default.

- [ ] **Step 5: Verify and commit.**

~~~text
PYTHONPATH=src .venv/bin/python -m unittest tests.test_relay -v
git diff --check HEAD
git add src/codeswarm/acp/collaboration.py src/codeswarm/acp/relay.py tests/test_relay.py
git commit -m "refactor: share collaboration turn context"
~~~

### Task 2: Add Persistent Pinned Swarm Orchestration

**Files:**
- Create: \`src/codeswarm/acp/pinned.py\`
- Create: \`tests/test_pinned.py\`

**Interfaces:**
- Produce \`PinnedConversation(agents, *, first_agent=0, context=None, on_turn_start=None, on_queued_turn_start=None, on_queued_turn_discarded=None, on_turn=None)\`.
- \`run(prompt: str, first_agent: int = 0) -> RelayResult\` dispatches exactly one public turn and leaves \`pinned_agent_index\` unchanged.
- \`select_agent(index: int) -> None\` validates and persists an active stable index.
- \`enqueue_human(prompt: str) -> bool\` captures the current pinned index.
- \`drop_agent(index: int) -> None\` discards only that target's queued work and never silently reroutes.

- [ ] **Step 1: Add failing tests.**

Cover default pin, repeated runs staying on one agent, selection changing only subsequent runs, public context on agent switch, queued target capture, pause/resume, invalid selection, and dropped-pinned-agent behavior. A normal result must be \`RelayResult(1, True, "turn_complete")\`; no reviewer stop token is required.

- [ ] **Step 2: Run the new tests and confirm they fail.**

~~~text
PYTHONPATH=src .venv/bin/python -m unittest tests.test_pinned -v
~~~

- [ ] **Step 3: Implement one-turn pinned dispatch.**

Validate the pinned target, honor pause, drain direct queue before human queue, build one prompt with unseen public updates and \`can_stop=False\`, await one \`send_prompt\`, compact/record the response, mark the target seen, invoke callbacks, and return without advancing the pin. Implement stable-index add/drop, queue cancellation, solo drain, pause, and resume by reusing the existing queue limit.

- [ ] **Step 4: Verify and commit.**

~~~text
PYTHONPATH=src .venv/bin/python -m unittest tests.test_pinned tests.test_relay -v
git add src/codeswarm/acp/pinned.py tests/test_pinned.py
git commit -m "feat: add pinned swarm collaboration"
~~~

### Task 3: Integrate Modes in \`SessionCoordinator\`

**Files:**
- Modify: \`src/codeswarm/session.py\`
- Modify: \`tests/test_session.py\`

**Interfaces:**
- Add \`CollaborationMode = Literal["roster", "swarm"]\` and \`DEFAULT_COLLABORATION_MODE = "roster"\`.
- Add \`collaboration_mode\`, \`collaboration_active\`, \`set_collaboration_mode(mode)\`, and \`select_pinned_agent(index)\`.
- Keep \`relay_active\` and \`select_agent\` compatible for existing callers/tests.

- [ ] **Step 1: Add failing session tests.**

Assert default roster construction, switching to \`PinnedConversation\` without replacing agent objects, switching back with shared public context and selected first recipient, persistent pinned selection, invalid/inactive selection rejection, same-slot replacement preservation, and solo-session behavior.

- [ ] **Step 2: Run focused tests and confirm failure.**

~~~text
PYTHONPATH=src .venv/bin/python -m unittest tests.test_session -v
~~~

- [ ] **Step 3: Implement coordinator mode selection.**

Own one \`CollaborationContext\`, construct \`RelayConversation\` for \`roster\` and \`PinnedConversation\` for \`swarm\`, defer transitions until an active request completes, and preserve roster, agents, context, and queued items. In roster mode keep one-shot \`selected_agent_index\`; in swarm mode preserve \`pinned_agent_index\`. Validate indices after add/drop/replacement and never fallback silently.

- [ ] **Step 4: Verify and commit.**

~~~text
PYTHONPATH=src .venv/bin/python -m unittest tests.test_session tests.test_pinned tests.test_relay -v
git add src/codeswarm/session.py tests/test_session.py
git commit -m "feat: select roster or pinned collaboration mode"
~~~

### Task 4: Add Commands, Click Routing, and UI Integration

**Files:**
- Modify: \`src/codeswarm/widgets/conversation.py\`
- Modify: \`src/codeswarm/widgets/prompt.py\`
- Modify: \`tests/test_conversation_acp.py\`
- Modify: \`docs/USER_MANUAL.md\`

**Interfaces:**
- Add \`/collab roster\` and \`/collab swarm\`, separate from \`/mode\` permission selection.
- Route roster clicks to existing one-shot selection in \`roster\` and persistent pin selection in \`swarm\`.
- Show the collaboration label and pin marker in the prompt footer.

- [ ] **Step 1: Add failing \`CodeSwarmApp.run_test\` coverage.**

Assert default \`Roster\`, Swarm command selection, one-agent-only dispatch, repeated pinned dispatch, click changing only later target, busy/paused queue target capture, invalid selection stability, and distinct working/pinned footer markers.

- [ ] **Step 2: Run the targeted UI tests.**

~~~text
PYTHONPATH=src .venv/bin/python -m unittest tests.test_conversation_acp -v
~~~

- [ ] **Step 3: Implement commands and routing.**

Parse only \`roster\` and \`swarm\`, Flash on invalid values, and leave the current mode unchanged on failure. Make busy untagged Swarm input queue to the pinned stable index; keep direct ACP slash commands private. On pinned-agent failure, discard only that target's queued items and require a user click to choose a replacement.

- [ ] **Step 4: Document the controls.**

Add \`/collab roster\` as sequential review relay and \`/collab swarm\` as manual routing. Document stale editable-install recovery with \`uv tool install --editable . --force\` followed by \`codeswarm --version\`.

- [ ] **Step 5: Verify and commit.**

~~~text
PYTHONPATH=src .venv/bin/python -m unittest tests.test_conversation_acp tests.test_session tests.test_pinned tests.test_relay -v
git add src/codeswarm/widgets/conversation.py src/codeswarm/widgets/prompt.py tests/test_conversation_acp.py docs/USER_MANUAL.md
git commit -m "feat: expose manual swarm routing"
~~~

### Task 5: Brighten Conversation Message Cards

**Files:**
- Modify: \`src/codeswarm/app.py\`
- Modify: \`src/codeswarm/screens/main.tcss\`
- Modify: \`src/codeswarm/widgets/agent_response.py\`
- Modify: \`src/codeswarm/widgets/user_input.py\`
- Modify: \`tests/test_conversation_acp.py\`

- [ ] **Step 1: Add style assertions.**

Assert that the primary theme remains \`#2DD4BF\`, new vivid variables exist, agent tone classes remain stable, and user bubbles have a stable semantic selector.

- [ ] **Step 2: Implement the palette and CSS.**

Add solid dark agent surfaces with saturated cyan/blue/violet/amber edges and a vivid teal/coral user surface. Preserve teal headers/input, near-white markdown, readable code blocks, right alignment, narrow layout, and subdued tool summaries.

- [ ] **Step 3: Verify and commit.**

~~~text
PYTHONPATH=src .venv/bin/python -m unittest tests.test_conversation_acp -v
PYTHONPATH=src .venv/bin/python -m codeswarm --help
git add src/codeswarm/app.py src/codeswarm/screens/main.tcss src/codeswarm/widgets/agent_response.py src/codeswarm/widgets/user_input.py tests/test_conversation_acp.py
git commit -m "style: brighten conversation message cards"
~~~

### Task 6: Version, Build, and Verify the Release

**Files:**
- Modify: \`pyproject.toml\`
- Modify: \`uv.lock\`
- Modify: \`CHANGELOG.md\`
- Modify: \`scripts/verify_package.py\` only if a concrete clean-wheel failure requires it.

- [ ] **Step 1: Bump the patch version.**

Change \`0.6.31\` to \`0.6.32\` in \`pyproject.toml\`, regenerate lock metadata with \`uv lock --python .venv/bin/python\`, and add a concise changelog entry for pinned Swarm mode, vivid cards, and launcher recovery guidance.

- [ ] **Step 2: Verify uv project execution.**

~~~text
uv lock --check --python .venv/bin/python
uv run --project . codeswarm --version
uv run --project . codeswarm --help
~~~

Expected: the commands import the current checkout, report \`0.6.32\`, and exit successfully.

- [ ] **Step 3: Build and install a clean wheel.**

~~~text
temp_dir=$(mktemp -d)
uv build --wheel --sdist --python .venv/bin/python --out-dir "$temp_dir"
uv venv --python .venv/bin/python "$temp_dir/venv"
uv pip install --python "$temp_dir/venv/bin/python" "$temp_dir"/codeswarm-0.6.32-py3-none-any.whl
"$temp_dir/venv/bin/codeswarm" --version
"$temp_dir/venv/bin/codeswarm" --help
~~~

Expected: the clean wheel exposes the package and console script without \`PYTHONPATH\`.

- [ ] **Step 4: Run the repository quality gate.**

~~~text
make verify
~~~

- [ ] **Step 5: Commit the release preparation.**

~~~text
git add pyproject.toml uv.lock CHANGELOG.md
git commit -m "release: prepare codeswarm 0.6.32"
~~~

### Task 7: Publish and Verify the New Package

**Files:**
- No source files; publish only the verified wheel and sdist from the clean build directory.

- [ ] **Step 1: Confirm the PyPI credential is available without printing it.**

~~~text
set -a
source /Users/hainan.zhao/.zsh_secrets
set +a
test -n "$PYPI_API_TOKEN"
~~~

- [ ] **Step 2: Publish the exact built artifacts.**

Use the same verified build directory:

~~~text
uv publish --token "$PYPI_API_TOKEN" "$temp_dir"/codeswarm-0.6.32-*.whl "$temp_dir"/codeswarm-0.6.32.tar.gz
~~~

Do not publish an unverified or differently-versioned artifact.

- [ ] **Step 3: Verify the public package with uv.**

~~~text
release_dir=$(mktemp -d)
uv venv --python .venv/bin/python "$release_dir/venv"
uv pip install --python "$release_dir/venv/bin/python" "codeswarm==0.6.32"
"$release_dir/venv/bin/codeswarm" --version
"$release_dir/venv/bin/codeswarm" --help
~~~

Expected: PyPI resolves \`codeswarm==0.6.32\`, the import succeeds, and the executable reports \`0.6.32\`.

- [ ] **Step 4: Verify the editable local launcher remains correct.**

~~~text
uv tool install --editable . --force
codeswarm --version
codeswarm --help
~~~

Expected: the launcher imports from this checkout and reports \`0.6.32\`.

### Task 8: Completion Audit

- [ ] **Step 1: Inspect status and diffs.**

~~~text
git diff --check HEAD
git status --short
git log --oneline -10
~~~

Expected: no whitespace errors or generated artifacts.

- [ ] **Step 2: Audit every requirement against evidence.**

Verify Roster alternation and reviewer-only stopping, Swarm one-turn pinning and user-only switching, queue/failure semantics, public-context privacy, vivid cards with teal identity, working \`uv run\`, clean-wheel installation, published PyPI package, and working \`codeswarm\` launcher.

