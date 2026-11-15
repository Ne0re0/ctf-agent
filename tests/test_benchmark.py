import asyncio

import pytest

from backend.agents.swarm import BENCHMARK_TIMEOUT, ChallengeSwarm
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.prompts import ChallengeMeta
from backend.solver_base import CANCELLED, FLAG_FOUND, POLICY_BLOCKED, SolverResult


def _result(status: str, flag: str | None = None) -> SolverResult:
    return SolverResult(
        flag=flag,
        status=status,
        findings_summary=f"result: {flag or status}",
        step_count=1,
        cost_usd=0.01,
        log_path="trace.jsonl",
    )


class FakeSolver:
    def __init__(
        self,
        model_spec: str,
        cancel_event: asyncio.Event,
        outcomes: list[tuple[float | None, SolverResult]],
    ) -> None:
        self.model_spec = model_spec
        self.agent_name = model_spec
        self.cancel_event = cancel_event
        self.outcomes = outcomes
        self.sandbox = object()
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def run_until_done_or_gave_up(self) -> SolverResult:
        delay, result = self.outcomes.pop(0)
        if delay is None:
            await asyncio.Event().wait()
        else:
            await asyncio.sleep(delay)
        return result

    def bump(self, insights: str) -> None:
        pass

    async def stop(self) -> None:
        self.stopped = True


def _swarm(model_specs: list[str], grace_seconds: float = 0.05) -> ChallengeSwarm:
    settings = Settings.model_construct(
        sandbox_image="ctf-sandbox",
        container_memory_limit="1g",
    )
    return ChallengeSwarm(
        challenge_dir="unused",
        meta=ChallengeMeta(name="benchmark"),
        ctfd=CTFdClient(),
        cost_tracker=CostTracker(),
        settings=settings,
        model_specs=model_specs,
        no_submit=True,
        benchmark=True,
        benchmark_grace_seconds=grace_seconds,
    )


@pytest.mark.asyncio
async def test_normal_race_still_stops_after_first_solve(monkeypatch):
    swarm = _swarm(["winner", "stalled"])
    swarm.benchmark = False
    solvers = {
        "winner": FakeSolver(
            "winner",
            swarm.cancel_event,
            [(0.01, _result(FLAG_FOUND, "ECW{ok}"))],
        ),
        "stalled": FakeSolver("stalled", swarm.cancel_event, [(None, _result(CANCELLED))]),
    }
    monkeypatch.setattr(swarm, "_create_solver", lambda model_spec: solvers[model_spec])

    winner = await asyncio.wait_for(swarm.run(), timeout=1)

    assert winner is not None
    assert winner.flag == "ECW{ok}"
    assert swarm.cancel_event.is_set()
    assert all(solver.stopped for solver in solvers.values())


@pytest.mark.asyncio
async def test_benchmark_keeps_models_running_and_times_out_remaining(monkeypatch):
    swarm = _swarm(["first", "second", "stalled"])
    solvers = {
        "first": FakeSolver("first", swarm.cancel_event, [(0.01, _result(FLAG_FOUND, "ECW{ok}"))]),
        "second": FakeSolver("second", swarm.cancel_event, [(0.03, _result(FLAG_FOUND, "ECW{ok}"))]),
        "stalled": FakeSolver("stalled", swarm.cancel_event, [(None, _result(CANCELLED))]),
    }
    monkeypatch.setattr(swarm, "_create_solver", lambda model_spec: solvers[model_spec])

    winner = await asyncio.wait_for(swarm.run(), timeout=1)

    assert winner is not None
    assert winner.flag == "ECW{ok}"
    assert swarm.benchmark_results["first"].order == 1
    assert swarm.benchmark_results["second"].order == 2
    assert swarm.benchmark_results["first"].solve_seconds is not None
    assert swarm.benchmark_results["second"].solve_seconds is not None
    assert (
        swarm.benchmark_results["first"].solve_seconds
        < swarm.benchmark_results["second"].solve_seconds
    )
    assert swarm.benchmark_results["stalled"].status == BENCHMARK_TIMEOUT
    assert swarm.findings == {}
    assert all(solver.stopped for solver in solvers.values())


@pytest.mark.asyncio
async def test_benchmark_records_safety_guard_even_after_recovery(monkeypatch):
    swarm = _swarm(["guarded"])
    swarm.POLICY_RESTART_DELAY_S = 0
    solvers = [
        FakeSolver("guarded", swarm.cancel_event, [(0, _result(POLICY_BLOCKED))]),
        FakeSolver("guarded", swarm.cancel_event, [(0, _result(FLAG_FOUND, "ECW{ok}"))]),
    ]
    monkeypatch.setattr(swarm, "_create_solver", lambda model_spec: solvers.pop(0))

    await asyncio.wait_for(swarm.run(), timeout=1)

    record = swarm.benchmark_results["guarded"]
    assert record.status == FLAG_FOUND
    assert record.order == 1
    assert record.safety_flagged is True


@pytest.mark.asyncio
async def test_benchmark_validates_later_submissions_against_confirmed_flag():
    swarm = _swarm(["first", "second"])
    swarm.no_submit = False
    swarm.confirmed_flag = "ECW{confirmed}"

    wrong_display, wrong_confirmed = await swarm.try_submit_flag("ECW{wrong}", "second")
    right_display, right_confirmed = await swarm.try_submit_flag("ECW{confirmed}", "second")

    assert wrong_confirmed is False
    assert wrong_display.startswith("INCORRECT")
    assert right_confirmed is True
    assert right_display.startswith("ALREADY SOLVED")
