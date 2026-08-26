# Live Config Roster Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/config` available during startup, improve its field and roster controls, reconcile saved membership with the live conversation, and rename the full-access display policy to Auto pilot.

**Architecture:** Conversation owns readiness-aware local-command dispatch and live roster reconciliation; SessionCoordinator continues to own adapter and relay lifecycles. ConfigScreen receives an optional Conversation and delegates live changes while its focused row widgets own safe inline ordering controls.

**Tech Stack:** Python 3.14, Textual 8.2.7, asyncio, unittest, `CodeSwarmApp.run_test`

**Spec:** `docs/superpowers/specs/2026-08-26-live-config-roster-design.md`

## Global Constraints

- Keep roster index 0 as the current session owner; it cannot be removed.
- Keep existing live roster indices and relative order stable; append new live peers.
- Save checkbox order for the next workspace without reordering the live relay.
- Keep the internal full-access ID `codeswarm:mode:full-access` and native aliases unchanged.
- Use CodeSwarm's Flash ribbon for conversation notifications; do not show Textual Toasts over the conversation.
- Preserve unrelated uncommitted Antigravity catalog and alias work already in the worktree.

---

### Task 1: Rename the full-access display policy

**Files:**
- Modify: `src/codeswarm/mode_policy.py:56-65`
- Modify: `tests/test_conversation_acp.py:409-510,2818-2865`
- Modify: `README.md:50-54`
- Modify: `docs/USER_MANUAL.md:180-190`
- Modify: `AGENTS.md:14-16`

**Interfaces:**
- Consumes: `ModePolicy(id, name, description, aliases)` and `DEFAULT_MODE_POLICY_ID`.
- Produces: user-facing full-access name `Auto pilot` while retaining ID `codeswarm:mode:full-access`.

- [ ] **Step 1: Change UI assertions to require Auto pilot**

```python
self.assertEqual(conversation.current_mode.name, "Auto pilot")
self.assertEqual(
    [conversation.modes[mode_id].name for mode_id in ordered_mode_ids],
    ["Chat", "Plan", "Manual", "Accept Edits", "Auto pilot"],
)
```

- [ ] **Step 2: Run the focused tests and verify the old name fails**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_conversation_acp.ConversationACPTests.test_roster_mode_is_translated_and_applied_to_every_agent -v`

Expected: FAIL because the current display name is `Fully Auto`.

- [ ] **Step 3: Rename only the policy display name and current documentation**

```python
ModePolicy(
    "codeswarm:mode:full-access",
    "Auto pilot",
    "Automatically approve all tools and bypass permission prompts",
    frozenset({"fullaccess", "yolo", "bypasspermissions", "skippermissions"}),
)
```

Update current README, user manual, and AGENTS guidance from `Fully Auto` to `Auto pilot`. Preserve historical CHANGELOG text and parser tests for adapter notices such as `fully-auto`.

- [ ] **Step 4: Run the focused mode tests**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_conversation_acp.ConversationACPTests.test_roster_mode_is_translated_and_applied_to_every_agent -v`

Expected: PASS.

---

### Task 2: Dispatch `/config` before ACP readiness

**Files:**
- Modify: `src/codeswarm/widgets/prompt.py:200-230,560-570`
- Modify: `src/codeswarm/widgets/conversation.py:934-975,1977-1981`
- Modify: `tests/test_conversation_acp.py:770-815`

**Interfaces:**
- Consumes: `UserInputSubmitted`, `Conversation.slash_command(text)`, and `Conversation.agent_ready`.
- Produces: local slash-command submission while not ready; readiness rejection for agent-bound input remains in Conversation.

- [ ] **Step 1: Add failing startup command tests**

```python
def test_config_command_opens_while_agent_is_loading(self) -> None:
    async def scenario() -> None:
        async with CodeSwarmApp(setup_prompt=False).run_test(size=(120, 40)) as pilot:
            conversation = pilot.app.screen.query_one(Conversation)
            conversation.agent_ready = False
            conversation.prompt.text = "/config"
            conversation.prompt.prompt_text_area.action_submit()
            await pilot.pause(0.1)
            self.assertIsInstance(pilot.app.screen, ConfigScreen)
    asyncio.run(scenario())
```

Add a second test that completes `/config` through `SlashComplete.Completed(..., submit=True)` while `agent_ready` is false and reaches the same screen.

- [ ] **Step 2: Run both startup command tests and verify the readiness guard blocks them**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_conversation_acp.ConversationACPTests.test_config_command_opens_while_agent_is_loading -v`

Expected: FAIL because `PromptTextArea.action_submit()` currently returns before posting `UserInputSubmitted`.

- [ ] **Step 3: Route slash syntax to Conversation and guard unresolved agent input there**

```python
# PromptTextArea.action_submit
if not self.agent_ready and not self.text.strip().startswith("/"):
    self.app.bell()
    self.post_message(messages.Flash(
        "Agent is not ready. Please wait while the agent connects…", "error"
    ))
    return
self.post_message(UserInputSubmitted(self.text))
self.clear()
```

In `Prompt.on_slash_complete_completed`, submit slash commands without checking `prompt_text_area.agent_ready`. In `Conversation.on_user_input_submitted`, after `slash_command()` returns false, reject forwarding when `self.agent_ready` is false and show the existing readiness Flash message.

Pass the current conversation when opening config:

```python
self.app.push_screen(ConfigScreen(self))
```

- [ ] **Step 4: Run startup, unknown-command, and agent-command routing tests**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_conversation_acp -v`

Expected: PASS; `/config` opens while loading, unknown commands remain local errors, and advertised agent commands are not sent before readiness.

---

### Task 3: Align numeric schema and Textual input validation

**Files:**
- Modify: `src/codeswarm/screens/config.py:49-65`
- Modify: `tests/test_config.py:14-120`

**Interfaces:**
- Consumes: `SettingField.type` values `integer`, `number`, and `string`.
- Produces: Textual Input types `integer`, `number`, and `text` respectively.

- [ ] **Step 1: Add a failing Flash Duration validity test**

```python
flash_duration = config.query_one("#config-ui-flash_duration", Input)
self.assertEqual(flash_duration.value, "3.0")
self.assertEqual(flash_duration.type, "number")
self.assertTrue(flash_duration.is_valid)
```

Then set `flash_duration.value = "not-a-number"`, trigger validation, and assert `is_valid` is false.

- [ ] **Step 2: Run the config test and verify integer validation rejects 3.0**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_config.ConfigScreenTests.test_flash_duration_uses_decimal_validation -v`

Expected: FAIL because the widget type is currently `integer` and `3.0` receives Textual's invalid class.

- [ ] **Step 3: Map number fields to Textual's number input**

```python
input_type = {
    "integer": "integer",
    "number": "number",
}.get(self.field.type, "text")
yield Input(str(self.value), type=input_type, id=self.field.control_id)
```

- [ ] **Step 4: Run the config tests**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_config -v`

Expected: PASS with a neutral valid border for the default and invalid state for non-numeric text.

---

### Task 4: Replace global roster movement with inline row controls

**Files:**
- Modify: `src/codeswarm/screens/config.py:45-292`
- Modify: `src/codeswarm/screens/config.tcss:38-75`
- Modify: `tests/test_config.py`
- Modify: `docs/USER_MANUAL.md:240-260`

**Interfaces:**
- Produces: `ConfigRosterRow(identity: str, control_id: str, label: str, selected: bool)` with `checkbox`, `up_button`, and `down_button` accessors.
- Consumes: `_read_roster()` and existing `Alt+Up` / `Alt+Down` bindings.

- [ ] **Step 1: Add failing mouse and keyboard ordering tests**

```python
rows = list(config.query(ConfigRosterRow))
selected = [row for row in rows if row.checkbox.value]
original_membership = {row.identity: row.checkbox.value for row in rows}
await selected[1].up_button.press()
await pilot.pause()
self.assertEqual(config._read_roster()[:2], [selected[1].identity, selected[0].identity])
self.assertEqual(
    {row.identity: row.checkbox.value for row in config.query(ConfigRosterRow)},
    original_membership,
)
```

Assert the first selected Up and last selected Down buttons are disabled, unchecked rows have both buttons disabled, and `Alt+Up` moves the row when its Down button has focus.

- [ ] **Step 2: Run the focused ordering tests and verify ConfigRosterRow is missing**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_config.ConfigScreenTests.test_inline_roster_buttons_move_without_toggling -v`

Expected: ERROR or FAIL because the row component and inline controls do not exist.

- [ ] **Step 3: Implement focused rows and direct movement**

```python
class ConfigRosterRow(containers.Horizontal):
    def __init__(self, identity: str, control_id: str, label: str, selected: bool) -> None:
        super().__init__(classes="config-roster-row")
        self.identity = identity
        self.control_id = control_id
        self.roster_label = label
        self.selected = selected

    def compose(self) -> ComposeResult:
        yield Checkbox("", self.selected, id=self.control_id, classes="config-roster-agent")
        yield Button("↑", id=f"{self.control_id}-up", classes="config-roster-move -up")
        yield Button("↓", id=f"{self.control_id}-down", classes="config-roster-move -down")
```

Replace global controls with row buttons. Resolve mouse events through `button.query_ancestor(ConfigRosterRow)`. Resolve keyboard movement through the focused widget's ancestor row. Move a selected row before or after the adjacent selected row, then refresh numbering and disabled states.

- [ ] **Step 4: Style compact row controls and update help text**

Give the checkbox `width: 1fr`; give each arrow button a compact fixed width; keep each row height 3; remove `#config-roster-order-actions` CSS. Update the manual to say click a row's arrow or use `Alt+Up` / `Alt+Down`.

- [ ] **Step 5: Run config integration tests**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_config -v`

Expected: PASS at narrow and normal terminal sizes.

---

### Task 5: Reconcile config membership with the live roster

**Files:**
- Modify: `src/codeswarm/screens/config.py`
- Modify: `src/codeswarm/session.py:500-520`
- Modify: `src/codeswarm/widgets/conversation.py:1548-1600,1941-1982`
- Modify: `tests/test_session.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_conversation_acp.py`
- Modify: `docs/USER_MANUAL.md:250-260`

**Interfaces:**
- `ConfigScreen(conversation: Conversation | None = None)`.
- `Conversation.reconcile_roster(identities: list[str], catalog: dict[str, AgentData]) -> list[str]` returns identities that failed to start.
- `SessionCoordinator.persist_roster(settings, save_settings, launcher_identities: Sequence[str] | None = None) -> None` persists explicit next-launch order while storing live order in session metadata.

- [ ] **Step 1: Add failing session persistence-order test**

Create an owner and two peers, keep their live order unchanged, call `persist_roster(..., launcher_identities=[peer2_id, owner_id, peer1_id])`, and assert `launcher.roster` uses the explicit order while session metadata uses active live order.

- [ ] **Step 2: Run the session test and verify the optional argument is unsupported**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_session.SessionCoordinatorTests.test_persist_roster_separates_live_and_next_launch_order -v`

Expected: FAIL because `persist_roster` does not accept `launcher_identities`.

- [ ] **Step 3: Extend roster persistence without changing default callers**

```python
async def persist_roster(
    self,
    settings: SettingsStore,
    save_settings: SaveSettings,
    launcher_identities: Sequence[str] | None = None,
) -> None:
    active_identities = [
        entry.data["identity"] for entry in self.roster if entry.active
    ]
    settings.set(
        "launcher.roster",
        "\n".join(active_identities if launcher_identities is None else launcher_identities),
    )
```

Keep session metadata `roster` equal to `active_identities`.

- [ ] **Step 4: Add failing live add/remove UI tests**

Construct a conversation roster with a fake ready owner and peer. Open `ConfigScreen(conversation)`, verify the owner checkbox is selected and disabled, select a catalog agent, uncheck the peer, save, and assert:

```python
self.assertEqual(
    [entry.data["identity"] for entry in conversation.session.roster if entry.active],
    [owner_identity, added_identity],
)
self.assertTrue(removed_agent.stopped)
self.assertEqual(conversation.session.roster[0].data["identity"], owner_identity)
```

Also assert reordering existing checked rows changes `launcher.roster` but not the live roster order. Add a startup-failure case whose fake factory raises, then assert healthy agents remain active, failed identity is omitted from persistence, and the Flash ribbon reports the failure. Add an active-turn case that keeps the screen open and changes no membership.

- [ ] **Step 5: Run live roster tests and verify save only changes settings**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_config tests.test_conversation_acp -v`

Expected: FAIL because ConfigScreen has no conversation context or reconciliation call.

- [ ] **Step 6: Implement live reconciliation and cleanup**

In `Conversation.reconcile_roster`, force the owner identity into the requested set, start missing identities through `SessionCoordinator.add()` with the same callbacks used by `_start_agents`, then drop unchecked non-owner entries. For every removed agent, discard its ID from `_ready_agents`, `_agent_modes`, and timing state; clear routing selections that reference it. Refresh `agent`, roster display, mode display, title, and slash commands; synchronize the desired mode; persist successful identities in requested next-launch order.

In `ConfigScreen`, use active identities as membership when a conversation is present, disable the owner checkbox, reject live save while `conversation.turn == "agent"`, and call `reconcile_roster` after ordinary field validation. Keep standalone ConfigScreen behavior unchanged.

- [ ] **Step 7: Update configuration documentation**

Document that config membership applies immediately to an idle current conversation, current owner removal requires `/close`, existing live order stays stable, and checkbox order controls the next workspace.

- [ ] **Step 8: Run all focused tests**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_session tests.test_config tests.test_conversation_acp -v`

Expected: PASS.

---

### Task 6: Verify the complete change set

**Files:**
- Verify: all modified source, tests, documentation, and existing user changes.

**Interfaces:**
- Consumes: repository quality gate.
- Produces: evidence that the integrated behavior passes project checks.

- [ ] **Step 1: Run formatting and whitespace checks**

Run: `git diff --check HEAD`

Expected: no output and exit 0.

- [ ] **Step 2: Run the full repository gate**

Run: `make verify`

Expected: package verification, all unittests, compileall, lock check, and mypy pass.

- [ ] **Step 3: Inspect the final diff for unrelated changes**

Run: `git status --short && git diff --stat HEAD && git diff HEAD -- src/codeswarm tests docs README.md AGENTS.md`

Expected: only approved live-roster, config UX, numeric validation, Auto pilot rename, tests, and the user's pre-existing Antigravity work are present.

