"""ctf-watch — browse and follow solver sessions in the terminal, from their traces.

Every solver appends JSONL events to ./logs/trace-<challenge>-<agent>-<time>.jsonl and
flushes each line, so this works live (next to a running ctf-solve) and afterwards.

  ctf-watch                    interactive browser (TUI): challenges > runs > agents
  ctf-watch --plain            live dashboard without interaction
  ctf-watch <chall>            all agents of the latest run, interleaved, live
  ctf-watch <chall> <agent>    one agent's session as a terminal (agent: claude, grok, omp...)
"""

from __future__ import annotations

import ast
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)

_TS_RE = re.compile(r"-(\d{8}-\d{6})\.jsonl$")
_COLORS = ["cyan", "magenta", "green", "yellow", "bright_blue", "bright_red", "bright_cyan", "bright_magenta"]
_RUN_GAP_S = 60       # solvers of one run start within this many seconds
_ACTIVE_S = 120       # a trace written this recently counts as running
_BASH_TOOLS = {"bash", "Bash", "ctf__bash"}
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _sanitize(name: str) -> str:
    return name.replace("/", "_").replace(" ", "_")


# ── trace model ───────────────────────────────────────────────────────────────


@dataclass
class Trace:
    path: Path
    challenge: str
    agent: str
    started: float
    color: str = "white"
    events: list[dict] = field(default_factory=list)
    offset: int = 0
    _partial: str = ""

    def read_new(self) -> list[dict]:
        """Append and return the events written since the last read."""
        try:
            with self.path.open("r", errors="replace") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
                self.offset = fh.tell()
        except OSError:
            return []
        lines = (self._partial + chunk).split("\n")
        self._partial = lines.pop()
        new = []
        for line in lines:
            try:
                new.append(json.loads(line))
            except ValueError:
                continue
        self.events.extend(new)
        return new

    @property
    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def summary(self) -> dict:
        steps, cost, flag, status, session, model, last_cmd = 0, 0.0, None, None, None, None, ""
        for e in self.events:
            kind = e.get("type")
            if kind == "tool_call":
                steps = max(steps, e.get("step") or 0)
                last_cmd = _command_text(e) or e.get("tool", "")
            elif kind in ("usage", "finish") and e.get("cost_usd") is not None:
                cost = e["cost_usd"]
            elif kind == "flag_confirmed":
                flag = e.get("flag")
            elif kind == "finish":
                status = e.get("status")
                flag = flag or (e.get("flag") if e.get("confirmed") else None)
            elif kind == "session":
                session = e.get("session_id") or e.get("thread_id")
            elif kind == "start":
                model = e.get("model")
            elif kind == "stop":
                status = status or "stopped"
        stopped = any(e.get("type") == "stop" for e in self.events)
        if flag:
            state = "flag"
        elif stopped:
            state = "stopped"
        elif time.time() - self.mtime < _ACTIVE_S:
            state = "running"
        else:
            state = "idle"  # no stop event and quiet: still thinking, or the run was killed
        return {"steps": steps, "cost": cost, "flag": flag, "status": status, "state": state,
                "session": session, "model": model, "last_cmd": last_cmd}


def _load_traces(logs: Path, challenge: str | None = None) -> list[Trace]:
    pattern = f"trace-{_sanitize(challenge)}-*.jsonl" if challenge else "trace-*.jsonl"
    traces = []
    for path in logs.glob(pattern):
        m = _TS_RE.search(path.name)
        if not m:
            continue
        started = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp()
        middle = _TS_RE.sub("", path.name).removeprefix("trace-")
        chall_name = None
        try:
            with path.open() as fh:
                first = json.loads(fh.readline() or "{}")
            if first.get("type") == "start":
                chall_name = first.get("challenge")
        except (OSError, ValueError):
            pass
        if chall_name and middle.startswith(_sanitize(chall_name) + "-"):
            agent = middle[len(_sanitize(chall_name)) + 1:]
        elif challenge:
            chall_name, agent = challenge, middle.removeprefix(_sanitize(challenge) + "-")
        else:
            chall_name, _, agent = middle.partition("-")
        if challenge and chall_name != challenge and _sanitize(chall_name) != _sanitize(challenge):
            continue
        traces.append(Trace(path, chall_name, agent or middle, started))
    traces.sort(key=lambda t: (t.challenge, t.started, t.agent))
    return traces


def _group_runs(traces: list[Trace]) -> dict[str, list[list[Trace]]]:
    """challenge -> runs (newest first), each run a list of traces started together."""
    by_chall: dict[str, list[Trace]] = {}
    for t in traces:
        by_chall.setdefault(t.challenge, []).append(t)
    out: dict[str, list[list[Trace]]] = {}
    for chall, items in by_chall.items():
        runs: list[list[Trace]] = []
        for t in sorted(items, key=lambda t: t.started):
            if runs and t.started - runs[-1][0].started <= _RUN_GAP_S:
                runs[-1].append(t)
            else:
                runs.append([t])
        out[chall] = runs[::-1]
    return out


# ── event rendering ───────────────────────────────────────────────────────────


def _args(event: dict):
    raw = event.get("args", "")
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        pass
    # The tracer cuts args at 2000 chars: recover the start of a long command
    prefix = '{"command": "'
    if isinstance(raw, str) and raw.startswith(prefix):
        body = raw[len(prefix):]
        for cut in range(0, 8):  # drop a dangling escape sequence at the cut
            try:
                return {"command": json.loads('"' + body[:len(body) - cut] + '"') + " …[cut]"}
            except ValueError:
                continue
    return raw


def _command_text(event: dict) -> str | None:
    args = _args(event)
    if event.get("tool") in _BASH_TOOLS and isinstance(args, dict):
        return str(args.get("command", ""))
    return None


def _result_text(raw: str) -> str:
    """Tool output as a terminal would show it.

    Claude's Bash results are logged as a Python dict repr ({'stdout': ..., 'stderr': ...}),
    possibly cut at 2000 chars; other backends log plain text.
    """
    raw = str(raw or "")
    if raw.startswith("{'stdout'"):
        try:
            data = ast.literal_eval(raw)
            out = str(data.get("stdout", ""))
            err = str(data.get("stderr", ""))
            return out + (("\n" if out and err else "") + err if err else "")
        except (ValueError, SyntaxError):
            body = raw[len("{'stdout': '"):]
            body = body.split("', 'stderr'")[0]
            try:
                return ast.literal_eval("'" + body.replace("'", "\\'") + "'")
            except (ValueError, SyntaxError):
                return body.replace("\\n", "\n").replace("\\t", "\t")
    return raw


def _clip_lines(text: str, max_lines: int) -> tuple[str, int]:
    lines = text.rstrip("\n").split("\n")
    if len(lines) <= max_lines:
        return "\n".join(lines), 0
    return "\n".join(lines[:max_lines]), len(lines) - max_lines


def _when(event: dict) -> str:
    return datetime.fromtimestamp(event.get("ts", time.time())).strftime("%H:%M:%S")


def _render_terminal(event: dict, max_lines: int, show_thoughts: bool = True) -> list:
    """One event in the 'agent terminal' view: prompt + command, raw output, thoughts."""
    kind = event.get("type")
    if kind == "tool_call":
        cmd = _command_text(event)
        prompt = Text.assemble(("root@sandbox", "bold green"), (":", "white"), ("/challenge", "bold blue"), ("# ", "white"))
        if cmd is not None:
            return [Text.assemble(prompt, (cmd, "bold white"))]
        args = _args(event)
        tool = event.get("tool", "?")
        if tool in ("submit_flag", "ctf__submit_flag") and isinstance(args, dict):
            return [Text.assemble(prompt, ("submit_flag ", "bold yellow"), (str(args.get("flag", "")), "bold yellow"))]
        shown = json.dumps(args, ensure_ascii=False) if isinstance(args, dict | list) else str(args)
        return [Text.assemble(("⚙ ", "bold cyan"), (tool, "bold cyan"), (" ", ""), (shown[:500], "cyan"))]
    if kind == "tool_result":
        text = _result_text(event.get("result", ""))
        if not text.strip():
            return []
        body, hidden = _clip_lines(text, max_lines)
        out = [Text.from_ansi(body)]
        if hidden:
            out.append(Text(f"… {hidden} more lines (--full to show everything the trace kept)", style="dim italic"))
        return out
    if kind == "model_response" and show_thoughts:
        text = str(event.get("text", "")).strip()
        if not text:
            return []
        return [Panel(Text(text), border_style="magenta", title="[magenta]agent[/magenta]", title_align="left",
                      subtitle=f"[dim]{_when(event)}[/dim]", subtitle_align="right", padding=(0, 1))]
    return _render_status(event)


def _render_status(event: dict) -> list:
    kind = event.get("type")
    w = _when(event)
    if kind == "flag_confirmed":
        dry = " (dry run)" if event.get("dry_run") else ""
        return [Panel(Text(f"🚩 {event.get('flag')}{dry}", style="bold green"), border_style="green", title="FLAG", expand=False)]
    if kind == "finish":
        status = event.get("status")
        style = "bold green" if status == "flag_found" else "yellow"
        return [Rule(f"[{style}]turn finished: {status} · ${event.get('cost_usd', 0)} · {w}[/{style}]", style="dim")]
    if kind in ("error", "turn_failed"):
        return [Text(f"✖ {event.get('error', '')}", style="bold red")]
    if kind == "bump":
        return [Text(f"↻ {w} relaunched with sibling insights: {str(event.get('insights', ''))[:300]}", style="yellow")]
    if kind == "findings_delivered":
        return [Text(f"✉ {w} findings from other agents: {str(event.get('text', ''))[:300]}", style="blue")]
    if kind == "loop_break":
        return [Text("⟲ loop detected, call blocked", style="red")]
    if kind == "start":
        return [Rule(f"[bold]session start · {event.get('model', '')} · {w}[/bold]", style="bright_black")]
    if kind == "session":
        return [Text(f"session {event.get('session_id') or event.get('thread_id')}", style="dim")]
    if kind == "stop":
        return [Rule(f"[dim]stopped after {event.get('step_count', '?')} steps · {w}[/dim]", style="bright_black")]
    return []


def _render_line(event: dict, max_lines: int) -> str | None:
    """Compact one-line rendering for the multi-agent view (rich markup)."""
    kind = event.get("type")
    if kind == "tool_call":
        cmd = _command_text(event)
        if cmd is not None:
            first, hidden = _clip_lines(cmd, 3)
            more = f" [dim]…+{hidden}[/dim]" if hidden else ""
            return f"[bold green]$[/bold green] [bold]{escape(first)}[/bold]{more}"
        args = _args(event)
        if event.get("tool") in ("submit_flag", "ctf__submit_flag") and isinstance(args, dict):
            return f"[bold yellow]submit_flag {escape(str(args.get('flag', '')))}[/bold yellow]"
        shown = json.dumps(args, ensure_ascii=False) if isinstance(args, dict | list) else str(args)
        return f"[cyan]⚙ {escape(str(event.get('tool')))}[/cyan] {escape(shown[:300])}"
    if kind == "tool_result":
        text = _ANSI_RE.sub("", _result_text(event.get("result", ""))).strip()
        if not text:
            return None
        body, hidden = _clip_lines(text, max_lines)
        more = f"\n  [dim]… {hidden} more lines[/dim]" if hidden else ""
        return "[bright_black]" + escape("\n".join("  │ " + line for line in body.split("\n"))) + "[/bright_black]" + more
    if kind == "model_response":
        text = str(event.get("text", "")).strip()
        return f"[magenta]💬 {escape(text[:800])}[/magenta]" if text else None
    if kind == "flag_confirmed":
        dry = " (dry run)" if event.get("dry_run") else ""
        return f"[bold green]🚩 FLAG {escape(str(event.get('flag')))}{dry}[/bold green]"
    if kind == "finish":
        style = "bold green" if event.get("status") == "flag_found" else "yellow"
        return f"[{style}]■ turn finished: {event.get('status')} (${event.get('cost_usd', 0)})[/{style}]"
    if kind in ("error", "turn_failed"):
        return f"[bold red]✖ {escape(str(event.get('error', '')))}[/bold red]"
    if kind == "bump":
        return "[yellow]↻ relaunched with sibling insights[/yellow]"
    if kind == "findings_delivered":
        return f"[blue]✉ findings from other agents: {escape(str(event.get('text', ''))[:200])}[/blue]"
    if kind == "loop_break":
        return "[red]⟲ loop detected, call blocked[/red]"
    if kind == "start":
        return f"[bold]▶ started ({escape(str(event.get('model', '')))})[/bold]"
    if kind == "stop":
        return f"[dim]■ stopped after {event.get('step_count', '?')} steps[/dim]"
    return None


def tagged_line(event: dict, trace: Trace, markup: str, width: int) -> Text:
    """'HH:MM:SS agent  <event>' with continuation lines indented under the event.

    The markup is parsed as a whole first: a style may span several lines.
    """
    body = Text.from_markup(markup).split("\n", allow_blank=True)
    name = short_agent(trace.agent).ljust(min(width, _TAG_WIDTH))
    out = Text.from_markup(f"[dim]{_when(event)}[/dim] [bold {trace.color}]{escape(name)}[/bold {trace.color}] ")
    out.append_text(body[0])
    for line in body[1:]:
        out.append("\n    ")
        out.append_text(line)
    return out


_TAG_WIDTH = 18


def short_agent(agent: str) -> str:
    """Agent label for the interleaved view: at most _TAG_WIDTH chars."""
    return agent if len(agent) <= _TAG_WIDTH else agent[: _TAG_WIDTH - 1] + "…"


# ── views ─────────────────────────────────────────────────────────────────────

_STATE_STYLE = {
    "flag": ("🚩 flag", "bold green"),
    "running": ("● running", "bold cyan"),
    "stopped": ("■ stopped", "bright_black"),
    "idle": ("◌ quiet", "yellow"),
}


def _dashboard(logs: Path, challenge: str | None, max_runs: int) -> Group:
    traces = _load_traces(logs, challenge)
    for t in traces:
        t.read_new()
    runs = _group_runs(traces)
    if not runs:
        return Group(Text(f"No traces in {logs.resolve()}" + (f" for '{challenge}'" if challenge else ""), style="yellow"))
    parts: list = []
    order = sorted(runs, key=lambda c: max(t.mtime for run in runs[c] for t in run), reverse=True)
    for chall in order:
        table = Table(box=None, expand=True, pad_edge=False, show_edge=False, header_style="bold dim")
        for col, kw in (("run", {"style": "dim", "no_wrap": True}), ("agent", {"no_wrap": True}),
                        ("state", {"no_wrap": True}), ("steps", {"justify": "right"}),
                        ("cost", {"justify": "right"}), ("last", {"style": "dim", "no_wrap": True}),
                        ("last command / flag", {"overflow": "ellipsis", "no_wrap": True, "ratio": 1})):
            table.add_column(col, **kw)
        for i, run in enumerate(runs[chall][:max_runs]):
            run_label = datetime.fromtimestamp(run[0].started).strftime("%m-%d %H:%M")
            for j, t in enumerate(run):
                s = t.summary()
                state, style = _STATE_STYLE[s["state"]]
                detail = (Text(f"🚩 {s['flag']}", style="bold green") if s["flag"]
                          else Text(_ANSI_RE.sub("", s["last_cmd"].split("\n")[0])[:200]))
                table.add_row(
                    f"#{i + 1} {run_label}" if j == 0 else "",
                    Text(t.agent, style="bold"),
                    Text(state, style=style),
                    str(s["steps"]),
                    f"${s['cost']:.2f}",
                    datetime.fromtimestamp(t.mtime).strftime("%H:%M:%S"),
                    detail,
                )
            if i < min(len(runs[chall]), max_runs) - 1:
                table.add_row("", "", "", "", "", "", "")
        more = len(runs[chall]) - max_runs
        subtitle = f"[dim]+{more} older run(s): --runs {len(runs[chall])}[/dim]" if more > 0 else None
        parts.append(Panel(table, title=f"[bold]{escape(chall)}[/bold]", title_align="left",
                           subtitle=subtitle, subtitle_align="right", border_style="bright_black"))
    parts.append(Text("ctf-watch <chall> · all agents live    ctf-watch <chall> <agent> · agent terminal", style="dim"))
    return Group(*parts)


def _pick_run(logs: Path, challenge: str, run_index: int) -> list[Trace]:
    runs = _group_runs(_load_traces(logs, challenge)).get(challenge)
    if not runs:
        # trace names are sanitized: accept the sanitized form too
        grouped = _group_runs(_load_traces(logs, challenge))
        runs = next(iter(grouped.values()), None)
    if not runs:
        raise click.ClickException(f"No traces for challenge '{challenge}' in {logs.resolve()} (see: ctf-watch)")
    if not 1 <= run_index <= len(runs):
        raise click.ClickException(f"'{challenge}' has {len(runs)} run(s); --run takes 1 (latest) to {len(runs)}")
    return runs[run_index - 1]


def _follow(traces: list[Trace], render, follow: bool, logs: Path, challenge: str, agent: str | None) -> None:
    """Print events of `traces` in time order; with follow, keep polling (and pick up new agents)."""
    known = {t.path for t in traces}
    try:
        while True:
            batch: list[tuple[float, Trace, dict]] = []
            for t in traces:
                batch.extend((e.get("ts", 0), t, e) for e in t.read_new())
            for _, t, e in sorted(batch, key=lambda x: x[0]):
                render(t, e)
            if not follow:
                return
            time.sleep(0.4)
            # agents that start later in the same run (e.g. after a quota fallback)
            newest = max((t.started for t in traces), default=0)
            for t in _load_traces(logs, challenge):
                if t.path not in known and t.started >= newest - _RUN_GAP_S and (not agent or agent in t.agent):
                    t.color = _COLORS[len(traces) % len(_COLORS)]
                    traces.append(t)
                    known.add(t.path)
    except KeyboardInterrupt:
        pass


@click.command()
@click.argument("challenge", required=False)
@click.argument("agent", required=False)
@click.option(
    "--logs", "log_dir", default="logs", show_default=True, envvar="CTF_LOGS_DIR", show_envvar=True,
    help="Trace directory (where ctf-solve runs)",
)
@click.option("--run", "run_index", default=1, show_default=True, help="Which run of the challenge: 1 = latest")
@click.option("--runs", "max_runs", default=3, show_default=True, help="Dashboard: runs shown per challenge")
@click.option("--full", is_flag=True, help="Show whole outputs (as much as the trace kept)")
@click.option("--lines", default=15, show_default=True, help="Output lines shown per command")
@click.option("--no-follow", is_flag=True, help="Print and exit instead of following live")
@click.option("--no-thoughts", is_flag=True, help="Agent view: hide the model's messages")
@click.option("--plain", is_flag=True, help="Without CHALLENGE: live dashboard instead of the interactive browser")
def main(challenge, agent, log_dir, run_index, max_runs, full, lines, no_follow, no_thoughts, plain) -> None:
    """Watch solver sessions: dashboard, a challenge's agents, or one agent's terminal.

    \b
      ctf-watch                  interactive browser: arrows to pick a run or an agent
      ctf-watch --plain          dashboard of challenges / runs / agents (live)
      ctf-watch baby-rsa         every agent of the latest run, live
      ctf-watch baby-rsa grok    grok's session as a terminal
      ctf-watch baby-rsa claude --run 2 --no-follow
    """
    logs = Path(log_dir)
    max_lines = 10_000 if full else lines

    if not challenge:
        if not plain and not no_follow and console.is_terminal:
            from backend.watch_tui import run_browser

            run_browser(logs)
            return
        if no_follow:
            console.print(_dashboard(logs, None, max_runs))
            return
        try:
            with Live(_dashboard(logs, None, max_runs), console=console, refresh_per_second=2, screen=False) as live:
                while True:
                    time.sleep(1)
                    live.update(_dashboard(logs, None, max_runs))
        except KeyboardInterrupt:
            pass
        return

    run = _pick_run(logs, challenge, run_index)
    for i, t in enumerate(run):
        t.color = _COLORS[i % len(_COLORS)]
    started = datetime.fromtimestamp(run[0].started).strftime("%Y-%m-%d %H:%M:%S")

    if agent:
        matches = [t for t in run if agent.lower() in t.agent.lower()]
        if not matches:
            names = ", ".join(t.agent for t in run)
            raise click.ClickException(f"No agent matching '{agent}' in this run. Agents: {names}")
        if len(matches) > 1:
            exact = [t for t in matches if t.agent.lower() == agent.lower()]
            if len(exact) != 1:
                names = ", ".join(t.agent for t in matches)
                raise click.ClickException(f"'{agent}' matches several agents: {names}. Be more specific.")
            matches = exact
        trace = matches[0]
        trace.read_new()
        s = trace.summary()
        trace.events.clear()
        trace.offset, trace._partial = 0, ""
        header = Table.grid(padding=(0, 2))
        header.add_column(style="dim")
        header.add_column()
        header.add_row("challenge", f"[bold]{escape(trace.challenge)}[/bold]   [dim]run {run_index} · {started}[/dim]")
        header.add_row("agent", f"[bold {trace.color}]{escape(trace.agent)}[/bold {trace.color}]  {escape(str(s['model'] or ''))}")
        header.add_row("session", escape(str(s["session"] or "-")))
        header.add_row("trace", escape(str(trace.path)))
        console.print(Panel(header, title="[bold]agent terminal[/bold]", title_align="left", border_style=trace.color))

        def render_terminal(t: Trace, e: dict) -> None:
            for item in _render_terminal(e, max_lines, show_thoughts=not no_thoughts):
                console.print(item)

        _follow([trace], render_terminal, not no_follow, logs, challenge, agent)
        return

    names = "  ".join(f"[bold {t.color}]{escape(t.agent)}[/bold {t.color}]" for t in run)
    console.print(Panel(f"{names}\n[dim]run {run_index} · {started} · {len(run)} agent(s)"
                        f"{'' if no_follow else ' · following, Ctrl+C to stop'}[/dim]",
                        title=f"[bold]{escape(challenge)}[/bold]", title_align="left", border_style="bright_black"))
    width = max(len(t.agent) for t in run)

    def render_line(t: Trace, e: dict) -> None:
        text = _render_line(e, max_lines)
        if text is None:
            return
        console.print(tagged_line(e, t, text, width))

    _follow(run, render_line, not no_follow, logs, challenge, None)


if __name__ == "__main__":
    main()
