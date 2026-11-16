"""Structured output types for solver agents."""

import re

from pydantic import BaseModel


class FlagFound(BaseModel):
    flag: str
    method: str  # brief description of how


# What a model writes in the flag field when it has no flag (compared after normalization)
_NO_FLAG = {
    "", "not_found", "notfound", "flag_not_found", "no_flag", "no_flag_found", "none", "null", "nil",
    "n_a", "na", "unknown", "unsolved", "not_solved", "not_recovered", "not_available", "missing",
    "tbd", "todo", "placeholder", "flag", "gave_up", "failed",
}


def is_placeholder_flag(flag: object) -> bool:
    """True for values that mean "I have no flag" (NOT_FOUND, N/A, empty...)."""
    return re.sub(r"[^a-z0-9]+", "_", str(flag or "").lower()).strip("_") in _NO_FLAG


def reported_flag(output: object) -> str | None:
    """The flag claimed by a solver's structured output, or None if it did not find one."""
    if not isinstance(output, dict) or output.get("type") != "flag_found":
        return None
    flag = str(output.get("flag") or "").strip()
    return None if is_placeholder_flag(flag) else flag


def solver_output_json_schema() -> dict:
    """JSON schema for solver structured output — shared by Claude SDK and Codex.

    Only flag_found is allowed: there is no way to give up. A model that stops anyway
    has to put something in `flag` (it writes NOT_FOUND): reported_flag() rejects that,
    so the turn counts as unsolved and the swarm relaunches the solver.
    """
    return {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["flag_found"],
                "description": (
                    "Answer only once you hold the real flag, read from the target. Giving up is "
                    "not allowed: without the flag, do not answer, keep working with another approach."
                ),
            },
            "flag": {
                "type": "string",
                "description": (
                    "The exact flag. Never a placeholder (NOT_FOUND, N/A...) or a guess: "
                    "those are rejected and you are sent back to work."
                ),
            },
            "method": {"type": "string", "description": "How the flag was obtained."},
        },
        "required": ["type", "flag", "method"],
        "additionalProperties": False,
    }


def writeup_output_json_schema() -> dict:
    """Structured output schema for a post-solve Markdown writeup."""
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["writeup"]},
            "writeup": {"type": "string"},
        },
        "required": ["type", "writeup"],
        "additionalProperties": False,
    }
