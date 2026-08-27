import asyncio
import unittest

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

