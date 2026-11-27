"""A model's final answer cannot turn an unsubmitted guess into a solve."""

import asyncio
from types import SimpleNamespace
from typing import Any

from backend.agents.codex_solver import CodexSolver, exec_sandbox_tool
from backend.agents.swarm import ChallengeSwarm
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND, GAVE_UP
from backend.tools.core import do_submit_flag
from backend.tools.flag import submit_flag


class RecordingTracer:
    path = "/tmp/test-unverified-flag.jsonl"

    def __init__(self):
        self.events = []

    def event(self, kind, **kwargs):
        self.events.append((kind, kwargs))


def make_codex_solver(*, reported_flag, no_submit=True, confirmed_flag=None):
    solver: Any = object.__new__(CodexSolver)
    solver._proc = object()
    solver._thread_id = "thread-1"
    solver._turn_done = asyncio.Event()
    solver._bump_insights = None
    solver._step_count = 1
    solver._confirmed = confirmed_flag is not None
    solver._flag = confirmed_flag
    solver._findings = ""
    solver._cost_usd = 0.0
    solver.no_submit = no_submit
    solver.tracer = RecordingTracer()

    async def complete_turn(method, params=None):
        solver._structured_output = {
            "type": "flag_found",
            "flag": reported_flag,
            "method": "Read from an account description",
        }
        solver._turn_done.set()
        return {}

    solver._rpc = complete_turn
    return solver


def test_codex_dry_run_does_not_accept_unsubmitted_password_as_flag():
    async def run():
        solver = make_codex_solver(reported_flag="KENO{temporary-password}")
        return await solver.run_until_done_or_gave_up()

    result = asyncio.run(run())
    assert result.status == GAVE_UP
    assert result.flag is None
    assert "Reported flag (not submitted): KENO{temporary-password}" in result.findings_summary


def test_codex_dry_run_submit_is_unverified_candidate_and_final_answer_cannot_replace_it():
    async def run():
        solver = make_codex_solver(reported_flag="KENO{different-guess}")
        display = await exec_sandbox_tool(solver, "submit_flag", {"flag": "KENO{read-from-target}"})
        return display, await solver.run_until_done_or_gave_up(), solver

    display, result, solver = asyncio.run(run())
    assert display == 'DRY RUN — would submit "KENO{read-from-target}"'
    assert result.status == FLAG_FOUND
    assert result.flag == "KENO{read-from-target}"
    assert solver._confirmed is True


def test_codex_live_confirmed_flag_survives_different_final_answer():
    async def run():
        solver = make_codex_solver(
            reported_flag="KENO{different-guess}",
            no_submit=False,
            confirmed_flag="KENO{accepted-by-ctfd}",
        )
        return await solver.run_until_done_or_gave_up()

    result = asyncio.run(run())
    assert result.status == FLAG_FOUND
    assert result.flag == "KENO{accepted-by-ctfd}"


def test_codex_dry_run_rejects_placeholder_submission():
    async def run():
        solver = SimpleNamespace(no_submit=True, _confirmed=False, _flag=None)
        display = await exec_sandbox_tool(solver, "submit_flag", {"flag": "NOT_FOUND"})
        return display, solver

    display, solver = asyncio.run(run())
    assert "is not a flag" in display
    assert solver._confirmed is False
    assert solver._flag is None


def test_pydantic_dry_run_requires_submit_tool_and_rejects_placeholder():
    async def run():
        deps = SimpleNamespace(no_submit=True, confirmed_flag=None)
        ctx: Any = SimpleNamespace(deps=deps)
        rejected = await submit_flag(ctx, "NOT_FOUND")
        assert deps.confirmed_flag is None
        accepted = await submit_flag(ctx, " KENO{read-from-target} ")
        return rejected, accepted, deps.confirmed_flag

    rejected, accepted, flag = asyncio.run(run())
    assert "is not a flag" in rejected
    assert accepted == 'DRY RUN — would submit "KENO{read-from-target}" but --no-submit is set.'
    assert flag == "KENO{read-from-target}"


def test_ctfd_already_solved_does_not_verify_submitted_candidate():
    async def run():
        ctfd = SimpleNamespace(
            submit_flag=lambda *args: asyncio.sleep(
                0, result=SimpleNamespace(status="already_solved", display="ALREADY SOLVED")
            )
        )
        return await do_submit_flag(ctfd, "santahome", "KENO{wrong}")

    display, confirmed = asyncio.run(run())
    assert display == "ALREADY SOLVED"
    assert confirmed is False


def test_normal_swarm_rejects_different_flag_after_winner():
    async def run():
        swarm = ChallengeSwarm(
            challenge_dir="unused",
            meta=ChallengeMeta(name="santahome"),
            ctfd=CTFdClient(),
            cost_tracker=CostTracker(),
            settings=Settings.model_construct(),
            model_specs=["first", "second"],
        )
        swarm.confirmed_flag = "KENO{accepted}"
        wrong = await swarm.try_submit_flag("KENO{wrong}", "second")
        same = await swarm.try_submit_flag("KENO{accepted}", "second")
        return wrong, same

    wrong, same = asyncio.run(run())
    assert wrong[1] is False
    assert wrong[0].startswith("INCORRECT")
    assert same[1] is True
