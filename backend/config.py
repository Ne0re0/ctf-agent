"""Pydantic Settings — credentials from .env files + environment variables.

Sources, lowest to highest priority: the user config file (USER_CONFIG, so an
installed `ctf-solve` works from any directory), ./.env, then real env vars.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from pydantic_settings import BaseSettings

USER_CONFIG = (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ctf-agent" / "config.env"
)


class Settings(BaseSettings):
    # CTFd
    ctfd_url: str = "http://localhost:8000"
    ctfd_user: str = "admin"
    ctfd_pass: str = "admin"
    ctfd_token: str = ""

    # API Keys
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # Provider-specific (optional, for Bedrock/Azure/Zen fallback)
    aws_region: str = "us-east-1"
    aws_bearer_token: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    opencode_zen_api_key: str = ""
    xai_api_key: str = ""  # xai/* (xAI API)

    # CLIs behind the claude-sdk/*, codex/*, grok/* and omp/* backends: a command on PATH or a path.
    # They run with your own login and config, so use whatever command works in
    # your terminal (e.g. a wrapper that sets proxy env vars, not a shell alias).
    claude_cli: str = "claude"
    codex_cli: str = "codex"
    grok_cli: str = "grok"  # grok/* (Grok Build), uses your grok login / subscription
    omp_cli: str = "omp"  # omp/* (oh-my-pi), uses omp's own logins and API keys

    # Infra
    sandbox_image: str = "ctf-sandbox"
    max_concurrent_challenges: int = 10
    max_attempts_per_challenge: int = 3
    container_memory_limit: str = "16g"

    model_config = {
        "env_file": (USER_CONFIG, ".env"),  # later file wins
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


def resolve_cli(command: str, setting: str) -> str:
    """Return the absolute path of a CLI command or path, or fail naming the setting to fix."""
    # os.path, not Path: Path("./x") normalises to "x", which would then be searched on PATH
    path = shutil.which(os.path.expanduser(command))  # noqa: PTH111
    if not path:
        raise RuntimeError(
            f"{setting}={command!r}: command not found. Set {setting} to the command or path "
            f"to use (environment variable, ./.env or {USER_CONFIG})."
        )
    return path


def claude_cli_env() -> dict[str, str]:
    """Extra environment for the `claude` processes ctf-agent starts.

    CLAUDECODE is cleared so a run started from inside Claude Code is not refused as
    a nested session. As root, the CLI refuses bypassPermissions unless IS_SANDBOX=1:
    ctf-agent's PreToolUse hooks already deny every host tool (solver Bash commands
    are rewritten to run in the Docker sandbox), so it is set there.
    """
    env = {"CLAUDECODE": ""}
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        env["IS_SANDBOX"] = "1"
    return env
