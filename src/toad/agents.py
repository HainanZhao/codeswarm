from importlib.resources import files
import asyncio
import shlex
import shutil

from toad.agent_schema import Agent


class AgentReadError(Exception):
    """Problem reading the agents."""


async def read_agents() -> dict[str, Agent]:
    """Read agent information from data/agents

    Raises:
        AgentReadError: If the files could not be read.

    Returns:
        A mapping of identity on to Agent dict.
    """
    import tomllib

    def read_agents() -> list[Agent]:
        """Read agent information.

        Stored in data/agents

        Returns:
            List of agent dicts.
        """
        agents: list[Agent] = []
        try:
            for file in files("toad.data").joinpath("agents").iterdir():
                agent: Agent = tomllib.load(file.open("rb"))
                if agent.get("active", True):
                    agents.append(agent)

        except Exception as error:
            raise AgentReadError(f"Failed to read agents; {error}")

        return agents

    agents = await asyncio.to_thread(read_agents)
    agent_map = {agent["identity"]: agent for agent in agents}

    return agent_map


async def detect_preferred_agents() -> list[Agent]:
    """Find locally usable preferred ACP agents without blocking the UI.

    The first two available agents are returned in collaboration order. For
    npm-backed adapters such as Codex, detecting ``npx`` is sufficient because
    npx can install the adapter on first launch.
    """
    agents = await read_agents()
    preferred = ("claude", "codex", "gemini")
    preferred_agents = {
        name: agent
        for name in preferred
        for agent in agents.values()
        if agent["short_name"].lower() == name
    }

    def is_available(agent: Agent) -> bool:
        command = agent["run_command"].get("*")
        if command is None:
            return False
        try:
            executable = shlex.split(command)[0]
        except (IndexError, ValueError):
            return False
        return shutil.which(executable) is not None

    available = await asyncio.gather(
        *(
            asyncio.to_thread(is_available, preferred_agents[agent])
            for agent in preferred
            if agent in preferred_agents
        )
    )
    return [
        preferred_agents[agent]
        for agent, found in zip(
            (agent for agent in preferred if agent in preferred_agents), available
        )
        if found
    ][:2]
