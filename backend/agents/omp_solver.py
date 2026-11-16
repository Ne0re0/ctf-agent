"""omp solver — drives oh-my-pi (`omp --mode rpc`) over JSON lines on stdio.

omp runs with its built-in tools disabled. The extension omp_bridge.ts registers the
same sandbox tools as the Codex backend (SANDBOX_TOOLS) and forwards each call to a
ToolBridge (tool_bridge.py) in this process, which runs it in the challenge's
Docker container.

RPC shapes (omp 17.x):
- stdin  {"id", "type": "prompt", "message"}  -> {"type": "response", "command": "prompt", "success", "error"?}
- stdin  {"type": "abort"}
- stdout {"type": "message_end", "message": {"role": "assistant", "content", "usage", "stopReason", "errorMessage"?}}
- stdout {"type": "agent_end"} when the prompt's run is over
- stdout {"type": "extension_ui_request", "id", "method"}: dialogs must be answered
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from backend.agents.codex_solver import SANDBOX_TOOLS
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

BRIDGE_EXTENSION = Path(__file__).with_name("omp_bridge.ts")

# extension_ui_request methods that block until the client answers
_DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})

# omp prints whole conversations in agent_end events: allow long lines
_STDOUT_LIMIT = 256 * 1024 * 1024

_QUOTA_MARKERS = ("quota", "rate limit", "rate_limit", "429", "usage limit", "capacity", "overloaded", "insufficient")

_id_counter = itertools.count(1)


def _omp_usage(u: dict) -> tuple[int, int, int, int, float | None]:
    """omp usage -> (total input, cache read, cache write, output, reported cost).

    omp's input excludes cached tokens; the cost tracker wants the total.
    """
    read = u.get("cacheRead") or 0
    write = u.get("cacheWrite") or 0
    cost = (u.get("cost") or {}).get("total")
    return (u.get("input") or 0) + read + write, read, write, u.get("output") or 0, cost


def final_flag_json(text: str) -> dict | None:
    """Find a trailing {"type": "flag_found", ...} object in the model's final answer."""
    end = text.rfind("}")
    while end != -1:
        start = text.rfind("{", 0, end)
        while start != -1:
            try:
                parsed = json.loads(text[start:end + 1])
            except ValueError:
                start = text.rfind("{", 0, start)
                continue
            if isinstance(parsed, dict) and parsed.get("type") == "flag_found" and parsed.get("flag"):
                return parsed
            break
        end = text.rfind("}", 0, end)
    return None


async def discover_omp_models(command: str) -> list[dict[str, Any]]:
    """Models omp can use with its current logins and keys (`omp models --json`)."""
    proc = await asyncio.create_subprocess_exec(
        resolve_cli(command, "OMP_CLI"), "models", "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    if proc.returncode:
        raise RuntimeError(stderr.decode(errors="replace").strip()[:300] or f"exit {proc.returncode}")
    return json.loads(stdout).get("models", [])


class OmpSolver:
    """oh-my-pi solver: omp's model and login, ctf-agent's sandbox tools."""

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
        self.thinking = effort_from_spec(model_spec)
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
        self.use_vision = False  # set from omp's model catalog in start()
        self.loop_detector = LoopDetector()
        label = self.model_id.replace("/", "_")
        if self.thinking:
            label = f"{label}-{self.thinking}"
        self.tracer = SolverTracer(meta.name, f"omp-{label}")
        self.agent_name = f"{meta.name}/omp-{label}"

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._bridge: ToolBridge | None = None
        self._workdir: tempfile.TemporaryDirectory | None = None
        self._stderr_file = None
        self._pending: dict[str, asyncio.Future] = {}
        self._turn_done = asyncio.Event()
        self._turn_error: str | None = None
        self._last_text = ""
        self._step_count = 0
        self._flag: str | None = None
        self._confirmed = False
        self._findings = ""
        self._cost_usd = 0.0
        self._prompted = False
        self._bump_insights: str | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        command = resolve_cli(getattr(self.settings, "omp_cli", "omp"), "OMP_CLI")
        await self.sandbox.start()

        arch_result = await self.sandbox.exec("uname -m", timeout_s=10)
        container_arch = arch_result.stdout.strip() or "unknown"
        await self._detect_vision(command)

        tool_names = [str(t["name"]) for t in SANDBOX_TOOLS]
        sandbox_preamble = (
            "IMPORTANT: You are running inside a Docker sandbox. "
            "All files are under /challenge/ — distfiles at /challenge/distfiles/, "
            "workspace at /challenge/workspace/. Do NOT use any paths outside /challenge/. "
            f"Your tools: {', '.join(tool_names)}. Use these for ALL operations.\n"
            "When you have the flag, submit it with submit_flag, then end your final answer "
            'with a JSON object: {"type": "flag_found", "flag": "<flag>", "method": "<how>"}.\n\n'
        )
        system_prompt = sandbox_preamble + build_prompt(
            self.meta, list_distfiles(self.challenge_dir), container_arch=container_arch,
            has_named_tools=True,
        )

        self._bridge = ToolBridge(lambda name, args: run_bridge_tool(self, name, args), self.agent_name)
        bridge_url = await self._bridge.start()

        # Empty working directory: omp must not pick up project files (AGENTS.md, ...) from ours
        self._workdir = tempfile.TemporaryDirectory(prefix="ctf-agent-omp-")
        args = [
            command, "--mode", "rpc",
            "--model", self.model_id,
            "--system-prompt", system_prompt,
            "--tools", ",".join(tool_names),
            "-e", str(BRIDGE_EXTENSION),
            "--no-extensions", "--no-skills", "--no-rules", "--no-lsp", "--no-title", "--no-pty",
            "--cwd", self._workdir.name,
        ]
        if self.thinking:
            args += ["--thinking", self.thinking]
        env = {
            **os.environ,
            "CTF_AGENT_BRIDGE_URL": bridge_url,
            "CTF_AGENT_BRIDGE_TOKEN": self._bridge.token,
            "CTF_AGENT_TOOLS": json.dumps(SANDBOX_TOOLS),
        }
        self._stderr_file = open(f"{self.tracer.path}.omp-stderr.log", "wb")  # noqa: SIM115
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self._stderr_file,
            cwd=self._workdir.name,
            env=env,
            limit=_STDOUT_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self.tracer.event("start", challenge=self.meta.name, model=self.model_id, thinking=self.thinking)
        logger.info(f"[{self.agent_name}] omp solver started (model {self.model_id})")

    async def _detect_vision(self, command: str) -> None:
        try:
            models = await discover_omp_models(command)
        except Exception as e:
            logger.debug(f"[{self.agent_name}] omp model list unavailable: {e}")
            return
        wanted = self.model_id.lower()
        for model in models:
            if wanted in (str(model.get("selector", "")).lower(), str(model.get("id", "")).lower()):
                self.use_vision = "image" in (model.get("input") or [])
                return

    async def stop(self) -> None:
        self.tracer.event("stop", step_count=self._step_count)
        self.tracer.close()
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._bridge:
            self._bridge.close()
            self._bridge = None
        if self._stderr_file:
            self._stderr_file.close()
            self._stderr_file = None
        if self._workdir:
            self._workdir.cleanup()
            self._workdir = None
        if self.sandbox:
            await self.sandbox.stop()

    # ── omp RPC ──────────────────────────────────────────────────────────

    async def _send(self, msg: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()

    async def _command(self, msg: dict, timeout: float = 120) -> dict:
        msg_id = f"ctf-{next(_id_counter)}"
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await self._send({"id": msg_id, **msg})
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(msg_id, None)

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                try:
                    await self._on_message(msg)
                except Exception as e:
                    logger.warning(f"[{self.agent_name}] Bad omp event {msg.get('type')}: {e}")
        finally:
            if not self._turn_done.is_set():
                self._turn_error = self._turn_error or f"omp exited (see {self.tracer.path}.omp-stderr.log)"
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("omp exited"))
            self._turn_done.set()

    async def _on_message(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "response":
            future = self._pending.get(msg.get("id", ""))
            if future and not future.done():
                future.set_result(msg)
        elif kind == "message_end":
            message = msg.get("message") or {}
            if message.get("role") == "assistant":
                self._on_assistant_message(message)
        elif kind == "agent_end":
            self._turn_done.set()
        elif kind == "extension_ui_request" and msg.get("method") in _DIALOG_METHODS:
            # Nobody is there to answer a dialog: decline it so omp doesn't wait forever
            await self._send({"type": "extension_ui_response", "id": msg.get("id"), "cancelled": True})

    def _on_assistant_message(self, message: dict) -> None:
        text = "".join(
            block.get("text", "") for block in message.get("content") or []
            if block.get("type") == "text"
        )
        if text:
            self._last_text = text
            self._findings = text[:2000]
            self.tracer.model_response(text, self._step_count)

        usage = message.get("usage") or {}
        input_tokens, cache_read, cache_write, output_tokens, cost = _omp_usage(usage)
        if input_tokens or output_tokens:
            self.cost_tracker.record_tokens(
                self.agent_name, self.model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                provider_spec="omp",
                # omp prices each response itself; 0 for local or unpriced models
                cost_usd=float(cost or 0.0),
            )
            agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
            self._cost_usd = agent_usage.cost_usd if agent_usage else 0.0
            self.tracer.usage(input_tokens, output_tokens, cache_read, self._cost_usd)

        if message.get("stopReason") != "error":
            self._turn_error = None  # omp retried and recovered
        else:
            error =message.get("errorMessage") or "unknown omp error"
            self._turn_error = error
            self._findings = f"Turn failed: {error}"
            logger.error(f"[{self.agent_name}] Turn failed: {error}")
            self.tracer.event("turn_failed", error=error, step=self._step_count)

    # ── solver protocol ──────────────────────────────────────────────────

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if not self._proc:
            await self.start()

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
        elif self._prompted:
            prompt = "Continue solving. Try a different approach."
        else:
            prompt = "Solve this CTF challenge."
        self._prompted = True

        self._turn_done.clear()
        self._turn_error = None
        self._last_text = ""
        try:
            response = await self._command({"type": "prompt", "message": prompt})
            if not response.get("success", False):
                self._turn_error = str(response.get("error") or "omp rejected the prompt")
                self._findings = f"Error: {self._turn_error}"
                self._turn_done.set()

            done = asyncio.create_task(self._turn_done.wait())
            cancelled = asyncio.create_task(self.cancel_event.wait())
            try:
                await asyncio.wait({done, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                done.cancel()
                cancelled.cancel()
            if not self._turn_done.is_set():
                try:
                    await self._send({"type": "abort"})
                except Exception:
                    pass
                return self._result(CANCELLED)

            self.tracer.event("turn_complete", duration=round(time.monotonic() - t0, 1), cost=round(self._cost_usd, 4))

            if not self._confirmed and (found := final_flag_json(self._last_text)):
                # Only submit_flag confirms a flag: a reported one may be a guess
                self._findings = f"Reported flag (not submitted): {found['flag']} via {found.get('method', '?')}"

            if self._confirmed and self._flag:
                return self._result(FLAG_FOUND)

            run_steps = self._step_count - steps_before
            run_cost = self._cost_usd - cost_before
            if self._turn_error:
                err = self._turn_error.lower()
                if is_policy_block(err):
                    return self._result(POLICY_BLOCKED, run_steps, run_cost)
                if any(m in err for m in _QUOTA_MARKERS):
                    return self._result(QUOTA_ERROR, run_steps, run_cost)
                return self._result(ERROR, run_steps, run_cost)
            return self._result(GAVE_UP, run_steps, run_cost)

        except asyncio.CancelledError:
            return self._result(CANCELLED)
        except Exception as e:
            logger.error(f"[{self.agent_name}] Error: {e}", exc_info=True)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=str(e))
            if is_policy_block(str(e)):
                return self._result(POLICY_BLOCKED)
            return self._result(ERROR)

    async def generate_writeup(self, prompt: str) -> str:
        """Continue the winning omp session and return its Markdown writeup."""
        if not self._proc:
            await self.start()
        self.loop_detector.reset()

        self._turn_done.clear()
        self._turn_error = None
        self._last_text = ""
        try:
            response = await self._command({"type": "prompt", "message": prompt})
            if not response.get("success", False):
                raise RuntimeError(str(response.get("error") or "omp rejected the prompt"))
            await self._turn_done.wait()
        except asyncio.CancelledError:
            try:
                await self._send({"type": "abort"})
            except Exception:
                pass
            raise

        if self._turn_error:
            raise RuntimeError(self._turn_error)
        writeup = self._last_text.strip()
        if not writeup:
            raise RuntimeError("omp returned an empty writeup")
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
