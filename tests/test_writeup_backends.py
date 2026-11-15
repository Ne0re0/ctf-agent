import asyncio
from types import SimpleNamespace

from claude_agent_sdk import ClaudeAgentOptions
from pydantic_ai.usage import RunUsage

import backend.agents.claude_solver as claude_module
from backend.agents.claude_solver import ClaudeSolver
from backend.agents.codex_solver import CodexSolver
from backend.agents.solver import Solver
from backend.cost_tracker import CostTracker


class RecordingTracer:
    def __init__(self):
        self.responses = []
        self.usages = []

    def model_response(self, text, step, **kwargs):
        self.responses.append((text, step))

    def usage(self, input_tokens, output_tokens, cache_read, cost_usd):
        self.usages.append((input_tokens, output_tokens, cache_read, cost_usd))

    def event(self, event_type, **data):
        pass


def test_pydantic_writeup_uses_existing_history_and_plain_text_output():
    calls = []

    class FakeAgent:
        async def run(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return SimpleNamespace(
                output="# Pydantic writeup",
                usage=RunUsage(),
                all_messages=lambda: ["updated-history"],
                new_messages=lambda: [],
            )

    solver = object.__new__(Solver)
    solver._agent = FakeAgent()
    solver._messages = ["solving-history"]
    solver.deps = object()
    solver.cost_tracker = CostTracker()
    solver.agent_name = "challenge/model"
    solver.model_id = "gemini-3-flash-preview"
    solver.model_spec = "google/gemini-3-flash-preview"
    solver._step_count = [4]
    solver.tracer = RecordingTracer()
    solver.loop_detector = SimpleNamespace(reset=lambda: None)

    writeup = asyncio.run(solver.generate_writeup("Write it up"))

    assert writeup == "# Pydantic writeup"
    assert calls[0][1]["message_history"] == ["solving-history"]
    assert calls[0][1]["output_type"] is str
    assert solver._messages == ["updated-history"]


def test_claude_writeup_resumes_session_with_writeup_schema(monkeypatch):
    created_clients = []

    class FakeResultMessage:
        is_error = False
        result = ""
        usage = None
        total_cost_usd = None
        session_id = "session-123"
        structured_output = {
            "type": "writeup",
            "writeup": "# Claude writeup",
        }

    class OldClient:
        def __init__(self):
            self.options = ClaudeAgentOptions(
                output_format={"type": "json_schema", "schema": {"title": "solver"}}
            )
            self.closed = False

        async def __aexit__(self, *args):
            self.closed = True

    class NewClient:
        def __init__(self, options):
            self.options = options
            self.prompt = None
            created_clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def query(self, prompt):
            self.prompt = prompt

        async def receive_response(self):
            yield FakeResultMessage()

    monkeypatch.setattr(claude_module, "ClaudeSDKClient", NewClient)
    monkeypatch.setattr(claude_module, "ResultMessage", FakeResultMessage)

    old_client = OldClient()
    solver = object.__new__(ClaudeSolver)
    solver._client = old_client
    solver._session_id = "session-123"
    solver._turn_usage = (0, 0, 0, 0)
    solver._counted_msg_ids = set()
    solver.cost_tracker = CostTracker()
    solver.agent_name = "challenge/claude"
    solver.model_id = "claude-opus"
    solver.tracer = RecordingTracer()
    solver._step_count = 5
    solver.loop_detector = SimpleNamespace(reset=lambda: None)

    writeup = asyncio.run(solver.generate_writeup("Write it up"))

    assert writeup == "# Claude writeup"
    assert old_client.closed is True
    assert created_clients[0].options.resume == "session-123"
    schema = created_clients[0].options.output_format["schema"]
    assert schema["properties"]["type"]["enum"] == ["writeup"]
    assert created_clients[0].prompt == "Write it up"


def test_codex_writeup_continues_thread_with_writeup_schema():
    requests = []
    solver = object.__new__(CodexSolver)
    solver._proc = object()
    solver._thread_id = "thread-123"
    solver._turn_done = asyncio.Event()
    solver._structured_output = None
    solver._turn_error = None
    solver.loop_detector = SimpleNamespace(reset=lambda: None)

    async def fake_rpc(method, params):
        requests.append((method, params))
        solver._structured_output = {
            "type": "writeup",
            "writeup": "# Codex writeup",
        }
        solver._turn_done.set()
        return {}

    solver._rpc = fake_rpc

    async def run():
        return await solver.generate_writeup("Write it up")

    writeup = asyncio.run(run())

    assert writeup == "# Codex writeup"
    method, params = requests[0]
    assert method == "turn/start"
    assert params["threadId"] == "thread-123"
    assert params["input"][0]["text"] == "Write it up"
    assert params["outputSchema"]["properties"]["type"]["enum"] == ["writeup"]
