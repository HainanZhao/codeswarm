from importlib.resources import files
import asyncio
import shlex
import shutil
from typing import Mapping

import codeswarm
from codeswarm.agent_schema import Agent


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
            for file in files("codeswarm.data").joinpath("agents").iterdir():
                with file.open("rb") as stream:
                    agent: Agent = tomllib.load(stream)
                if agent.get("active", True):
                    agents.append(agent)

        except Exception as error:
            raise AgentReadError(f"Failed to read agents; {error}")

        return agents

    agents = await asyncio.to_thread(read_agents)
    agent_map = {agent["identity"]: agent for agent in agents}

    return agent_map


async def resolve_agent(name: str) -> Agent | None:
    """Match a short name or identity to its agent data, case-insensitively.

    Args:
        name: A ``short_name`` (e.g. ``"claude"``) or full ``identity``.

    Returns:
        The matching `Agent`, or `None` if nothing matched.
    """
    name = name.lower()
    try:
        agents = await read_agents()
    except AgentReadError:
        return None

    for agent_data in agents.values():
        aliases = [alias.lower() for alias in agent_data.get("aliases", [])]
        if (
            agent_data["short_name"].lower() == name
            or agent_data["identity"].lower() == name
            or name in aliases
        ):
            name = agent_data["identity"]
            break

    return agents.get(name)


def is_agent_available(agent: Agent) -> bool:
    """Best-effort check for whether an agent's run command resolves on PATH.

    This is a heuristic, not proof of a working install: an npm-backed
    adapter such as Codex only needs ``npx`` on PATH, since npx can install
    the adapter on first launch.
    """
    command = codeswarm.get_os_matrix(agent.get("detect_command", agent["run_command"]))
    if command is None:
        return False
    try:
        executable = shlex.split(command)[0]
    except (IndexError, ValueError):
        return False
    return shutil.which(executable) is not None


async def available_identities(agents: list[Agent]) -> set[str]:
    """Identities of the given agents whose run command resolves on PATH.

    Runs the (blocking) PATH lookups off the event loop.
    """
    available = await asyncio.gather(
        *(asyncio.to_thread(is_agent_available, agent) for agent in agents)
    )
    return {
        agent["identity"] for agent, found in zip(agents, available) if found
    }


async def detect_preferred_agents(
    agents: Mapping[str, Agent] | None = None,
    installed_identities: set[str] | None = None,
) -> list[Agent]:
    """Find locally usable preferred ACP agents without blocking the UI.

    Available agents are returned in preference order, as candidates to
    pre-select on the landing screen — not as a roster to auto-start.

    Callers that have already read the catalog or probed executable
    availability may supply those results to avoid repeating filesystem and
    PATH work during startup.
    """
    if agents is None:
        agents = await read_agents()
    preferred = ("claude", "codex", "gemini", "antigravity")
    preferred_agents = {
        name: agent
        for name in preferred
        for agent in agents.values()
        if agent["short_name"].lower() == name
        or name in [alias.lower() for alias in agent.get("aliases", [])]
    }

    ordered = [preferred_agents[name] for name in preferred if name in preferred_agents]
    available = (
        installed_identities
        if installed_identities is not None
        else await available_identities(ordered)
    )
    return [agent for agent in ordered if agent["identity"] in available]
