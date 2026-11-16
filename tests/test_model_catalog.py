import json
from io import BytesIO
from subprocess import CompletedProcess

from backend import model_catalog

CLAUDE_HELP = """
  --effort <level>  Effort level for the current session
                    (low, medium, high, xhigh, max)
  --model <model>   Model for the current session. Provide an alias for the
                    latest model (e.g. 'fable', 'opus', or 'sonnet') or a
                    model's full name.
"""


def test_discover_claude_models_reads_aliases_and_efforts(monkeypatch):
    monkeypatch.setattr(model_catalog, "resolve_cli", lambda command, setting: command)
    monkeypatch.setattr(
        model_catalog.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, CLAUDE_HELP, ""),
    )

    aliases, efforts = model_catalog.discover_claude_models("claude-wrapper")

    assert aliases == ["fable", "opus", "sonnet"]
    assert efforts == ["low", "medium", "high", "xhigh", "max"]


def test_discover_claude_models_falls_back_from_wrapper_help(monkeypatch):
    calls = []

    def fake_resolve(command, setting):
        return f"/usr/bin/{command}"

    def fake_run(args, **kwargs):
        calls.append(args)
        stdout = "ourclaude wrapper help" if args[0].endswith("ourclaude") else CLAUDE_HELP
        return CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(model_catalog, "resolve_cli", fake_resolve)
    monkeypatch.setattr(model_catalog.subprocess, "run", fake_run)

    aliases, efforts = model_catalog.discover_claude_models("ourclaude")

    assert aliases == ["fable", "opus", "sonnet"]
    assert efforts == ["low", "medium", "high", "xhigh", "max"]
    assert calls == [["/usr/bin/ourclaude", "--help"], ["/usr/bin/claude", "--help"]]


def test_discover_claude_versioned_models_reads_local_catalog(tmp_path):
    catalog_dir = tmp_path / "cache" / "model-catalog"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "published-floor.json").write_text(
        json.dumps({"version": 1, "sources": []}), encoding="utf-8"
    )
    (catalog_dir / "account-cc.json").write_text(
        json.dumps(
            {
                "catalog": {
                    "config": {
                        "models": [
                            {
                                "id": "claude-opus-5-5",
                                "runtime": {
                                    "effort_levels": ["low", "medium", "high"],
                                    "default_effort": "medium",
                                },
                            },
                            {"id": "claude-haiku-4-5-20251001", "runtime": {}},
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    models = model_catalog.discover_claude_versioned_models(tmp_path)

    assert [model["id"] for model in models] == [
        "claude-opus-5-5",
        "claude-haiku-4-5-20251001",
    ]


def test_discover_claude_gateway_models_matches_configured_proxy(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (tmp_path / "proxy.json").write_text(
        json.dumps({"server_url": "https://proxy.example/", "token": "secret"}), encoding="utf-8"
    )
    (cache_dir / "gateway-models.json").write_text(
        json.dumps({"baseUrl": "https://proxy.example/proxy", "models": [
            {"id": "claude-mythos-5-2", "display_name": "Claude Mythos 5.2"},
            {"display_name": "invalid"},
        ]}), encoding="utf-8"
    )

    assert [model["id"] for model in model_catalog.discover_claude_gateway_models(tmp_path, live=False)] == [
        "claude-mythos-5-2"
    ]

    (cache_dir / "gateway-models.json").write_text(
        json.dumps({"baseUrl": "https://other.example/proxy", "models": []}), encoding="utf-8"
    )
    try:
        model_catalog.discover_claude_gateway_models(tmp_path, live=False)
    except RuntimeError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("expected wrong gateway cache to be rejected")


def test_discover_claude_gateway_models_prefers_live_list(tmp_path, monkeypatch):
    (tmp_path / "proxy.json").write_text(
        json.dumps({"server_url": "https://proxy.example", "token": "test-token"}), encoding="utf-8"
    )
    requested = []

    class FakeOpener:
        def open(self, request, timeout):
            requested.append((request.full_url, timeout))
            return BytesIO(b'{"data":[{"id":"claude-mythos-5-2"}]}')

    monkeypatch.setattr(model_catalog.urllib.request, "build_opener", lambda *args: FakeOpener())

    models = model_catalog.discover_claude_gateway_models(tmp_path)

    assert [model["id"] for model in models] == ["claude-mythos-5-2"]
    assert requested == [("https://proxy.example/proxy/v1/models?limit=1000", 5)]


def test_render_available_models_includes_claude_specs(monkeypatch):
    monkeypatch.setattr(
        model_catalog,
        "discover_claude_models",
        lambda command: (["opus", "sonnet"], ["medium", "high"]),
    )
    monkeypatch.setattr(
        model_catalog,
        "discover_claude_versioned_models",
        lambda: [
            {
                "id": "claude-opus-5-5",
                "runtime": {
                    "effort_levels": ["low", "medium", "high"],
                    "default_effort": "medium",
                },
            },
            {"id": "claude-haiku-4-5-20251001", "runtime": {}},
        ],
    )
    monkeypatch.setattr(model_catalog, "discover_codex_models", lambda command: _empty_models())

    output = model_catalog.render_available_models(model_catalog.Settings())

    assert "Claude models advertised by the local CLI:" in output
    assert "claude-sdk/opus [medium, high]" in output
    assert "claude-sdk/sonnet [medium, high]" in output
    assert "Versioned Claude models in the local Claude Code catalog:" in output
    assert "claude-sdk/claude-opus-5-5 [low, medium (default), high]" in output
    assert "claude-sdk/claude-haiku-4-5-20251001" in output


def test_render_available_models_includes_ourclaude_gateway_models(monkeypatch):
    monkeypatch.setattr(model_catalog, "resolve_cli", lambda command, setting: "/usr/local/bin/ourclaude")
    monkeypatch.setattr(model_catalog, "discover_claude_models", lambda command: (["opus"], []))
    monkeypatch.setattr(model_catalog, "discover_claude_versioned_models", lambda: [{"id": "claude-opus-5-5"}])
    monkeypatch.setattr(model_catalog, "discover_claude_gateway_models", lambda: [
        {"id": "claude-opus-5-5"}, {"id": "claude-mythos-5-2"}
    ])
    monkeypatch.setattr(model_catalog, "discover_codex_models", lambda command: _empty_models())

    output = model_catalog.render_available_models(model_catalog.Settings())

    assert "OurClaude gateway models:" in output
    assert "claude-sdk/claude-mythos-5-2" in output
    assert output.count("claude-sdk/claude-opus-5-5") == 1


async def _empty_models():
    return []
