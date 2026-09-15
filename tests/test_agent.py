import asyncio
import json

import pytest
from fakes import FakeMessage, FakeOpenAI, tool_call

from app.agent import Agent
from app.config import AgentSettings
from app.prompt import FALLBACK_REPLY, MAX_REPLY_CHARS, TOO_LONG_REPLY
from app.tools.escalation import EscalationContext, LoggingEscalation
from app.tools.knowledge import FixtureKnowledgeSource

SETTINGS = AgentSettings(
    model="test-model",
    timeout_seconds=5,
    max_tool_iterations=3,
    top_k=3,
    min_knowledge_score=0.3,
    memory_mode="always",
    reasoning_effort="none",
)
SYSTEM_PROMPT = "Answer from the knowledge base and always include the link."


def build(client, escalation=None):
    return Agent(
        openai_client=client,
        knowledge_source=FixtureKnowledgeSource(),
        escalation=escalation or LoggingEscalation(),
        system_prompt=SYSTEM_PROMPT,
        settings=SETTINGS,
    )


def context():
    return EscalationContext(
        conversation_id="conv_1", channel="SMS", contact_address="+15555550100"
    )


async def test_direct_answer_is_returned():
    agent = build(FakeOpenAI(turns=[FakeMessage(content="Hello.")]))

    reply = await agent.respond([{"role": "user", "content": "hi"}], context())

    assert reply == "Hello."


async def test_search_result_is_fed_back_to_the_model():
    client = FakeOpenAI(
        turns=[
            FakeMessage(
                tool_calls=[tool_call("search_knowledge", json.dumps({"query": "fundraiser"}))]
            ),
            FakeMessage(content="Here's how: https://curesyngap1.org/fundraise/"),
        ]
    )
    agent = build(client)
    history = [{"role": "user", "content": "how do I fundraise?"}]

    reply = await agent.respond(history, context())

    assert "curesyngap1.org/fundraise/" in reply
    tool_results = [entry for entry in history if entry["role"] == "tool"]
    assert len(tool_results) == 1
    assert "fundraise" in tool_results[0]["content"]


async def test_tools_are_offered_to_the_model():
    client = FakeOpenAI(turns=[FakeMessage(content="ok")])

    await build(client).respond([{"role": "user", "content": "hi"}], context())

    offered = {tool["function"]["name"] for tool in client.calls[0]["tools"]}
    assert offered == {"search_knowledge", "escalate_to_team"}


async def test_reasoning_effort_is_sent_when_configured():
    client = FakeOpenAI(turns=[FakeMessage(content="ok")])

    await build(client).respond([{"role": "user", "content": "hi"}], context())

    assert client.calls[0]["reasoning_effort"] == "none"


async def test_reasoning_effort_is_omitted_when_unset():
    client = FakeOpenAI(turns=[FakeMessage(content="ok")])
    agent = Agent(
        openai_client=client,
        knowledge_source=FixtureKnowledgeSource(),
        escalation=LoggingEscalation(),
        system_prompt=SYSTEM_PROMPT,
        settings=AgentSettings(
            model="test-model",
            timeout_seconds=5,
            max_tool_iterations=3,
            top_k=3,
            min_knowledge_score=0.3,
            memory_mode="always",
            reasoning_effort=None,
        ),
    )

    await agent.respond([{"role": "user", "content": "hi"}], context())

    assert "reasoning_effort" not in client.calls[0]


async def test_system_prompt_leads_every_request():
    client = FakeOpenAI(turns=[FakeMessage(content="ok")])

    await build(client).respond([{"role": "user", "content": "hi"}], context())

    assert client.calls[0]["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}


async def test_escalation_carries_the_conversation_details():
    escalation = LoggingEscalation()
    client = FakeOpenAI(
        turns=[
            FakeMessage(
                tool_calls=[
                    tool_call(
                        "escalate_to_team",
                        json.dumps(
                            {"question": "Is there a trial in Denver?", "reason": "not in kb"}
                        ),
                    )
                ]
            ),
            FakeMessage(content="Someone from the team will follow up."),
        ]
    )
    agent = build(client, escalation)

    await agent.respond([{"role": "user", "content": "Is there a trial in Denver?"}], context())

    assert len(escalation.sent) == 1
    request = escalation.sent[0]
    assert request.conversation_id == "conv_1"
    assert request.channel == "SMS"
    assert "Denver" in request.render()
    # The rendered escalation masks the contact's number.
    assert "+15555550100" not in request.render()


async def test_tool_iterations_are_capped_then_answered_without_tools():
    search = tool_call("search_knowledge", json.dumps({"query": "syngap1"}))
    client = FakeOpenAI(
        turns=[
            FakeMessage(tool_calls=[search]),
            FakeMessage(tool_calls=[search]),
            FakeMessage(tool_calls=[search]),
            FakeMessage(content="Final answer."),
        ]
    )

    reply = await build(client).respond([{"role": "user", "content": "tell me"}], context())

    assert reply == "Final answer."
    assert len(client.calls) == SETTINGS.max_tool_iterations + 1
    assert "tools" not in client.calls[-1]


async def test_model_error_yields_the_fallback_reply():
    client = FakeOpenAI(error=RuntimeError("upstream is down"))

    reply = await build(client).respond([{"role": "user", "content": "hi"}], context())

    assert reply == FALLBACK_REPLY


async def test_timeout_yields_the_fallback_reply():
    class SlowClient(FakeOpenAI):
        async def _create(self, **kwargs):
            await asyncio.sleep(1)
            raise AssertionError("should have timed out")

    agent = Agent(
        openai_client=SlowClient(),
        knowledge_source=FixtureKnowledgeSource(),
        escalation=LoggingEscalation(),
        system_prompt=SYSTEM_PROMPT,
        settings=AgentSettings(
            model="test-model",
            timeout_seconds=0.05,
            max_tool_iterations=3,
            top_k=3,
            min_knowledge_score=0.3,
            memory_mode="always",
            reasoning_effort="none",
        ),
    )

    reply = await agent.respond([{"role": "user", "content": "hi"}], context())

    assert reply == FALLBACK_REPLY


async def test_unknown_tool_is_reported_to_the_model_not_raised():
    client = FakeOpenAI(
        turns=[
            FakeMessage(tool_calls=[tool_call("delete_everything", "{}")]),
            FakeMessage(content="I can't do that."),
        ]
    )
    history = [{"role": "user", "content": "hi"}]

    reply = await build(client).respond(history, context())

    assert reply == "I can't do that."
    assert "unknown tool" in [e for e in history if e["role"] == "tool"][0]["content"]


async def test_per_turn_client_overrides_the_default():
    default = FakeOpenAI(turns=[FakeMessage(content="default")])
    per_turn = FakeOpenAI(turns=[FakeMessage(content="per turn")])

    reply = await build(default).respond(
        [{"role": "user", "content": "hi"}], context(), client=per_turn
    )

    assert reply == "per turn"
    assert default.calls == []


@pytest.mark.parametrize("content", ["", None])
async def test_empty_model_content_yields_the_fallback_reply(content):
    client = FakeOpenAI(turns=[FakeMessage(content=content)])

    reply = await build(client).respond([{"role": "user", "content": "hi"}], context())

    assert reply == FALLBACK_REPLY


async def test_a_timeout_mid_tool_call_leaves_history_usable():
    """A turn that dies between a `tool_calls` message and its results must
    not leave that message behind: the API rejects a conversation where one is
    unanswered, which would break every later turn."""

    class HangsAfterTheToolCall(FakeOpenAI):
        async def _create(self, **kwargs):
            if self.calls:
                await asyncio.sleep(1)
                raise AssertionError("should have timed out")
            return await super()._create(**kwargs)

    agent = Agent(
        openai_client=HangsAfterTheToolCall(
            turns=[
                FakeMessage(tool_calls=[tool_call("search_knowledge", '{"query": "x"}')]),
            ]
        ),
        knowledge_source=FixtureKnowledgeSource(),
        escalation=LoggingEscalation(),
        system_prompt=SYSTEM_PROMPT,
        settings=AgentSettings(
            model="test-model",
            timeout_seconds=0.05,
            max_tool_iterations=3,
            top_k=3,
            min_knowledge_score=0.3,
            memory_mode="always",
            reasoning_effort="none",
        ),
    )
    history = [{"role": "user", "content": "how do I fundraise?"}]

    assert await agent.respond(history, context()) == FALLBACK_REPLY
    assert history == [{"role": "user", "content": "how do I fundraise?"}]


async def test_a_model_error_leaves_history_unchanged():
    history = [{"role": "user", "content": "hi"}]

    await build(FakeOpenAI(error=RuntimeError("upstream is down"))).respond(history, context())

    assert history == [{"role": "user", "content": "hi"}]


async def test_a_successful_turn_commits_its_tool_calls_to_history():
    client = FakeOpenAI(
        turns=[
            FakeMessage(
                tool_calls=[
                    tool_call("search_knowledge", '{"query": "fundraiser"}')
                ]
            ),
            FakeMessage(content="Start here: https://curesyngap1.org/fundraise/"),
        ]
    )
    history = [{"role": "user", "content": "how do I fundraise?"}]

    await build(client).respond(history, context())

    assert [entry["role"] for entry in history] == ["user", "assistant", "tool", "assistant"]
    assert history[2]["tool_call_id"] == "call_1"


async def test_a_reply_too_long_to_deliver_is_replaced():
    """Twilio drops an oversized body after accepting it, so the family would
    get nothing at all. They get a short reply instead."""
    essay = "Grocery shopping is a weekly ritual. " * 60
    assert len(essay) > MAX_REPLY_CHARS
    history = [{"role": "user", "content": "Write a 300 word essay about grocery shopping"}]

    reply = await build(FakeOpenAI(turns=[FakeMessage(content=essay)])).respond(
        history, context()
    )

    assert reply == TOO_LONG_REPLY
    assert len(reply) <= MAX_REPLY_CHARS
    # History holds what was sent, not the essay the family never saw.
    assert history[-1] == {"role": "assistant", "content": TOO_LONG_REPLY}


async def test_a_reply_at_the_limit_is_sent_unchanged():
    reply_text = "x" * MAX_REPLY_CHARS

    reply = await build(FakeOpenAI(turns=[FakeMessage(content=reply_text)])).respond(
        [{"role": "user", "content": "hi"}], context()
    )

    assert reply == reply_text
