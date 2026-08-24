from pathlib import Path
import re
import shlex

import click
from wingmen.app import WingmenApp
from wingmen.agent_schema import Agent


def check_directory(path: str) -> None:
    """Check a path is directory, or exit the app.

    Args:
        path: Path to check.
    """
    if not Path(path).resolve().is_dir():
        raise click.ClickException(f"Not a directory: {path}")


async def get_agent_data(launch_agent) -> Agent | None:
    from wingmen.agents import resolve_agent

    return await resolve_agent(launch_agent)


def custom_agent_name(command: str) -> str:
    """Return a stable display/identity stem for a custom ACP command."""
    try:
        executable = shlex.split(command)[0]
    except (IndexError, ValueError) as error:
        raise click.ClickException(f"Invalid ACP command: {error}") from error
    name = Path(executable).name or "agent"
    return re.sub(r"[^a-z0-9.-]+", "-", name.lower()).strip(".-") or "agent"


def _read_saved_roster_identities() -> list[str]:
    """Read the last-used agent roster's identities from the settings file.

    Read directly off disk rather than through `Settings`/`WingmenApp`: this
    runs before the app (and its settings lifecycle) exists.
    """
    import json

    from wingmen import paths

    settings_path = paths.get_config() / "wingmen.json"
    if not settings_path.exists():
        return []
    try:
        settings = json.loads(settings_path.read_text("utf-8"))
    except Exception:
        return []
    roster = settings.get("launcher", {}).get("roster", "")
    if not isinstance(roster, str):
        return []
    return [line.strip() for line in roster.splitlines() if line.strip()]


class DefaultCommandGroup(click.Group):
    def parse_args(self, ctx, args):
        if "--help" in args or "-h" in args:
            return super().parse_args(ctx, args)
        if "--version" in args or "-v" in args:
            return super().parse_args(ctx, args)
        # Check if first arg is a known subcommand
        if not args or args[0] not in self.commands:
            # If not a subcommand, prepend the default command name
            args.insert(0, "run")
        return super().parse_args(ctx, args)

    def format_usage(self, ctx, formatter):
        formatter.write_usage(ctx.command_path, "[OPTIONS] PATH OR COMMAND [ARGS]...")


@click.group(
    cls=DefaultCommandGroup,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option("-v", "--version", is_flag=True, help="Show version and exit.")
@click.pass_context
def main(ctx, version):
    """Wingmen — AI for your terminal."""
    if version:
        from wingmen import get_version

        click.echo(get_version())
        ctx.exit()
    # If no command and no version flag, let the default command handling proceed
    if ctx.invoked_subcommand is None and not version:
        pass


@main.command("run")
@click.argument("project_dir", metavar="PATH", required=False, default=".")
@click.option(
    "-a",
    "--agent",
    "agents",
    metavar="AGENT",
    multiple=True,
    help="ACP agent to run (short name or identity). Repeat for a "
    "multi-agent relay, e.g. -a claude -a codex.",
)
@click.option(
    "--first-agent",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
    help="Which configured agent (1-based) receives the initial prompt.",
)
@click.option(
    "--max-rounds",
    type=click.IntRange(min=1),
    default=100,
    show_default=True,
    help="Maximum automated relay turns before stopping.",
)
def run(
    project_dir: str = ".",
    agents: tuple[str, ...] = (),
    first_agent: int = 1,
    max_rounds: int = 100,
):
    """Run an installed agent."""

    check_directory(project_dir)
    setup_prompt = False

    if agents:
        import asyncio

        names = list(agents)

        async def resolve_all() -> list[Agent | None]:
            return [await get_agent_data(name) for name in names]

        resolved_or_none = asyncio.run(resolve_all())
        for name, data in zip(names, resolved_or_none):
            if data is None:
                raise click.ClickException(f"Agent not found: {name}")
        resolved: list[Agent] = resolved_or_none  # type: ignore[assignment]
        if not 1 <= first_agent <= len(resolved):
            raise click.ClickException(
                f"--first-agent {first_agent} but only {len(resolved)} "
                "agent(s) configured"
            )
        agent_data = resolved[0]
        peers = resolved[1:]
        first_agent_index = first_agent - 1
    else:
        import asyncio

        from wingmen import paths

        setup_prompt = not (paths.get_config() / "wingmen.json").exists()

        async def resolve_saved_roster() -> list[Agent]:
            identities = _read_saved_roster_identities()
            resolved = [await get_agent_data(identity) for identity in identities]
            return [data for data in resolved if data is not None]

        saved_roster = asyncio.run(resolve_saved_roster())
        if saved_roster:
            if not 1 <= first_agent <= len(saved_roster):
                raise click.ClickException(
                    f"--first-agent {first_agent} but the saved roster has "
                    f"only {len(saved_roster)} agent(s)"
                )
            agent_data = saved_roster[0]
            peers = saved_roster[1:]
            first_agent_index = first_agent - 1
        else:
            # No usable saved roster: land on the store to pick one, rather
            # than silently auto-starting whatever detection happens to find.
            agent_data = None
            peers = []
            first_agent_index = 0

    app = WingmenApp(
        mode=None if agent_data else "store",
        agent_data=agent_data,
        peers=peers,
        first_agent=first_agent_index,
        setup_prompt=(setup_prompt and agent_data is None),
        max_rounds=max_rounds,
        project_dir=project_dir,
    )
    app.run()


@main.command("acp")
@click.argument("command", metavar="COMMAND")
@click.argument("project_dir", metavar="PATH", default=None)
@click.option(
    "-t",
    "--title",
    metavar="TITLE",
    help="Optional title to display in the status bar",
    default=None,
)
@click.option(
    "-d",
    "--project-dir",
    "project_dir_option",
    metavar="PATH",
    default=None,
    help="Project directory (overrides the PATH argument).",
)
def acp(
    command: str,
    title: str | None,
    project_dir: str | None,
    project_dir_option: str | None,
) -> None:
    """Run an ACP agent from a command."""

    project_dir = project_dir_option or project_dir
    if project_dir is not None:
        check_directory(project_dir)

    from wingmen.agent_schema import Agent as AgentData

    command_name = custom_agent_name(command)
    identity = f"{command_name}.custom.batrachian.ai"

    agent_data: AgentData = {
        "identity": identity,
        "name": title or command_name,
        "short_name": "agent",
        "url": "https://github.com/batrachianai/wingmen",
        "protocol": "acp",
        "type": "coding",
        "author_name": "Will McGugan",
        "author_url": "https://willmcgugan.github.io/",
        "publisher_name": "Will McGugan",
        "publisher_url": "https://willmcgugan.github.io/",
        "description": "Agent launched from CLI",
        "tags": [],
        "help": "",
        "run_command": {"*": command},
        "actions": {},
    }
    app = WingmenApp(agent_data=agent_data, project_dir=project_dir)
    app.run()


if __name__ == "__main__":
    main()
