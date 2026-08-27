import asyncio
import unittest

from codeswarm import jsonrpc
from codeswarm.acp.relay import MAX_QUEUED_PROMPTS, MAX_RELAY_EVENTS, RelayConversation


class FakeAgent:
    def __init__(self, name: str, responses: list[tuple[str, str]]) -> None:
        self.name = name
        self.responses = iter(responses)
        self.prompts: list[str] = []
        self.last_response = ""

    async def send_prompt(self, prompt: str) -> str:
        self.prompts.append(prompt)
        stop_reason, self.last_response = next(self.responses)
        return stop_reason

    def get_info(self) -> str:
        return self.name


class RelayConversationTests(unittest.TestCase):
    def test_cancelled_run_resumes_queued_human_prompt_without_synthetic_context(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("cancelled", "Handled correction")])
            gemini = FakeAgent("Gemini", [])
            relay = RelayConversation((claude, gemini))
            relay.context.shared_task = "Original task"
            relay.last_active_index = 0
            self.assertTrue(relay.enqueue_human("queued correction"))

            result = await relay.run("", resume_queued=True)

            self.assertEqual(result.reason, "cancelled")
            self.assertIn("queued correction", claude.prompts[0])
            self.assertNotIn("Human follow-up:\n\n", claude.prompts[0])
            self.assertEqual(relay.queued_prompt_count, 0)

        asyncio.run(scenario())

    def test_failed_first_turn_does_not_commit_a_stale_shared_task(self) -> None:
        async def scenario() -> None:
            class CapacityLimitedAgent(FakeAgent):
                async def send_prompt(self, prompt: str) -> str:
                    self.prompts.append(prompt)
                    if len(self.prompts) == 1:
                        raise jsonrpc.APIError(429, "No capacity available", None)
                    self.last_response = "Recovered"
                    return "end_turn"

            gemini = CapacityLimitedAgent("Gemini", [])
            claude = FakeAgent("Claude", [("end_turn", "[CODESWARM:STOP]")])
            relay = RelayConversation((gemini, claude))

            with self.assertRaises(jsonrpc.APIError):
                await relay.run("Original task")

            result = await relay.run("Retry task")

            self.assertEqual(result.reason, "stop_token")
            self.assertIn("Shared task:\nRetry task", gemini.prompts[1])
            self.assertNotIn("Human follow-up", gemini.prompts[1])
            self.assertEqual(relay.active_indices, [0, 1])

        asyncio.run(scenario())

    def test_idle_direct_prompt_includes_unseen_public_history(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent(
                "Claude",
                [("end_turn", "Public answer"), ("end_turn", "private reply")],
            )
            gemini = FakeAgent(
                "Gemini",
                [("end_turn", "[CODESWARM:STOP]"), ("end_turn", "private reply")],
            )
            relay = RelayConversation((claude, gemini))
            await relay.run("Public question")

            await relay.send_direct_prompt(0, "private question")

            self.assertIn("private question", claude.prompts[1])
            self.assertIn("Gemini:\n👍", claude.prompts[1])

        asyncio.run(scenario())

    def test_cancel_queued_prompt_removes_only_the_requested_occurrence(self) -> None:
        first = FakeAgent("A", [])
        second = FakeAgent("B", [])
        relay = RelayConversation((first, second))

        self.assertTrue(relay.enqueue_human("same"))
        self.assertTrue(relay.enqueue_human("keep"))
        self.assertTrue(relay.enqueue_human("same"))

        self.assertTrue(relay.cancel_queued("same", False, occurrence=1))
        self.assertEqual(list(relay._steering_queue), [(0, "same"), (0, "keep")])


    def test_public_journal_is_hard_bounded(self) -> None:
        first = FakeAgent("A", [])
        second = FakeAgent("B", [])
        relay = RelayConversation((first, second))

        for index in range(MAX_RELAY_EVENTS + 20):
            relay._record_event("A", f"event {index}")

        self.assertLessEqual(len(relay._public_events), MAX_RELAY_EVENTS)
        self.assertIn("omitted older unseen updates", relay._unseen_updates(1, excluding=None))

    def test_first_speaker_cannot_use_stop_token_to_skip_peer_review(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent(
                "Claude",
                [("end_turn", "The answer is 42.\n[CODESWARM:STOP]")],
            )
            gemini = FakeAgent(
                "Gemini",
                [("end_turn", "👍\n[CODESWARM:STOP]")],
            )

            result = await RelayConversation((claude, gemini)).run(
                "What is the answer?"
            )

            self.assertEqual(result.reason, "stop_token")
            self.assertEqual(result.rounds, 2)
            self.assertIn("The answer is 42.", gemini.prompts[0])

        asyncio.run(scenario())

    def test_empty_reviewer_stop_uses_a_visible_default_acknowledgment(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "The fix is ready")])
            gemini = FakeAgent("Gemini", [("end_turn", "[CODESWARM:STOP]")])
            visible_turns: list[str] = []

            async def on_turn(round_number, agent, response) -> None:
                visible_turns.append(response)

            result = await RelayConversation(
                (claude, gemini), on_turn=on_turn
            ).run("Fix it")

            self.assertEqual(result.reason, "stop_token")
            self.assertEqual(visible_turns, ["The fix is ready", "👍"])

        asyncio.run(scenario())

    def test_turns_alternate_and_relay_previous_response(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "review"), ("end_turn", "[CODESWARM:STOP]")])
            codex = FakeAgent("Codex", [("end_turn", "improve")])
            result = await RelayConversation((claude, codex)).run("build it")

            self.assertEqual(result.reason, "stop_token")
            self.assertEqual(result.rounds, 3)
            self.assertIn("Shared task:\nbuild it", claude.prompts[0])
            self.assertIn("improve", claude.prompts[1])
            self.assertIn("Shared task:\nbuild it", codex.prompts[0])
            self.assertIn("Turn context:\nreview", codex.prompts[0])

        asyncio.run(scenario())

    def test_round_limit_stops_without_an_extra_prompt(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "one"), ("end_turn", "three")])
            codex = FakeAgent("Codex", [("end_turn", "two")])
            result = await RelayConversation((claude, codex), max_rounds=2).run("build it")

            self.assertEqual(result.reason, "max_rounds")
            self.assertEqual(result.rounds, 2)
            self.assertEqual(len(claude.prompts) + len(codex.prompts), 2)

        asyncio.run(scenario())

    def test_configured_first_agent_receives_initial_prompt(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "[CODESWARM:STOP]")])
            codex = FakeAgent("Codex", [("end_turn", "Initial answer")])
            result = await RelayConversation((claude, codex)).run(
                "build it", first_agent=1
            )

            self.assertEqual(result.reason, "stop_token")
            self.assertIn("Shared task:\nbuild it", codex.prompts[0])
            self.assertEqual(len(claude.prompts), 1)
            self.assertIn("Initial answer", claude.prompts[0])

        asyncio.run(scenario())

    def test_stop_token_is_internal_and_is_not_forwarded(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "Finished\n[CODESWARM:STOP]")])
            codex = FakeAgent("Codex", [("end_turn", "[CODESWARM:STOP]")])
            result = await RelayConversation((claude, codex)).run("finish it")

            self.assertEqual(result.reason, "stop_token")
            self.assertIn("Do not use [CODESWARM:STOP]", claude.prompts[0])
            self.assertIn("reviewing another participant's answer", codex.prompts[0])
            self.assertIn("Finished", codex.prompts[0])
            self.assertNotIn("Finished\n[CODESWARM:STOP]", codex.prompts[0])

        asyncio.run(scenario())

    def test_relay_context_is_bounded(self) -> None:
        async def scenario() -> None:
            long_response = "A" * 20_000
            claude = FakeAgent("Claude", [("end_turn", long_response)])
            codex = FakeAgent("Codex", [("end_turn", "[CODESWARM:STOP]")])
            await RelayConversation((claude, codex)).run("build it")

            self.assertLess(len(codex.prompts[0]), 13_000)
            self.assertIn("omitted the middle", codex.prompts[0])

        asyncio.run(scenario())

    def test_turn_start_callback_identifies_active_agent(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "review")])
            codex = FakeAgent("Codex", [("end_turn", "[CODESWARM:STOP]")])
            started: list[str] = []

            async def on_turn_start(round_number, agent) -> None:
                started.append(agent.get_info())

            await RelayConversation(
                (claude, codex), on_turn_start=on_turn_start
            ).run("build it")
            self.assertEqual(started, ["Claude", "Codex"])

        asyncio.run(scenario())

    def test_queued_turn_callback_runs_when_a_prompt_is_dispatched(self) -> None:
        async def scenario() -> None:
            started = asyncio.Event()
            release = asyncio.Event()
            dispatched: list[tuple[str, bool]] = []

            class WaitingAgent(FakeAgent):
                async def send_prompt(self, prompt: str) -> str:
                    self.prompts.append(prompt)
                    if len(self.prompts) == 1:
                        started.set()
                        await release.wait()
                    self.last_response = "response"
                    return "end_turn"

            claude = WaitingAgent("Claude", [])
            codex = FakeAgent("Codex", [("end_turn", "[CODESWARM:STOP]")])

            async def on_queued_turn_start(_round, _agent, prompt, direct) -> None:
                dispatched.append((prompt, direct))

            relay = RelayConversation(
                (claude, codex), on_queued_turn_start=on_queued_turn_start
            )
            task = asyncio.create_task(relay.run("build it"))
            await started.wait()
            relay.enqueue_human("review this change")
            self.assertEqual(dispatched, [])
            release.set()
            await task
            self.assertEqual(dispatched, [("review this change", False)])

        asyncio.run(scenario())

    def test_direct_prompt_targets_tagged_agent_without_relaying_response(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "[CODESWARM:STOP]")])
            codex = FakeAgent("Codex", [("end_turn", "[CODESWARM:STOP]")])
            relay = RelayConversation((claude, codex))
            relay.enqueue_direct(1, "inspect this specific file")
            result = await relay.run("continue")

            self.assertIn("inspect this specific file", codex.prompts[0])
            self.assertEqual(len(claude.prompts), 1)
            self.assertEqual(result.reason, "stop_token")

        asyncio.run(scenario())

    def test_pause_blocks_dispatch_until_resumed(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "finished")])
            codex = FakeAgent("Codex", [("end_turn", "[CODESWARM:STOP]")])
            relay = RelayConversation((claude, codex))
            relay.pause()

            paused = await relay.run("build it")
            self.assertEqual(paused.reason, "paused")
            self.assertEqual(claude.prompts, [])

            relay.resume()
            finished = await relay.run("build it")
            self.assertEqual(finished.reason, "stop_token")
            self.assertEqual(len(claude.prompts), 1)

        asyncio.run(scenario())

    def test_follow_up_uses_next_agent_and_keeps_shared_task(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent(
                "Claude",
                [("end_turn", "Initial implementation"), ("end_turn", "Error handling checked")],
            )
            codex = FakeAgent(
                "Codex",
                [("end_turn", "[CODESWARM:STOP]"), ("end_turn", "[CODESWARM:STOP]")],
            )
            relay = RelayConversation((claude, codex))

            first = await relay.run("Build the integration")
            follow_up = await relay.run("Please review the error handling")

            self.assertEqual(first.reason, "stop_token")
            self.assertEqual(follow_up.reason, "stop_token")
            self.assertIn("Shared task:\nBuild the integration", claude.prompts[1])
            self.assertIn(
                "Human follow-up:\nPlease review the error handling",
                claude.prompts[1],
            )
            self.assertIn("Error handling checked", codex.prompts[1])

        asyncio.run(scenario())

    def test_agent_receives_human_and_agent_updates_missed_since_its_last_turn(
        self,
    ) -> None:
        async def scenario() -> None:
            gemini = FakeAgent(
                "Gemini",
                [
                    ("end_turn", "Initial answer"),
                    ("end_turn", "[CODESWARM:STOP]"),
                ],
            )
            claude = FakeAgent(
                "Claude",
                [
                    ("end_turn", "[CODESWARM:STOP]"),
                    ("end_turn", "Claude answered the follow-up"),
                ],
            )
            relay = RelayConversation((gemini, claude))

            await relay.run("First question", first_agent=0)
            relay.next_agent_index = 1
            await relay.run("Second question", first_agent=0)

            gemini_follow_up = gemini.prompts[1]
            self.assertIn("Second question", gemini_follow_up)
            self.assertIn("Claude answered the follow-up", gemini_follow_up)
            self.assertLess(
                gemini_follow_up.index("Second question"),
                gemini_follow_up.index("Claude answered the follow-up"),
            )

        asyncio.run(scenario())

    def test_token_after_an_answer_ends_the_relay_and_keeps_the_answer(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "The fix is ready [CODESWARM:STOP]")])
            codex = FakeAgent("Codex", [("end_turn", "✅ [CODESWARM:STOP]")])
            visible_turns: list[str] = []

            async def on_turn(round_number, agent, response) -> None:
                visible_turns.append(response)

            result = await RelayConversation(
                (claude, codex), on_turn=on_turn
            ).run("Build it")

            self.assertEqual(result.reason, "stop_token")
            self.assertEqual(result.rounds, 2)
            self.assertEqual(visible_turns, ["The fix is ready", "✅"])
            self.assertEqual(len(codex.prompts), 1)

        asyncio.run(scenario())

    def test_non_trailing_token_does_not_end_the_relay(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent(
                "Claude", [("end_turn", "The token [CODESWARM:STOP] is documented here.")]
            )
            codex = FakeAgent("Codex", [("end_turn", "[CODESWARM:STOP]")])

            result = await RelayConversation((claude, codex)).run("Document it")

            self.assertEqual(result.reason, "stop_token")
            self.assertEqual(result.rounds, 2)
            self.assertIn("[CODESWARM:STOP] is documented here", codex.prompts[0])

        asyncio.run(scenario())

    def test_human_follow_up_returns_to_current_agent_before_handoff(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent(
                "Claude",
                [("end_turn", "reviewed"), ("end_turn", "revised")],
            )
            codex = FakeAgent("Codex", [("end_turn", "[CODESWARM:STOP]")])
            relay: RelayConversation

            async def inject_follow_up(round_number, agent, response) -> None:
                if round_number == 1:
                    relay.enqueue_human("please focus on the failing test")

            relay = RelayConversation(
                (claude, codex), max_rounds=4, on_turn=inject_follow_up
            )
            result = await relay.run("build it")

            self.assertEqual(result.reason, "stop_token")
            self.assertEqual(len(claude.prompts), 2)
            self.assertIn("please focus on the failing test", claude.prompts[1])
            self.assertIn("Turn context:\nrevised", codex.prompts[0])

        asyncio.run(scenario())

    def test_human_message_waits_for_current_agent_output(self) -> None:
        async def scenario() -> None:
            started = asyncio.Event()
            release = asyncio.Event()

            class WaitingAgent(FakeAgent):
                async def send_prompt(self, prompt: str) -> str:
                    self.prompts.append(prompt)
                    if len(self.prompts) == 1:
                        started.set()
                        await release.wait()
                        self.last_response = "current output"
                    else:
                        self.last_response = "steered output"
                    return "end_turn"

            claude = WaitingAgent("Claude", [])
            codex = FakeAgent("Codex", [("end_turn", "[CODESWARM:STOP]")])
            relay = RelayConversation((claude, codex))
            task = asyncio.create_task(relay.run("build it"))
            await started.wait()
            relay.enqueue_human("please check the failing test")
            self.assertEqual(codex.prompts, [])
            release.set()
            await task
            self.assertEqual(len(claude.prompts), 2)
            self.assertIn("please check the failing test", claude.prompts[1])
            self.assertIn("steered output", codex.prompts[0])

        asyncio.run(scenario())

    def test_multiple_human_messages_steer_same_agent_in_fifo_order(self) -> None:
        async def scenario() -> None:
            started = asyncio.Event()
            release = asyncio.Event()

            class WaitingAgent(FakeAgent):
                async def send_prompt(self, prompt: str) -> str:
                    self.prompts.append(prompt)
                    if len(self.prompts) == 1:
                        started.set()
                        await release.wait()
                    self.last_response = f"response {len(self.prompts)}"
                    return "end_turn"

            claude = WaitingAgent("Claude", [])
            codex = FakeAgent("Codex", [("end_turn", "[CODESWARM:STOP]")])
            relay = RelayConversation((claude, codex), max_rounds=5)
            task = asyncio.create_task(relay.run("build it"))

            await started.wait()
            self.assertTrue(relay.enqueue_human("first correction"))
            self.assertTrue(relay.enqueue_human("second correction"))
            release.set()
            await task

            self.assertEqual(len(claude.prompts), 3)
            self.assertIn("first correction", claude.prompts[1])
            self.assertIn("second correction", claude.prompts[2])
            self.assertIn("response 3", codex.prompts[0])

        asyncio.run(scenario())

    def test_queued_human_message_takes_priority_over_stop_token(self) -> None:
        async def scenario() -> None:
            started = asyncio.Event()
            release = asyncio.Event()

            class StoppingAgent(FakeAgent):
                async def send_prompt(self, prompt: str) -> str:
                    self.prompts.append(prompt)
                    if len(self.prompts) == 1:
                        started.set()
                        await release.wait()
                        self.last_response = "Initial answer\n[CODESWARM:STOP]"
                    else:
                        self.last_response = "Revised answer\n[CODESWARM:STOP]"
                    return "end_turn"

            claude = StoppingAgent("Claude", [])
            codex = FakeAgent("Codex", [("end_turn", "[CODESWARM:STOP]")])
            relay = RelayConversation((claude, codex))
            task = asyncio.create_task(relay.run("initial question"))

            await started.wait()
            self.assertTrue(relay.enqueue_human("one more question"))
            release.set()
            result = await task

            self.assertEqual(result.reason, "stop_token")
            self.assertEqual(result.rounds, 3)
            self.assertEqual(len(codex.prompts), 1)
            self.assertIn("Turn context:\none more question", claude.prompts[1])

        asyncio.run(scenario())

    def test_dropping_agent_discards_its_queued_steering(self) -> None:
        claude = FakeAgent("Claude", [])
        codex = FakeAgent("Codex", [])
        discarded: list[tuple[str, bool]] = []
        relay = RelayConversation(
            (claude, codex),
            on_queued_turn_discarded=lambda prompt, direct: discarded.append(
                (prompt, direct)
            ),
        )
        relay.last_active_index = 1

        self.assertTrue(relay.enqueue_human("keep checking"))
        self.assertTrue(relay.enqueue_direct(1, "inspect the trace"))
        relay.drop_agent(1)

        self.assertEqual(relay.queued_prompt_count, 0)
        self.assertEqual(
            discarded,
            [("inspect the trace", True), ("keep checking", False)],
        )


    def test_queued_prompts_have_a_shared_bound(self) -> None:
        first = FakeAgent("A", [])
        second = FakeAgent("B", [])
        relay = RelayConversation((first, second))

        for index in range(MAX_QUEUED_PROMPTS):
            self.assertTrue(relay.enqueue_human(f"message {index}"))

        self.assertFalse(relay.enqueue_direct(0, "one too many"))
        self.assertEqual(relay.queued_prompt_count, MAX_QUEUED_PROMPTS)


class RelayConversationRosterTests(unittest.TestCase):
    def test_queued_work_survives_a_collapse_to_one_agent(self) -> None:
        first = FakeAgent("A", [])
        second = FakeAgent("B", [])
        relay = RelayConversation([first, second])
        relay.enqueue_human("continue the review")
        relay.enqueue_direct(0, "focus on the failing test")
        relay.drop_agent(1)

        self.assertEqual(
            relay.drain_for_solo_agent(),
            ["focus on the failing test", "continue the review"],
        )
        self.assertEqual(relay.drain_for_solo_agent(), [])

    """N>2 behavior: round-robin order, roster mutation, and range checks."""

    def test_three_agents_round_robin(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "a")])
            codex = FakeAgent("Codex", [("end_turn", "b")])
            gemini = FakeAgent("Gemini", [("end_turn", "[CODESWARM:STOP]")])
            order: list[str] = []

            async def on_turn_start(round_number, agent) -> None:
                order.append(agent.get_info())

            await RelayConversation(
                (claude, codex, gemini), on_turn_start=on_turn_start
            ).run("build it")

            self.assertEqual(order, ["Claude", "Codex", "Gemini"])

        asyncio.run(scenario())

    def test_direct_turn_resumes_after_target_at_n3(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "should not run")])
            codex = FakeAgent("Codex", [("end_turn", "private answer")])
            gemini = FakeAgent("Gemini", [("end_turn", "[CODESWARM:STOP]")])
            relay = RelayConversation((claude, codex, gemini))
            relay.enqueue_direct(1, "inspect this specific file")
            result = await relay.run("continue")

            self.assertIn("inspect this specific file", codex.prompts[0])
            self.assertEqual(claude.prompts, [])
            # The tagged response is never relayed onward as context.
            self.assertNotIn("private answer", gemini.prompts[0])
            self.assertEqual(result.reason, "stop_token")

        asyncio.run(scenario())

    def test_requires_at_least_two_agents(self) -> None:
        claude = FakeAgent("Claude", [])
        with self.assertRaises(ValueError):
            RelayConversation((claude,))

    def test_first_agent_out_of_range_raises(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "[CODESWARM:STOP]")])
            codex = FakeAgent("Codex", [])
            gemini = FakeAgent("Gemini", [])
            relay = RelayConversation((claude, codex, gemini))
            with self.assertRaises(ValueError):
                await relay.run("x", first_agent=3)

        asyncio.run(scenario())

    def test_first_agent_two_on_three_roster(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "[CODESWARM:STOP]")])
            codex = FakeAgent("Codex", [])
            gemini = FakeAgent("Gemini", [("end_turn", "Initial answer")])
            relay = RelayConversation((claude, codex, gemini))
            result = await relay.run("build it", first_agent=2)

            self.assertEqual(result.reason, "stop_token")
            self.assertIn("Shared task:\nbuild it", gemini.prompts[0])
            self.assertEqual(len(claude.prompts), 1)
            self.assertEqual(codex.prompts, [])

        asyncio.run(scenario())

    def test_add_agent_joins_rotation_mid_run(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent(
                "Claude", [("end_turn", "one"), ("end_turn", "three")]
            )
            codex = FakeAgent("Codex", [("end_turn", "two")])
            gemini = FakeAgent("Gemini", [("end_turn", "[CODESWARM:STOP]")])
            relay = RelayConversation((claude, codex), max_rounds=4)

            async def on_turn(round_number, agent, response) -> None:
                if round_number == 1:
                    relay.add_agent(gemini)

            relay.on_turn = on_turn
            result = await relay.run("build it")

            self.assertEqual(result.reason, "stop_token")
            self.assertEqual(len(gemini.prompts), 1)

        asyncio.run(scenario())

    def test_drop_agent_is_skipped_and_indices_are_stable(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent(
                "Claude", [("end_turn", "one"), ("end_turn", "[CODESWARM:STOP]")]
            )
            codex = FakeAgent("Codex", [("end_turn", "should not run")])
            gemini = FakeAgent("Gemini", [("end_turn", "two")])
            relay = RelayConversation((claude, codex, gemini), max_rounds=4)

            async def on_turn(round_number, agent, response) -> None:
                if round_number == 1:
                    relay.drop_agent(1)

            relay.on_turn = on_turn
            await relay.run("build it")

            self.assertEqual(codex.prompts, [])
            self.assertEqual(relay.agents.index(gemini), 2)

        asyncio.run(scenario())

    def test_next_agent_index_normalizes_past_a_dropped_agent(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "one"), ("end_turn", "[CODESWARM:STOP]")])
            codex = FakeAgent("Codex", [])
            gemini = FakeAgent("Gemini", [("end_turn", "continued")])
            relay = RelayConversation((claude, codex, gemini))

            async def pause_after_first(round_number, agent, response) -> None:
                if round_number == 1:
                    relay.pause()

            relay.on_turn = pause_after_first
            first = await relay.run("build it", first_agent=0)
            self.assertEqual(first.reason, "paused")
            # Claude ran; the rotation had already advanced to codex (index 1)
            # before the pause took effect on the next loop iteration.
            self.assertEqual(relay.next_agent_index, 1)

            relay.drop_agent(1)
            relay.resume()
            relay.on_turn = None
            result = await relay.run("continue")

            self.assertEqual(codex.prompts, [])
            self.assertEqual(result.reason, "stop_token")
            self.assertEqual(len(gemini.prompts), 1)
            self.assertIn("continue", gemini.prompts[0])

        asyncio.run(scenario())

    def test_human_follow_up_returns_to_current_agent_at_n3(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent(
                "Claude",
                [("end_turn", "reviewed"), ("end_turn", "revised")],
            )
            codex = FakeAgent("Codex", [("end_turn", "[CODESWARM:STOP]")])
            gemini = FakeAgent("Gemini", [("end_turn", "should not run")])
            relay: RelayConversation

            async def inject_follow_up(round_number, agent, response) -> None:
                if round_number == 1:
                    relay.enqueue_human("please focus on the failing test")

            relay = RelayConversation(
                (claude, codex, gemini), max_rounds=5, on_turn=inject_follow_up
            )
            result = await relay.run("build it")

            self.assertEqual(result.reason, "stop_token")
            self.assertEqual(len(claude.prompts), 2)
            self.assertIn("please focus on the failing test", claude.prompts[1])
            self.assertIn("Turn context:\nrevised", codex.prompts[0])
            self.assertEqual(len(codex.prompts), 1)
            self.assertEqual(gemini.prompts, [])

        asyncio.run(scenario())

    def test_enqueue_direct_rejects_dropped_and_out_of_range(self) -> None:
        claude = FakeAgent("Claude", [])
        codex = FakeAgent("Codex", [])
        gemini = FakeAgent("Gemini", [])
        relay = RelayConversation((claude, codex, gemini))
        relay.drop_agent(1)

        with self.assertRaises(ValueError):
            relay.enqueue_direct(1, "x")
        with self.assertRaises(ValueError):
            relay.enqueue_direct(3, "x")


if __name__ == "__main__":
    unittest.main()
