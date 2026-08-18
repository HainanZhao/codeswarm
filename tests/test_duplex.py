import asyncio
import unittest

from toad.acp.duplex import DuplexConversation


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


class DuplexConversationTests(unittest.TestCase):
    def test_turns_alternate_and_relay_previous_response(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "review"), ("end_turn", "done [TAIJI:STOP]")])
            codex = FakeAgent("Codex", [("end_turn", "improve")])
            result = await DuplexConversation((claude, codex)).run("build it")

            self.assertEqual(result.reason, "stop_token")
            self.assertEqual(result.rounds, 3)
            self.assertIn("Human task:\nbuild it", claude.prompts[0])
            self.assertIn("improve", claude.prompts[1])
            self.assertIn("review", codex.prompts[0])

        asyncio.run(scenario())

    def test_round_limit_stops_without_an_extra_prompt(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "one"), ("end_turn", "three")])
            codex = FakeAgent("Codex", [("end_turn", "two")])
            result = await DuplexConversation((claude, codex), max_rounds=2).run("build it")

            self.assertEqual(result.reason, "max_rounds")
            self.assertEqual(result.rounds, 2)
            self.assertEqual(len(claude.prompts) + len(codex.prompts), 2)

        asyncio.run(scenario())

    def test_configured_first_agent_receives_initial_prompt(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "done [TAIJI:STOP]")])
            codex = FakeAgent("Codex", [("end_turn", "start [TAIJI:STOP]")])
            result = await DuplexConversation((claude, codex)).run(
                "build it", first_agent=1
            )

            self.assertEqual(result.reason, "stop_token")
            self.assertIn("Human task:\nbuild it", codex.prompts[0])
            self.assertEqual(claude.prompts, [])

        asyncio.run(scenario())

    def test_safe_word_is_in_first_prompt_and_is_not_forwarded(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "finished [TAIJI:STOP]")])
            codex = FakeAgent("Codex", [("end_turn", "should not run")])
            result = await DuplexConversation((claude, codex)).run("finish it")

            self.assertEqual(result.reason, "stop_token")
            self.assertIn("safe word is [TAIJI:STOP]", claude.prompts[0])
            self.assertEqual(codex.prompts, [])

        asyncio.run(scenario())

    def test_relay_context_is_bounded(self) -> None:
        async def scenario() -> None:
            long_response = "A" * 20_000
            claude = FakeAgent("Claude", [("end_turn", long_response)])
            codex = FakeAgent("Codex", [("end_turn", "done [TAIJI:STOP]")])
            await DuplexConversation((claude, codex)).run("build it")

            self.assertLess(len(codex.prompts[0]), 13_000)
            self.assertIn("omitted the middle", codex.prompts[0])

        asyncio.run(scenario())

    def test_turn_start_callback_identifies_active_agent(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "review")])
            codex = FakeAgent("Codex", [("end_turn", "done [TAIJI:STOP]")])
            started: list[str] = []

            async def on_turn_start(round_number, agent) -> None:
                started.append(agent.get_info())

            await DuplexConversation(
                (claude, codex), on_turn_start=on_turn_start
            ).run("build it")
            self.assertEqual(started, ["Claude", "Codex"])

        asyncio.run(scenario())

    def test_direct_prompt_targets_tagged_agent_without_relaying_response(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "private answer")])
            codex = FakeAgent("Codex", [("end_turn", "done [TAIJI:STOP]")])
            relay = DuplexConversation((claude, codex))
            relay.enqueue_direct(1, "inspect this specific file")
            result = await relay.run("continue")

            self.assertIn("inspect this specific file", codex.prompts[0])
            self.assertEqual(claude.prompts, [])
            self.assertEqual(result.reason, "stop_token")

        asyncio.run(scenario())

    def test_pause_blocks_dispatch_until_resumed(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "done [TAIJI:STOP]")])
            codex = FakeAgent("Codex", [])
            relay = DuplexConversation((claude, codex))
            relay.pause()

            paused = await relay.run("build it")
            self.assertEqual(paused.reason, "paused")
            self.assertEqual(claude.prompts, [])

            relay.resume()
            finished = await relay.run("build it")
            self.assertEqual(finished.reason, "stop_token")
            self.assertEqual(len(claude.prompts), 1)

        asyncio.run(scenario())

    def test_human_follow_up_goes_to_next_agent(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", [("end_turn", "reviewed")])
            codex = FakeAgent("Codex", [("end_turn", "verified [TAIJI:STOP]")])
            relay: DuplexConversation

            async def inject_follow_up(round_number, agent, response) -> None:
                if round_number == 1:
                    relay.enqueue_human("please focus on the failing test")

            relay = DuplexConversation(
                (claude, codex), max_rounds=4, on_turn=inject_follow_up
            )
            result = await relay.run("build it")

            self.assertEqual(result.reason, "stop_token")
            self.assertIn("please focus on the failing test", codex.prompts[0])
            self.assertEqual(len(codex.prompts), 1)

        asyncio.run(scenario())

    def test_human_message_waits_for_current_agent_output(self) -> None:
        async def scenario() -> None:
            started = asyncio.Event()
            release = asyncio.Event()

            class WaitingAgent(FakeAgent):
                async def send_prompt(self, prompt: str) -> str:
                    self.prompts.append(prompt)
                    started.set()
                    await release.wait()
                    self.last_response = "current output"
                    return "end_turn"

            claude = WaitingAgent("Claude", [])
            codex = FakeAgent("Codex", [("end_turn", "done [TAIJI:STOP]")])
            relay = DuplexConversation((claude, codex))
            task = asyncio.create_task(relay.run("build it"))
            await started.wait()
            relay.enqueue_human("please check the failing test")
            self.assertEqual(codex.prompts, [])
            release.set()
            await task
            self.assertIn("please check the failing test", codex.prompts[0])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
