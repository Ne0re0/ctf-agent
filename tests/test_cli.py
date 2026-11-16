from click.testing import CliRunner

from backend import cli


def test_bare_models_prints_catalog_without_starting_solver(monkeypatch):
    seen = {}

    def fake_catalog(settings):
        seen["claude_cli"] = settings.claude_cli
        seen["codex_cli"] = settings.codex_cli
        return "MODEL CATALOG"

    monkeypatch.setattr(cli, "render_available_models", fake_catalog)

    result = CliRunner().invoke(
        cli.main,
        [
            "--models",
            "--claude-cli",
            "/tmp/custom-claude",
            "--codex-cli",
            "/tmp/custom-codex",
        ],
    )

    assert result.exit_code == 0
    assert result.output == "MODEL CATALOG\n"
    assert seen == {
        "claude_cli": "/tmp/custom-claude",
        "codex_cli": "/tmp/custom-codex",
    }


def test_empty_models_assignment_prints_catalog(monkeypatch):
    monkeypatch.setattr(cli, "render_available_models", lambda settings: "MODEL CATALOG")

    result = CliRunner().invoke(cli.main, ["--models="])

    assert result.exit_code == 0
    assert result.output == "MODEL CATALOG\n"


def test_valued_models_remains_repeatable(monkeypatch):
    captured = {}

    async def fake_run_single(
        settings,
        challenge,
        model_specs,
        no_submit,
        max_challenges,
        msg_port,
        benchmark,
        writeup,
        **kwargs,
    ):
        captured["models"] = model_specs
        captured["benchmark"] = benchmark
        captured["writeup"] = writeup

    monkeypatch.setattr(cli, "_run_single", fake_run_single)

    result = CliRunner().invoke(
        cli.main,
        [
            "--challenge",
            "unused",
            "--models",
            "google/gemini-3-flash-preview",
            "--models",
            "azure/gpt-5.4",
        ],
    )

    assert result.exit_code == 0
    assert captured["models"] == [
        "google/gemini-3-flash-preview",
        "azure/gpt-5.4",
    ]
    assert captured["benchmark"] is False
    assert captured["writeup"] is False


def test_benchmark_is_forwarded_to_single_challenge(monkeypatch):
    captured = {}

    async def fake_run_single(
        settings,
        challenge,
        model_specs,
        no_submit,
        max_challenges,
        msg_port,
        benchmark,
        writeup,
        **kwargs,
    ):
        captured["challenge"] = challenge
        captured["models"] = model_specs
        captured["benchmark"] = benchmark
        captured["writeup"] = writeup

    monkeypatch.setattr(cli, "_run_single", fake_run_single)

    result = CliRunner().invoke(
        cli.main,
        [
            "--challenge",
            "benchmark-challenge",
            "--benchmark",
            "--models",
            "google/gemini-3-flash-preview",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "challenge": "benchmark-challenge",
        "models": ["google/gemini-3-flash-preview"],
        "benchmark": True,
        "writeup": False,
    }


def test_benchmark_requires_single_challenge():
    result = CliRunner().invoke(cli.main, ["--benchmark"])

    assert result.exit_code == 2
    assert "--benchmark requires --challenge" in result.output


def test_writeup_is_forwarded_to_single_challenge(monkeypatch):
    captured = {}

    async def fake_run_single(
        settings,
        challenge,
        model_specs,
        no_submit,
        max_challenges,
        msg_port,
        benchmark,
        writeup,
        **kwargs,
    ):
        captured["challenge"] = challenge
        captured["writeup"] = writeup

    monkeypatch.setattr(cli, "_run_single", fake_run_single)

    result = CliRunner().invoke(
        cli.main,
        [
            "--challenge",
            "writeup-challenge",
            "--writeup",
            "--models",
            "google/gemini-3-flash-preview",
        ],
    )

    assert result.exit_code == 0
    assert captured == {"challenge": "writeup-challenge", "writeup": True}


def test_writeup_cannot_be_combined_with_benchmark():
    result = CliRunner().invoke(
        cli.main,
        ["--challenge", "unused", "--benchmark", "--writeup"],
    )

    assert result.exit_code == 2
    assert "--writeup cannot be combined with --benchmark" in result.output
