"""Create the Twilio resources the agent needs, from an Account SID and Auth Token.

    docker compose run --rm provision --webhook-domain <public-host>

Ensures three things exist and prints the `.env` lines for them:

1. A scoped API key and secret. Every other call here, and the application
   itself, authenticates with the key rather than the Auth Token.
2. A Memory Store, which holds profiles and conversation memory.
3. A Conversation Configuration bound to that store, with memory extraction on,
   capture rules for the Messaging Service's senders, and a status callback at
   `<domain>/webhook`.

`TWILIO_MESSAGING_SERVICE_SID` is the only sender configuration and is
required. Senders are read off that service — phone numbers, short codes and
WhatsApp senders — and a capture rule is written for each, per channel. So
adding a sender to the service in the Console and re-running this is what makes
it reachable; nothing has to be listed in `.env` to match. The service is also
what the agent sends as, which is why no sender address is needed anywhere else.

Alphanumeric Sender IDs are skipped: they are send-only, so nothing arrives
inbound on one and a capture rule for one would match nothing.

`--dry-run` reads the account and prints what it would create or change,
including the capture rules it would write, and changes nothing. Every request
is checked before it leaves, so it cannot write even by mistake. It needs an
API key, because minting one is itself a change.

Re-running is safe. Each resource is reused when its id is already in the
environment, or when one with the same display name already exists on the
account, and is created only otherwise. Display names are unique per account,
which is what makes that check reliable.

A reused Conversation Configuration is patched in place to match this run, so
changing `--webhook-domain` or adding a sender updates it and keeps its id.

The one thing that cannot be re-derived is an API secret: it is returned only at
creation. A key whose secret is lost is unusable here, so `--new-api-key` mints
a replacement.
"""

import argparse
import asyncio
import base64
import json
import os
import sys

import httpx
from dotenv import load_dotenv

from app.config import MESSAGING_SERVICE_SID

CLASSIC_API = "https://api.twilio.com/2010-04-01"
MEMORY_API = "https://memory.twilio.com/v1/ControlPlane"
CONVERSATION_API = "https://conversations.twilio.com/v2/ControlPlane"
MESSAGING_API = "https://messaging.twilio.com/v1"

POLL_ATTEMPTS = 30
POLL_SECONDS = 2.0


def basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class ProvisionError(RuntimeError):
    pass


# Stands in for an id on a dry run, where the resource that would carry it has
# not been created. It is only ever printed.
WOULD_CREATE = "(would be created)"


async def refuse_mutation(request: httpx.Request) -> None:
    """Fail any request that is not a read.

    `dry_run` is threaded through the ensure/sync functions so each can say
    what it would do, and this is what makes that a guarantee rather than a
    promise: a write that slips past a missed branch fails here instead of
    changing a live account. Registered as an httpx event hook, so it sees
    every request the script makes.
    """
    if request.method not in ("GET", "HEAD"):
        raise ProvisionError(
            f"--dry-run tried to {request.method} {request.url}, which would have "
            "changed the account. Nothing mutates on a dry run, so this is a bug in "
            "the dry-run branches rather than something you did."
        )


def _json_or_raise(response: httpx.Response, what: str) -> dict:
    if response.status_code not in (200, 201, 202):
        raise ProvisionError(f"{what} failed: HTTP {response.status_code} {response.text}")
    try:
        return response.json()
    except ValueError:
        raise ProvisionError(
            f"{what} returned HTTP {response.status_code} with a non-JSON body: {response.text!r}"
        ) from None


def _items(body: dict) -> list[object]:
    """The collection out of a list response, whatever the envelope calls it.

    Entries are objects on some control-plane collections and bare id strings on
    others, so callers handle both.
    """
    if isinstance(body.get("meta"), dict):
        key = body["meta"].get("key")
        if isinstance(key, str) and isinstance(body.get(key), list):
            return list(body[key])
    for value in body.values():
        if isinstance(value, list):
            return list(value)
    return []


async def create_api_key(client: httpx.AsyncClient, account_sid: str, auth_token: str) -> dict:
    """Mint a standard API key. Authenticates with the Auth Token, which is the
    only step that needs it."""
    response = await client.post(
        f"{CLASSIC_API}/Accounts/{account_sid}/Keys.json",
        headers={"Authorization": basic_auth(account_sid, auth_token)},
        data={"FriendlyName": "curesyngap1-agent"},
    )
    key = _json_or_raise(response, "API key creation")
    return {"sid": key["sid"], "secret": key["secret"]}


async def poll_operation(client: httpx.AsyncClient, status_url: str, auth: str) -> dict:
    """Wait for an async control-plane operation and return its result."""
    for _ in range(POLL_ATTEMPTS):
        body = _json_or_raise(
            await client.get(status_url, headers={"Authorization": auth}), "operation poll"
        )
        status = (body.get("status") or "").upper()
        if status == "COMPLETED":
            return body.get("result") or {}
        if status == "FAILED":
            raise ProvisionError(f"operation failed: {body.get('error', 'unknown error')}")
        await asyncio.sleep(POLL_SECONDS)
    raise ProvisionError(f"operation did not complete after {POLL_ATTEMPTS} polls: {status_url}")


async def ensure_memory_store(
    client: httpx.AsyncClient,
    auth: str,
    name: str,
    known_id: str | None,
    dry_run: bool = False,
) -> tuple[str, bool]:
    """Return the Memory Store id, creating one only if none exists.

    The second element is True when this call created it, or on a dry run would
    have. `dry_run` returns `WOULD_CREATE` in place of the id it never made.
    """
    if known_id:
        return known_id, False

    existing = await find_by_display_name(client, auth, f"{MEMORY_API}/Stores", name)
    if existing:
        return existing, False

    if dry_run:
        return WOULD_CREATE, True
    return await create_memory_store(client, auth, name), True


async def ensure_conversation_configuration(
    client: httpx.AsyncClient,
    auth: str,
    name: str,
    known_id: str | None,
    dry_run: bool = False,
    **create_kwargs: object,
) -> tuple[str, bool]:
    """Return the Conversation Configuration id, creating one only if none exists.

    An existing configuration is returned as it stands; `sync_configuration`
    is what brings it in line with the current run. `dry_run` returns
    `WOULD_CREATE` in place of the id it never made.
    """
    if known_id:
        return known_id, False

    existing = await find_by_display_name(client, auth, f"{CONVERSATION_API}/Configurations", name)
    if existing:
        return existing, False

    if dry_run:
        return WOULD_CREATE, True
    created = await create_conversation_configuration(client, auth, name, **create_kwargs)
    return created, True


def _routing(channel_settings: dict[str, object]) -> dict[str, list[tuple[str, str]]]:
    """The addresses each channel captures, as a comparable value.

    Comparing whole `channelSettings` would patch on every run, because the
    server fills in fields this script does not send. Comparing only the
    channel names would miss a changed phone number, leaving the capture rules
    pointed at the previous one.
    """
    return {
        channel: sorted(
            (rule.get("from", ""), rule.get("to", ""))
            for rule in (settings or {}).get("captureRules", [])
        )
        for channel, settings in channel_settings.items()
    }


async def sync_configuration(
    client: httpx.AsyncClient,
    auth: str,
    configuration_id: str,
    *,
    webhook_url: str,
    channel_settings: dict[str, object],
    dry_run: bool = False,
) -> list[str]:
    """Bring an existing configuration in line with this run, in place.

    Patching rather than rebuilding keeps the configuration id stable, so a
    changed tunnel domain does not mean editing `.env` again. Returns a
    description of what changed, or on a dry run what would change.
    """
    current = await describe_configuration(client, auth, configuration_id)
    patch: dict[str, object] = {}
    changes: list[str] = []

    callbacks = [callback.get("url") for callback in (current.get("statusCallbacks") or [])]
    if callbacks != [webhook_url]:
        patch["statusCallbacks"] = [{"url": webhook_url, "method": "POST"}]
        changes.append(f"status callback {callbacks or 'unset'} -> {webhook_url}")

    if not current.get("memoryExtractionEnabled"):
        patch["memoryExtractionEnabled"] = True
        changes.append("memory extraction on")

    current_channels = current.get("channelSettings") or {}
    if _routing(current_channels) != _routing(channel_settings):
        patch["channelSettings"] = channel_settings
        changes.append(f"channels {sorted(current_channels)} -> {sorted(channel_settings)}")

    if not patch or dry_run:
        return changes

    response = await client.patch(
        f"{CONVERSATION_API}/Configurations/{configuration_id}",
        headers={"Authorization": auth, "Content-Type": "application/json"},
        json=patch,
    )
    body = _json_or_raise(response, "configuration update")
    if response.status_code == 202:
        await poll_operation(client, body["statusUrl"], auth)
    return changes


async def describe_configuration(
    client: httpx.AsyncClient, auth: str, configuration_id: str
) -> dict:
    return _json_or_raise(
        await client.get(
            f"{CONVERSATION_API}/Configurations/{configuration_id}",
            headers={"Authorization": auth},
        ),
        "configuration fetch",
    )


async def create_memory_store(client: httpx.AsyncClient, auth: str, name: str) -> str:
    response = await client.post(
        f"{MEMORY_API}/Stores",
        headers={"Authorization": auth, "Content-Type": "application/json"},
        json={
            "displayName": name,
            "description": "Conversation memory and profiles for the CURE SYNGAP1 agent",
        },
    )
    body = _json_or_raise(response, "Memory Store creation")
    if response.status_code == 202:
        body = await poll_operation(client, body["statusUrl"], auth)
    store_id = body.get("id")
    if not store_id:
        raise ProvisionError(f"Memory Store creation returned no id: {body}")
    return store_id


async def find_by_display_name(
    client: httpx.AsyncClient, auth: str, url: str, display_name: str
) -> str | None:
    """Look up a control-plane resource's id by its unique display name.

    Collections that list bare ids are expanded one resource at a time, since
    the display name is only on the detail representation.
    """
    body = _json_or_raise(await client.get(url, headers={"Authorization": auth}), "list")
    for item in _items(body):
        if isinstance(item, str):
            detail = _json_or_raise(
                await client.get(f"{url}/{item}", headers={"Authorization": auth}), "detail"
            )
            if detail.get("displayName") == display_name:
                return detail.get("id") or item
        elif isinstance(item, dict) and item.get("displayName") == display_name:
            return item.get("id")
    return None


async def create_conversation_configuration(
    client: httpx.AsyncClient,
    auth: str,
    name: str,
    *,
    memory_store_id: str,
    webhook_url: str,
    channel_settings: dict[str, object],
) -> str:
    """Create the configuration that routes inbound messages to the webhook.

    `GROUP_BY_PARTICIPANT_ADDRESSES_AND_CHANNEL_TYPE` is the grouping the TAC
    setup wizard uses. It is also what merges a returning contact's SMS and
    WhatsApp threads, since both carry the same phone number.
    """
    response = await client.post(
        f"{CONVERSATION_API}/Configurations",
        headers={"Authorization": auth, "Content-Type": "application/json"},
        json={
            "displayName": name,
            "description": "CURE SYNGAP1 information agent",
            "conversationGroupingType": "GROUP_BY_PARTICIPANT_ADDRESSES_AND_CHANNEL_TYPE",
            "memoryStoreId": memory_store_id,
            "memoryExtractionEnabled": True,
            "channelSettings": channel_settings,
            "statusCallbacks": [{"url": webhook_url, "method": "POST"}],
        },
    )
    body = _json_or_raise(response, "Conversation Configuration creation")
    created = dict(body)
    if response.status_code == 202:
        status_url = body.get("statusUrl")
        if not status_url:
            raise ProvisionError(
                f"HTTP 202 without a statusUrl to poll; body was {body!r}"
            )
        created = await poll_operation(client, status_url, auth)

    configuration_id = created.get("id")
    if configuration_id:
        return configuration_id

    # Creation reported success without echoing an id. The resource usually
    # exists anyway, and displayName is unique, so look it up.
    print(
        f"  Creation returned HTTP {response.status_code} with no id "
        f"(body {body!r}, result {created!r}); looking it up by display name",
        file=sys.stderr,
    )
    configuration_id = await find_by_display_name(
        client, auth, f"{CONVERSATION_API}/Configurations", name
    )
    if not configuration_id:
        raise ProvisionError(
            f"Conversation Configuration {name!r} returned no id and is not in the "
            "configuration list. Re-run with --list to inspect the account."
        )
    return configuration_id


def _capture_rules(*addresses: str) -> list[dict[str, str]]:
    """Capture both directions of traffic for each agent address."""
    return [
        rule
        for address in addresses
        for rule in ({"from": "*", "to": address}, {"from": address, "to": "*"})
    ]


def build_channel_settings(senders: dict[str, list[str]]) -> dict[str, object]:
    """Channel settings for the senders a Messaging Service actually holds.

    Keyed by Conversation Orchestrator channel, each with the addresses found
    on that channel. A channel with no sender is left out entirely rather than
    given empty capture rules, so it captures nothing.
    """
    settings: dict[str, object] = {
        channel: {
            "statusTimeouts": {"inactive": 2, "closed": 3},
            "captureRules": _capture_rules(*addresses),
        }
        for channel, addresses in sorted(senders.items())
        if addresses
    }
    if not settings:
        raise ProvisionError("No senders configured; nothing to capture.")
    return settings


# The sender subresources of a Messaging Service, and the field each one names
# its address in. Alphanumeric Sender IDs (`/AlphaSenders`) are deliberately
# absent: they are send-only, so nothing can be captured inbound on one and a
# capture rule for one would match nothing.
#
# `/ChannelSenders` covers WhatsApp. Unlike the others it has no reference page
# of its own — it appears only in the Service resource's `links` — so the field
# its address lands in is not documented. The candidates are tried in order and
# the first that looks like an address wins; `_channel_sender_address` says what
# happens when none does.
SENDER_SOURCES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("PhoneNumbers", "SMS", ("phone_number",)),
    ("ShortCodes", "SMS", ("short_code",)),
    ("ChannelSenders", "WHATSAPP", ("sender_id", "address", "channel_sender", "sender")),
)


def _channel_sender_address(item: dict, fields: tuple[str, ...]) -> str | None:
    """The address out of one sender record, or None if it carries none.

    Conversation Orchestrator wants an E.164 address, and a WhatsApp sender's
    is reported with a `whatsapp:` prefix that has to come off: CO accepts
    either form, but a rule written one way does not match traffic described
    the other.
    """
    for field in fields:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip().removeprefix("whatsapp:")
    return None


async def fetch_senders(
    client: httpx.AsyncClient, auth: str, service_sid: str
) -> dict[str, list[str]]:
    """The senders in a Messaging Service, by Conversation Orchestrator channel.

    This is what makes the service the only place a sender is configured: the
    capture rules are written from what the service actually holds rather than
    from an environment variable that has to be kept in step with it.

    A subresource that reports a sender whose address cannot be read is an
    error rather than a silent omission, because the result is a sender nobody
    can reach and nothing that says so.
    """
    senders: dict[str, list[str]] = {}
    for subresource, channel, fields in SENDER_SOURCES:
        url = f"{MESSAGING_API}/Services/{service_sid}/{subresource}?PageSize=1000"
        while url:
            body = _json_or_raise(
                await client.get(url, headers={"Authorization": auth}),
                f"Messaging Service {service_sid} {subresource} read",
            )
            for item in _items(body):
                if not isinstance(item, dict):
                    continue
                address = _channel_sender_address(item, fields)
                if not address:
                    raise ProvisionError(
                        f"A sender in {subresource} on Messaging Service {service_sid} "
                        f"has no address in any of {', '.join(fields)}: {item!r}. It "
                        "would be left uncaptured, so it is not being skipped "
                        "quietly — add the field it does use to SENDER_SOURCES."
                    )
                senders.setdefault(channel, []).append(address)
            url = (body.get("meta") or {}).get("next_page_url") or ""
    return senders


async def list_resources(client: httpx.AsyncClient, auth: str) -> None:
    for label, url in (
        ("Memory Stores", f"{MEMORY_API}/Stores"),
        ("Conversation Configurations", f"{CONVERSATION_API}/Configurations"),
    ):
        body = _json_or_raise(await client.get(url, headers={"Authorization": auth}), label)
        items = _items(body)
        print(f"\n{label}:")
        if not items:
            print("  (none)")
        for item in items:
            if isinstance(item, str):
                item = _json_or_raise(
                    await client.get(f"{url}/{item}", headers={"Authorization": auth}), "detail"
                )
            print(f"  {item.get('id')}  {item.get('displayName')}  {item.get('status', '')}")


def validate_display_name(name: str) -> str:
    """Display names must be unique, URL-safe, and at most 32 characters."""
    if len(name) > 32 or not all(char.isalnum() or char in "._~-" for char in name):
        raise ProvisionError(
            f"Invalid display name {name!r}: max 32 characters, letters, numbers, "
            "and . _ ~ - only"
        )
    return name


async def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--webhook-domain",
        help="Public host serving POST /webhook, without a scheme (e.g. abc.ngrok.app)",
    )
    parser.add_argument("--name", default="curesyngap1-agent", help="Display name prefix")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List existing Memory Stores and Conversation Configurations and exit",
    )
    parser.add_argument(
        "--api-key",
        help="API key SID to use, overriding TWILIO_API_KEY (with --api-secret)",
    )
    parser.add_argument("--api-secret", help="Secret for --api-key")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read the account and print what would be created or changed, changing nothing",
    )
    parser.add_argument(
        "--new-api-key",
        action="store_true",
        help="Mint a new API key even though one is configured",
    )
    parser.add_argument(
        "--memory-store-id",
        help="Memory Store to use, overriding TWILIO_MEMORY_STORE_ID",
    )

    args = parser.parse_args()

    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    service_sid = (os.environ.get("TWILIO_MESSAGING_SERVICE_SID") or "").strip()
    known_store_id = (os.environ.get("TWILIO_MEMORY_STORE_ID") or "").strip()

    if not account_sid or not auth_token:
        print("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env first.", file=sys.stderr)
        return 1

    # An existing key is reused rather than piling up keys on the account. The
    # secret cannot be read back, so a key without its secret is unusable and
    # counts as absent.
    api_key = args.api_key or os.environ.get("TWILIO_API_KEY") or ""
    api_secret = args.api_secret or os.environ.get("TWILIO_API_SECRET") or ""
    have_key = bool(api_key.strip() and api_secret.strip()) and not args.new_api_key

    if api_key.strip() and not api_secret.strip() and not args.new_api_key:
        print(
            f"TWILIO_API_KEY is set ({api_key.strip()}) but TWILIO_API_SECRET is empty. "
            "The secret is only readable at creation, so that key cannot be used here. "
            "Pass --new-api-key to mint a replacement, or fill in the secret if you "
            "have it.",
            file=sys.stderr,
        )
        return 1

    # On a dry run every request is checked before it leaves, so a write that
    # slips past a dry-run branch cannot reach the account.
    hooks = {"request": [refuse_mutation]} if args.dry_run else {}
    async with httpx.AsyncClient(timeout=30.0, event_hooks=hooks) as client:
        if args.dry_run and not have_key:
            # Minting a key is itself a change, so a dry run cannot be the run
            # that does it. Making one by hand is the only way to keep this
            # read-only, and the key is needed either way.
            print(
                "--dry-run needs an API key already set, because minting one is itself "
                "a change to the account.\n\n"
                "Create one in the Console under Account > API keys & tokens > Create "
                "API key, of type Standard, and put both values in .env:\n\n"
                "  TWILIO_API_KEY=SK...\n"
                "  TWILIO_API_SECRET=...\n\n"
                "The secret is shown once. Alternatively pass --api-key and "
                "--api-secret, or run without --dry-run and let this mint the key for "
                "you — that writes to the account, which is what --dry-run is for "
                "avoiding.",
                file=sys.stderr,
            )
            return 1
        if args.dry_run:
            print("Dry run: reading the account only. Nothing will be created or changed.\n")
        if have_key:
            api_key, api_secret = api_key.strip(), api_secret.strip()
            print(f"Using configured API key {api_key}")
        elif args.list:
            print(
                "--list needs an API key: set TWILIO_API_KEY and TWILIO_API_SECRET, "
                "or pass --api-key and --api-secret.",
                file=sys.stderr,
            )
            return 1
        else:
            key = await create_api_key(client, account_sid, auth_token)
            api_key, api_secret = key["sid"], key["secret"]
            # Printed immediately: the secret is readable only at creation, so a
            # failure further down must not be what loses it.
            print("Created API key. Put these in .env now:")
            print(f"TWILIO_API_KEY={api_key}")
            print(f"TWILIO_API_SECRET={api_secret}\n")

        auth = basic_auth(api_key, api_secret)

        if args.list:
            await list_resources(client, auth)
            return 0

        if not args.webhook_domain:
            print("--webhook-domain is required.", file=sys.stderr)
            return 1

        if not service_sid:
            print(
                "Set TWILIO_MESSAGING_SERVICE_SID in .env. Every sender lives in a "
                "Messaging Service's pool, and that service is what the agent sends "
                "as and what these capture rules are built from.",
                file=sys.stderr,
            )
            return 1
        if not MESSAGING_SERVICE_SID.match(service_sid):
            print(
                f"TWILIO_MESSAGING_SERVICE_SID is {service_sid!r}; it must be a "
                "Messaging Service SID, which is MG followed by 32 hex characters.",
                file=sys.stderr,
            )
            return 1

        # The service is the only place a sender is configured, so what it holds
        # is what gets captured.
        senders = await fetch_senders(client, auth, service_sid)
        if not senders:
            print(
                f"Messaging Service {service_sid} holds no senders that can carry a "
                "conversation, so there is nothing to capture. Add a phone number, "
                "short code or WhatsApp sender to it in the Console under "
                "Messaging > Services > Senders.",
                file=sys.stderr,
            )
            return 1
        print(f"Messaging Service {service_sid} senders:")
        for channel, addresses in sorted(senders.items()):
            print(f"  {channel}: {', '.join(addresses)}")

        # An inbound webhook on the service answers alongside the agent: the
        # capture rules deliver the message here regardless, so a contact gets
        # two replies. Worth catching now rather than from a confused report.
        service = _json_or_raise(
            await client.get(
                f"{MESSAGING_API}/Services/{service_sid}",
                headers={"Authorization": auth},
            ),
            f"Messaging Service {service_sid} read",
        )
        if (service.get("inbound_request_url") or "").strip():
            print(
                f"  Warning: the service has inbound_request_url set to "
                f"{service['inbound_request_url']}. Capture rules deliver inbound "
                "messages to this agent anyway, so whatever that URL replies with "
                "reaches the contact alongside the agent's answer. Clear it under "
                "Messaging > Services > Integration unless it is deliberate."
            )

        store_name = validate_display_name(f"{args.name}-memory")
        config_name = validate_display_name(args.name)
        webhook_url = f"https://{args.webhook_domain.rstrip('/')}/webhook"

        memory_store_id, store_created = await ensure_memory_store(
            client,
            auth,
            store_name,
            args.memory_store_id or known_store_id or None,
            dry_run=args.dry_run,
        )
        made = ("Would create" if args.dry_run else "Created") if store_created else "Reusing"
        print(f"{made} Memory Store {memory_store_id}")

        known_configuration_id = (
            os.environ.get("TWILIO_CONVERSATION_CONFIGURATION_ID") or ""
        ).strip() or None

        channel_settings = build_channel_settings(senders)

        configuration_id, configuration_created = await ensure_conversation_configuration(
            client,
            auth,
            config_name,
            known_configuration_id,
            dry_run=args.dry_run,
            memory_store_id=memory_store_id,
            webhook_url=webhook_url,
            channel_settings=channel_settings,
        )
        made = (
            ("Would create" if args.dry_run else "Created")
            if configuration_created
            else "Reusing"
        )
        print(f"{made} Conversation Configuration {configuration_id}")

        if not configuration_created:
            changes = await sync_configuration(
                client,
                auth,
                configuration_id,
                webhook_url=webhook_url,
                channel_settings=channel_settings,
                dry_run=args.dry_run,
            )
            for change in changes:
                print(f"  {'Would update' if args.dry_run else 'Updated'}: {change}")
            if args.dry_run and not changes:
                print("  Nothing to update; it already matches this run.")

    if args.dry_run:
        print("\nCapture rules it would write:\n")
        print(json.dumps(channel_settings, indent=2, sort_keys=True))
        print(f"\nWebhook the configuration would call: {webhook_url}")
        print("\nRe-run without --dry-run to apply this.")
        return 0

    print("\nAdd these to .env, so a re-run reuses them instead of looking them up:\n")
    print(f"TWILIO_MEMORY_STORE_ID={memory_store_id}")
    print(f"TWILIO_CONVERSATION_CONFIGURATION_ID={configuration_id}")
    if not have_key:
        print("\nAnd the API key printed above, if you have not already:\n")
        print(f"TWILIO_API_KEY={api_key}")
        print(f"TWILIO_API_SECRET={api_secret}")
    for channel, addresses in sorted(senders.items()):
        print(f"Captured on {channel}: {', '.join(addresses)}")
    if service_sid:
        print(f"SMS sends as Messaging Service {service_sid}, which picks the sender")
    print(f"Webhook the configuration will call: {webhook_url}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except ProvisionError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
