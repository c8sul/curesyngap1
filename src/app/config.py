"""Application settings read from the environment."""

import os
from dataclasses import dataclass

# Twilio credentials consumed by TACConfig.from_env(). TWILIO_PHONE_NUMBER is
# not here: it is only needed to serve SMS, and a WhatsApp-only setup leaves it
# empty. TACConfig still reads it as a required key, so it has to be present as
# an empty string rather than absent.
TAC_REQUIRED_ENV = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_API_KEY",
    "TWILIO_API_SECRET",
)


@dataclass(frozen=True)
class AgentSettings:
    """Settings for the agent loop, independent of Twilio wiring."""

    model: str
    timeout_seconds: float
    max_tool_iterations: int
    top_k: int
    min_knowledge_score: float
    memory_mode: str
    reasoning_effort: str | None

    @classmethod
    def from_env(cls) -> "AgentSettings":
        return cls(
            model=os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"),
            # Luna rejects function tools on /v1/chat/completions unless
            # reasoning is off. Set it empty to omit the parameter for a model
            # that does not accept it.
            reasoning_effort=os.environ.get("AGENT_REASONING_EFFORT", "none") or None,
            timeout_seconds=float(os.environ.get("AGENT_TIMEOUT_SECONDS", "12")),
            max_tool_iterations=int(os.environ.get("AGENT_MAX_TOOL_ITERATIONS", "3")),
            top_k=int(os.environ.get("KB_TOP_K", "5")),
            min_knowledge_score=float(os.environ.get("KB_MIN_SCORE", "0.3")),
            # "always" retrieves what is remembered on every message,
            # "never" turns recall off. What may be retained about a family is
            # an open decision; see docs/decisions.md.
            memory_mode=os.environ.get("MEMORY_MODE", "always"),
        )


def missing_env(names: tuple[str, ...]) -> list[str]:
    """Return the names in `names` that are absent or empty in the environment."""
    return [name for name in names if not os.environ.get(name)]
