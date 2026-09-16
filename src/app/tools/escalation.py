"""Escalation when the agent cannot answer.

There is no live human handoff. An unanswered question is sent to a CURE
SYNGAP1 contact who can reply to the family directly.

The transport is an open decision (see docs/decisions.md). `LoggingEscalation`
records the escalation so the rest of the agent is complete and testable;
swapping in a real transport means writing one more `Escalation` implementation.
"""

from dataclasses import dataclass, field
from typing import Protocol

from tac.core.logging import get_logger
from tac.tools.base import TACTool, function_tool
from tac.utils.redaction import mask_phone

logger = get_logger(__name__)

# How much of what a family wrote reaches a log line. A question can itself be
# a health detail, and logs travel further than the developer who reads them.
LOGGED_CHARS = 200


def _clip(text: str, limit: int | None) -> str:
    """`text`, shortened to `limit` characters and marked when it was cut."""
    if limit is None or len(text) <= limit:
        return text
    return f"{text[:limit]}... [{len(text) - limit} more characters]"


@dataclass(frozen=True)
class EscalationRequest:
    """An unanswered question, ready to send to a person."""

    question: str
    reason: str
    conversation_id: str
    channel: str
    contact_address: str | None = None
    transcript: list[dict[str, str]] = field(default_factory=list)

    def render(self, truncate_to: int | None = None) -> str:
        """Render the escalation as plain text for a human reader.

        `truncate_to` shortens every piece of what the family wrote. A question
        can carry health details, so a destination that is not a person reading
        it — a log line, above all — gets the gist rather than the whole thing.
        """
        lines = [
            f"Channel: {self.channel}",
            f"Conversation: {self.conversation_id}",
            f"From: {mask_phone(self.contact_address) if self.contact_address else 'unknown'}",
            "",
            f"Question: {_clip(self.question, truncate_to)}",
            f"Why the agent could not answer: {_clip(self.reason, truncate_to)}",
        ]
        if self.transcript:
            lines += ["", "Conversation so far:"]
            lines += [
                f"  {turn['role']}: {_clip(turn['content'], truncate_to)}"
                for turn in self.transcript
            ]
        return "\n".join(lines)


@dataclass(frozen=True)
class EscalationContext:
    """Conversation details the agent loop knows and the model is not asked for."""

    conversation_id: str
    channel: str
    contact_address: str | None = None
    transcript: list[dict[str, str]] = field(default_factory=list)


class Escalation(Protocol):
    """Somewhere an unanswered question can be sent."""

    async def send(self, request: EscalationRequest) -> None: ...


class LoggingEscalation:
    """Log the escalation and keep it in memory.

    `sent` lets tests and the local chat harness assert on what was escalated,
    and holds the request whole; only the log line is clipped.
    """

    def __init__(self) -> None:
        self.sent: list[EscalationRequest] = []

    async def send(self, request: EscalationRequest) -> None:
        self.sent.append(request)
        logger.warning(
            f"Escalating unanswered question\n{request.render(truncate_to=LOGGED_CHARS)}"
        )


def build_escalation_tool(escalation: Escalation, context: EscalationContext) -> TACTool:
    """Build the LLM-facing escalation tool for one conversation turn.

    The conversation and contact details come from `context` rather than being
    asked of the model, so the model supplies only the question and the reason.
    Build one tool per turn: the context differs per conversation.
    """

    @function_tool(
        name="escalate_to_team",
        description=(
            "Send a question you cannot answer from the knowledge base to the CURE "
            "SYNGAP1 team, so a person can follow up. Use this instead of guessing. "
            "Tell the user afterwards that someone from the team will follow up."
        ),
    )
    async def escalate_to_team(question: str, reason: str) -> dict[str, str]:
        """Send an unanswered question to the team.

        Args:
            question: The user's question, in their own words.
            reason: Why the knowledge base could not answer it.

        Returns:
            Confirmation that the question was sent.
        """
        await escalation.send(
            EscalationRequest(
                question=question,
                reason=reason,
                conversation_id=context.conversation_id,
                channel=context.channel,
                contact_address=context.contact_address,
                transcript=list(context.transcript),
            )
        )
        return {"status": "sent"}

    return escalate_to_team
