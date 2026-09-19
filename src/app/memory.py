"""Resolving which Conversation Memory profile a contact belongs to.

`tac.retrieve_memory()` resolves a profile itself when the session carries no
`profile_id`, but only for email and phone: it derives the identifier type as
`"email" if "@" in address else "phone"` and passes the address through
unchanged. A WhatsApp address is `whatsapp:+1...` and its profile is keyed on
identifier type `whatsapp` with the prefix intact, so that lookup matches
nothing, and a returning family is met as a stranger.

Resolving the profile here and setting it on the session before retrieval is
what makes recall work on WhatsApp.
"""

from typing import Any

# Identifier types Conversation Memory accepts. A profile is keyed on one of
# these plus a value, and a lookup under the wrong type returns no profile,
# which is indistinguishable from a first-time contact.
IDENTIFIER_TYPES = frozenset({"email", "phone", "pushUserID", "whatsapp", "chat"})


def identifier_for(address: str) -> tuple[str, str]:
    """The identifier type and value a profile for `address` is keyed on.

    WhatsApp keeps the channel prefix; SMS and voice use the bare E.164 number.
    """
    if "@" in address:
        return "email", address
    scheme, _, rest = address.partition(":")
    if rest and scheme in IDENTIFIER_TYPES:
        # whatsapp:+1... and chat:... keep the whole address as the value.
        return scheme, address
    return "phone", rest or address


async def resolve_profile_id(memory_client: Any, address: str | None) -> str | None:
    """The profile id for `address`, or None if the contact has none yet.

    A contact has no profile until their first inbound message has been
    processed, so None is an ordinary state rather than a failure.
    """
    if not memory_client or not address:
        return None
    id_type, value = identifier_for(address)
    lookup = await memory_client.lookup_profile(id_type=id_type, value=value)
    profiles = getattr(lookup, "profiles", None) or []
    return profiles[0] if profiles else None


async def patch_traits(
    memory_client: Any, profile_id: str, traits: dict[str, dict[str, Any]]
) -> None:
    """Merge `traits` into a profile, trait by trait.

    TAC 2.4.0 reads and creates profiles but has no update, so this sends the
    Memory API's profile PATCH through the client TAC already authenticates.
    Unmentioned traits are left alone; the write is queued (202), so a read
    straight after may not reflect it yet. Only traits declared on the store
    are accepted, which `scripts/provision.py` takes care of.
    """
    url = f"{memory_client.base_url}/v1/Stores/{memory_client.store_id}/Profiles/{profile_id}"
    async with memory_client._get_client() as client:
        response = await client.patch(url, json={"traits": traits})
        response.raise_for_status()
