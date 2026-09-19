"""What is written to a profile when a conversation closes.

The traits are the structured, non-clinical alternative to free-text memory, so
the behavior worth protecting is that only the fixed vocabularies ever reach a
write, and that one conversation does not undo what earlier ones recorded.
"""

import re

from fakes import FakeMessage, FakeOpenAI

from app.traits import (
    CLASSIFIER_SCHEMA,
    ENGAGEMENT,
    INTERESTS,
    TRAIT_GROUPS,
    classify_interests,
    count_escalations,
    engagement_update,
    interests_update,
    now_iso,
)

NOW = "2026-09-19T12:00:00Z"


def _engagement(existing, **overrides):
    kwargs = {"conversation_id": "conv_1", "channel": "SMS", "escalations": 0, "now": NOW}
    return engagement_update(existing, **(kwargs | overrides))


# --- Engagement ---


def test_a_first_conversation_starts_the_counters():
    assert _engagement({}) == {
        "conversationCount": 1,
        "firstContactAt": NOW,
        "lastContactAt": NOW,
        "lastConversationId": "conv_1",
        "lastChannel": "SMS",
    }


def test_a_later_conversation_keeps_the_first_contact_time():
    update = _engagement({"conversationCount": 3, "firstContactAt": "2026-01-01T00:00:00Z"})

    assert update["conversationCount"] == 4
    assert "firstContactAt" not in update


def test_escalations_accumulate_and_are_timestamped():
    update = _engagement({"escalationCount": 2}, escalations=1)

    assert update["escalationCount"] == 3
    assert update["lastEscalatedAt"] == NOW


def test_a_malformed_stored_counter_counts_as_zero():
    assert _engagement({"conversationCount": "lots"})["conversationCount"] == 1


def test_an_unknown_channel_is_not_written():
    assert "lastChannel" not in _engagement({}, channel="VOICE")


def test_escalations_are_counted_from_the_tool_calls_in_the_history():
    history = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "search_knowledge"}},
                {"function": {"name": "escalate_to_team"}},
            ],
        },
        {"role": "assistant", "tool_calls": [{"function": {"name": "request_email_followup"}}]},
    ]

    assert count_escalations(history) == 2


def test_timestamps_are_utc_to_the_second():
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", now_iso())


# --- Interests ---


def test_topics_accumulate_across_conversations():
    update = interests_update({"topics": ["events"]}, {"topics": ["fundraising", "events"]})

    assert update["topics"] == ["events", "fundraising"]


def test_unknown_never_replaces_a_known_role():
    assert "role" not in interests_update({"role": "clinician"}, {"role": "unknown"})


def test_a_new_known_role_replaces_the_old_one():
    assert interests_update({"role": "donor_supporter"}, {"role": "parent_caregiver"}) == {
        "role": "parent_caregiver"
    }


def test_nothing_outside_the_vocabularies_is_written():
    """Whatever the model returns, only enum values and a language code pass."""
    update = interests_update(
        {},
        {
            "role": "mother of a child with seizures",
            "topics": ["epilepsy medication", "research"],
            "preferred_language": "English, mostly",
        },
    )

    assert update == {"topics": ["research"]}


def test_an_unchanged_classification_writes_nothing():
    existing = {"role": "researcher", "topics": ["research"], "preferredLanguage": "en"}

    assert (
        interests_update(
            existing, {"role": "researcher", "topics": ["research"], "preferred_language": "en"}
        )
        == {}
    )


# --- the classifier ---


async def test_the_classifier_returns_what_the_model_labeled():
    client = FakeOpenAI(
        turns=[FakeMessage(content='{"role": "unknown", "topics": [], "preferred_language": "es"}')]
    )

    classified = await classify_interests(
        client,
        model="m",
        reasoning_effort=None,
        transcript=[{"role": "user", "content": "hola"}],
        timeout_seconds=5,
    )

    assert classified == {"role": "unknown", "topics": [], "preferred_language": "es"}
    assert client.calls[0]["response_format"]["json_schema"]["schema"] is CLASSIFIER_SCHEMA


async def test_malformed_output_is_no_classification():
    client = FakeOpenAI(turns=[FakeMessage(content="role: parent")])

    assert (
        await classify_interests(
            client,
            model="m",
            reasoning_effort=None,
            transcript=[{"role": "user", "content": "hi"}],
            timeout_seconds=5,
        )
        is None
    )


async def test_a_transcript_with_nothing_from_the_person_is_not_sent():
    client = FakeOpenAI()

    assert (
        await classify_interests(
            client,
            model="m",
            reasoning_effort=None,
            transcript=[{"role": "assistant", "content": "hello"}],
            timeout_seconds=5,
        )
        is None
    )
    assert client.calls == []


# --- the declarations ---


def test_every_written_trait_is_declared():
    """The profile PATCH refuses an undeclared trait, so what the merge
    functions can produce must be a subset of what provisioning declares."""
    engagement = _engagement({}, escalations=1)
    interests = interests_update(
        {}, {"role": "researcher", "topics": ["research"], "preferred_language": "en"}
    )

    assert set(engagement) <= set(TRAIT_GROUPS[ENGAGEMENT]["traits"])
    assert set(interests) <= set(TRAIT_GROUPS[INTERESTS]["traits"])


def test_group_and_trait_names_are_valid_for_the_memory_api():
    for name, group in TRAIT_GROUPS.items():
        assert re.fullmatch(r"[a-zA-Z0-9_\-.]{1,64}", name)
        assert len(group["traits"]) <= 95
        for trait in group["traits"].values():
            assert trait["dataType"] in {"STRING", "NUMBER", "BOOLEAN", "ARRAY"}
