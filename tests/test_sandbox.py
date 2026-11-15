import asyncio
import re

import backend.sandbox as sandbox_module
from backend.sandbox import DockerSandbox


class FakeContainer:
    id = "0123456789abcdef"

    def __init__(self):
        self.started = False
        self.deleted = False

    async def start(self):
        self.started = True

    async def show(self):
        return {"Id": self.id}

    async def delete(self, force=False):
        self.deleted = force


class FakeContainers:
    def __init__(self, container):
        self.container = container
        self.config = None
        self.name = None

    async def create(self, config, *, name=None):
        self.config = config
        self.name = name
        return self.container


class FakeDocker:
    def __init__(self):
        self.container = FakeContainer()
        self.containers = FakeContainers(self.container)
        self.closed = False

    async def close(self):
        self.closed = True


def test_container_name_contains_sanitized_challenge_and_model(tmp_path):
    first = DockerSandbox(
        image="ctf-sandbox",
        challenge_dir=str(tmp_path),
        challenge_name="Plug It In!",
        model_name="claude-sdk/Claude Opus 5.5/max",
    )
    second = DockerSandbox(
        image="ctf-sandbox",
        challenge_dir=str(tmp_path),
        challenge_name="Plug It In!",
        model_name="claude-sdk/Claude Opus 5.5/max",
    )

    assert first.container_name.startswith(
        "ctf-agent-plug-it-in-claude-sdk-claude-opus-5.5-max-"
    )
    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", first.container_name)
    assert first.container_name != second.container_name


def test_start_passes_generated_name_to_docker(monkeypatch, tmp_path):
    fake_docker = FakeDocker()
    monkeypatch.setattr(sandbox_module.aiodocker, "Docker", lambda: fake_docker)
    monkeypatch.setattr(sandbox_module, "_start_semaphore", None)

    sandbox = DockerSandbox(
        image="ctf-sandbox",
        challenge_dir=str(tmp_path),
        challenge_name="Web Challenge",
        model_name="codex/gpt-6.1-sol/max",
    )

    async def exercise_sandbox():
        await sandbox.start()

        assert fake_docker.containers.name == sandbox.container_name
        assert "web-challenge" in sandbox.container_name
        assert "codex-gpt-6.1-sol-max" in sandbox.container_name
        assert fake_docker.container.started is True

        await sandbox.stop()

    asyncio.run(exercise_sandbox())

    assert fake_docker.container.deleted is True
    assert fake_docker.closed is True
