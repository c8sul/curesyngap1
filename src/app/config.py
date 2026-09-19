"""Application settings read from the environment."""

import os
import re
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

MESSAGING_SERVICE_SID = re.compile(r"^MG[0-9a-fA-F]{32}$")


def messaging_service_sid() -> str | None:
    """The Messaging Service to route SMS through, or None to send from the number.

    A Messaging Service adds Twilio's STOP/HELP keyword handling and a sender
    pool to draw from, and for a US 10DLC sender it is where the campaign
    registration lives. Optional: SMS serves from a bare `TWILIO_PHONE_NUMBER`
    without it, which is how the verified toll-free sender runs today.
    WhatsApp is unaffected either way: the sandbox sender is in no service's pool.

    A malformed value is refused rather than ignored. Twilio rejects a send
    naming a service that does not exist, and it does so after accepting the
    request, so nothing reports the failure back and the family gets no reply.
    """
    sid = (os.environ.get("TWILIO_MESSAGING_SERVICE_SID") or "").strip()
    if not sid:
        return None
    if not MESSAGING_SERVICE_SID.match(sid):
        raise RuntimeError(
            f"TWILIO_MESSAGING_SERVICE_SID is {sid!r}; it must be a Messaging Service "
            "SID, which is MG followed by 32 hex characters."
        )
    return sid


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
            # TAC handles each webhook in a background task, so Twilio is not
            # waiting on this and a short timeout buys nothing. It is what makes
            # a cold start answer with the fallback instead of an answer.
            timeout_seconds=float(os.environ.get("AGENT_TIMEOUT_SECONDS", "25")),
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
