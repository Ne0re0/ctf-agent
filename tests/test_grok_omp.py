"""Tests for the grok/* (Grok Build CLI), xai/* (API) and omp/* (oh-my-pi) backends."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from backend import model_catalog
from backend.agents import grok_mcp_bridge
from backend.agents.codex_solver import SANDBOX_TOOLS
from backend.agents.grok_solver import ALLOWED_TOOLS, DENY_RULES, DISALLOWED_TOOLS, GrokSolver
from backend.agents.omp_solver import _omp_usage, final_flag_json
from backend.agents.tool_bridge import ToolBridge, run_bridge_tool
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.models import effort_from_spec, model_id_from_spec, provider_from_spec, resolve_model


@pytest.mark.parametrize(
    ("spec", "provider", "model", "effort"),
    [
        ("omp/xai/grok-4/high", "omp", "xai/grok-4", "high"),
        ("omp/opus", "omp", "opus", None),
        ("omp/opus/xhigh", "omp", "opus", "xhigh"),
        ("omp/orcarouter/deepseek/deepseek-v4.1-flash", "omp", "orcarouter/deepseek/deepseek-v4.1-flash", None),
        ("grok/grok-4.6", "grok", "grok-4.6", None),
        ("grok/grok-4.6/high", "grok", "grok-4.6", "high"),
        ("xai/grok-4-1-fast-reasoning", "xai", "grok-4-1-fast-reasoning", None),
        ("claude-sdk/claude-opus-4-6/max", "claude-sdk", "claude-opus-4-6", "max"),
    ],
)
def test_spec_parsing(spec, provider, model, effort):
    assert provider_from_spec(spec) == provider
    assert model_id_from_spec(spec) == model
    assert effort_from_spec(spec) == effort


def test_xai_resolves_to_openai_compatible_endpoint():
    model = resolve_model("xai/grok-4-1-fast-reasoning", Settings(xai_api_key="test-key"))
    assert model.model_name == "grok-4-1-fast-reasoning"
    assert str(model.client.base_url).startswith("https://api.x.ai/v1")


def test_xai_requires_api_key():
    with pytest.raises(ValueError, match="XAI_API_KEY"):
        resolve_model("xai/grok-4", Settings(xai_api_key=""))


def test_final_flag_json_finds_trailing_object():
    text = 'Done.\n{"type": "flag_found", "flag": "CTF{a{b}c}", "method": "cat"}'
    assert final_flag_json(text) == {"type": "flag_found", "flag": "CTF{a{b}c}", "method": "cat"}
    assert final_flag_json("no json, just CTF{x}") is None
    assert final_flag_json('{"type": "other"}') is None


def test_omp_usage_adds_cached_tokens_to_input():
    usage = {"input": 100, "output": 20, "cacheRead": 50, "cacheWrite": 5, "cost": {"total": 0.25}}
    assert _omp_usage(usage) == (155, 50, 5, 20, 0.25)


def test_discover_grok_models_parses_list(monkeypatch):
    out = "You are logged in.\n\nAvailable models:\n  - grok-4.7\n  * grok-4.6 (default)\n"
    monkeypatch.setattr(model_catalog, "resolve_cli", lambda command, setting: command)
    monkeypatch.setattr(
        model_catalog.subprocess, "run", lambda *a, **k: CompletedProcess(a[0], 0, out, ""),
    )
    assert model_catalog.discover_grok_models("grok") == [("grok-4.7", False), ("grok-4.6", True)]


def test_grok_command_line_removes_host_tools(tmp_path):
    solver = GrokSolver.__new__(GrokSolver)
    solver._command, solver._system_prompt = "grok", "PROMPT"
    solver.model_id, solver.effort, solver._session_id = "grok-4.6", "high", None
    solver._workdir = SimpleNamespace(name=str(tmp_path))

    args = solver._args("go")
    assert args[args.index("--disallowed-tools") + 1] == ",".join(DISALLOWED_TOOLS)
    assert args[args.index("--deny") + 1] == DENY_RULES
    assert "run_terminal_cmd" in DISALLOWED_TOOLS and "read_file" in DISALLOWED_TOOLS
    assert ALLOWED_TOOLS.isdisjoint(DISALLOWED_TOOLS)
    assert "--resume" not in args
    solver._session_id = "abc"
    assert solver._args("go")[-2:] == ["--resume", "abc"]


def _fake_solver(no_submit=True):
    events = []
    tracer = SimpleNamespace(
        tool_call=lambda *a: events.append(("call", a)),
        tool_result=lambda *a: events.append(("result", a)),
        event=lambda *a, **k: events.append(("event", a, k)),
    )
    loop = SimpleNamespace(check=lambda name, args: "ok")
    return SimpleNamespace(
        _step_count=0, _confirmed=False, _flag=None, _findings="", no_submit=no_submit,
        tracer=tracer, loop_detector=loop, message_bus=None, model_spec="omp/x",
    ), events


def test_bridge_dry_run_submit_confirms_flag():
    solver, _ = _fake_solver()
    reply = asyncio.run(run_bridge_tool(solver, "submit_flag", {"flag": " CTF{x} "}))
    assert "DRY RUN" in reply["text"]
    assert solver._confirmed and solver._flag == "CTF{x}"


def test_tool_bridge_requires_token():
    async def scenario():
        calls = []

        async def handler(name, args):
            calls.append((name, args))
            return {"text": "ok"}

        bridge = ToolBridge(handler)
        url = await bridge.start()
        port = int(url.split(":")[2].split("/")[0])

        async def post(token):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"tool": "bash", "args": {"command": "id"}}).encode()
            writer.write(
                f"POST /tool HTTP/1.1\r\nAuthorization: Bearer {token}\r\n"
                f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            )
            await writer.drain()
            data = await reader.read()
            writer.close()
            return data

        denied = await post("wrong")
        allowed = await post(bridge.token)
        bridge.close()
        return denied, allowed, calls

    denied, allowed, calls = asyncio.run(scenario())
    assert denied.startswith(b"HTTP/1.1 403")
    assert allowed.startswith(b"HTTP/1.1 200") and allowed.endswith(b'{"text": "ok"}')
    assert calls == [("bash", {"command": "id"})]


def test_grok_mcp_bridge_speaks_mcp_over_stdio():
    async def scenario():
        async def handler(name, args):
            return {"text": f"ran {name} {args['command']}"}

        bridge = ToolBridge(handler)
        url = await bridge.start()
        env = {
            **os.environ,
            "CTF_AGENT_BRIDGE_URL": url,
            "CTF_AGENT_BRIDGE_TOKEN": bridge.token,
            "CTF_AGENT_TOOLS": json.dumps(SANDBOX_TOOLS),
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable, grok_mcp_bridge.__file__,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
        )
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "bash", "arguments": {"command": "id"}}},
        ]
        replies = {}
        for request in requests:
            proc.stdin.write((json.dumps(request) + "\n").encode())
            await proc.stdin.drain()
            if "id" in request:
                reply = json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))
                replies[reply["id"]] = reply
        proc.stdin.close()
        await proc.wait()
        bridge.close()
        return replies

    replies = asyncio.run(scenario())
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert [t["name"] for t in replies[2]["result"]["tools"]] == [t["name"] for t in SANDBOX_TOOLS]
    assert replies[3]["result"]["content"] == [{"type": "text", "text": "ran bash id"}]


def test_grok_result_usage_is_recorded_with_reported_cost():
    solver = GrokSolver.__new__(GrokSolver)
    solver.cost_tracker, solver.agent_name, solver.model_id = CostTracker(), "c/grok", "grok-4.6"
    solver.tracer = SimpleNamespace(usage=lambda *a: None)
    solver._record_result_usage({
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 50, "cache_creation_input_tokens": 0, "output_tokens": 10},
        "total_cost_usd": 0.5,
    })
    agent = solver.cost_tracker.by_agent["c/grok"]
    assert agent.usage.input_tokens == 150 and agent.usage.cache_read_tokens == 50
    assert solver._cost_usd == 0.5


def test_omp_bridge_extension_is_packaged():
    ext = Path(__file__).parents[1] / "backend" / "agents" / "omp_bridge.ts"
    assert "registerTool" in ext.read_text()


def test_challenge_folder_without_metadata(tmp_path):
    from backend.prompts import ChallengeMeta, distfiles_dir, list_distfiles

    chall = tmp_path / "baby-rev"
    chall.mkdir()
    (chall / "crackme").write_bytes(b"\x7fELF")
    (chall / "README.md").write_text("Find the password.\n")
    (chall / ".hidden").write_text("x")

    meta = ChallengeMeta.load_or_create(chall)
    assert meta.name == "baby-rev" and meta.description == "Find the password."
    assert distfiles_dir(chall) == chall
    assert list_distfiles(str(chall)) == ["README.md", "crackme"]


def test_challenge_folder_with_metadata_keeps_distfiles_subdir(tmp_path):
    from backend.prompts import ChallengeMeta, distfiles_dir

    (tmp_path / "metadata.yml").write_text("name: rsa\ndescription: decrypt\n")
    (tmp_path / "distfiles").mkdir()
    assert ChallengeMeta.load_or_create(tmp_path).name == "rsa"
    assert distfiles_dir(tmp_path) == tmp_path / "distfiles"
