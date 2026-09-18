"""The SMS channel, sending through a Messaging Service.

A Messaging Service adds Twilio's STOP/HELP keyword handling and a sender pool
to draw from, and for a US 10DLC sender it is where the campaign registration
lives. This agent's sender is a toll-free number whose verification is already
approved, so opt-out handling is the part that applies to it.

`From` on the Messages API accepts a phone number, an Alphanumeric Sender ID or
a Messaging Service SID, so naming the service as the sender is the whole of
"send this through the service": Twilio picks a sender from the pool and applies
the service's registration and opt-out handling. Nothing here needs a sender
number of its own, which is why `TWILIO_PHONE_NUMBER` is optional once a service
is configured.

`send_response` is reimplemented rather than extended because TAC builds the
action's `from` from the agent participant's id and offers no hook for the
address — see `from_=ActionParticipantRef(...)` in `tac.channels.messaging`. The
rest of the action is assembled the same way it is there, so this stays a small
divergence, and it is the method to re-read after a TAC upgrade.

The inbound side is left alone. Conversation Orchestrator creates the agent
participant at the pool number the family texted, and `get_agent_address`
returns that address so TAC's participant reconciliation finds it rather than
adding a second one. CO's SMS addresses are E.164 (see its Channels reference),
so the service SID belongs on the send and not in the participant model.

`MessagingChannel` is subclassed directly rather than `SMSChannel`, whose only
additions are the three methods below and a constructor guard requiring
`TWILIO_PHONE_NUMBER`. That guard is the thing being dropped, and inheriting it
only to work around it would be worse than not inheriting it. The one thing left
behind is `initiate_outbound_conversation`, which needs a sender number and
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


class MessagingServiceSMSChannel(MessagingChannel):
    """SMS, sent with a Messaging Service as the sender."""

    def __init__(
        self,
        tac: TAC,
        messaging_service_sid: str,
        dedup_capacity: int = 10000,
        memory_mode: MemoryMode = "never",
    ) -> None:
        super().__init__(tac, dedup_capacity=dedup_capacity, memory_mode=memory_mode)
        self._messaging_service_sid = messaging_service_sid
        self._agent_addresses: dict[str, str] = {}

    def get_channel_name(self) -> str:
        return "SMS"

    async def process_webhook(
        self, webhook_data: dict[str, Any], idempotency_token: str | None = None
    ) -> None:
        """Note which of our numbers was texted, then handle the message.

        This runs before the base class, because the address is needed during
        the participant reconciliation the base class does on the way to the
        agent, not after it.
        """
        self._remember_agent_address(webhook_data)
        await super().process_webhook(webhook_data, idempotency_token)

    def get_agent_address(self, conversation_id: str) -> ParticipantAddress:
        """The agent's address in Conversation Orchestrator for this conversation.

        This is what reconciliation matches the agent participant on, not what
        the reply is sent from, so it is the pool number the family texted —
        the address CO already created that participant at.

        `TWILIO_PHONE_NUMBER` is the fallback for a conversation no inbound
        message has arrived on, which is to say one this process started itself.
        This app never starts one.
        """
        address = self._agent_addresses.get(conversation_id) or self.tac.config.phone_number
        return ParticipantAddress(channel=self.get_channel_name(), address=address)

    def is_default_agent_address(self, author_address: str) -> bool:
        """Whether this address is one of ours, so its message is not answered.

        A pool holds more than one number in general, and with a service there
        may be no configured number at all, so the remembered addresses are the
        ones that matter. Every one of them was the recipient of an inbound
        message and so is ours; a family's address never reaches that dict.
        Missing one costs a participant lookup over the API, which is TAC's
        fallback check, rather than correctness.
        """
        configured = self.tac.config.phone_number
        if configured and author_address == configured:
            return True
        return author_address in self._agent_addresses.values()

    async def send_response(
        self,
        conversation_id: str,
        response: str | AsyncGenerator[str | dict[str, Any], None],
        role: str | None = None,
    ) -> None:
        """Send the reply with the Messaging Service as its sender.

        The same action TAC builds, with `from` naming the service instead of
        the agent participant. Only the customer side is resolved by
        participant id, which is what keeps the reply on the right thread.

        A failure is logged rather than raised, matching TAC: the callback has
        already produced an answer and there is nothing further to do with it.
        """
        channel_name = self.get_channel_name()
        if not isinstance(response, str):
            raise TypeError(f"{channel_name} channel only supports string responses")

        session = self._conversations.get(conversation_id)
        if session is None or not session.author_info:
            raise RuntimeError(
                f"Unable to send {channel_name} message: send_response called without a "
                f"reconciled session for conversation {conversation_id}. Wait for an "
                "inbound webhook first."
            )

        customer_participant_id = session.author_info.participant_id
        if not customer_participant_id:
            raise RuntimeError(
                f"Unable to send {channel_name} message: session for conversation "
                f"{conversation_id} is missing the customer participant id."
            )

        try:
            await self.conversation_orchestrator_client.create_action(
                conversation_id,
                SendMessageActionRequest(
                    payload=SendMessageActionPayload(
                        from_=ActionParticipantRef(
                            channel=channel_name,
                            address=self._messaging_service_sid,
                        ),
                        to=[
                            ActionParticipantRef(
                                channel=channel_name,
                                participant_id=customer_participant_id,
                            )
                        ],
                        content=ActionTextContent(text=response),
                        channel_settings=self._build_channel_settings(conversation_id, session),
                    ),
                ),
            )
            self.logger.info(
                f"Sent {channel_name} response via Actions API",
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

    def _remember_agent_address(self, webhook_data: dict[str, Any]) -> None:
        """Record which of our numbers an inbound message was addressed to.

        Only an inbound message says anything about this. The capture rules
        cover both directions, so a reply the agent sent is captured too, and
        it has our sender as its author and the family's number as its
        recipient — reading the recipient off one of those would point
        reconciliation at the family's own number. So an address is recorded
        once, from the message that opened the conversation, and an event whose
        author is already known to be ours is left alone. Every conversation
        here starts with a family texting in; this app never initiates one.

        Anything unexpected in the payload is left alone rather than guessed
        at: `get_agent_address` falls back to the configured number, which is
        the behaviour TAC has without this class.
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
        author_address = author.get("address") if isinstance(author, dict) else None
        if isinstance(author_address, str) and self.is_default_agent_address(author_address):
            return

        for recipient in data.get("recipients") or []:
            if not isinstance(recipient, dict):
                continue
            address = recipient.get("address")
            if recipient.get("channel") != self.get_channel_name():
                continue
            if not isinstance(address, str) or not address:
                continue
            # Insertion order makes the dict least recently used first, so the
            # oldest conversations are the ones dropped at the cap.
            self._agent_addresses[conversation_id] = address
            while len(self._agent_addresses) > MAX_CONVERSATIONS:
                self._agent_addresses.pop(next(iter(self._agent_addresses)))
            return
