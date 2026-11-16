import yaml

from backend.agents.coordinator_loop import build_deps
from backend.config import Settings
from backend.prompts import ChallengeMeta


def test_load_or_create_generates_minimal_metadata(tmp_path):
    challenge_dir = tmp_path / "Learning_from_errors"
    challenge_dir.mkdir()

    meta = ChallengeMeta.load_or_create(challenge_dir)

    assert meta.name == "Learning_from_errors"
    assert meta.description == "Find the flag."
    assert meta.category == ""
    assert meta.value == 0
    assert yaml.safe_load((challenge_dir / "metadata.yml").read_text()) == {
        "name": "Learning_from_errors",
        "description": "Find the flag.",
    }


def test_load_or_create_preserves_existing_metadata(tmp_path):
    challenge_dir = tmp_path / "folder-name"
    challenge_dir.mkdir()
    metadata = challenge_dir / "metadata.yml"
    metadata.write_text("name: Exact CTFd Name\ndescription: Existing description\n")

    meta = ChallengeMeta.load_or_create(challenge_dir)

    assert meta.name == "Exact CTFd Name"
    assert meta.description == "Existing description"
    assert metadata.read_text() == "name: Exact CTFd Name\ndescription: Existing description\n"


def test_coordinator_preload_generates_missing_metadata(tmp_path):
    challenge_dir = tmp_path / "local-challenge"
    challenge_dir.mkdir()
    hidden_dir = tmp_path / ".cache"
    hidden_dir.mkdir()
    settings = Settings.model_construct(
        ctfd_url="http://localhost:8000",
        ctfd_token="",
        ctfd_user="admin",
        ctfd_pass="admin",
        max_concurrent_challenges=10,
    )

    _, _, deps = build_deps(settings, challenges_root=str(tmp_path))

    assert deps.challenge_dirs == {"local-challenge": str(challenge_dir)}
    assert deps.challenge_metas["local-challenge"].description == "Find the flag."
    assert (challenge_dir / "metadata.yml").exists()
    assert not (hidden_dir / "metadata.yml").exists()


def test_plain_string_hints_reach_the_prompt(tmp_path):
    from backend.prompts import build_prompt

    path = tmp_path / "metadata.yml"
    path.write_text("name: Plug It In\nhints:\n  - No bruteforce\n  - {cost: 0, content: Look at the plugin}\n")
    meta = ChallengeMeta.from_yaml(path)
    assert meta.hints == [{"content": "No bruteforce"}, {"cost": 0, "content": "Look at the plugin"}]
    prompt = build_prompt(meta, [])
    assert "- No bruteforce" in prompt and "- Look at the plugin" in prompt

    path.write_text("name: x\nhints: No bruteforce\n")
    assert ChallengeMeta.from_yaml(path).hints == [{"content": "No bruteforce"}]
