import sys

import click
from toad.app import ToadApp
from toad.agent_schema import Agent


def set_process_title(title: str) -> None:
    """Set the process title.

    Args:
        title: Desired title.
    """
    try:
        import setproctitle

        setproctitle.setproctitle(title)
    except Exception:
        pass


def check_directory(path: str) -> None:
    """Check a path is directory, or exit the app.

    Args:
        path: Path to check.
    """
    from pathlib import Path

    if not Path(path).resolve().is_dir():
        print(f"Not a directory: {path}")
        sys.exit(-1)


async def get_agent_data(launch_agent) -> Agent | None:
    launch_agent = launch_agent.lower()

    from toad.agents import read_agents, AgentReadError

    try:
        agents = await read_agents()
    except AgentReadError:
        agents = {}

    for agent_data in agents.values():
        if (
            agent_data["short_name"].lower() == launch_agent
            or agent_data["identity"].lower() == launch_agent
        ):
            launch_agent = agent_data["identity"]
            break

    return agents.get(launch_agent)


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


@click.group(cls=DefaultCommandGroup, invoke_without_command=True)
@click.option("-v", "--version", is_flag=True, help="Show version and exit.")
@click.pass_context
def main(ctx, version):
    """Taiji — AI for your terminal."""
    if version:
        from toad import get_version

        click.echo(get_version())
        ctx.exit()
    # If no command and no version flag, let the default command handling proceed
    if ctx.invoked_subcommand is None and not version:
        pass


# @click.group(invoke_without_command=True)
# @click.pass_context
@main.command("run")
@click.argument("project_dir", metavar="PATH", required=False, default=".")
@click.option("-a", "--agent", metavar="AGENT", default="")
@click.option("--agent2", metavar="AGENT", default="", help="Second ACP agent for an automated relay.")
@click.option(
    "--first-agent",
    type=click.Choice(["1", "2"]),
    default="1",
    show_default=True,
    help="Which configured agent receives the initial prompt.",
)
@click.option(
    "--max-rounds",
    type=click.IntRange(min=1),
    default=100,
    show_default=True,
    help="Maximum automated relay turns before stopping.",
)
@click.option(
    "-p",
    "--port",
    metavar="PORT",
    default=8000,
    type=int,
    help="Port to use in conjunction with --serve",
)
@click.option(
    "-H",
    "--host",
    metavar="HOST",
    default="localhost",
    type=str,
    help="Host to use in conjunction with --serve",
)
@click.option(
    "--public-url",
    metavar="URL",
    default=None,
    help="Public URL to use in conjunction with --serve",
)
@click.option("-s", "--serve", is_flag=True, help="Serve Taiji as a web application")
def run(
    port: int,
    host: str,
    serve: bool,
    project_dir: str = ".",
    agent: str = "1",
    agent2: str = "",
    first_agent: str = "1",
    max_rounds: int = 100,
    public_url: str | None = None,
):
    """Run an installed agent."""

    check_directory(project_dir)
    setup_prompt = False

    if agent:
        import asyncio

        agent_data = asyncio.run(get_agent_data(agent))
        second_agent_data = asyncio.run(get_agent_data(agent2)) if agent2 else None
        if agent2 and second_agent_data is None:
            raise click.ClickException(f"Agent not found: {agent2}")
        first_agent_index = int(first_agent) - 1
        if first_agent_index == 1 and second_agent_data is None:
            raise click.ClickException("--first-agent 2 requires --agent2")
    else:
        import asyncio

        from toad import paths
        from toad.agents import AgentReadError, detect_preferred_agents

        setup_prompt = not (paths.get_config() / "taiji.json").exists()
        try:
            detected = asyncio.run(detect_preferred_agents())
        except AgentReadError:
            detected = []
        agent_data = detected[0] if detected else None
        second_agent_data = detected[1] if len(detected) > 1 else None
        first_agent_index = 0

    app = ToadApp(
        mode=None if agent_data else "store",
        agent_data=agent_data,
        second_agent_data=second_agent_data,
        first_agent=first_agent_index,
        setup_prompt=(setup_prompt and agent_data is None),
        max_rounds=max_rounds,
        project_dir=project_dir,
    )
    if serve:
        import shlex
        from textual_serve.server import Server

        command_args = sys.argv
        # Remove serve flag from args (could be either --serve or -s)
        for flag in ["--serve", "-s"]:
            try:
                command_args.remove(flag)
                break
            except ValueError:
                pass
        serve_command = shlex.join(command_args)
        server = Server(
            serve_command,
            host=host,
            port=port,
            title=serve_command,
            public_url=public_url,
        )
        set_process_title("taiji --serve")
        server.serve()
    else:
        app.run()
    app.run_on_exit()


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
@click.option("-d", "--project-dir", metavar="PATH", default=None)
@click.option(
    "-p",
    "--port",
    metavar="PORT",
    default=8000,
    type=int,
    help="Port to use in conjunction with --serve",
)
@click.option(
    "-H",
    "--host",
    metavar="HOST",
    default="localhost",
    help="Host to use in conjunction with --serve",
)
@click.option("-s", "--serve", is_flag=True, help="Serve Taiji as a web application")
def acp(
    command: str,
    host: str,
    port: int,
    title: str | None,
    project_dir: str | None,
    serve: bool = False,
) -> None:
    """Run an ACP agent from a command."""

    from rich import print

    from toad.agent_schema import Agent as AgentData

    command_name = command.split(" ", 1)[0].lower()
    identity = f"{command_name}.custom.batrachian.ai"

    agent_data: AgentData = {
        "identity": identity,
        "name": title or command.partition(" ")[0],
        "short_name": "agent",
        "url": "https://github.com/batrachianai/toad",
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
    if serve:
        import shlex
        from textual_serve.server import Server

        command_components = [sys.argv[0], "acp", command]
        if project_dir:
            command_components.append(f"--project-dir={project_dir}")
        serve_command = shlex.join(command_components)

        server = Server(
            serve_command,
            host=host,
            port=port,
            title=serve_command,
        )
        set_process_title("taiji acp --serve")
        server.serve()

    else:
        app = ToadApp(agent_data=agent_data, project_dir=project_dir)
        app.run()
        app.run_on_exit()

    print("")
    print("[bold magenta]Thanks for trying out Taiji!")
    print("Please head to Discussions to share your experiences (good or bad).")
    print("https://github.com/batrachianai/toad/discussions")


@main.command("settings")
def settings() -> None:
    """Settings information."""
    app = ToadApp()
    print(f"{app.settings_path}")


@main.command("replay")
@click.argument("path", metavar="FILE")
def replay(path: str) -> None:
    """Replay interaction from a log file.

    This is a debugging aid. You probably won't need it unless you are building an agent.

    Run it in place of a command line to run an ACP agent:

    taiji acp "taiji replay taiji.log"

    This will replay the agents output, and Taiji will update the conversation as it would a real agent.
    """
    import time

    stdout = sys.stdout.buffer
    with open(path, "rb") as replay_file:
        for line in replay_file.readlines():
            sender, space, json_line = line.partition(b" ")
            if sender == b"[agent]":
                stdout.write(json_line.strip() + b"\n")
            time.sleep(0.01)
            stdout.write(line)
            stdout.flush()


@main.command("serve")
@click.option("-p", "--port", metavar="PORT", default=8000, type=int)
@click.option("-H", "--host", metavar="HOST", default="localhost")
@click.option(
    "--public-url",
    metavar="URL",
    default=None,
    help="Public URL for textual_serve Server (e.g. https://example.com)",
)
def serve(port: int, host: str, public_url: str | None = None) -> None:
    """Serve Taiji as a web application."""
    from textual_serve.server import Server

    server = Server(
        sys.argv[0], host=host, port=port, title="Taiji", public_url=public_url
    )
    set_process_title("taiji serve")
    server.serve()


@main.command("about")
def about() -> None:
    """Show about information."""

    from toad import about

    app = ToadApp()

    print(about.render(app))


if __name__ == "__main__":
    main()
