"""Tests for ctf-watch (trace parsing, rendering and the interactive browser)."""

import asyncio
import json
import os

from backend.watch import _args, _command_text, _group_runs, _load_traces, _result_text, short_agent


def _write_trace(logs, name, events):
    path = logs / name
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def test_truncated_command_is_recovered():
    full = json.dumps({"command": "cat > x.py <<'EOF'\nprint('héllo')\n" + "a" * 3000})
    event = {"type": "tool_call", "tool": "bash", "args": full[:2000]}
    assert isinstance(_args(event), dict)
    assert _command_text(event).startswith("cat > x.py <<'EOF'\nprint('héllo')")


def test_claude_bash_result_shows_stdout_and_stderr():
    raw = str({"stdout": "line1\nline2", "stderr": "warn", "interrupted": False})
    assert _result_text(raw) == "line1\nline2\nwarn"
    assert _result_text(raw[:30]).startswith("line1\nline2")  # cut by the tracer
    assert _result_text("plain output") == "plain output"


def test_traces_group_into_runs_per_challenge(tmp_path):
    start = {"type": "start", "challenge": "baby-rsa", "model": "m"}
    _write_trace(tmp_path, "trace-baby-rsa-grok-grok-4.6-20261114-180102.jsonl", [start])
    _write_trace(tmp_path, "trace-baby-rsa-claude-opus-5-20261114-180103.jsonl", [start])
    _write_trace(tmp_path, "trace-baby-rsa-claude-opus-5-20261114-190000.jsonl", [start])
    runs = _group_runs(_load_traces(tmp_path))
    assert list(runs) == ["baby-rsa"]
    assert [len(r) for r in runs["baby-rsa"]] == [1, 2]  # newest run first
    assert {t.agent for t in runs["baby-rsa"][1]} == {"grok-grok-4.6", "claude-opus-5"}


def test_trace_without_start_event_joins_its_challenge(tmp_path):
    """A just-created (still empty) trace must not show up as a second 'Plug_It_In' challenge."""
    start = {"type": "start", "challenge": "Plug It In", "model": "m"}
    _write_trace(tmp_path, "trace-Plug_It_In-claude-opus-5-5-max-20261116-191238.jsonl", [start])
    empty = _write_trace(tmp_path, "trace-Plug_It_In-claude-sonnet-4-6-max-20261116-191239.jsonl", [])
    runs = _group_runs(_load_traces(tmp_path))
    assert list(runs) == ["Plug It In"]
    assert {t.agent for t in runs["Plug It In"][0]} == {"claude-opus-5-5-max", "claude-sonnet-4-6-max"}

    # alone, the name can only be guessed; it is corrected when the start event arrives
    (tmp_path / "trace-Plug_It_In-claude-opus-5-5-max-20261116-191238.jsonl").unlink()
    (trace,) = _load_traces(tmp_path)
    assert trace.challenge == "Plug_It_In"
    empty.write_text(json.dumps(start) + "\n")
    trace.read_new()
    assert (trace.challenge, trace.agent) == ("Plug It In", "claude-sonnet-4-6-max")


def test_browser_merges_renamed_challenge(tmp_path):
    from backend.watch_tui import SessionBrowser

    name = "trace-Plug_It_In-claude-opus-5-5-max-20261116-191238.jsonl"
    _write_trace(tmp_path, name, [])

    async def scenario():
        app = SessionBrowser(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.3)
            before = [chall for _, chall in app.chall_nodes]
            _write_trace(tmp_path, name, [{"ts": 1, "type": "start", "challenge": "Plug It In", "model": "m"}])
            app.refresh_tree()
            await pilot.pause(0.3)
            from textual.widgets import Tree
            shown = [n.data[1] for section in app.query_one(Tree).root.children for n in section.children]
            return before, [chall for _, chall in app.chall_nodes], shown, len(app.agent_nodes)

    before, after, shown, leaves = asyncio.run(scenario())
    assert before == ["Plug_It_In"]
    assert after == shown == ["Plug It In"] and leaves == 1


def _state(tmp_path, name, events):
    (trace,) = _load_traces(tmp_path, name)
    trace.path.write_text("".join(json.dumps(e) + "\n" for e in events))
    os.utime(trace.path, (1, 1))  # quiet for a long time
    trace.offset, trace.events = 0, []
    trace.read_new()
    return trace.summary()["state"]


def test_state_follows_the_solver_process(tmp_path):
    import os
    import socket
    import subprocess
    import sys

    _write_trace(tmp_path, "trace-chall-grok-4.6-20261114-180102.jsonl", [])
    here = {"type": "start", "challenge": "chall", "model": "m", "host": socket.gethostname()}
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()

    def state(events):
        return _state(tmp_path, "chall", events)

    assert state([{**here, "pid": os.getpid()}]) == "running"  # thinking for a long time
    assert state([{**here, "pid": gone.pid}]) == "dead"  # ctf-solve was killed
    assert state([{**here, "pid": os.getpid()}, {"type": "stop"}]) == "stopped"
    assert state([{**here, "pid": gone.pid, "host": "elsewhere"}]) == "idle"  # cannot know
    assert state([{"type": "start", "challenge": "chall", "model": "m"}]) == "idle"  # trace of an older version


def _sections(app):
    from textual.widgets import Tree

    live, past = app.query_one(Tree).root.children
    return [[(c.data[1], len(c.children)) for c in section.children] for section in (live, past)]


def test_browser_separates_running_agents_from_history(tmp_path):
    import os
    import socket

    from backend.watch_tui import SessionBrowser

    start = {"ts": 1, "type": "start", "challenge": "web", "model": "m"}
    mine = {**start, "pid": os.getpid(), "host": socket.gethostname()}
    _write_trace(tmp_path, "trace-web-claude-20261114-180102.jsonl", [start, {"ts": 2, "type": "stop"}])
    _write_trace(tmp_path, "trace-old-claude-20261113-180102.jsonl", [{**start, "challenge": "old"}, {"ts": 2, "type": "stop"}])
    running = _write_trace(tmp_path, "trace-web-claude-20261116-180102.jsonl", [mine])
    _write_trace(tmp_path, "trace-web-grok-20261116-180103.jsonl", [mine, {"ts": 2, "type": "stop"}])

    async def scenario():
        app = SessionBrowser(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.3)
            during, view = _sections(app), app.view
            label = str(app.sections["live"].label)
            with running.open("a") as fh:
                fh.write(json.dumps({"ts": 3, "type": "stop"}) + "\n")
            app.refresh_tree()
            await pilot.pause(0.3)
            return during, view, label, _sections(app), app.view, str(app.sections["live"].label)

    during, view, label, after, view_after, label_after = asyncio.run(scenario())
    # the running run (both its agents) is apart from the two finished ones
    assert during[0] == [("web", 1)] and sorted(during[1]) == [("old", 1), ("web", 1)]
    assert "1 agent" in label and view == ("run", ("web", view[1][1]))
    # once its last agent stops it joins the history; the selection follows it
    assert after[0] == [] and sorted(after[1]) == [("old", 1), ("web", 2)]
    assert "nothing is running" in label_after and view_after == view


def test_dashboard_lists_running_runs_first(tmp_path):
    import os
    import socket

    from rich.console import Console

    from backend.watch import _dashboard

    start = {"ts": 1, "type": "start", "challenge": "web", "model": "m"}
    _write_trace(tmp_path, "trace-web-old-20261114-180102.jsonl", [start, {"ts": 2, "type": "stop"}])
    _write_trace(tmp_path, "trace-web-new-20261116-180102.jsonl", [{**start, "pid": os.getpid(), "host": socket.gethostname()}])
    out = Console(width=120, record=True, file=open(os.devnull, "w"))
    out.print(_dashboard(tmp_path, None, 3))
    text = out.export_text()
    assert text.index("RUNNING · 1 agent") < text.index("#1 11-16") < text.index("HISTORY") < text.index("#2 11-14")


def test_short_agent():
    assert short_agent("grok-4.6") == "grok-4.6"
    assert len(short_agent("omp-orcarouter_deepseek_deepseek-v4.1-flash")) == 18


def test_browser_shows_agent_terminal(tmp_path):
    from textual.widgets import RichLog

    from backend.watch_tui import SessionBrowser

    _write_trace(tmp_path, "trace-chall-grok-4.6-20261114-180102.jsonl", [
        {"ts": 1, "type": "start", "challenge": "chall", "model": "grok-4.6"},
        {"ts": 2, "type": "tool_call", "tool": "bash", "args": json.dumps({"command": "id"}), "step": 1},
        {"ts": 3, "type": "tool_result", "tool": "bash", "result": "uid=0(root)", "step": 1},
        {"ts": 4, "type": "flag_confirmed", "flag": "FLAG{x}", "step": 2},
    ])

    async def scenario():
        app = SessionBrowser(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.5)
            leaf = next(iter(app.agent_nodes.values()))
            from textual.widgets import Tree
            app.query_one(Tree).move_cursor(leaf)
            await pilot.pause(0.8)
            view = app.view
            lines = [str(line.text) for line in app.query_one(RichLog).lines]
            await pilot.press("t", "o", "f", "g")  # toggles and "latest run" must not crash
            await pilot.pause(0.3)
            assert app.view[0] == "run"
            return view, lines

    view, lines = asyncio.run(scenario())
    assert view[0] == "agent"
    text = "\n".join(lines)
    assert "root@sandbox:/challenge# id" in text and "uid=0(root)" in text and "FLAG{x}" in text


def test_tracer_honours_ctf_logs_dir(tmp_path, monkeypatch):
    from backend.tracing import SolverTracer

    monkeypatch.setenv("CTF_LOGS_DIR", str(tmp_path / "shared-logs"))
    tracer = SolverTracer("chall", "model")
    tracer.close()
    assert tracer.path.startswith(str(tmp_path / "shared-logs"))
