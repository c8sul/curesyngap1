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
from fastapi import FastAPI
from fastapi.testclient import TestClient

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

SERVICE_SID = "MG" + "0" * 32


@dataclass
class FakeProfileLookup:
    profiles: list


@dataclass
class FakeMemoryClient:
    tac: "FakeTAC"

    async def lookup_profile(self, id_type: str, value: str) -> FakeProfileLookup:
        self.tac.lookups.append((id_type, value))
        return FakeProfileLookup([self.tac.profile_id] if self.tac.profile_id else [])


@dataclass
class FakeTAC:
    """Stands in for TAC, recording the callback, lookups and recalls."""

    orchestrator_enabled: bool = True
    callback: Any = None
    profile_id: str | None = "mem_profile_1"
    recall_error: Exception | None = None
    lookups: list = field(default_factory=list)
    recalls: list = field(default_factory=list)

    @property
    def conversation_memory_client(self) -> FakeMemoryClient:
        return FakeMemoryClient(self)

    def is_orchestrator_enabled(self) -> bool:
        return self.orchestrator_enabled

    def on_message_ready(self, callback: Any) -> None:
        self.callback = callback

    async def retrieve_memory(self, context, query=None, conversation_id=None):
        if self.recall_error:
            raise self.recall_error
        self.recalls.append((conversation_id, query))
        return object()


@dataclass
class FakeServer:
    tac: FakeTAC
    messaging_channels: list = field(default_factory=list)
    # A real app, because `create_app` registers its own routes on it and they
    # are part of what the deployed service serves.
    app: FastAPI = field(default_factory=FastAPI)


@dataclass
class FakeChannel:
    tac: object
    name: str = "STUB"
    messaging_service_sid: str | None = None

    def get_channel_name(self) -> str:
        return self.name


@dataclass
class FakeAuthor:
    address: str


@dataclass
class FakeSession:
    conversation_id: str = "conv_conversation_1"
    channel: str = "WHATSAPP"
    profile_id: str | None = None
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
    for key in (
        "TWILIO_PHONE_NUMBER",
        "TWILIO_KNOWLEDGE_BASE_ID",
        "TWILIO_MESSAGING_SERVICE_SID",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(main, "TAC", lambda config: tac)
    monkeypatch.setattr(main.TACConfig, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(main, "AsyncOpenAI", lambda: client)
    monkeypatch.setattr(main, "TACFastAPIServer", FakeServer)
    monkeypatch.setattr(main, "SMSChannel", lambda tac: FakeChannel(tac, "SMS"))
    monkeypatch.setattr(main, "WhatsAppChannel", lambda tac: FakeChannel(tac, "WHATSAPP"))
    monkeypatch.setattr(
        main,
        "MessagingServiceSMSChannel",
        lambda tac, service_sid: FakeChannel(tac, "SMS", service_sid),
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


# --- the Messaging Service ---


def test_a_messaging_service_routes_sms_through_it(monkeypatch, wired):
    """A service is what carries the 10DLC registration and the STOP/HELP
    handling, so SMS goes through it rather than out of the number directly."""
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550100")
    monkeypatch.setenv("TWILIO_MESSAGING_SERVICE_SID", SERVICE_SID)

    server = main.build_server()

    sms = next(channel for channel in server.messaging_channels if channel.name == "SMS")
    assert sms.messaging_service_sid == SERVICE_SID


def test_sms_without_a_messaging_service_still_sends_from_the_number(monkeypatch, wired):
    """The service is additive: SMS has to keep working without one."""
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550100")

    server = main.build_server()

    sms = next(channel for channel in server.messaging_channels if channel.name == "SMS")
    assert sms.messaging_service_sid is None


def test_a_messaging_service_alone_serves_sms(monkeypatch, wired):
    """The service is the sender — Twilio picks the number from its pool — so
    it needs no TWILIO_PHONE_NUMBER beside it."""
    monkeypatch.setenv("TWILIO_MESSAGING_SERVICE_SID", SERVICE_SID)

    server = main.build_server()

    sms = next(channel for channel in server.messaging_channels if channel.name == "SMS")
    assert sms.messaging_service_sid == SERVICE_SID


def test_a_messaging_service_takes_precedence_over_a_configured_number(monkeypatch, wired):
    """Both set is not two SMS channels: the service is the sender either way."""
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550100")
    monkeypatch.setenv("TWILIO_MESSAGING_SERVICE_SID", SERVICE_SID)

    server = main.build_server()

    assert [channel.name for channel in server.messaging_channels] == ["SMS", "WHATSAPP"]


@pytest.mark.parametrize("value", ["MG123", "SK" + "0" * 32, "not-a-sid", "MG" + "z" * 32])
def test_a_malformed_messaging_service_sid_is_refused_at_startup(monkeypatch, wired, value):
    """Twilio accepts a send naming a service that does not exist and drops it
    afterwards, so nothing would report this back to the family."""
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550100")
    monkeypatch.setenv("TWILIO_MESSAGING_SERVICE_SID", value)

    with pytest.raises(RuntimeError, match="TWILIO_MESSAGING_SERVICE_SID"):
        main.build_server()


def test_whatsapp_is_not_routed_through_the_messaging_service(monkeypatch, wired):
    """The sandbox sender is in no service's pool, so naming one on a WhatsApp
    send would be rejected with error 21711."""
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550100")
    monkeypatch.setenv("TWILIO_MESSAGING_SERVICE_SID", SERVICE_SID)

    server = main.build_server()

    whatsapp = next(channel for channel in server.messaging_channels if channel.name == "WHATSAPP")
    assert whatsapp.messaging_service_sid is None


async def test_what_is_remembered_reaches_the_model(wired):
    """`tac.retrieve_memory` cannot key a WhatsApp address to its profile, so
    the profile is resolved here and set on the session before recall."""
    tac, client = wired
    client.turns = [FakeMessage(content="ok")]
    main.build_server()
    session = FakeSession()

    await tac.callback("hi", session, None)

    assert tac.lookups == [("whatsapp", "whatsapp:+15550100")]
    assert session.profile_id == "mem_profile_1"
    assert tac.recalls == [("conv_conversation_1", "hi")]


async def test_recall_is_skipped_for_a_contact_with_no_profile(wired):
    """A contact has no profile until their first message is processed."""
    tac, client = wired
    tac.profile_id = None
    client.turns = [FakeMessage(content="ok")]
    main.build_server()

    assert await tac.callback("hi", FakeSession(), None) == "ok"
    assert tac.recalls == []


async def test_recall_can_be_turned_off(monkeypatch, wired):
    """MEMORY_MODE=never is the lever for a retention decision, so it has to
    stop retrieval without stopping the agent answering."""
    tac, client = wired
    monkeypatch.setenv("MEMORY_MODE", "never")
    client.turns = [FakeMessage(content="ok")]
    main.build_server()

    assert await tac.callback("hi", FakeSession(), None) == "ok"
    assert tac.lookups == []
    assert tac.recalls == []


async def test_a_reply_still_goes_out_when_recall_fails(wired):
    """A family gets a knowledge-base answer whether or not memory works."""
    tac, client = wired
    tac.recall_error = RuntimeError("memory is down")
    client.turns = [FakeMessage(content="ok")]
    main.build_server()

    assert await tac.callback("hi", FakeSession(), None) == "ok"


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


async def test_history_is_capped_so_a_long_conversation_cannot_grow_without_bound(
    monkeypatch, wired
):
    tac, client = wired
    client.turns = [FakeMessage(content=f"reply {index}") for index in range(40)]
    # A conversation this long is what the rate limiter exists to stop; this
    # test is about the history cap, so lift it.
    monkeypatch.setattr(main, "RATE_LIMIT_MESSAGES", 100)
    main.build_server()

    for index in range(40):
        await tac.callback(f"message {index}", FakeSession(), None)

    assert len(main.HISTORIES["conv_conversation_1"]) == main.MAX_HISTORY_MESSAGES


async def test_conversations_are_capped_so_the_process_cannot_leak(monkeypatch, wired):
    """Nothing tells this module a conversation closed, so the oldest ones are
    dropped rather than kept forever."""
    tac, client = wired
    monkeypatch.setattr(main, "MAX_CONVERSATIONS", 3)
    client.turns = [FakeMessage(content="ok") for _ in range(5)]
    main.build_server()

    for index in range(5):
        await tac.callback("hi", FakeSession(conversation_id=f"conv_{index}"), None)

    assert list(main.HISTORIES) == ["conv_2", "conv_3", "conv_4"]


async def test_an_active_conversation_is_not_dropped_for_being_old(monkeypatch, wired):
    """The cap is least-recently-used, so a family still messaging keeps their
    history however long the conversation has been open."""
    tac, client = wired
    monkeypatch.setattr(main, "MAX_CONVERSATIONS", 2)
    client.turns = [FakeMessage(content="ok") for _ in range(4)]
    main.build_server()

    await tac.callback("hi", FakeSession(conversation_id="conv_old"), None)
    await tac.callback("hi", FakeSession(conversation_id="conv_other"), None)
    await tac.callback("still here", FakeSession(conversation_id="conv_old"), None)
    await tac.callback("hi", FakeSession(conversation_id="conv_new"), None)

    assert list(main.HISTORIES) == ["conv_old", "conv_new"]


async def test_an_overlong_message_is_truncated_before_it_reaches_the_model(monkeypatch, wired):
    tac, client = wired
    monkeypatch.setattr(main, "MAX_INBOUND_CHARS", 50)
    client.turns = [FakeMessage(content="ok")]
    main.build_server()

    await tac.callback("x" * 500, FakeSession(), None)

    sent = client.calls[0]["messages"][-1]["content"]
    assert sent == "x" * 50


async def test_a_contact_over_the_rate_limit_is_told_once_then_not_answered(wired):
    """One sender cannot spend an OpenAI call per message, and answering every
    message over the limit would just as happily answer a loop."""
    tac, client = wired
    client.turns = [FakeMessage(content="ok") for _ in range(main.RATE_LIMIT_MESSAGES)]
    main.build_server()
    session = FakeSession()

    replies = [
        await tac.callback(f"message {index}", session, None)
        for index in range(main.RATE_LIMIT_MESSAGES + 3)
    ]

    assert replies[: main.RATE_LIMIT_MESSAGES] == ["ok"] * main.RATE_LIMIT_MESSAGES
    assert replies[main.RATE_LIMIT_MESSAGES] == main.RATE_LIMITED_REPLY
    # None tells TAC to send nothing at all.
    assert replies[main.RATE_LIMIT_MESSAGES + 1 :] == [None, None]
    assert len(client.calls) == main.RATE_LIMIT_MESSAGES


async def test_one_contact_over_the_limit_does_not_silence_another(wired):
    tac, client = wired
    client.turns = [FakeMessage(content="ok") for _ in range(main.RATE_LIMIT_MESSAGES + 1)]
    main.build_server()

    for index in range(main.RATE_LIMIT_MESSAGES + 2):
        await tac.callback(
            f"message {index}",
            FakeSession(author_info=FakeAuthor("whatsapp:+15550100")),
            None,
        )
    reply = await tac.callback(
        "hi", FakeSession(author_info=FakeAuthor("whatsapp:+15550199")), None
    )

    assert reply == "ok"


def test_the_rate_limit_window_rolls():
    limiter = main.RateLimiter(limit=2, window_seconds=60.0)

    assert [limiter.check("a", now=0.0) for _ in range(4)] == [
        main.Allowance.OK,
        main.Allowance.OK,
        main.Allowance.JUST_OVER_LIMIT,
        main.Allowance.OVER_LIMIT,
    ]
    assert limiter.check("a", now=61.0) is main.Allowance.OK


def test_the_rate_limiter_forgets_contacts_whose_window_has_rolled():
    """Its own bookkeeping must not become the leak it was added to prevent."""
    limiter = main.RateLimiter(limit=2, window_seconds=60.0)

    for index in range(100):
        limiter.check(f"contact_{index}", now=0.0)
    limiter.check("later", now=61.0)

    assert list(limiter._counts) == ["later"]


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


# --- the routes create_app adds ---
#
# `python -m app.main` serves TAC's app directly and has neither of these, so
# these tests also pin the reason the deployed command is `uvicorn --factory
# app.main:create_app` rather than the Dockerfile's default.


def test_the_health_check_answers_without_a_twilio_signature(wired):
    """Every other route validates a Twilio signature, so a platform health
    check has nowhere else to go."""
    client = TestClient(main.create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_the_sandbox_silencer_returns_empty_twiml(wired):
    """The WhatsApp sandbox echoes "You said ..." at the user unless its
    Inbound URL is answered with a well-formed, empty TwiML document."""
    client = TestClient(main.create_app())

    response = client.post("/whatsapp-sandbox-silence")

    assert response.status_code == 200
    assert "<Response></Response>" in response.text
    assert "You said" not in response.text
