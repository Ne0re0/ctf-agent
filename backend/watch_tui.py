"""Interactive session browser for ctf-watch (Textual).

Left: challenges > runs > agents, with live state, steps and cost.
Right: the highlighted agent's terminal (commands, outputs, its messages), or the
whole run interleaved when a run is highlighted. Everything updates while solvers run.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, RichLog, Static, Tree
from textual.widgets._tree import TreeNode

from backend.watch import (
    _COLORS,
    _STATE_STYLE,
    Trace,
    _group_runs,
    _load_traces,
    _render_line,
    _render_terminal,
    tagged_line,
)

_CLIPPED_LINES = 15


class SessionBrowser(App):
    CSS = """
    #sidebar { width: 46; min-width: 30; border-right: solid $panel-lighten-2; }
    #tree { height: 1fr; }
    #help { height: auto; color: $text-muted; padding: 0 1; }
    #info { height: auto; padding: 0 1; border-bottom: solid $panel-lighten-2; }
    #log { height: 1fr; padding: 0 1; }
    """
    TITLE = "CTF Agent — sessions"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("t", "toggle_thoughts", "Agent messages"),
        Binding("o", "toggle_full", "Full outputs"),
        Binding("f", "toggle_follow", "Follow"),
        Binding("g", "jump_latest", "Latest run"),
        Binding("tab", "focus_next", "Switch pane", show=False),
    ]

    def __init__(self, logs: Path) -> None:
        super().__init__()
        self.logs = logs
        self.traces: dict[Path, Trace] = {}
        self.chall_nodes: dict[str, TreeNode] = {}
        self.run_nodes: dict[tuple[str, float], TreeNode] = {}
        self.agent_nodes: dict[Path, TreeNode] = {}
        self.runs: dict[tuple[str, float], list[Trace]] = {}
        self.view: tuple | None = None  # ("agent", path) | ("run", key)
        self.shown: dict[Path, int] = {}  # events already written to the log, per trace
        self.show_thoughts = True
        self.full = False
        self.follow = True

    # ── layout ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                tree: Tree = Tree("logs", id="tree")
                tree.show_root = False
                tree.guide_depth = 2
                yield tree
                yield Static("[b]↑↓[/b] move  [b]←→[/b] fold  [b]tab[/b] pane", id="help")
            with Vertical():
                yield Static("Pick a challenge run or an agent on the left.", id="info")
                yield RichLog(id="log", wrap=True, markup=False, highlight=False, auto_scroll=True, max_lines=20_000)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = str(self.logs.resolve())
        self.refresh_tree(first=True)
        self.set_interval(0.5, self.poll_view)
        self.set_interval(2.0, self.refresh_tree)
        self.query_one("#tree", Tree).focus()

    # ── data / tree ───────────────────────────────────────────────────────

    def refresh_tree(self, first: bool = False) -> None:
        for t in _load_traces(self.logs):
            if t.path not in self.traces:
                self.traces[t.path] = t
        for t in self.traces.values():
            t.read_new()
        grouped = _group_runs(list(self.traces.values()))
        self.runs = {(chall, run[0].started): run for chall, runs in grouped.items() for run in runs}

        tree = self.query_one("#tree", Tree)
        order = sorted(grouped, key=lambda c: max(t.mtime for run in grouped[c] for t in run), reverse=True)
        for chall in order if first else order[::-1]:
            runs = grouped[chall]
            node = self.chall_nodes.get(chall)
            if node is None:
                node = tree.root.add("", data=("chall", chall), expand=first and chall == order[0],
                                     before=None if first else 0)
                self.chall_nodes[chall] = node
            flagged = any(t.summary()["flag"] for run in runs for t in run)
            running = any(t.summary()["state"] == "running" for run in runs for t in run)
            label = Text()
            label.append("🚩 " if flagged else ("● " if running else "  "),
                         style="bold green" if flagged else "bold cyan")
            label.append(chall, style="bold")
            label.append(f"  {len(runs)} run{'s' if len(runs) > 1 else ''}", style="dim")
            node.set_label(label)

            for run in (runs if first or not node.children else runs[::-1]):
                key = (chall, run[0].started)
                run_node = self.run_nodes.get(key)
                if run_node is None:
                    run_node = node.add("", data=("run", key), expand=first and run is runs[0] and chall == order[0],
                                        before=None if first or not node.children else 0)
                    self.run_nodes[key] = run_node
                started = datetime.fromtimestamp(run[0].started)
                run_label = Text(started.strftime("%m-%d %H:%M:%S"), style="bold")
                run_label.append(f"  {len(run)} agent{'s' if len(run) > 1 else ''}", style="dim")
                run_node.set_label(run_label)
                for i, t in enumerate(run):
                    if t.color == "white":
                        t.color = _COLORS[i % len(_COLORS)]
                    s = t.summary()
                    icon, style = _STATE_STYLE[s["state"]]
                    agent_label = Text()
                    agent_label.append(icon.split(" ")[0] + " ", style=style)
                    agent_label.append(t.agent, style=f"bold {t.color}")
                    agent_label.append(f"  {s['steps']}st ${s['cost']:.2f}", style="dim")
                    leaf = self.agent_nodes.get(t.path)
                    if leaf is None:
                        leaf = run_node.add_leaf(agent_label, data=("agent", t.path))
                        self.agent_nodes[t.path] = leaf
                    else:
                        leaf.set_label(agent_label)
        if first and order:
            latest = self.run_nodes.get((order[0], grouped[order[0]][0][0].started))
            if latest is not None:
                tree.move_cursor(latest)
        self.update_info()

    # ── right pane ────────────────────────────────────────────────────────

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        data = event.node.data
        if not data or data[0] == "chall":
            return
        if data != self.view:
            self.view = data
            self.render_view()

    def render_view(self) -> None:
        log = self.query_one("#log", RichLog)
        log.clear()
        self.shown = {}
        self.update_info()
        self.poll_view()
        if not any(t.events for t in self.view_traces()):
            log.write(Text("No events yet: the solver has not run a command (or the run stopped at startup).",
                           style="dim italic"))

    def view_traces(self) -> list[Trace]:
        if not self.view:
            return []
        kind, key = self.view
        if kind == "agent":
            return [self.traces[key]] if key in self.traces else []
        return self.runs.get(key, [])

    def poll_view(self) -> None:
        traces = self.view_traces()
        if not traces:
            return
        for t in traces:
            t.read_new()
        pending = []
        for t in traces:
            start = self.shown.get(t.path, 0)
            pending.extend((e.get("ts", 0), t, e) for e in t.events[start:])
            self.shown[t.path] = len(t.events)
        if not pending:
            return
        log = self.query_one("#log", RichLog)
        max_lines = 10_000 if self.full else _CLIPPED_LINES
        single = self.view[0] == "agent"
        width = max(len(t.agent) for t in traces)
        for _, t, e in sorted(pending, key=lambda x: x[0]):
            if single:
                for item in _render_terminal(e, max_lines, show_thoughts=self.show_thoughts):
                    log.write(item, scroll_end=self.follow)
            else:
                if e.get("type") == "model_response" and not self.show_thoughts:
                    continue
                markup = _render_line(e, max_lines)
                if markup is None:
                    continue
                log.write(tagged_line(e, t, markup, width), scroll_end=self.follow)
        self.update_info()

    def update_info(self) -> None:
        info = self.query_one("#info", Static)
        traces = self.view_traces()
        if not traces:
            return
        flags = f"thoughts {'on' if self.show_thoughts else 'off'} · outputs {'full' if self.full else 'clipped'} · follow {'on' if self.follow else 'off'}"
        if self.view[0] == "agent":
            t = traces[0]
            s = t.summary()
            icon, style = _STATE_STYLE[s["state"]]
            flag = f"   [bold green]🚩 {escape(str(s['flag']))}[/bold green]" if s["flag"] else ""
            info.update(
                f"[bold {t.color}]{escape(t.agent)}[/bold {t.color}]  [dim]{escape(str(s['model'] or ''))} · "
                f"{escape(t.challenge)}[/dim]\n"
                f"[{style}]{icon}[/{style}]  {s['steps']} steps  ${s['cost']:.2f}  "
                f"[dim]session {escape(str(s['session'] or '-'))}[/dim]{flag}\n[dim]{flags}[/dim]"
            )
        else:
            chall, started = self.view[1]
            parts = []
            for t in traces:
                s = t.summary()
                icon, style = _STATE_STYLE[s["state"]]
                parts.append(f"[{t.color}]{escape(t.agent)}[/{t.color}] [{style}]{icon.split(' ')[0]}[/{style}] ${s['cost']:.2f}")
            when = datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S")
            info.update(f"[bold]{escape(chall)}[/bold]  [dim]run {when}[/dim]\n" + "   ".join(parts) + f"\n[dim]{flags}[/dim]")

    # ── actions ───────────────────────────────────────────────────────────

    def action_toggle_thoughts(self) -> None:
        self.show_thoughts = not self.show_thoughts
        self.render_view()

    def action_toggle_full(self) -> None:
        self.full = not self.full
        self.render_view()

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        if self.follow:
            self.query_one("#log", RichLog).scroll_end(animate=False)
        self.update_info()

    def action_jump_latest(self) -> None:
        if not self.run_nodes:
            return
        key = max(self.run_nodes, key=lambda k: max(t.mtime for t in self.runs.get(k, [])) if self.runs.get(k) else 0)
        node = self.run_nodes[key]
        if node.parent:
            node.parent.expand()
        node.expand()
        tree = self.query_one("#tree", Tree)
        tree.move_cursor(node)


def run_browser(logs: Path) -> None:
    SessionBrowser(logs).run()
