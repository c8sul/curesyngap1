"""Profile traits written when a conversation closes.

Observations and summaries are free text extracted from whatever a family said,
which is what Decision 3 in docs/decisions.md is about. Traits are the opposite:
each one is declared on the Memory Store with a type and a validation rule, so
what can be stored is decided here rather than by an extraction model.

Two groups are written:

- `Engagement`: facts the app already knows, with no model involved. For staff
  and reporting; kept out of the agent's prompt.
- `Interests`: one model call per closed conversation, constrained by a strict
  JSON schema to fixed vocabularies. There is nowhere in that schema for a
  diagnosis, a symptom or a medication to go, which is the point.

Nothing about the child and no email address is stored.

This module imports nothing beyond the standard library at load time, because
`scripts/provision.py` reads `TRAIT_GROUPS` from it to declare the groups.
"""

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any

ENGAGEMENT = "Engagement"
INTERESTS = "Interests"

# Trait groups the agent sees in its Customer Context. `Contact` is the default
# group holding the address; `Engagement` is counters and timestamps, which are
# noise to the model.
PROMPT_TRAIT_GROUPS = ["Contact", INTERESTS]

CHANNELS = ("SMS", "WHATSAPP")
ROLES = (
    "parent_caregiver",
    "family_member",
    "clinician",
    "researcher",
    "donor_supporter",
    "other",
    "unknown",
)
TOPICS = (
    "research",
    "clinical_trials",
    "family_support",
    "getting_started",
    "events",
    "fundraising",
    "donating",
    "advocacy",
    "other",
)
LANGUAGE_PATTERN = r"^[a-z]{2,3}(-[A-Z]{2})?$"

# The tools whose use means a question went to the team.
ESCALATION_TOOLS = frozenset({"escalate_to_team", "request_email_followup"})


def _one_of(values: tuple[str, ...]) -> dict[str, str]:
    return {"ruleType": "STRING", "pattern": "^(" + "|".join(values) + ")$"}


def _timestamp(description: str) -> dict[str, object]:
    return {"dataType": "STRING", "description": description}


# Declared on the Memory Store by `scripts/provision.py`. A trait cannot move
# between groups once written, so a rename here is a new trait, not a move.
TRAIT_GROUPS: dict[str, dict[str, object]] = {
    ENGAGEMENT: {
        "description": (
            "How often and how a contact has used the agent. Written by the app "
            "when a conversation closes; not shown to the model."
        ),
        "traits": {
            "conversationCount": {
                "dataType": "NUMBER",
                "description": "Conversations closed so far. Best effort: a "
                "conversation that closes while the service is asleep is not counted.",
            },
            "firstContactAt": _timestamp(
                "ISO-8601 UTC time the first counted conversation closed."
            ),
            "lastContactAt": _timestamp("ISO-8601 UTC time the latest conversation closed."),
            "lastChannel": {
                "dataType": "STRING",
                "description": "Channel of the latest conversation.",
                "validationRule": _one_of(CHANNELS),
            },
            "lastConversationId": {
                "dataType": "STRING",
                "description": "Latest Conversation Orchestrator conversation id.",
            },
            "escalationCount": {
                "dataType": "NUMBER",
                "description": "Questions sent to the team, across all conversations.",
            },
            "lastEscalatedAt": _timestamp("ISO-8601 UTC time of the latest escalation."),
        },
    },
    INTERESTS: {
        "description": (
            "Who the contact is to the foundation and what they ask about, from "
            "fixed vocabularies only. Never clinical."
        ),
        "traits": {
            "role": {
                "dataType": "STRING",
                "description": "The contact's relationship to SYNGAP1: " + ", ".join(ROLES) + ".",
                "validationRule": _one_of(ROLES),
            },
            "topics": {
                "dataType": "ARRAY",
                "description": "Topics asked about, accumulated across conversations: "
                + ", ".join(TOPICS)
                + ".",
            },
            "preferredLanguage": {
                "dataType": "STRING",
                "description": "BCP-47 language the contact writes in, such as en or es.",
                "validationRule": {"ruleType": "STRING", "pattern": LANGUAGE_PATTERN},
            },
        },
    },
}

CLASSIFIER_PROMPT = """\
You label a finished conversation between a person and the CURE SYNGAP1 \
information assistant, for the foundation's records. Choose only from the \
allowed values.

- role: the person's relationship to SYNGAP1, if they made it clear. Use \
"unknown" when they did not.
- topics: the subjects they asked about. Empty if they only said hello.
- preferred_language: the BCP-47 code of the language the person wrote in, \
such as "en" or "es".

Label only what the person said, never what the assistant said. Do not infer \
anything about anyone's health."""

CLASSIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": list(ROLES)},
        "topics": {"type": "array", "items": {"type": "string", "enum": list(TOPICS)}},
        "preferred_language": {"type": "string"},
    },
    "required": ["role", "topics", "preferred_language"],
    "additionalProperties": False,
}


def now_iso(now: datetime | None = None) -> str:
    """UTC time in the form the traits store, to the second."""
    return (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def count_escalations(history: list[dict[str, Any]]) -> int:
    """How many times this conversation sent a question to the team."""
    return sum(
        1
        for entry in history
        if entry.get("role") == "assistant"
        for call in entry.get("tool_calls") or []
        if (call.get("function") or {}).get("name") in ESCALATION_TOOLS
    )


def engagement_update(
    existing: dict[str, Any],
    *,
    conversation_id: str,
    channel: str | None,
    escalations: int,
    now: str,
) -> dict[str, Any]:
    """The `Engagement` traits to write for one closed conversation.

    `existing` is the group as currently stored, possibly empty. Only the
    traits that change are returned, since the profile PATCH merges per trait.
    """
    update: dict[str, Any] = {
        "conversationCount": _number(existing.get("conversationCount")) + 1,
        "lastContactAt": now,
        "lastConversationId": conversation_id,
    }
    if not existing.get("firstContactAt"):
        update["firstContactAt"] = now
    if channel in CHANNELS:
        update["lastChannel"] = channel
    if escalations:
        update["escalationCount"] = _number(existing.get("escalationCount")) + escalations
        update["lastEscalatedAt"] = now
    return update


def interests_update(existing: dict[str, Any], classified: dict[str, Any]) -> dict[str, Any]:
    """The `Interests` traits to write, merging a classification into `existing`.

    A role of "unknown" never replaces one already known: one conversation
    about an event says nothing about whether the person is a parent. Topics
    accumulate. Anything outside the vocabularies is dropped rather than
    stored, whatever the model returned.
    """
    update: dict[str, Any] = {}

    role = classified.get("role")
    if role in ROLES and (role != "unknown" or not existing.get("role")):
        if role != existing.get("role"):
            update["role"] = role

    stored = [topic for topic in existing.get("topics") or [] if topic in TOPICS]
    new = [topic for topic in classified.get("topics") or [] if topic in TOPICS]
    merged = sorted(set(stored) | set(new))
    if merged != sorted(stored):
        update["topics"] = merged

    language = classified.get("preferred_language")
    if (
        isinstance(language, str)
        and re.match(LANGUAGE_PATTERN, language)
        and language != existing.get("preferredLanguage")
    ):
        update["preferredLanguage"] = language

    return update


async def classify_interests(
    openai_client: Any,
    *,
    model: str,
    reasoning_effort: str | None,
    transcript: list[dict[str, str]],
    timeout_seconds: float,
) -> dict[str, Any] | None:
    """Label a finished conversation, or None if the model could not.

    Only the fixed vocabularies in `CLASSIFIER_SCHEMA` can come back, and
    `interests_update` checks them again before anything is written.
    """
    if not any(turn["role"] == "user" for turn in transcript):
        return None
    lines = "\n".join(f"{turn['role']}: {turn['content']}" for turn in transcript)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": CLASSIFIER_PROMPT},
            {"role": "user", "content": lines},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "interests", "strict": True, "schema": CLASSIFIER_SCHEMA},
        },
    }
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort

    response = await asyncio.wait_for(
        openai_client.chat.completions.create(**kwargs), timeout=timeout_seconds
    )
    content = response.choices[0].message.content
    try:
        classified = json.loads(content or "")
    except ValueError:
        return None
    return classified if isinstance(classified, dict) else None


def _number(value: Any) -> int:
    """A stored counter as an int; absent or malformed counts as zero."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
