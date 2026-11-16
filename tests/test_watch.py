"""Tests for ctf-watch (trace parsing, rendering and the interactive browser)."""

import asyncio
import json

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
