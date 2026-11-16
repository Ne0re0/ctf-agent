"""Discover and format model specs accepted by the solver backends."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from backend.config import Settings, resolve_cli
from backend.models import DEFAULT_MODELS


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _claude_help(command: str) -> str:
    executable = resolve_cli(command, "CLAUDE_CLI")
    result = subprocess.run(
        [executable, "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"Claude CLI exited with status {result.returncode}")
    return f"{result.stdout}\n{result.stderr}"


def _parse_claude_help(help_text: str) -> tuple[list[str], list[str]]:
    model_block = re.search(r"--model <model>(.{0,500})", help_text, flags=re.DOTALL)
    aliases = (
        re.findall(r"'([^']+)'", model_block.group(1).split("model's full name", 1)[0])
        if model_block
        else []
    )

    effort_block = re.search(r"--effort <level>(.{0,300})", help_text, flags=re.DOTALL)
    effort_match = re.search(r"\(([^)]+)\)", effort_block.group(1)) if effort_block else None
    efforts = (
        [effort.strip() for effort in effort_match.group(1).split(",") if effort.strip()]
        if effort_match
        else []
    )
    return aliases, efforts


def discover_claude_models(command: str) -> tuple[list[str], list[str]]:
    """Return aliases and efforts advertised by Claude Code or its configured wrapper."""
    help_text = _claude_help(command)
    aliases, efforts = _parse_claude_help(help_text)
    if aliases:
        return aliases, efforts

    executable = resolve_cli(command, "CLAUDE_CLI")
    if Path(executable).name != "claude":
        aliases, efforts = _parse_claude_help(_claude_help("claude"))
        if aliases:
            return aliases, efforts

    raise RuntimeError("Claude CLI did not advertise any model aliases")


def discover_claude_versioned_models(
    config_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return full model versions from Claude Code's local model catalog."""
    if config_dir is None:
        configured_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        config_dir = (
            Path(configured_dir).expanduser()
            if configured_dir
            else Path.home() / ".claude"
        )

    cache_dir = config_dir / "cache" / "model-catalog"
    try:
        candidates = sorted(
            cache_dir.glob("*.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError as exc:
        raise RuntimeError(f"could not read {cache_dir}: {exc}") from exc

    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        models = payload.get("catalog", {}).get("config", {}).get("models")
        if not isinstance(models, list):
            continue
        return [
            model
            for model in models
            if isinstance(model, dict) and isinstance(model.get("id"), str)
        ]

    raise RuntimeError(f"no model catalog found under {cache_dir}")


def discover_claude_gateway_models(
    config_dir: Path | None = None,
    *,
    live: bool = True,
) -> list[dict[str, Any]]:
    """Return live OurClaude models, falling back to Claude Code's gateway cache."""
    if config_dir is None:
        configured_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        config_dir = Path(configured_dir).expanduser() if configured_dir else Path.home() / ".claude"

    proxy_path = config_dir / "proxy.json"
    cache_path = config_dir / "cache" / "gateway-models.json"
    try:
        proxy = json.loads(proxy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("OurClaude proxy configuration is unavailable") from exc

    server_url = proxy.get("server_url")
    token = proxy.get("token")
    if not isinstance(server_url, str) or not server_url.startswith(("https://", "http://")):
        raise RuntimeError("OurClaude proxy URL is invalid")
    gateway_url = server_url.rstrip("/") + "/proxy"

    if live and isinstance(token, str) and token:
        request = urllib.request.Request(
            gateway_url + "/v1/models?limit=1000",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "claude-code/2.1.325",
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=5) as response:
                payload = json.load(response)
            models = payload.get("data")
            if isinstance(models, list):
                return [model for model in models if isinstance(model, dict) and isinstance(model.get("id"), str)]
        except (OSError, ValueError, AttributeError):
            pass

    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("OurClaude gateway model cache is unavailable") from exc
    if cached.get("baseUrl") != gateway_url:
        raise RuntimeError("gateway model cache does not match the configured OurClaude server")

    models = cached.get("models")
    if not isinstance(models, list):
        raise RuntimeError("OurClaude gateway model cache has no model list")
    return [model for model in models if isinstance(model, dict) and isinstance(model.get("id"), str)]


async def _codex_rpc(
    proc: asyncio.subprocess.Process,
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    assert proc.stdin and proc.stdout
    message = {"id": request_id, "method": method, "params": params}
    proc.stdin.write((json.dumps(message) + "\n").encode())
    await proc.stdin.drain()

    while True:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
        if not line:
            raise RuntimeError("Codex app-server exited before replying")
        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            continue
        if response.get("id") != request_id:
            continue
        if "error" in response:
            raise RuntimeError(str(response["error"]))
        return response.get("result", {})


async def discover_codex_models(command: str) -> list[dict[str, Any]]:
    """Return visible models advertised by ``codex app-server model/list``."""
    executable = resolve_cli(command, "CODEX_CLI")
    proc = await asyncio.create_subprocess_exec(
        executable,
        "app-server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await _codex_rpc(
            proc,
            1,
            "initialize",
            {
                "clientInfo": {"name": "ctf-agent", "version": "2.0.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        assert proc.stdin
        proc.stdin.write(b'{"method":"initialized","params":{}}\n')
        await proc.stdin.drain()

        result = await _codex_rpc(proc, 2, "model/list", {})
        return [model for model in result.get("data", []) if not model.get("hidden")]
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except TimeoutError:
                proc.kill()
                await proc.wait()


def _format_efforts(model: dict[str, Any]) -> str:
    efforts = [
        item.get("reasoningEffort", "")
        for item in model.get("supportedReasoningEfforts", [])
        if item.get("reasoningEffort")
    ]
    if not efforts:
        return ""
    default = model.get("defaultReasoningEffort")
    rendered = ", ".join(
        f"{effort} (default)" if effort == default else effort for effort in efforts
    )
    return f" [{rendered}]"


def _format_claude_efforts(model: dict[str, Any]) -> str:
    runtime = model.get("runtime", {})
    if not isinstance(runtime, dict):
        return ""
    efforts = [
        effort for effort in runtime.get("effort_levels", []) if isinstance(effort, str)
    ]
    if not efforts:
        return ""
    default = runtime.get("default_effort")
    rendered = ", ".join(
        f"{effort} (default)" if effort == default else effort for effort in efforts
    )
    return f" [{rendered}]"


def render_available_models(settings: Settings) -> str:
    """Render configured defaults and models available through local provider CLIs."""
    lines = ["Configured defaults:"]
    lines.extend(f"  {spec}" for spec in DEFAULT_MODELS)

    lines.extend(["", "Claude models advertised by the local CLI:"])
    try:
        aliases, efforts = discover_claude_models(settings.claude_cli)
    except Exception as exc:
        lines.append(f"  unavailable ({exc})")
    else:
        effort_text = f" [{', '.join(efforts)}]" if efforts else ""
        lines.extend(f"  claude-sdk/{alias}{effort_text}" for alias in aliases)

    lines.extend(["", "Versioned Claude models in the local Claude Code catalog:"])
    claude_models: list[dict[str, Any]] = []
    try:
        claude_models = discover_claude_versioned_models()
    except Exception as exc:
        lines.append(f"  unavailable ({exc})")
    else:
        if not claude_models:
            lines.append("  none advertised")
        for model in claude_models:
            lines.append(f"  claude-sdk/{model['id']}{_format_claude_efforts(model)}")

    try:
        is_ourclaude = Path(resolve_cli(settings.claude_cli, "CLAUDE_CLI")).name == "ourclaude"
    except RuntimeError:
        is_ourclaude = False
    if is_ourclaude:
        lines.extend(["", "OurClaude gateway models:"])
        try:
            gateway_models = discover_claude_gateway_models()
        except Exception as exc:
            lines.append(f"  unavailable ({exc})")
        else:
            if not gateway_models:
                lines.append("  none advertised")
            local_ids = {model["id"] for model in claude_models}
            for model in gateway_models:
                if model["id"] not in local_ids:
                    lines.append(f"  claude-sdk/{model['id']}")

    lines.extend(["", "Codex models available to this account:"])
    try:
        models = asyncio.run(discover_codex_models(settings.codex_cli))
    except Exception as exc:
        lines.append(f"  unavailable ({exc})")
    else:
        if not models:
            lines.append("  none advertised")
        for model in models:
            model_id = model.get("model") or model.get("id")
            lines.append(f"  codex/{model_id}{_format_efforts(model)}")

    lines.extend(
        [
            "",
            "Other accepted forms:",
            "  claude-sdk/<model-or-alias>/<effort>",
            "  bedrock/<model-id>",
            "  azure/<model-id>",
            "  zen/<model-id>",
            "  google/<model-id>",
            "",
            "Repeat --models to race multiple selections.",
        ]
    )
    return "\n".join(lines)
