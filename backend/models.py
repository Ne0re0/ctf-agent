"""Model resolution — Bedrock, Azure OpenAI, Zen, Google AI Studio, xAI."""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from pydantic_ai.models import Model
from pydantic_ai.models.bedrock import BedrockConverseModel, BedrockModelSettings
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.profiles.grok import grok_model_profile
from pydantic_ai.providers.bedrock import BedrockProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

if TYPE_CHECKING:
    from backend.config import Settings

# Default model specs — claude-sdk and codex providers use the new solver backends
# Codex with a ChatGPT login only serves the account's current lineup (see
# `codex app-server` model/list); older ids like gpt-5.4 are rejected.
DEFAULT_MODELS: list[str] = [
    "claude-sdk/claude-opus-4-6/medium",
    "claude-sdk/claude-opus-4-6/max",
    "codex/gpt-6.1-sol",
]

DEFAULT_CODEX_COORDINATOR_MODEL = "gpt-6.1-sol"

# Context window sizes (tokens)
CONTEXT_WINDOWS: dict[str, int] = {
    "us.anthropic.claude-opus-4-6-v1": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "gpt-5.4": 1_000_000,
    "gpt-5.4-mini": 400_000,
    "gpt-5.3-codex": 1_000_000,
    "gpt-5.3-codex-spark": 128_000,
    "gemini-3-flash-preview": 1_000_000,
    "grok-4-1-fast-reasoning": 2_000_000,
    "grok-code-fast-1": 256_000,
}

# xAI's OpenAI-compatible endpoint (xai/* specs)
XAI_BASE_URL = "https://api.x.ai/v1"

# Models that support vision
VISION_MODELS: set[str] = {
    "us.anthropic.claude-opus-4-6-v1",
    "claude-opus-4-6",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gemini-3-flash-preview",
    # Codex ChatGPT-account models (inputModalities ["text","image"] per `codex app-server` model/list)
    "gpt-6.1-sol",
    "gpt-6.1-terra",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
}


def resolve_model(spec: str, settings: Settings) -> Model:
    """Resolve a 'provider/model_id' spec to a Pydantic AI Model."""
    provider = provider_from_spec(spec)
    model_id = model_id_from_spec(spec)
    match provider:
        case "bedrock":
            if settings.aws_bearer_token:
                return BedrockConverseModel(
                    model_id,
                    provider=BedrockProvider(
                        api_key=settings.aws_bearer_token,
                        region_name=settings.aws_region,
                    ),
                )
            else:
                session = boto3.Session()
                client = session.client("bedrock-runtime", region_name=settings.aws_region)
                return BedrockConverseModel(
                    model_id,
                    provider=BedrockProvider(bedrock_client=client),
                )
        case "azure":
            return OpenAIChatModel(
                model_id,
                provider=OpenAIProvider(
                    base_url=settings.azure_openai_endpoint,
                    api_key=settings.azure_openai_api_key,
                ),
            )
        case "zen":
            return OpenAIChatModel(
                model_id,
                provider=OpenAIProvider(
                    base_url="https://opencode.ai/zen/v1",
                    api_key=settings.opencode_zen_api_key,
                ),
            )
        case "google":
            return GoogleModel(
                model_id,
                provider=GoogleProvider(api_key=settings.gemini_api_key),
            )
        case "xai":
            if not settings.xai_api_key:
                raise ValueError(f"{spec}: set XAI_API_KEY to use xai/* models")
            return OpenAIChatModel(
                model_id,
                provider=OpenAIProvider(base_url=XAI_BASE_URL, api_key=settings.xai_api_key),
                profile=grok_model_profile(model_id),
            )
        case "claude-sdk" | "codex" | "omp" | "grok":
            raise ValueError(
                f"Provider '{provider}' uses its own solver backend, not Pydantic AI. "
                f"resolve_model() should not be called for {spec}."
            )
        case _:
            raise ValueError(f"Unknown provider: {provider}")


def resolve_model_settings(spec: str) -> ModelSettings:
    """Get provider-specific model settings with caching enabled."""
    provider = spec.split("/", 1)[0]
    match provider:
        case "bedrock":
            return BedrockModelSettings(
                max_tokens=128_000,
                bedrock_cache_instructions=True,
                bedrock_cache_tool_definitions=True,
                bedrock_cache_messages=True,
            )
        case "azure" | "zen" | "xai":
            # Azure/Zen/xAI use OpenAI chat completions — server-side prompt caching
            # is automatic, no explicit config needed. Set max_tokens to avoid
            # reserving the full context window.
            return OpenAIChatModelSettings(
                max_tokens=128_000,
            )
        case "google":
            return GoogleModelSettings(
                max_tokens=64_000,
                google_thinking_config={
                    "thinking_level": "high",
                    "include_thoughts": True,
                },
            )
        case _:
            return ModelSettings(max_tokens=128_000)


# omp/* specs: omp's own model ids often contain a slash ("anthropic/claude-opus-4-6"),
# so everything after "omp/" is the model, minus a trailing thinking level.
OMP_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


def _omp_parts(spec: str) -> tuple[str, str | None]:
    """'omp/xai/grok-4/high' -> ('xai/grok-4', 'high')."""
    rest = spec.split("/")[1:]
    if len(rest) >= 2 and rest[-1] in OMP_THINKING_LEVELS:
        return "/".join(rest[:-1]), rest[-1]
    return "/".join(rest), None


def model_id_from_spec(spec: str) -> str:
    """Extract just the model ID from a spec (strips effort suffix)."""
    if provider_from_spec(spec) == "omp":
        return _omp_parts(spec)[0]
    parts = spec.split("/")
    return parts[1] if len(parts) >= 2 else spec


def provider_from_spec(spec: str) -> str:
    """Extract the provider from a spec."""
    return spec.split("/", 1)[0]


def effort_from_spec(spec: str) -> str | None:
    """Extract effort level from a spec like 'claude-sdk/claude-opus-4-6/max'.

    For omp/* specs this is omp's --thinking level (off ... xhigh, max); for grok/*
    specs, grok's --effort value.
    """
    if provider_from_spec(spec) == "omp":
        return _omp_parts(spec)[1]
    parts = spec.split("/")
    if provider_from_spec(spec) == "grok":
        # grok/<model>/<effort>: passed to `grok --effort`, which validates it
        return parts[2] if len(parts) >= 3 and parts[2] else None
    if len(parts) >= 3 and parts[2] in ("low", "medium", "high", "xhigh", "max"):
        return parts[2]
    return None


def supports_vision(spec: str) -> bool:
    """Check if a model spec supports vision."""
    return model_id_from_spec(spec) in VISION_MODELS


def context_window(spec: str) -> int:
    """Get context window size for a model spec."""
    return CONTEXT_WINDOWS.get(model_id_from_spec(spec), 200_000)
