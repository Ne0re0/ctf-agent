"""Local HTTP bridge that lets an external agent CLI call ctf-agent's sandbox tools.

The omp extension (omp_bridge.ts) and the Grok MCP server (grok_mcp_bridge.py) run
inside the agent CLI's process tree. They POST {"tool", "args"} to this bridge,
which answers {"text"} or {"image", "mimeType"}. It listens on 127.0.0.1 only and
requires a per-solver bearer token.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from backend.output_types import is_placeholder_flag

logger = logging.getLogger(__name__)

ToolHandler = Callable[[str, dict], Awaitable[dict]]


class ToolBridge:
    def __init__(self, handler: ToolHandler, name: str = "bridge") -> None:
        self._handler = handler
        self._name = name
        self.token = secrets.token_urlsafe(32)
        self._server: asyncio.Server | None = None
        self.url = ""

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/tool"
        return self.url

    def close(self) -> None:
        if self._server:
            self._server.close()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()
            body = await asyncio.wait_for(
                reader.readexactly(int(headers.get("content-length") or 0)), timeout=30,
            )
            parts = request_line.decode("latin-1").split()
            if parts[:2] != ["POST", "/tool"]:
                status, payload = 404, {"text": "not found"}
            elif not secrets.compare_digest(headers.get("authorization", ""), f"Bearer {self.token}"):
                status, payload = 403, {"text": "forbidden"}
            else:
                request = json.loads(body or b"{}")
                args = request.get("args")
                status, payload = 200, await self._handler(
                    str(request.get("tool", "")), args if isinstance(args, dict) else {},
                )
        except Exception as e:
            logger.warning(f"[{self._name}] Bridge request failed: {e}")
            status, payload = 500, {"text": f"bridge error: {e}"}
        try:
            data = json.dumps(payload).encode()
            reason = {200: "OK", 403: "Forbidden", 404: "Not Found"}.get(status, "Error")
            writer.write(
                f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()


async def run_bridge_tool(solver: Any, name: str, args: dict) -> dict:
    """Run a SANDBOX_TOOLS call for a CLI-backed solver and shape the bridge reply.

    Shared by OmpSolver and GrokSolver: step counting, tracing, loop detection,
    dry-run flags, sibling findings every 5 steps. The solver provides the
    attributes exec_sandbox_tool needs plus tracer, loop_detector, message_bus,
    model_spec, _step_count and _findings.
    """
    from backend.agents.codex_solver import exec_sandbox_tool
    from backend.loop_detect import LOOP_WARNING_MESSAGE

    solver._step_count += 1
    step = solver._step_count
    solver.tracer.tool_call(name, args, step)

    loop_status = solver.loop_detector.check(name, args)
    if loop_status == "break":
        solver.tracer.event("loop_break", tool=name, step=step)
        return {"text": "Loop detected — try a completely different approach."}

    if name == "submit_flag" and solver.no_submit:
        # Dry run: like the Claude backend, the first reported flag wins
        flag = str(args.get("flag", "")).strip()
        result: str | tuple[bytes, str]
        if is_placeholder_flag(flag):
            result = f'"{flag}" is not a flag. Submit only the real flag, read from the target.'
        else:
            solver._confirmed, solver._flag = True, flag
            solver._findings = f"Flag found via submit_flag: {flag}"
            solver.tracer.event("flag_confirmed", flag=flag, step=step, dry_run=True)
            result = f'DRY RUN — would submit "{flag}"'
    else:
        try:
            result = await exec_sandbox_tool(solver, name, args)
        except Exception as e:
            result = f"Error running {name}: {e}"

    if isinstance(result, tuple):
        image_bytes, mime_type = result
        solver.tracer.tool_result(name, f"image:{mime_type}:{len(image_bytes)}b", step)
        return {"image": base64.b64encode(image_bytes).decode(), "mimeType": mime_type}

    text = str(result)
    if loop_status == "warn":
        text = f"{text}\n\n{LOOP_WARNING_MESSAGE}"
    solver.tracer.tool_result(name, text[:500], step)

    if step % 5 == 0 and solver.message_bus:
        from backend.tools.core import do_check_findings
        findings = await do_check_findings(solver.message_bus, solver.model_spec)
        if findings and "No new findings" not in findings:
            text = f"{text}\n\n---\n{findings}"
            solver.tracer.event("findings_delivered", text=findings[:2000], step=step)
    return {"text": text}
