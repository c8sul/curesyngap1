"""Messaging channels that send as a Messaging Service.

A Messaging Service is the sender. The Messages API's `From` accepts a
Messaging Service SID in place of a phone number, an Alphanumeric Sender ID or
a short code, and given the SID Twilio picks the sender from the service's own
pool. So one SID replaces every per-channel sender in the environment, and the
service's Senders page — phone numbers, short codes, alpha senders, WhatsApp,
RCS — is the one place a sender is configured.

That is also what makes the sender config self-describing: adding a number to
the service and re-provisioning is enough for it to reach the agent, because
`scripts/provision.py` reads the service's senders and writes the Conversation
Configuration's capture rules from them. Nothing here has to be told which
numbers exist, and a channel whose service holds no sender simply never
receives anything — there are no capture rules for it, so Conversation
Orchestrator captures nothing to deliver.

`send_response` is reimplemented rather than extended because TAC builds the
SEND_MESSAGE action's `from` from the agent participant's id and offers no hook
for the address — see `from_=ActionParticipantRef(...)` in
`tac.channels.messaging`. The rest of the action is assembled the way TAC
assembles it, so this stays a small divergence, and it is the method to re-read
after a TAC upgrade.

The inbound side stays address-based. Conversation Orchestrator creates the
agent participant at the sender the contact actually messaged, and its SMS and
WhatsApp addresses are E.164, so `get_agent_address` reports that address for
participant reconciliation to match rather than adding a second participant.
The service SID belongs on the send, not in the participant model.

`MessagingChannel` is subclassed directly rather than TAC's `SMSChannel` and
`WhatsAppChannel`, whose only additions are the three methods here plus
constructor guards requiring `TWILIO_PHONE_NUMBER` and `TWILIO_WHATSAPP_NUMBER`
— those guards being the thing this removes. What is left behind is
`initiate_outbound_conversation`, which needs a sender address of its own and
which this app never calls.
"""

from collections.abc import AsyncGenerator
from typing import Any

from tac import TAC
from tac.channels.messaging import MemoryMode, MessagingChannel
from tac.models.conversation import (
    ActionParticipantRef,
    ActionTextContent,
    ParticipantAddress,
    SendMessageActionPayload,
    SendMessageActionRequest,
)
from tac.utils.redaction import mask_address

# Agent addresses per conversation, least recently used first. Nothing here is
# ever deleted on its own: TAC removes its own session when a conversation
# closes and this class never hears about it. So the number of conversations
# remembered is capped, oldest first, which bounds the process rather than
# leaving a slow leak that only a restart clears.
MAX_CONVERSATIONS = 500


class MessagingServiceChannel(MessagingChannel):
    """One messaging channel, sent with a Messaging Service as the sender."""

    # The Conversation Orchestrator channel name. Set by each subclass; it is
    # what TAC filters inbound webhooks on.
    channel_name: str = ""

    def __init__(
        self,
        tac: TAC,
        messaging_service_sid: str,
        dedup_capacity: int = 10000,
        memory_mode: MemoryMode = "never",
    ) -> None:
        super().__init__(tac, dedup_capacity=dedup_capacity, memory_mode=memory_mode)
        if not self.channel_name:
            raise ValueError(f"{type(self).__name__} must set channel_name")
        self._messaging_service_sid = messaging_service_sid
        self._agent_addresses: dict[str, str] = {}

    def get_channel_name(self) -> str:
        return self.channel_name

    async def process_webhook(
        self, webhook_data: dict[str, Any], idempotency_token: str | None = None
    ) -> None:
        """Note which of our senders was messaged, then handle the message.

        This runs before the base class, because the address is needed during
        the participant reconciliation the base class does on the way to the
        agent, not after it.
        """
        await self._remember_agent_address(webhook_data)
        await super().process_webhook(webhook_data, idempotency_token)

    def get_agent_address(self, conversation_id: str) -> ParticipantAddress:
        """The agent's Conversation Orchestrator address for this conversation.

        This is what reconciliation matches the agent participant on, not what
        the reply is sent from, so it is the sender the contact messaged — the
        address CO already created that participant at.

        There is no configured sender to fall back to, by design: the Messaging
        Service is the sender and it has no single address. So an unknown
        conversation is an error rather than a guess. Reaching it means
        something asked to reply to a conversation no inbound message arrived
        on, which is an outbound-initiated conversation; this app starts none.
        """
        address = self._agent_addresses.get(conversation_id)
        if not address:
            raise RuntimeError(
                f"No agent address known for {self.channel_name} conversation "
                f"{conversation_id}. It is learned from the inbound message that "
                "opens a conversation, so either none arrived on this process or the "
                "webhook carried no usable recipient."
            )
        return ParticipantAddress(channel=self.channel_name, address=address)

    def is_default_agent_address(self, author_address: str) -> bool:
        """Whether this address is one of ours, so its message is not answered.

        A service's pool holds more than one sender in general, and there is no
        configured sender to compare against, so the remembered addresses are
        all there is. Every one of them was the recipient of an inbound message
        and so is ours; a contact's address never reaches that dict. Missing one
        costs a participant lookup over the API, which is TAC's fallback check,
        rather than correctness.
        """
        return author_address in self._agent_addresses.values()

    async def send_response(
        self,
        conversation_id: str,
        response: str | AsyncGenerator[str | dict[str, Any], None],
        role: str | None = None,
    ) -> None:
        """Send the reply with the Messaging Service as its sender.

        The same action TAC builds, with `from` naming the service instead of
        the agent participant. Only the contact side is resolved by participant
        id, which is what keeps the reply on the thread TAC reconciled.

        A failure is logged rather than raised, matching TAC: the callback has
        already produced an answer and there is nothing further to do with it.
        """
        if not isinstance(response, str):
            raise TypeError(f"{self.channel_name} channel only supports string responses")

        session = self._conversations.get(conversation_id)
        if session is None or not session.author_info:
            raise RuntimeError(
                f"Unable to send {self.channel_name} message: send_response called "
                f"without a reconciled session for conversation {conversation_id}. "
                "Wait for an inbound webhook first."
            )

        contact_participant_id = session.author_info.participant_id
        if not contact_participant_id:
            raise RuntimeError(
                f"Unable to send {self.channel_name} message: session for conversation "
                f"{conversation_id} is missing the contact's participant id."
            )

        try:
            await self.conversation_orchestrator_client.create_action(
                conversation_id,
                SendMessageActionRequest(
                    payload=SendMessageActionPayload(
                        from_=ActionParticipantRef(
                            channel=self.channel_name,
                            address=self._messaging_service_sid,
                        ),
                        to=[
                            ActionParticipantRef(
                                channel=self.channel_name,
                                participant_id=contact_participant_id,
                            )
                        ],
                        content=ActionTextContent(text=response),
                        channel_settings=self._build_channel_settings(conversation_id, session),
                    ),
                ),
            )
            self.logger.info(
                f"Sent {self.channel_name} response via Actions API",
                conversation_id=conversation_id,
                to_address=mask_address(session.author_info.address),
                messaging_service_sid=self._messaging_service_sid,
            )
        except Exception as e:
            self.logger.error(
                "Failed to create action",
                conversation_id=conversation_id,
                error=str(e),
                exc_info=True,
            )

    async def _remember_agent_address(self, webhook_data: dict[str, Any]) -> None:
        """Record which of our senders an inbound message was addressed to.

        Only an inbound message says anything about this. The capture rules
        cover both directions, so a reply the agent sent is captured too, and
        it has our sender as its author and the contact's address as its
        recipient — reading the recipient off one of those would point
        reconciliation at the contact's own address.

        Which direction an event is can't be read off the payload, so this asks
        TAC's own `_is_own_message`. Its first tier compares the addresses
        already remembered here, and its second asks Conversation Orchestrator
        for the author participant's type, which is what catches a reply the
        agent sent before this process started — after a restart there is
        nothing remembered, and the next event on a live conversation may well
        be that reply rather than a new inbound message. Getting it wrong there
        is not recoverable: an address is recorded once and never overwritten,
        so the conversation would reconcile against the contact's participant
        for as long as it stays open.
        """
        if webhook_data.get("eventType") != "COMMUNICATION_CREATED":
            return
        data = webhook_data.get("data")
        if not isinstance(data, dict):
            return
        conversation_id = data.get("conversationId")
        if not isinstance(conversation_id, str) or not conversation_id:
            return

        if conversation_id in self._agent_addresses:
            # Touch it, so an active conversation is not the one evicted.
            self._agent_addresses[conversation_id] = self._agent_addresses.pop(conversation_id)
            return

        author = data.get("author")
        if not isinstance(author, dict) or not isinstance(author.get("address"), str):
            return
        if await self._is_own_message(
            author["address"], conversation_id, author.get("participantId")
        ):
            return

        for recipient in data.get("recipients") or []:
            if not isinstance(recipient, dict):
                continue
            address = recipient.get("address")
            if recipient.get("channel") != self.channel_name:
                continue
            if not isinstance(address, str) or not address:
                continue
            # Insertion order makes the dict least recently used first, so the
            # oldest conversations are the ones dropped at the cap.
            self._agent_addresses[conversation_id] = address
            while len(self._agent_addresses) > MAX_CONVERSATIONS:
                self._agent_addresses.pop(next(iter(self._agent_addresses)))
            return


class MessagingServiceSMSChannel(MessagingServiceChannel):
    """SMS, sent as a Messaging Service."""

    channel_name = "SMS"


class MessagingServiceWhatsAppChannel(MessagingServiceChannel):
    """WhatsApp, sent as a Messaging Service.

    Needs an approved WhatsApp sender in the service's pool. Twilio's shared
    WhatsApp sandbox sender cannot be one — it is not the account's to add — so
    the sandbox is not a way to run this channel.
    """

    channel_name = "WHATSAPP"
