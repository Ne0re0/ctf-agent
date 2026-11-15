import asyncio

import pytest

from backend.agents.coordinator_core import do_spawn_swarm
from backend.agents.swarm import ChallengeSwarm
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.deps import CoordinatorDeps
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND, SolverResult


def _solved_result() -> SolverResult:
    return SolverResult(
        flag="CTF{winner}",
        status=FLAG_FOUND,
        findings_summary="Solved with a test exploit",
        step_count=3,
        cost_usd=0.01,
        log_path="trace.jsonl",
    )


class RecordingTracer:
    def __init__(self):
        self.events = []

    def event(self, event_type, **data):
        self.events.append((event_type, data))


class FakeSolver:
    def __init__(self, model_spec, result, timeline, *, delay=0, writeup=None):
        self.model_spec = model_spec
        self.agent_name = model_spec
        self.result = result
        self.timeline = timeline
        self.delay = delay
        self.writeup = writeup
        self.sandbox = object()
        self.tracer = RecordingTracer()
        self.stopped = False
        self.writeup_prompts = []

    async def start(self):
        self.timeline.append(f"start:{self.model_spec}")

    async def run_until_done_or_gave_up(self):
        if self.delay is None:
            await asyncio.Event().wait()
        await asyncio.sleep(self.delay or 0)
        return self.result

    async def generate_writeup(self, prompt):
        self.timeline.append(f"writeup:{self.model_spec}")
        self.writeup_prompts.append(prompt)
        if self.writeup is None:
            await asyncio.Event().wait()
        return self.writeup

    def bump(self, insights):
        pass

    async def stop(self):
        self.timeline.append(f"stop:{self.model_spec}")
        self.stopped = True


def _swarm(tmp_path, *, timeout=1) -> ChallengeSwarm:
    return ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="Writeup Challenge"),
        ctfd=CTFdClient(),
        cost_tracker=CostTracker(),
        settings=Settings.model_construct(
            sandbox_image="ctf-sandbox",
            container_memory_limit="1g",
        ),
        model_specs=["winner", "sibling"],
        no_submit=True,
        writeup=True,
        writeup_timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_winner_writes_after_siblings_stop_then_cleans_up(monkeypatch, tmp_path):
    timeline = []
    swarm = _swarm(tmp_path)
    swarm.on_winner = lambda result: timeline.append(f"announce:{result.flag}")
    solvers = {
        "winner": FakeSolver(
            "winner",
            _solved_result(),
            timeline,
            delay=0.01,
            writeup="# Writeup\n\nDetailed solution.",
        ),
        "sibling": FakeSolver("sibling", _solved_result(), timeline, delay=None),
    }
    monkeypatch.setattr(swarm, "_create_solver", lambda model: solvers[model])

    result = await asyncio.wait_for(swarm.run(), timeout=1)

    assert result is not None and result.flag == "CTF{winner}"
    assert (tmp_path / "writeup.md").read_text() == "# Writeup\n\nDetailed solution.\n"
    assert timeline.index("announce:CTF{winner}") < timeline.index("stop:sibling")
    assert timeline.index("stop:sibling") < timeline.index("writeup:winner")
    assert timeline[-1] == "stop:winner"
    assert all(solver.stopped for solver in solvers.values())
    assert "unverified by CTFd" in solvers["winner"].writeup_prompts[0]
    assert [event[0] for event in solvers["winner"].tracer.events] == [
        "writeup_start",
        "writeup_complete",
    ]


@pytest.mark.asyncio
async def test_writeup_timeout_still_cleans_up_winner(monkeypatch, tmp_path):
    timeline = []
    swarm = _swarm(tmp_path, timeout=0.01)
    winner = FakeSolver("winner", _solved_result(), timeline, writeup=None)
    sibling = FakeSolver("sibling", _solved_result(), timeline, delay=None)
    monkeypatch.setattr(
        swarm,
        "_create_solver",
        lambda model: {"winner": winner, "sibling": sibling}[model],
    )

    result = await asyncio.wait_for(swarm.run(), timeout=1)

    assert result is not None and result.flag == "CTF{winner}"
    assert not (tmp_path / "writeup.md").exists()
    assert winner.stopped is True
    assert winner.tracer.events[-1] == ("writeup_failed", {"error": "timeout"})


@pytest.mark.asyncio
async def test_coordinator_forwards_writeup_to_spawned_swarm(monkeypatch, tmp_path):
    captured = {}

    class FakeSwarm:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.cancel_event = asyncio.Event()
            self.writeup_in_progress = False
            self.writeup_path = None

        async def run(self):
            return None

    import backend.agents.swarm as swarm_module

    monkeypatch.setattr(swarm_module, "ChallengeSwarm", FakeSwarm)
    settings = Settings.model_construct(
        sandbox_image="ctf-sandbox",
        container_memory_limit="1g",
        max_concurrent_challenges=1,
    )
    deps = CoordinatorDeps(
        ctfd=CTFdClient(),
        cost_tracker=CostTracker(),
        settings=settings,
        model_specs=["model"],
        challenges_root=str(tmp_path),
        writeup=True,
        challenge_dirs={"challenge": str(tmp_path)},
        challenge_metas={"challenge": ChallengeMeta(name="challenge")},
    )

    await do_spawn_swarm(deps, "challenge")
    await deps.swarm_tasks["challenge"]

    assert captured["writeup"] is True
