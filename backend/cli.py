"""Click CLI entry point."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from backend.config import Settings, resolve_cli
from backend.model_catalog import render_available_models
from backend.models import DEFAULT_MODELS

console = Console()


def _option_value(args: list[str], name: str) -> str | None:
    """Return a CLI option value without consuming or otherwise parsing args."""
    for index, arg in enumerate(args):
        if arg.startswith(f"{name}="):
            return arg.split("=", 1)[1]
        if arg == name and index + 1 < len(args):
            return args[index + 1]
    return None


class ModelsCommand(click.Command):
    """Allow bare ``--models`` to act as a model-discovery command."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        for index, arg in enumerate(args):
            bare_models = arg == "--models" and (
                index + 1 == len(args) or args[index + 1].startswith("-")
            )
            if bare_models or arg == "--models=":
                settings = Settings()
                claude_cli = _option_value(args, "--claude-cli")
                codex_cli = _option_value(args, "--codex-cli")
                grok_cli = _option_value(args, "--grok-cli")
                omp_cli = _option_value(args, "--omp-cli")
                if claude_cli:
                    settings.claude_cli = claude_cli
                if codex_cli:
                    settings.codex_cli = codex_cli
                if grok_cli:
                    settings.grok_cli = grok_cli
                if omp_cli:
                    settings.omp_cli = omp_cli
                click.echo(render_available_models(settings))
                ctx.exit()
        return super().parse_args(ctx, args)


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiodocker").setLevel(logging.WARNING)
    # e.g. "Skipping unknown message type: tool_progress" for every running command
    logging.getLogger("claude_agent_sdk").setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s", datefmt="%X"))
    logging.basicConfig(level=level, handlers=[handler], force=True)


@click.command(cls=ModelsCommand)
@click.option("--ctfd-url", default=None, help="CTFd URL (overrides .env)")
@click.option("--ctfd-token", default=None, help="CTFd API token (overrides .env)")
@click.option("--image", default="ctf-sandbox", help="Docker sandbox image name")
@click.option(
    "--models",
    multiple=True,
    help="Model spec (repeat to race several); omit the value to list available models",
)
@click.option("--challenge", default=None, help="Solve a single challenge directory")
@click.option("--description", default=None, help="Challenge statement (overrides metadata.yml / README)")
@click.option("--connection-info", default=None, help="Service to attack, e.g. 'nc host 1337' (overrides metadata.yml)")
@click.option("--challenges-dir", default="challenges", help="Directory for challenge files")
@click.option("--no-submit", is_flag=True, help="Dry run — don't submit flags")
@click.option(
    "--writeup",
    is_flag=True,
    help="Ask the winning solver for writeup.md before cleaning up its sandbox",
)
@click.option(
    "--benchmark",
    is_flag=True,
    help="Keep all models running for up to 10 minutes after the first solve (requires --challenge)",
)
@click.option("--coordinator-model", default=None, help="Model for coordinator (default: claude-opus-4-6)")
@click.option("--coordinator", default="claude", type=click.Choice(["claude", "codex"]), help="Coordinator backend")
@click.option("--max-challenges", default=10, type=int, help="Max challenges solved concurrently")
@click.option("--msg-port", default=0, type=int, help="Operator message port (0 = auto)")
@click.option("--claude-cli", default=None, help="Claude Code command or path (overrides CLAUDE_CLI, default: claude)")
@click.option("--codex-cli", default=None, help="Codex command or path (overrides CODEX_CLI, default: codex)")
@click.option("--grok-cli", default=None, help="Grok Build command or path (overrides GROK_CLI, default: grok)")
@click.option("--omp-cli", default=None, help="oh-my-pi command or path (overrides OMP_CLI, default: omp)")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging")
def main(
    ctfd_url: str | None,
    ctfd_token: str | None,
    image: str,
    models: tuple[str, ...],
    challenge: str | None,
    description: str | None,
    connection_info: str | None,
    challenges_dir: str,
    no_submit: bool,
    writeup: bool,
    benchmark: bool,
    coordinator_model: str | None,
    coordinator: str,
    max_challenges: int,
    msg_port: int,
    claude_cli: str | None,
    codex_cli: str | None,
    grok_cli: str | None,
    omp_cli: str | None,
    verbose: bool,
) -> None:
    """CTF Agent — multi-model solver swarm.

    Run without --challenge to start the full coordinator (Ctrl+C to stop).
    """
    _setup_logging(verbose)

    settings = Settings(sandbox_image=image)
    if ctfd_url:
        settings.ctfd_url = ctfd_url
    if ctfd_token:
        settings.ctfd_token = ctfd_token
    if claude_cli:
        settings.claude_cli = claude_cli
    if codex_cli:
        settings.codex_cli = codex_cli
    if grok_cli:
        settings.grok_cli = grok_cli
    if omp_cli:
        settings.omp_cli = omp_cli
    settings.max_concurrent_challenges = max_challenges

    model_specs = list(models) if models else list(DEFAULT_MODELS)
    if benchmark and not challenge:
        raise click.UsageError("--benchmark requires --challenge")
    if benchmark and writeup:
        raise click.UsageError("--writeup cannot be combined with --benchmark")

    # Resolve the CLIs this run needs now, so a wrong command fails here with a clear message
    providers = {spec.split("/", 1)[0] for spec in model_specs}
    if not challenge:
        providers.add("claude-sdk" if coordinator == "claude" else "codex")
    cli_paths: dict[str, str] = {}
    for provider, setting, command in (
        ("claude-sdk", "CLAUDE_CLI", settings.claude_cli),
        ("codex", "CODEX_CLI", settings.codex_cli),
        ("grok", "GROK_CLI", settings.grok_cli),
        ("omp", "OMP_CLI", settings.omp_cli),
    ):
        if provider in providers:
            try:
                cli_paths[setting] = resolve_cli(command, setting)
            except RuntimeError as e:
                console.print(f"[red]{e}[/red]")
                sys.exit(1)

    console.print("[bold]CTF Agent v2[/bold]")
    console.print(f"  CTFd: {settings.ctfd_url}")
    console.print(f"  Models: {', '.join(model_specs)}")
    for setting, path in cli_paths.items():
        console.print(f"  {setting}: {path}")
    console.print(f"  Image: {settings.sandbox_image}")
    console.print(f"  Max challenges: {max_challenges}")
    if benchmark:
        console.print("  Benchmark: 10-minute grace period after first solve")
    if writeup:
        console.print("  Winner writeup: enabled")
    console.print()

    if challenge:
        asyncio.run(
            _run_single(
                settings,
                challenge,
                model_specs,
                no_submit,
                max_challenges,
                msg_port,
                benchmark,
                writeup,
                description=description,
                connection_info=connection_info,
            )
        )
    else:
        asyncio.run(
            _run_coordinator(
                settings,
                model_specs,
                challenges_dir,
                no_submit,
                coordinator_model,
                coordinator,
                max_challenges,
                msg_port,
                writeup,
            )
        )


async def _run_single(
    settings: Settings,
    challenge_dir: str,
    model_specs: list[str],
    no_submit: bool,
    max_challenges: int,
    msg_port: int = 0,
    benchmark: bool = False,
    writeup: bool = False,
    description: str | None = None,
    connection_info: str | None = None,
) -> None:
    """Run a single challenge with a swarm."""
    from backend.agents.swarm import ChallengeSwarm
    from backend.cost_tracker import CostTracker
    from backend.ctfd import CTFdClient
    from backend.prompts import ChallengeMeta, list_distfiles
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore

    max_containers = max_challenges * len(model_specs)
    configure_semaphore(max_containers)
    await cleanup_orphan_containers()

    challenge_path = Path(challenge_dir)
    if not challenge_path.is_dir():
        console.print(f"[red]Challenge directory not found: {challenge_dir}[/red]")
        sys.exit(1)

    meta = ChallengeMeta.load_or_create(challenge_path)
    if description:
        meta.description = description
    if connection_info:
        meta.connection_info = connection_info
    console.print(f"[bold]Challenge:[/bold] {meta.name} ({meta.category or 'no category'}, {meta.value} pts)")
    if not (challenge_path / "distfiles").exists():
        files = list_distfiles(str(challenge_path))
        console.print(f"  No distfiles/: {len(files)} file(s) handed out from {challenge_path}")

    ctfd = CTFdClient(
        base_url=settings.ctfd_url,
        token=settings.ctfd_token,
        username=settings.ctfd_user,
        password=settings.ctfd_pass,
    )
    cost_tracker = CostTracker()
    winner_announced = False

    def _announce_winner(result) -> None:
        nonlocal winner_announced
        console.print(f"\n[bold green]FLAG FOUND:[/bold green] {result.flag}")
        winner_announced = True

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_path),
        meta=meta,
        ctfd=ctfd,
        cost_tracker=cost_tracker,
        settings=settings,
        model_specs=model_specs,
        no_submit=no_submit,
        benchmark=benchmark,
        writeup=writeup,
        on_winner=_announce_winner,
    )

    # Operator messages: `ctf-msg --port N "hint"` reaches every solver within ~5 tool calls
    from backend.agents.coordinator_loop import _start_msg_server
    inbox: asyncio.Queue = asyncio.Queue()
    msg_server = await _start_msg_server(inbox, msg_port)
    if msg_server:
        port = msg_server.sockets[0].getsockname()[1]
        console.print(f"[bold]Message the solvers:[/bold] ctf-msg --port {port} \"your hint\"\n")

    async def _relay_operator_messages() -> None:
        while True:
            message = await inbox.get()
            await swarm.message_bus.broadcast(message, source="operator")
            console.print(f"[cyan]Operator message queued for solvers:[/cyan] {message[:200]}")

    relay_task = asyncio.create_task(_relay_operator_messages())

    try:
        result = await swarm.run()
        from backend.solver_base import FLAG_FOUND
        if result and result.status == FLAG_FOUND:
            if not winner_announced:
                console.print(f"\n[bold green]FLAG FOUND:[/bold green] {result.flag}")
        else:
            console.print("\n[bold red]No flag found.[/bold red]")

        console.print("\n[bold]Cost Summary:[/bold]")
        for agent_name in cost_tracker.by_agent:
            console.print(f"  {agent_name}: {cost_tracker.format_usage(agent_name)}")
        console.print(f"  [bold]Total: ${cost_tracker.total_cost_usd:.2f}[/bold]")
        if benchmark:
            _print_benchmark_summary(swarm)
        if swarm.writeup_path:
            console.print(f"\n[bold green]Writeup:[/bold green] {swarm.writeup_path}")
        elif writeup and result and result.status == FLAG_FOUND:
            console.print("\n[bold red]Writeup generation failed; check the winner trace.[/bold red]")
    finally:
        relay_task.cancel()
        if msg_server:
            msg_server.close()
        await ctfd.close()


def _format_benchmark_time(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    minutes, remaining = divmod(seconds, 60)
    if minutes >= 1:
        return f"{int(minutes)}m {remaining:04.1f}s"
    return f"{remaining:.1f}s"


def _print_benchmark_summary(swarm) -> None:
    """Render solve order, wall-clock time, and safety-filter outcomes."""
    from backend.agents.swarm import BENCHMARK_TIMEOUT
    from backend.solver_base import ERROR, FLAG_FOUND, GAVE_UP, POLICY_BLOCKED, QUOTA_ERROR

    labels = {
        FLAG_FOUND: "Solved",
        BENCHMARK_TIMEOUT: "Timed out",
        POLICY_BLOCKED: "Safety blocked",
        QUOTA_ERROR: "Quota error",
        GAVE_UP: "Gave up",
        ERROR: "Error",
        "cancelled": "Cancelled",
        "running": "Running",
    }
    records = sorted(
        swarm.benchmark_results.values(),
        key=lambda record: (
            record.order is None,
            record.order if record.order is not None else 0,
            record.model_spec,
        ),
    )
    table = Table(title="Benchmark Results")
    table.add_column("Order", justify="right")
    table.add_column("Model")
    table.add_column("Result")
    table.add_column("Solve time", justify="right")
    table.add_column("Safety guard", justify="center")
    for record in records:
        table.add_row(
            str(record.order) if record.order is not None else "—",
            record.model_spec,
            labels.get(record.status, record.status.replace("_", " ").title()),
            _format_benchmark_time(record.solve_seconds),
            "[red]Yes[/red]" if record.safety_flagged else "No",
        )
    console.print()
    console.print(table)


async def _run_coordinator(
    settings: Settings,
    model_specs: list[str],
    challenges_dir: str,
    no_submit: bool,
    coordinator_model: str | None,
    coordinator_backend: str,
    max_challenges: int,
    msg_port: int = 0,
    writeup: bool = False,
) -> None:
    """Run the full coordinator (continuous until Ctrl+C)."""
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore

    max_containers = max_challenges * len(model_specs)
    configure_semaphore(max_containers)
    await cleanup_orphan_containers()
    console.print(f"[bold]Starting coordinator ({coordinator_backend}, Ctrl+C to stop)...[/bold]\n")

    if coordinator_backend == "codex":
        from backend.agents.codex_coordinator import run_codex_coordinator
        results = await run_codex_coordinator(
            settings=settings,
            model_specs=model_specs,
            challenges_root=challenges_dir,
            no_submit=no_submit,
            coordinator_model=coordinator_model,
            msg_port=msg_port,
            writeup=writeup,
        )
    else:
        from backend.agents.claude_coordinator import run_claude_coordinator
        results = await run_claude_coordinator(
            settings=settings,
            model_specs=model_specs,
            challenges_root=challenges_dir,
            no_submit=no_submit,
            coordinator_model=coordinator_model,
            msg_port=msg_port,
            writeup=writeup,
        )

    console.print("\n[bold]Final Results:[/bold]")
    for challenge, data in results.get("results", {}).items():
        console.print(f"  {challenge}: {data.get('flag', 'no flag')}")
    console.print(f"\n[bold]Total cost: ${results.get('total_cost_usd', 0):.2f}[/bold]")


@click.command()
@click.argument("message")
@click.option("--port", default=9400, type=int, help="Coordinator message port")
@click.option("--host", default="127.0.0.1", help="Coordinator host")
def msg(message: str, port: int, host: str) -> None:
    """Send a message to the running coordinator."""
    import json
    import urllib.request

    body = json.dumps({"message": message}).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/msg",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            console.print(f"[green]Sent:[/green] {data.get('queued', message[:200])}")
    except Exception as e:
        console.print(f"[red]Failed:[/red] {e}")
        console.print("Is the coordinator running?")
        sys.exit(1)


if __name__ == "__main__":
    main()
