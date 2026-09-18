"""Tests for the SMS channel routed through a Messaging Service.

Two failures are worth protecting against, because both lose a family's reply
without reporting anything: a send that does not name the service, and a send
whose `From` is outside the service's sender pool (Twilio error 21711). The
first is `_build_channel_settings`, the second is `get_agent_address`.

TAC is stubbed, so nothing here touches the network.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest
from tac.channels.messaging import MessagingChannel
from tac.models.session import AuthorInfo

from app.channels import MAX_CONVERSATIONS, MessagingServiceSMSChannel

SERVICE_SID = "MG" + "0" * 32
AGENT_NUMBER = "+15550100"
POOL_NUMBER = "+15550199"
FAMILY_NUMBER = "+15557654321"


@dataclass
class FakeConfig:
    phone_number: str = AGENT_NUMBER
    conversation_configuration_id: str = "conv_configuration_1"


@dataclass
class FakeOrchestrator:
    """Records the actions the channel creates, or fails on demand."""

    actions: list[dict[str, Any]] = field(default_factory=list)
    error: Exception | None = None

    async def create_action(self, conversation_id: str, request: Any) -> None:
        if self.error:
            raise self.error
        self.actions.append(request.model_dump(by_alias=True, exclude_none=True))


@dataclass
class FakeTAC:
    """The surface MessagingChannel reads at construction time."""

    config: FakeConfig = field(default_factory=FakeConfig)
    conversation_orchestrator_client: FakeOrchestrator = field(default_factory=FakeOrchestrator)


def channel(
    phone_number: str = AGENT_NUMBER, orchestrator: FakeOrchestrator | None = None
) -> MessagingServiceSMSChannel:
    tac = FakeTAC(
        config=FakeConfig(phone_number=phone_number),
        conversation_orchestrator_client=orchestrator or FakeOrchestrator(),
    )
    return MessagingServiceSMSChannel(tac, SERVICE_SID)


def reconciled(sms: MessagingServiceSMSChannel, conversation_id: str = "conv_conversation_1"):
    """A session in the state an inbound webhook leaves behind."""
    session = sms._start_conversation(conversation_id)
    session.author_info = AuthorInfo(address=FAMILY_NUMBER, participant_id="p_family")
    return session


def communication(
    author: str, to: str, conversation_id: str = "conv_conversation_1"
) -> dict[str, Any]:
    """A COMMUNICATION_CREATED webhook, in either direction."""
    return {
        "eventType": "COMMUNICATION_CREATED",
        "data": {
            "id": "comms_communication_1",
            "accountId": "AC" + "0" * 32,
            "conversationId": conversation_id,
            "author": {"address": author, "channel": "SMS", "participantId": "p_author"},
            "recipients": [{"address": to, "channel": "SMS", "participantId": "p_recipient"}],
            "content": {"type": "TEXT", "text": "hi"},
        },
    }


def inbound(to: str, conversation_id: str = "conv_conversation_1") -> dict[str, Any]:
    """A family texting one of our numbers."""
    return communication(author=FAMILY_NUMBER, to=to, conversation_id=conversation_id)


def outbound(frm: str, conversation_id: str = "conv_conversation_1") -> dict[str, Any]:
    """A reply the agent sent, coming back through the capture rules."""
    return communication(author=frm, to=FAMILY_NUMBER, conversation_id=conversation_id)


async def _generator():
    yield "a streamed chunk"


# --- the Messaging Service is the sender ---


async def test_the_send_names_the_messaging_service_as_its_sender():
    """`From` accepts a Messaging Service SID, so naming it there is the whole
    of routing the send through the service: Twilio picks the number, and the
    service's registration and opt-out handling apply."""
    orchestrator = FakeOrchestrator()
    sms = channel(orchestrator=orchestrator)
    reconciled(sms)

    await sms.send_response("conv_conversation_1", "Here is the answer.")

    assert orchestrator.actions == [
        {
            "type": "SEND_MESSAGE",
            "payload": {
                "from": {"address": SERVICE_SID, "channel": "SMS"},
                "to": [{"participantId": "p_family", "channel": "SMS"}],
                "content": {"text": "Here is the answer."},
            },
        }
    ]


async def test_no_sender_number_is_needed_to_send():
    """The point of naming the service: nothing here reads a configured number,
    so TWILIO_PHONE_NUMBER can be empty."""
    orchestrator = FakeOrchestrator()
    sms = channel(phone_number="", orchestrator=orchestrator)
    reconciled(sms)

    await sms.send_response("conv_conversation_1", "Here is the answer.")

    assert orchestrator.actions[0]["payload"]["from"] == {"address": SERVICE_SID, "channel": "SMS"}


async def test_the_reply_is_addressed_to_the_family_by_participant():
    """Resolving the recipient by participant id rather than address is what
    keeps the reply on the thread TAC reconciled."""
    orchestrator = FakeOrchestrator()
    sms = channel(orchestrator=orchestrator)
    session = reconciled(sms)
    session.author_info.participant_id = "p_someone_else"

    await sms.send_response("conv_conversation_1", "ok")

    assert orchestrator.actions[0]["payload"]["to"] == [
        {"participantId": "p_someone_else", "channel": "SMS"}
    ]


async def test_a_channel_id_on_the_session_still_rides_along():
    """`channelSettings` is TAC's pass-through and this send keeps building it
    the way TAC does."""
    orchestrator = FakeOrchestrator()
    sms = channel(orchestrator=orchestrator)
    reconciled(sms).metadata["channel_id"] = "SM" + "0" * 32

    await sms.send_response("conv_conversation_1", "ok")

    assert orchestrator.actions[0]["payload"]["channelSettings"] == {"channelId": "SM" + "0" * 32}


async def test_a_streamed_response_is_refused():
    """Messaging channels send one complete message; TAC raises rather than
    silently sending the repr of a generator."""
    sms = channel()
    reconciled(sms)

    with pytest.raises(TypeError, match="only supports string responses"):
        await sms.send_response("conv_conversation_1", _generator())


async def test_sending_without_a_reconciled_session_is_a_misuse():
    with pytest.raises(RuntimeError, match="without a reconciled session"):
        await channel().send_response("conv_never_seen", "ok")


async def test_a_failed_send_is_logged_rather_than_raised():
    """The callback has already produced an answer; raising here would only
    surface as an unhandled error inside TAC's background task."""
    orchestrator = FakeOrchestrator(error=RuntimeError("Actions API is down"))
    sms = channel(orchestrator=orchestrator)
    reconciled(sms)

    await sms.send_response("conv_conversation_1", "ok")

    assert orchestrator.actions == []


# --- answering from an address that is in the pool ---


def test_the_reply_goes_out_on_the_number_the_family_texted():
    """A pool can hold several numbers. Answering from the configured one would
    reply from a number the family never wrote to, and error 21711 if that
    number is not in the pool at all."""
    sms = channel()

    sms._remember_agent_address(inbound(to=POOL_NUMBER))

    assert sms.get_agent_address("conv_conversation_1").address == POOL_NUMBER


def test_each_conversation_keeps_its_own_number():
    sms = channel()

    sms._remember_agent_address(inbound(to=POOL_NUMBER, conversation_id="conv_a"))
    sms._remember_agent_address(inbound(to=AGENT_NUMBER, conversation_id="conv_b"))

    assert sms.get_agent_address("conv_a").address == POOL_NUMBER
    assert sms.get_agent_address("conv_b").address == AGENT_NUMBER


async def test_the_number_is_noted_before_the_base_class_handles_the_message(monkeypatch):
    """TAC asks for the agent address during the participant reconciliation it
    does on the way to the agent, so noting it afterwards would be too late."""
    sms = channel()
    address_during_handling = []

    async def record(self, webhook_data, idempotency_token=None):
        address_during_handling.append(self.get_agent_address("conv_conversation_1").address)

    monkeypatch.setattr(MessagingChannel, "process_webhook", record)

    await sms.process_webhook(inbound(to=POOL_NUMBER))

    assert address_during_handling == [POOL_NUMBER]


def test_a_conversation_with_no_inbound_message_falls_back_to_the_configured_number():
    """Only an outbound-initiated conversation reaches this, which this app
    never starts; the fallback is TAC's own behavior."""
    assert channel().get_agent_address("conv_unseen").address == AGENT_NUMBER


def test_the_agents_own_reply_does_not_become_the_agent_address():
    """The capture rules cover both directions, so a reply the agent sent comes
    back with our number as its author and the family's as its recipient.
    Reading the recipient off that would answer from the family's own number."""
    sms = channel()
    sms._remember_agent_address(inbound(to=POOL_NUMBER))

    sms._remember_agent_address(outbound(frm=POOL_NUMBER))

    assert sms.get_agent_address("conv_conversation_1").address == POOL_NUMBER


def test_an_outbound_message_from_the_configured_number_is_recognized_on_its_own():
    """Recognizing it must not depend on having seen the inbound message first,
    because the configured number is known from the start."""
    sms = channel()

    sms._remember_agent_address(outbound(frm=AGENT_NUMBER, conversation_id="conv_a"))

    assert "conv_a" not in sms._agent_addresses


def test_the_first_number_a_conversation_arrived_on_is_the_one_kept():
    """Conversation grouping is per participant address, so the number cannot
    change mid-conversation; a later event claiming otherwise is not trusted."""
    sms = channel()
    sms._remember_agent_address(inbound(to=POOL_NUMBER))

    sms._remember_agent_address(inbound(to=AGENT_NUMBER))

    assert sms.get_agent_address("conv_conversation_1").address == POOL_NUMBER


@pytest.mark.parametrize(
    "webhook",
    [
        {"eventType": "CONVERSATION_UPDATED", "data": {"id": "conv_conversation_1"}},
        {"eventType": "COMMUNICATION_CREATED", "data": None},
        {"eventType": "COMMUNICATION_CREATED", "data": {"recipients": [{"address": "+1"}]}},
        {"eventType": "COMMUNICATION_CREATED", "data": {"conversationId": "", "recipients": []}},
        {
            "eventType": "COMMUNICATION_CREATED",
            "data": {"conversationId": "conv_conversation_1", "recipients": "not-a-list"},
        },
        {
            "eventType": "COMMUNICATION_CREATED",
            "data": {
                "conversationId": "conv_conversation_1",
                "recipients": [{"address": "whatsapp:+1", "channel": "WHATSAPP"}],
            },
        },
    ],
)
def test_a_payload_without_an_sms_recipient_is_left_alone(webhook):
    """Anything unexpected keeps the configured number rather than being
    guessed at, which is what TAC would have done without this class."""
    sms = channel()

    sms._remember_agent_address(webhook)

    assert sms.get_agent_address("conv_conversation_1").address == AGENT_NUMBER


# --- not answering our own messages ---


def test_an_address_the_agent_speaks_from_is_recognized_as_its_own():
    """Outbound traffic is captured too, so a reply the agent sent from a pool
    number comes back as an inbound author and must not be answered."""
    sms = channel()
    sms._remember_agent_address(inbound(to=POOL_NUMBER))

    assert sms.is_default_agent_address(POOL_NUMBER)
    assert sms.is_default_agent_address(AGENT_NUMBER)
    assert not sms.is_default_agent_address("+15557654321")


# --- bounded memory ---


def test_remembered_numbers_are_capped_oldest_first():
    """Nothing deletes an entry on its own, so the cap is what keeps a
    long-running process from growing without limit."""
    sms = channel()

    for index in range(MAX_CONVERSATIONS + 10):
        sms._remember_agent_address(inbound(to=POOL_NUMBER, conversation_id=f"conv_{index}"))

    assert len(sms._agent_addresses) == MAX_CONVERSATIONS
    assert "conv_0" not in sms._agent_addresses
    assert f"conv_{MAX_CONVERSATIONS + 9}" in sms._agent_addresses
