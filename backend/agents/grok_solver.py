"""Grok solver — drives the Grok Build CLI (`grok -p`) headless, one run per turn.

grok runs with every host-side built-in tool removed (--disallowed-tools, plus deny
rules as a backstop). The sandbox tools come from a stdio MCP server
(grok_mcp_bridge.py) declared in a throwaway project config, .grok/config.toml in
an empty working directory. It forwards each call to the ToolBridge in this
process, which runs it in the challenge's Docker container. The model reaches MCP
tools through grok's search_tool / use_tool meta-tools, as `ctf__<tool>`.

Each turn is `grok -p ... --output-format streaming-messages-json`, which emits
Messages-API-shaped lines (system/init, assistant, user, result). Later turns
resume the same session with `-r <session_id>`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from backend.agents.codex_solver import SANDBOX_TOOLS
from backend.agents.omp_solver import final_flag_json
from backend.agents.tool_bridge import ToolBridge, run_bridge_tool
from backend.config import resolve_cli
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.loop_detect import LoopDetector
from backend.models import effort_from_spec, model_id_from_spec
from backend.prompts import ChallengeMeta, build_prompt, list_distfiles
from backend.sandbox import DockerSandbox
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    POLICY_BLOCKED,
    QUOTA_ERROR,
    SolverResult,
    is_policy_block,
)
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)

MCP_SERVER_SCRIPT = Path(__file__).with_name("grok_mcp_bridge.py")
MCP_SERVER_NAME = "ctf"

# Built-in tools that touch the host (shell, files, subagents, schedulers, media).
# Ids and model-facing names both work; unknown entries only warn on stderr.
DISALLOWED_TOOLS = (
    "run_terminal_cmd", "run_terminal_command", "read_file", "search_replace", "write", "list_dir",
    "grep", "kill_task", "get_task_output", "wait_tasks", "todo_write", "scheduler_create",
    "scheduler_delete", "scheduler_list", "monitor", "workflow", "enter_plan_mode", "exit_plan_mode",
    "ask_user_question", "send_feedback", "image_gen", "image_edit", "image_to_video",
    "reference_to_video", "task", "Agent", "lsp",
)
# Backstop if a future grok adds a host tool under a new name: deny by permission rule
DENY_RULES = "Bash,Edit,Write,Read,Grep"
# Tools grok may keep besides our ctf__* tools: MCP meta-tools and network-only web tools
ALLOWED_TOOLS = frozenset({"search_tool", "use_tool", "web_search", "web_fetch"})

_QUOTA_MARKERS = ("quota", "rate limit", "rate_limit", "429", "usage limit", "limit reached", "capacity", "overloaded")

# grok prints whole messages per line
_STDOUT_LIMIT = 256 * 1024 * 1024


def _toml_str(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


class GrokSolver:
    """Grok Build CLI solver: your grok login, ctf-agent's sandbox tools over MCP."""

    def __init__(
        self,
        model_spec: str,
        challenge_dir: str,
        meta: ChallengeMeta,
        ctfd: CTFdClient,
        cost_tracker: CostTracker,
        settings: object,
        cancel_event: asyncio.Event | None = None,
        no_submit: bool = False,
        submit_fn=None,
        message_bus=None,
        notify_coordinator=None,
    ) -> None:
        self.model_spec = model_spec
        self.model_id = model_id_from_spec(model_spec)
        self.effort = effort_from_spec(model_spec)
        self.challenge_dir = challenge_dir
        self.meta = meta
        self.ctfd = ctfd
        self.cost_tracker = cost_tracker
        self.settings = settings
        self.cancel_event = cancel_event or asyncio.Event()
        self.no_submit = no_submit
        self.submit_fn = submit_fn
        self.message_bus = message_bus
        self.notify_coordinator = notify_coordinator

        self.sandbox = DockerSandbox(
            image=getattr(settings, "sandbox_image", "ctf-sandbox"),
            challenge_dir=challenge_dir,
            memory_limit=getattr(settings, "container_memory_limit", "4g"),
            challenge_name=meta.name,
            model_name=model_spec,
        )
        # grok models take images (grok-4 family); view_image falls back to text otherwise
        self.use_vision = True
        self.loop_detector = LoopDetector()
        label = f"{self.model_id}-{self.effort}" if self.effort else self.model_id
        self.tracer = SolverTracer(meta.name, f"grok-{label}")
        self.agent_name = f"{meta.name}/grok-{label}"

        self._command = ""
        self._system_prompt = ""
        self._bridge: ToolBridge | None = None
        self._workdir: tempfile.TemporaryDirectory | None = None
        self._session_id: str | None = None
        self._step_count = 0
        self._flag: str | None = None
        self._confirmed = False
        self._findings = ""
        self._cost_usd = 0.0
        self._bump_insights: str | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._command = resolve_cli(getattr(self.settings, "grok_cli", "grok"), "GROK_CLI")
        await self.sandbox.start()

        arch_result = await self.sandbox.exec("uname -m", timeout_s=10)
        container_arch = arch_result.stdout.strip() or "unknown"

        mcp_names = ", ".join(f"{MCP_SERVER_NAME}__{t['name']}" for t in SANDBOX_TOOLS)
        sandbox_preamble = (
            "IMPORTANT: You are running inside a Docker sandbox. "
            "All files are under /challenge/ — distfiles at /challenge/distfiles/, "
            "workspace at /challenge/workspace/. Do NOT use any paths outside /challenge/.\n"
            "Your sandbox tools are MCP tools; call them with use_tool, e.g. "
            f'use_tool(tool_name="{MCP_SERVER_NAME}__bash", tool_input={{"command": "ls -la /challenge"}}). '
            f"Available: {mcp_names}. Use these for ALL operations: there is no other shell or file access. "
            f"`bash` means {MCP_SERVER_NAME}__bash, `submit_flag` means {MCP_SERVER_NAME}__submit_flag, and so on.\n"
            "A flag only counts once you submit it with submit_flag. After that, end your final answer "
            'with a JSON object: {"type": "flag_found", "flag": "<flag>", "method": "<how>"}.\n\n'
        )
        self._system_prompt = sandbox_preamble + build_prompt(
            self.meta, list_distfiles(self.challenge_dir), container_arch=container_arch,
            has_named_tools=True,
        )

        self._bridge = ToolBridge(lambda name, args: run_bridge_tool(self, name, args), self.agent_name)
        bridge_url = await self._bridge.start()

        # Empty working directory whose project config declares only our MCP server
        self._workdir = tempfile.TemporaryDirectory(prefix="ctf-agent-grok-")
        server_env = {
            "CTF_AGENT_BRIDGE_URL": bridge_url,
            "CTF_AGENT_BRIDGE_TOKEN": self._bridge.token,
            "CTF_AGENT_TOOLS": json.dumps(SANDBOX_TOOLS),
        }
        config_dir = Path(self._workdir.name) / ".grok"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(
            f"[mcp_servers.{MCP_SERVER_NAME}]\n"
            f"command = {_toml_str(sys.executable)}\n"
            f"args = [{_toml_str(str(MCP_SERVER_SCRIPT))}]\n"
            "env = { " + ", ".join(f"{k} = {_toml_str(v)}" for k, v in server_env.items()) + " }\n"
            "startup_timeout_sec = 60\n"
        )
        (config_dir / "config.toml").chmod(0o600)  # holds the bridge token

        self.tracer.event("start", challenge=self.meta.name, model=self.model_id, effort=self.effort)
        logger.info(f"[{self.agent_name}] Grok solver started (model {self.model_id})")

    async def stop(self) -> None:
        self.tracer.event("stop", step_count=self._step_count)
        self.tracer.close()
        if self._bridge:
            self._bridge.close()
            self._bridge = None
        if self._workdir:
            self._workdir.cleanup()
            self._workdir = None
        if self.sandbox:
            await self.sandbox.stop()

    # ── one headless run ─────────────────────────────────────────────────

    def _args(self, prompt: str) -> list[str]:
        assert self._workdir
        args = [
            self._command, "-p", prompt,
            "--output-format", "streaming-messages-json",
            "--cwd", self._workdir.name,
            "--system-prompt-override", self._system_prompt,
            "--disallowed-tools", ",".join(DISALLOWED_TOOLS),
            "--deny", DENY_RULES,
            "--always-approve",
            "--no-subagents", "--no-plan",
        ]
        if self.model_id:
            args += ["--model", self.model_id]
        if self.effort:
            args += ["--effort", self.effort]
        if self._session_id:
            args += ["--resume", self._session_id]
        return args

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if not self._workdir:
            await self.start()
        assert self._workdir

        t0 = time.monotonic()
        cost_before = self._cost_usd
        steps_before = self._step_count
        if self._bump_insights:
            prompt = (
                "Your previous attempt did not find the flag. "
                f"Insights from other agents:\n\n{self._bump_insights}\n\n"
                "Try a different approach. Do NOT repeat what was tried."
            )
            self._bump_insights = None
        elif self._session_id:
            prompt = "Continue solving. Try a different approach."
        else:
            prompt = "Solve this CTF challenge."

        stderr_path = f"{self.tracer.path}.grok-stderr.log"
        error: str | None = None
        final_text: str | None = None
        policy_blocked = False
        proc: asyncio.subprocess.Process | None = None
        try:
            with open(stderr_path, "ab") as stderr_file:
                proc = await asyncio.create_subprocess_exec(
                    *self._args(prompt),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr_file,
                    cwd=self._workdir.name,
                    # The working directory is ours and empty: trust it for its MCP config
                    # without recording a grant in ~/.grok/trusted_folders.toml
                    env={**os.environ, "GROK_FOLDER_TRUST": "0"},
                    limit=_STDOUT_LIMIT,
                    start_new_session=True,
                )
                reader = asyncio.create_task(self._read_stream(proc))
                cancelled = asyncio.create_task(self.cancel_event.wait())
                try:
                    await asyncio.wait({reader, cancelled}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    cancelled.cancel()
                if not reader.done():
                    await self._terminate(proc)
                    reader.cancel()
                    return self._result(CANCELLED)
                error, final_text, policy_blocked = reader.result()
                await proc.wait()
                if proc.returncode and not error:
                    error = f"grok exited with code {proc.returncode} (see {stderr_path})"
        except asyncio.CancelledError:
            if proc:
                await self._terminate(proc)
            return self._result(CANCELLED)
        except Exception as e:
            logger.error(f"[{self.agent_name}] Error: {e}", exc_info=True)
            error = str(e)

        self.tracer.event("turn_complete", duration=round(time.monotonic() - t0, 1), cost=round(self._cost_usd, 4))

        if not self._confirmed and (found := final_flag_json(final_text or "")):
            # Only submit_flag confirms a flag: a reported one may be a guess
            self._findings = f"Reported flag (not submitted): {found['flag']} via {found.get('method', '?')}"
        if self._confirmed and self._flag:
            return self._result(FLAG_FOUND)

        run_steps = self._step_count - steps_before
        run_cost = self._cost_usd - cost_before
        if error:
            logger.error(f"[{self.agent_name}] Grok error: {error}")
            self._findings = f"Error: {error}"
            self.tracer.event("error", error=error)
            err = error.lower()
            if policy_blocked or is_policy_block(err):
                return self._result(POLICY_BLOCKED, run_steps, run_cost)
            if any(m in err for m in _QUOTA_MARKERS):
                return self._result(QUOTA_ERROR, run_steps, run_cost)
            return self._result(ERROR, run_steps, run_cost)
        if policy_blocked:
            return self._result(POLICY_BLOCKED, run_steps, run_cost)
        return self._result(GAVE_UP, run_steps, run_cost)

    async def _read_stream(self, proc: asyncio.subprocess.Process) -> tuple[str | None, str | None, bool]:
        """Consume one run's output. Returns (error, final answer text, refused)."""
        assert proc.stdout
        error: str | None = None
        final_text: str | None = None
        refused = False
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "system" and msg.get("subtype") == "init":
                self._note_session(msg.get("session_id"))
                unexpected = {
                    tool for tool in msg.get("tools") or []
                    if tool not in ALLOWED_TOOLS and not tool.startswith(f"{MCP_SERVER_NAME}__")
                }
                if unexpected:
                    # Fail closed: a host-side tool we did not remove would escape the sandbox
                    await self._terminate(proc)
                    return (
                        f"grok offered host tools that ctf-agent does not remove: {sorted(unexpected)}. "
                        "Add them to DISALLOWED_TOOLS in backend/agents/grok_solver.py.",
                        None, False,
                    )
            elif kind == "assistant":
                for block in (msg.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        self._findings = block["text"][:2000]
                        self.tracer.model_response(block["text"], self._step_count)
                if (msg.get("message") or {}).get("stop_reason") == "refusal":
                    refused = True
            elif kind == "result":
                self._note_session(msg.get("session_id"))
                self._record_result_usage(msg)
                final_text = msg.get("result") if isinstance(msg.get("result"), str) else None
                if msg.get("stop_reason") == "refusal":
                    refused = True
                if msg.get("is_error"):
                    errors = msg.get("errors") or []
                    error = "; ".join(str(e) for e in errors) or str(msg.get("result") or msg.get("subtype"))
                    if msg.get("stop_reason") == "cancelled":
                        error = None  # we stopped it
        return error, final_text, refused

    def _record_result_usage(self, msg: dict) -> None:
        """Record one run's spend. result.usage buckets are disjoint (input is uncached)."""
        usage = msg.get("usage") or {}
        read = usage.get("cache_read_input_tokens") or 0
        write = usage.get("cache_creation_input_tokens") or 0
        uncached = usage.get("input_tokens") or 0
        output = usage.get("output_tokens") or 0
        cost = msg.get("total_cost_usd")
        if not (uncached or read or write or output or cost):
            return
        self.cost_tracker.record_tokens(
            self.agent_name, self.model_id,
            input_tokens=uncached + read + write,
            output_tokens=output,
            cache_read_tokens=read,
            cache_write_tokens=write,
            provider_spec="grok",
            # grok's own price when it reports a complete one, else genai-prices
            cost_usd=float(cost) if cost is not None else None,
        )
        agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
        self._cost_usd = agent_usage.cost_usd if agent_usage else 0.0
        self.tracer.usage(uncached + read + write, output, read, self._cost_usd)

    def _note_session(self, session_id: str | None) -> None:
        """Remember grok's session id so later turns resume it. `grok -r <id>` from
        the same working directory reopens it (the directory is removed on stop)."""
        if session_id and session_id != self._session_id:
            logger.info(f"[{self.agent_name}] Grok session {session_id}")
            self.tracer.event("session", session_id=session_id)
            self._session_id = session_id

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        """SIGTERM grok's process group (it and its MCP server), SIGKILL if it lingers."""
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            await asyncio.wait_for(proc.wait(), timeout=15)
        except (ProcessLookupError, PermissionError):
            pass
        except TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    # ── solver protocol ──────────────────────────────────────────────────

    async def generate_writeup(self, prompt: str) -> str:
        """Resume the winning grok session and return its Markdown writeup."""
        if not self._workdir:
            await self.start()
        assert self._workdir
        if not self._session_id:
            raise RuntimeError("grok session ID is unavailable for writeup generation")
        self.loop_detector.reset()

        stderr_path = f"{self.tracer.path}.grok-stderr.log"
        proc: asyncio.subprocess.Process | None = None
        try:
            with open(stderr_path, "ab") as stderr_file:
                proc = await asyncio.create_subprocess_exec(
                    *self._args(prompt),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr_file,
                    cwd=self._workdir.name,
                    env={**os.environ, "GROK_FOLDER_TRUST": "0"},
                    limit=_STDOUT_LIMIT,
                    start_new_session=True,
                )
                error, final_text, _ = await self._read_stream(proc)
                await proc.wait()
        except asyncio.CancelledError:
            if proc:
                await self._terminate(proc)
            raise

        if error:
            raise RuntimeError(error)
        writeup = (final_text or "").strip()
        if not writeup:
            raise RuntimeError("grok returned an empty writeup")
        return writeup

    def bump(self, insights: str) -> None:
        self._bump_insights = insights
        self.loop_detector.reset()
        self.tracer.event("bump", insights=insights[:500])

    def _result(self, status: str, run_steps: int | None = None, run_cost: float | None = None) -> SolverResult:
        self.tracer.event("finish", status=status, flag=self._flag, confirmed=self._confirmed, cost_usd=round(self._cost_usd, 4))
        return SolverResult(
            flag=self._flag, status=status,
            findings_summary=self._findings[:2000],
            step_count=run_steps if run_steps is not None else self._step_count,
            cost_usd=run_cost if run_cost is not None else self._cost_usd,
            log_path=self.tracer.path,
        )
