"""Server entrypoint: Twilio Conversations webhook to the agent and back.

TAC owns the transport. `TACFastAPIServer` registers `/webhook` for messaging
channels with Twilio signature validation on every route, retrieves conversation
memory before invoking the callback, and routes whatever the callback returns
back to the channel the message arrived on.

This module owns one thing: turning a ready message into a reply.
"""

import os
import sys
import time
from enum import Enum

from dotenv import load_dotenv
from fastapi import Response
from openai import AsyncOpenAI
from tac import TAC, TACConfig, get_logger
from tac.adapters.openai import with_tac_memory
from tac.channels.messaging import MessagingChannel
from tac.channels.sms import SMSChannel
from tac.channels.whatsapp import WhatsAppChannel
from tac.models.session import ConversationSession
from tac.models.tac import TACMemoryResponse
from tac.server import TACFastAPIServer
from tac.utils.redaction import mask_address

from app.agent import Agent
from app.config import TAC_REQUIRED_ENV, AgentSettings, missing_env
from app.memory import resolve_profile_id
from app.prompt import RATE_LIMITED_REPLY, load_system_prompt
from app.tools.escalation import EscalationContext, LoggingEscalation
from app.tools.knowledge import FixtureKnowledgeSource, TwilioKnowledgeSource

load_dotenv()
logger = get_logger(__name__)

# Conversation history per conversation, in OpenAI message form. In-process, so
# it is lost on restart and not shared between replicas; TAC's Conversation
# Memory is what carries context across sessions.
#
# Nothing here is ever deleted on its own: TAC removes its own session when a
# conversation closes, and this module never hears about it. So the number of
# conversations is capped as well as the length of each, least recently used
# first, which bounds the process rather than leaving a slow leak that only a
# restart clears.
HISTORIES: dict[str, list[dict[str, object]]] = {}
MAX_HISTORY_MESSAGES = 40
MAX_CONVERSATIONS = 500

# Whatever a family sends is what the model reads. A very long message costs
# tokens on a nonprofit's budget and is where an injection attempt would hide,
# and no real question needs this much room.
MAX_INBOUND_CHARS = 2000

# One contact's messages per window. Above it the agent stops answering rather
# than spending an OpenAI call per message; a family typing quickly stays well
# under it.
RATE_LIMIT_MESSAGES = 12
RATE_LIMIT_WINDOW_SECONDS = 60.0


# Empty TwiML: a well-formed reply that sends the user nothing.
SILENT_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


class Allowance(Enum):
    """What the rate limiter says about one message."""

    OK = "ok"
    # The message that crosses the limit: answered once, with the canned reply.
    JUST_OVER_LIMIT = "just_over_limit"
    # Every message after that, until the window rolls: not answered at all.
    OVER_LIMIT = "over_limit"


class RateLimiter:
    """A fixed window of messages per contact.

    In-process, so it is per replica and lost on restart. That is enough for
    what it is for: stopping one sender, looping or not, from spending an
    OpenAI call per message on a nonprofit's budget. It is not a security
    control, and a determined sender across replicas is not what it stops.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._counts: dict[str, tuple[float, int]] = {}

    def check(self, key: str, now: float | None = None) -> Allowance:
        """Count one message from `key` and say whether to answer it."""
        now = time.monotonic() if now is None else now
        self._forget_stale_windows(now)

        started, count = self._counts.get(key, (now, 0))
        if now - started >= self._window:
            started, count = now, 0
        count += 1
        self._counts[key] = (started, count)

        if count <= self._limit:
            return Allowance.OK
        return Allowance.JUST_OVER_LIMIT if count == self._limit + 1 else Allowance.OVER_LIMIT

    def _forget_stale_windows(self, now: float) -> None:
        """Drop contacts whose window has rolled, so this cannot grow forever."""
        for key, (started, _) in list(self._counts.items()):
            if now - started >= self._window:
                del self._counts[key]


def _history_for(conversation_id: str) -> list[dict[str, object]]:
    """This conversation's history, kept as one of the most recent ones.

    Re-inserting on every message makes `HISTORIES` ordered least recently used
    first, so the oldest conversations are the ones dropped at the cap.
    """
    history = HISTORIES.pop(conversation_id, [])
    HISTORIES[conversation_id] = history
    while len(HISTORIES) > MAX_CONVERSATIONS:
        HISTORIES.pop(next(iter(HISTORIES)))
    return history


def create_app() -> object:
    """The FastAPI app, for `uvicorn --factory app.main:create_app`."""
    server = build_server()

    # The WhatsApp sandbox has its own Inbound URL, separate from the
    # Conversation Configuration's status callback. Left at its default it
    # echoes "You said ..." to the user on every message, alongside the real
    # answer. Pointing it here silences it, while Conversation Orchestrator
    # continues to deliver the message to /webhook for the agent to handle.
    @server.app.post("/whatsapp-sandbox-silence")
    async def whatsapp_sandbox_silence() -> Response:
        return Response(content=SILENT_TWIML, media_type="application/xml")

    # Liveness for a platform health check. Every other route requires a valid
    # Twilio signature, so none of them can serve as one. Reaching this means
    # the process started and `build_server()` found its configuration, which
    # is what distinguishes a bad deploy from a working one.
    @server.app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return server.app


def build_server() -> TACFastAPIServer:
    """Wire TAC, the agent, and the messaging channels together."""
    absent = missing_env((*TAC_REQUIRED_ENV, "OPENAI_API_KEY"))
    if absent:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(absent)
            + ". Copy .env.example to .env and fill it in."
        )

    # TACConfig reads this key unconditionally, including when only WhatsApp is
    # in use and there is no SMS number to name.
    os.environ.setdefault("TWILIO_PHONE_NUMBER", "")

    settings = AgentSettings.from_env()
    tac = TAC(config=TACConfig.from_env())

    if not tac.is_orchestrator_enabled():
        raise RuntimeError(
            "TWILIO_CONVERSATION_CONFIGURATION_ID is not set, so TAC starts in "
            "ConversationRelay-only mode and the messaging channels are disabled. "
            "Create a Conversation Configuration with a Memory Store attached."
        )

    knowledge_base_id = os.environ.get("TWILIO_KNOWLEDGE_BASE_ID")
    if knowledge_base_id:
        knowledge_source = TwilioKnowledgeSource(
            knowledge_base_id=knowledge_base_id,
            api_key=os.environ["TWILIO_API_KEY"],
            api_secret=os.environ["TWILIO_API_SECRET"],
            min_score=settings.min_knowledge_score,
        )
        logger.info(f"Knowledge: Enterprise Knowledge base {knowledge_base_id}")
    else:
        knowledge_source = FixtureKnowledgeSource()
        logger.warning(
            "Knowledge: checked-in fixture, not curesyngap1.org content. Set "
            "TWILIO_KNOWLEDGE_BASE_ID to search Enterprise Knowledge."
        )

    openai_client = AsyncOpenAI()
    agent = Agent(
        openai_client=openai_client,
        knowledge_source=knowledge_source,
        escalation=LoggingEscalation(),
        system_prompt=load_system_prompt(),
        settings=settings,
    )

    rate_limiter = RateLimiter(RATE_LIMIT_MESSAGES, RATE_LIMIT_WINDOW_SECONDS)

    async def handle_message_ready(
        message: str,
        context: ConversationSession,
        memory: TACMemoryResponse | None,
    ) -> str | None:
        """The reply to send, or None to send nothing.

        TAC sends whatever this returns and sends nothing for None, which is
        what keeps a contact over the rate limit from being answered message
        for message.
        """
        address = context.author_info.address if context.author_info else None
        allowance = rate_limiter.check(address or context.conversation_id)
        if allowance is Allowance.OVER_LIMIT:
            logger.warning(
                f"Rate limit: {mask_address(address)} is over "
                f"{RATE_LIMIT_MESSAGES} messages per "
                f"{int(RATE_LIMIT_WINDOW_SECONDS)}s; not answering"
            )
            return None
        if allowance is Allowance.JUST_OVER_LIMIT:
            logger.warning(
                f"Rate limit: {mask_address(address)} reached "
                f"{RATE_LIMIT_MESSAGES} messages per "
                f"{int(RATE_LIMIT_WINDOW_SECONDS)}s; telling them to wait"
            )
            return RATE_LIMITED_REPLY

        if len(message) > MAX_INBOUND_CHARS:
            logger.info(
                f"Inbound message of {len(message)} characters truncated to "
                f"{MAX_INBOUND_CHARS} on conversation {context.conversation_id}"
            )
            message = message[:MAX_INBOUND_CHARS]

        history = _history_for(context.conversation_id)
        history.append({"role": "user", "content": message})

        memory = await _recall(tac, context, message, settings.memory_mode)

        # `with_tac_memory` folds the caller's memory and profile into the
        # request, so the agent sees who it is talking to without this module
        # assembling that context itself.
        reply = await agent.respond(
            client=with_tac_memory(openai_client, memory, context),
            history=history,
            escalation_context=EscalationContext(
                conversation_id=context.conversation_id,
                channel=context.channel,
                contact_address=context.author_info.address if context.author_info else None,
                transcript=_transcript(history),
            ),
        )
        HISTORIES[context.conversation_id] = history[-MAX_HISTORY_MESSAGES:]
        return reply

    tac.on_message_ready(handle_message_ready)

    # Each channel is registered only when its sender is configured. The
    # WhatsApp sandbox needs no Meta verification, so WhatsApp alone is a
    # working setup while carrier registration for SMS is still pending.
    #
    # The channels keep TAC's default `memory_mode` of "never", so TAC does no
    # retrieval of its own: `_recall` does it, because resolving a WhatsApp
    # contact's profile needs an identifier type TAC does not derive. Leaving
    # both on would spend a failing lookup on every message.
    channels: list[MessagingChannel] = []
    if (os.environ.get("TWILIO_PHONE_NUMBER") or "").strip().startswith("+"):
        channels.append(SMSChannel(tac))
    if os.environ.get("TWILIO_WHATSAPP_NUMBER"):
        channels.append(WhatsAppChannel(tac))

    if not channels:
        raise RuntimeError(
            "No messaging channel is configured. Set TWILIO_PHONE_NUMBER to an "
            "E.164 number for SMS, TWILIO_WHATSAPP_NUMBER for WhatsApp, or both."
        )
    logger.info(f"Channels: {', '.join(channel.get_channel_name() for channel in channels)}")

    return TACFastAPIServer(tac=tac, messaging_channels=channels)


async def _recall(
    tac: TAC, context: ConversationSession, message: str, mode: str
) -> TACMemoryResponse | None:
    """What is remembered about this contact, or None if nothing can be.

    The profile is resolved here rather than left to `tac.retrieve_memory`,
    which cannot key a WhatsApp address to its profile. See `app.memory`.

    `mode` of "never" turns recall off, which is the lever for a decision about
    what may be retained about a family; see docs/decisions.md.

    Recall failing is not worth failing a reply over: the agent answers from
    the knowledge base either way, and the family repeats themselves at worst.
    """
    if mode == "never":
        return None
    try:
        if not context.profile_id:
            context.profile_id = await resolve_profile_id(
                tac.conversation_memory_client,
                context.author_info.address if context.author_info else None,
            )
        address = context.author_info.address if context.author_info else None
        if not context.profile_id:
            logger.info(f"Recall: no profile for {address}; answering without memory")
            return None
        memory = await tac.retrieve_memory(
            context, query=message, conversation_id=context.conversation_id
        )
        # Logged at INFO because a family silently not being remembered is
        # indistinguishable from the agent working correctly.
        logger.info(
            f"Recall: profile={context.profile_id} "
            f"observations={len(getattr(memory, 'observations', None) or [])} "
            f"summaries={len(getattr(memory, 'summaries', None) or [])} "
            f"communications={len(getattr(memory, 'communications', None) or [])}"
        )
        return memory
    except Exception:
        logger.warning(
            f"Recall failed for conversation {context.conversation_id}; "
            "answering without memory",
            exc_info=True,
        )
        return None


def _transcript(history: list[dict[str, object]]) -> list[dict[str, str]]:
    """The user and assistant turns, for a human reading an escalation."""
    return [
        {"role": str(entry["role"]), "content": str(entry.get("content") or "")}
        for entry in history
        if entry.get("role") in ("user", "assistant") and entry.get("content")
    ]


if __name__ == "__main__":
    try:
        build_server().start()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
