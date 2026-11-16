"""stdio MCP server that GrokSolver registers with the `grok` CLI.

It exposes the sandbox tools (bash, read_file, submit_flag, ...) and forwards every
call to the solver's ToolBridge (tool_bridge.py), which runs it in the challenge's
Docker container. Standard library only, so it starts fast in grok's process tree
and does not depend on the MCP SDK's API.

Environment, set by GrokSolver:
  CTF_AGENT_BRIDGE_URL    http://127.0.0.1:<port>/tool
  CTF_AGENT_BRIDGE_TOKEN  bearer token for the bridge
  CTF_AGENT_TOOLS         JSON list of {name, description, inputSchema}
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request

BRIDGE_URL = os.environ.get("CTF_AGENT_BRIDGE_URL", "")
BRIDGE_TOKEN = os.environ.get("CTF_AGENT_BRIDGE_TOKEN", "")
TOOLS = json.loads(os.environ.get("CTF_AGENT_TOOLS", "[]"))
DEFAULT_PROTOCOL = "2025-06-18"

_write_lock = threading.Lock()


def _send(message: dict) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def _call_bridge(name: str, arguments: dict) -> dict:
    request = urllib.request.Request(
        BRIDGE_URL,
        data=json.dumps({"tool": name, "args": arguments}).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {BRIDGE_TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=None) as response:  # noqa: S310 (local bridge)
            out = json.loads(response.read())
    except urllib.error.HTTPError as e:
        return {"content": [{"type": "text", "text": f"ctf-agent bridge error {e.code}: {e.read()[:500]!r}"}], "isError": True}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"ctf-agent bridge error: {e}"}], "isError": True}
    if out.get("image"):
        return {"content": [{"type": "image", "data": out["image"], "mimeType": out.get("mimeType", "image/png")}]}
    return {"content": [{"type": "text", "text": out.get("text", "")}]}


def _handle_call(msg_id: object, params: dict) -> None:
    arguments = params.get("arguments")
    result = _call_bridge(str(params.get("name", "")), arguments if isinstance(arguments, dict) else {})
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def main() -> None:
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if msg_id is None:
            continue  # notifications (initialized, cancelled) need no answer
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ctf", "version": "1.0"},
            }})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
                {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                for t in TOOLS
            ]}})
        elif method == "tools/call":
            # Sandbox commands can run for minutes: answer from a thread so calls overlap
            threading.Thread(target=_handle_call, args=(msg_id, params), daemon=True).start()
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        else:
            _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})


if __name__ == "__main__":
    main()
