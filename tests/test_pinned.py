import asyncio
import unittest

from codeswarm import jsonrpc
from codeswarm.acp.pinned import PinnedConversation


class FakeAgent:
    def __init__(self, name: str, responses: list[str]) -> None:
        self.name = name
        self.responses = iter(responses)
        self.prompts: list[str] = []
        self.last_response = ""

    async def send_prompt(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.last_response = next(self.responses)
        return "end_turn"

    def get_info(self) -> str:
        return self.name


class PinnedConversationTests(unittest.TestCase):
    def test_transport_loss_preserves_first_turn_for_nonresumable_reload(self) -> None:
        async def scenario() -> None:
            class DisconnectedAgent(FakeAgent):
                async def send_prompt(self, prompt: str) -> str:
                    self.prompts.append(prompt)
                    raise jsonrpc.TransportClosed("stdout closed")

            conversation = PinnedConversation(
                (DisconnectedAgent("Claude", []), FakeAgent("Codex", []))
            )

            with self.assertRaises(jsonrpc.TransportClosed):
                await conversation.run("Original task")

            self.assertEqual(conversation.context.shared_task, "Original task")

        asyncio.run(scenario())

    def test_busy_follow_up_is_drained_before_the_next_submitted_prompt(self) -> None:
        async def scenario() -> None:
            started = asyncio.Event()
            release = asyncio.Event()

            class BlockingAgent(FakeAgent):
                async def send_prompt(self, prompt: str) -> str:
                    self.prompts.append(prompt)
                    if len(self.prompts) == 1:
                        started.set()
                        await release.wait()
                    self.last_response = f"answer {len(self.prompts)}"
                    return "end_turn"

            agent = BlockingAgent("Claude", [])
            conversation = PinnedConversation((agent, FakeAgent("Codex", [])))
            first_turn = asyncio.create_task(conversation.run("first"))
            await started.wait()
            self.assertTrue(conversation.enqueue_human("second"))
            release.set()
            await first_turn

            await conversation.run("third")

            self.assertEqual(len(agent.prompts), 3)
            self.assertIn("Turn context:\nfirst", agent.prompts[0])
            self.assertIn("Turn context:\nsecond", agent.prompts[1])
            self.assertIn("Turn context:\nHuman follow-up:\nthird", agent.prompts[2])
            self.assertEqual(conversation.queued_prompt_count, 0)

        asyncio.run(scenario())

    def test_new_prompt_survives_when_an_older_queued_turn_aborts(self) -> None:
        async def scenario(stop: str | Exception) -> None:
            class AbortingAgent(FakeAgent):
                async def send_prompt(self, prompt: str) -> str:
                    self.prompts.append(prompt)
                    if len(self.prompts) == 1:
                        if isinstance(stop, Exception):
                            raise stop
                        return stop
                    self.last_response = "recovered"
                    return "end_turn"

            agent = AbortingAgent("Claude", [])
            conversation = PinnedConversation((agent, FakeAgent("Codex", [])))
            conversation.context.shared_task = "Original"
            self.assertTrue(conversation.enqueue_human("older one"))
            self.assertTrue(conversation.enqueue_human("older two"))

            if isinstance(stop, Exception):
                with self.assertRaises(type(stop)):
                    await conversation.run("new")
            else:
                result = await conversation.run("new")
                self.assertEqual(result.reason, stop)
            self.assertEqual(conversation.queued_prompt_count, 2)

            await conversation.run("", resume_queued=True)
            self.assertIn("older two", agent.prompts[1])
            self.assertIn("new", agent.prompts[2])
            self.assertEqual(conversation.queued_prompt_count, 0)

        for stop in (
            "max_tokens",
            "refusal",
            jsonrpc.TransportClosed("stdout closed"),
        ):
            with self.subTest(stop=stop):
                asyncio.run(scenario(stop))

    def test_run_sends_only_to_the_default_pinned_agent(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", ["first answer"])
            codex = FakeAgent("Codex", ["unused"])
            conversation = PinnedConversation((claude, codex))

            result = await conversation.run("inspect the project")

            self.assertEqual(result.reason, "turn_complete")
            self.assertEqual(claude.prompts.__len__(), 1)
            self.assertEqual(codex.prompts, [])
            self.assertEqual(conversation.pinned_agent_index, 0)

        asyncio.run(scenario())

    def test_repeated_runs_keep_the_same_pinned_agent(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", ["first", "second"])
            codex = FakeAgent("Codex", ["unused"])
            conversation = PinnedConversation((claude, codex))

            await conversation.run("first task")
            await conversation.run("follow up")

            self.assertEqual(len(claude.prompts), 2)
            self.assertEqual(codex.prompts, [])

        asyncio.run(scenario())

    def test_selecting_agent_changes_only_subsequent_runs(self) -> None:
        async def scenario() -> None:
            claude = FakeAgent("Claude", ["first"])
            codex = FakeAgent("Codex", ["second"])
            conversation = PinnedConversation((claude, codex))

            await conversation.run("first task")
            conversation.select_agent(1)
            await conversation.run("second task")

            self.assertEqual(len(claude.prompts), 1)
            self.assertEqual(len(codex.prompts), 1)
            self.assertIn("first", codex.prompts[0])

        asyncio.run(scenario())

    def test_invalid_selection_does_not_change_the_pin(self) -> None:
        conversation = PinnedConversation((FakeAgent("A", []), FakeAgent("B", [])))

        with self.assertRaises(ValueError):
            conversation.select_agent(4)

        self.assertEqual(conversation.pinned_agent_index, 0)
