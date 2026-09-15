"""Wiring tests for the server entrypoint.

`build_server` is where every env-driven decision is made: which channels are
registered, whether the agent searches the fixture or Enterprise Knowledge, and
which misconfigurations are refused at startup rather than at the first message.
TAC and OpenAI are stubbed, so nothing here touches the network.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest
from fakes import FakeMessage, FakeOpenAI

from app import main
from app.agent import Agent
from app.tools.knowledge import FixtureKnowledgeSource, TwilioKnowledgeSource

ENV = {
    "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
    "TWILIO_AUTH_TOKEN": "token",
    "TWILIO_API_KEY": "SK" + "0" * 32,
    "TWILIO_API_SECRET": "secret",
    "OPENAI_API_KEY": "sk-test",
    "TWILIO_CONVERSATION_CONFIGURATION_ID": "conv_configuration_1",
    "TWILIO_WHATSAPP_NUMBER": "whatsapp:+14155238886",
}


@dataclass
class FakeTAC:
    """Stands in for TAC, recording the callback it is given."""

    orchestrator_enabled: bool = True
    callback: Any = None

    def is_orchestrator_enabled(self) -> bool:
        return self.orchestrator_enabled

    def on_message_ready(self, callback: Any) -> None:
        self.callback = callback


@dataclass
class FakeServer:
    tac: FakeTAC
    messaging_channels: list = field(default_factory=list)
    app: object = None


@dataclass
class FakeChannel:
    tac: object
    config: dict | None = None
    name: str = "STUB"

    def get_channel_name(self) -> str:
        return self.name


@dataclass
class FakeAuthor:
    address: str


@dataclass
class FakeSession:
    conversation_id: str = "conv_conversation_1"
    channel: str = "WHATSAPP"
    author_info: FakeAuthor | None = field(default_factory=lambda: FakeAuthor("whatsapp:+15550100"))


@pytest.fixture
def wired(monkeypatch):
    """`build_server` with TAC, OpenAI and the channels replaced.

    Returns the FakeTAC, so a test can read what was registered and invoke the
    message callback directly.
    """
    tac = FakeTAC()
    client = FakeOpenAI()

    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("TWILIO_PHONE_NUMBER", "TWILIO_KNOWLEDGE_BASE_ID"):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(main, "TAC", lambda config: tac)
    monkeypatch.setattr(main.TACConfig, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(main, "AsyncOpenAI", lambda: client)
    monkeypatch.setattr(main, "TACFastAPIServer", FakeServer)
    monkeypatch.setattr(main, "SMSChannel", lambda tac, config: FakeChannel(tac, config, "SMS"))
    monkeypatch.setattr(
        main, "WhatsAppChannel", lambda tac, config: FakeChannel(tac, config, "WHATSAPP")
    )
    monkeypatch.setattr(main, "with_tac_memory", lambda client, memory, context: client)
    monkeypatch.setattr(main, "HISTORIES", {})
    return tac, client


# --- misconfigurations refused at startup ---


def test_missing_credentials_are_refused_with_the_names_that_are_missing(monkeypatch, wired):
    monkeypatch.delenv("TWILIO_API_SECRET")

    with pytest.raises(RuntimeError, match="TWILIO_API_SECRET"):
        main.build_server()


def test_an_absent_conversation_configuration_is_refused(wired):
    """Without one, TAC starts in ConversationRelay-only mode and silently
    serves no messaging channel at all."""
    tac, _ = wired
    tac.orchestrator_enabled = False

    with pytest.raises(RuntimeError, match="TWILIO_CONVERSATION_CONFIGURATION_ID"):
        main.build_server()


def test_no_configured_sender_is_refused(monkeypatch, wired):
    monkeypatch.delenv("TWILIO_WHATSAPP_NUMBER")

    with pytest.raises(RuntimeError, match="No messaging channel"):
        main.build_server()


# --- channel registration ---


def test_whatsapp_alone_is_a_working_setup(wired):
    """The WhatsApp sandbox needs no carrier registration, so WhatsApp without
    an SMS number has to be servable."""
    server = main.build_server()

    assert [channel.name for channel in server.messaging_channels] == ["WHATSAPP"]


def test_an_sms_number_registers_the_sms_channel(monkeypatch, wired):
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550100")

    server = main.build_server()

    assert [channel.name for channel in server.messaging_channels] == ["SMS", "WHATSAPP"]


@pytest.mark.parametrize("value", ["", "   ", "not-a-number", "15550100"])
def test_a_phone_number_that_is_not_e164_registers_no_sms_channel(monkeypatch, wired, value):
    """TACConfig reads TWILIO_PHONE_NUMBER unconditionally, so it is present and
    empty in a WhatsApp-only setup; a placeholder must not register SMS."""
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", value)

    server = main.build_server()

    assert [channel.name for channel in server.messaging_channels] == ["WHATSAPP"]


def test_every_channel_is_told_to_retrieve_memory(monkeypatch, wired):
    """TAC defaults `memory_mode` to "never", which skips retrieval and hands
    the callback no memory, so a returning family is met as a stranger. The
    channels have to be constructed with it."""
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550100")

    server = main.build_server()

    assert [channel.config["memory_mode"] for channel in server.messaging_channels] == [
        "always",
        "always",
    ]


def test_the_memory_mode_can_be_overridden(monkeypatch, wired):
    monkeypatch.setenv("MEMORY_MODE", "once")

    server = main.build_server()

    assert server.messaging_channels[0].config["memory_mode"] == "once"


# --- knowledge source selection ---


def test_the_fixture_is_used_when_no_knowledge_base_is_configured(wired):
    tac, _ = wired

    main.build_server()

    assert isinstance(_knowledge_source_of(tac), FixtureKnowledgeSource)


def test_setting_a_knowledge_base_id_switches_to_enterprise_knowledge(monkeypatch, wired):
    tac, _ = wired
    monkeypatch.setenv("TWILIO_KNOWLEDGE_BASE_ID", "know_knowledgebase_1")

    main.build_server()

    assert isinstance(_knowledge_source_of(tac), TwilioKnowledgeSource)


def test_the_knowledge_source_is_given_the_configured_api_key(monkeypatch, wired):
    """Enterprise Knowledge is reached with the scoped API key, not the Auth
    Token, so a misconfigured key fails at startup rather than mid-answer."""
    tac, _ = wired
    monkeypatch.setenv("TWILIO_KNOWLEDGE_BASE_ID", "know_knowledgebase_1")

    main.build_server()

    source = _knowledge_source_of(tac)
    assert source._knowledge_base_id == "know_knowledgebase_1"
    assert source._auth == (ENV["TWILIO_API_KEY"], ENV["TWILIO_API_SECRET"])


def _knowledge_source_of(tac: FakeTAC) -> object:
    """The knowledge source the registered callback's agent will search.

    The agent is a closure variable of the callback, which is the only handle
    `build_server` leaves on it.
    """
    for cell in tac.callback.__closure__ or ():
        if isinstance(cell.cell_contents, Agent):
            return cell.cell_contents._knowledge_source
    raise AssertionError("no agent found on the registered callback")


# --- the message callback ---


async def test_a_message_becomes_a_reply(wired):
    tac, client = wired
    client.turns = [FakeMessage(content="Start here: https://curesyngap1.org/fundraise/")]
    main.build_server()

    reply = await tac.callback("how do I fundraise?", FakeSession(), None)

    assert reply == "Start here: https://curesyngap1.org/fundraise/"


async def test_history_is_kept_per_conversation(wired):
    tac, client = wired
    client.turns = [FakeMessage(content="first"), FakeMessage(content="second")]
    main.build_server()

    await tac.callback("one", FakeSession(conversation_id="conv_a"), None)
    await tac.callback("two", FakeSession(conversation_id="conv_b"), None)

    assert [entry["content"] for entry in main.HISTORIES["conv_a"]] == ["one", "first"]
    assert [entry["content"] for entry in main.HISTORIES["conv_b"]] == ["two", "second"]


async def test_history_is_capped_so_a_long_conversation_cannot_grow_without_bound(wired):
    tac, client = wired
    client.turns = [FakeMessage(content=f"reply {index}") for index in range(40)]
    main.build_server()

    for index in range(40):
        await tac.callback(f"message {index}", FakeSession(), None)

    assert len(main.HISTORIES["conv_conversation_1"]) == main.MAX_HISTORY_MESSAGES


async def test_a_conversation_with_no_author_survives_a_missing_address(wired):
    """`author_info` is optional on the session, and an escalation must still be
    buildable without it."""
    tac, client = wired
    client.turns = [FakeMessage(content="ok")]
    main.build_server()

    assert await tac.callback("hi", FakeSession(author_info=None), None) == "ok"


# --- the transcript handed to a human ---


def test_the_transcript_keeps_only_what_a_person_needs_to_read():
    transcript = main._transcript(
        [
            {"role": "user", "content": "how do I fundraise?"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": '{"results": []}'},
            {"role": "assistant", "content": "Start here: ..."},
        ]
    )

    assert transcript == [
        {"role": "user", "content": "how do I fundraise?"},
        {"role": "assistant", "content": "Start here: ..."},
    ]
