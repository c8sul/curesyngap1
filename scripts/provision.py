"""Create the Twilio resources the agent needs, from an Account SID and Auth Token.

    docker compose run --rm provision --webhook-domain <public-host>

Ensures three things exist and prints the `.env` lines for them:

1. A scoped API key and secret. Every other call here, and the application
   itself, authenticates with the key rather than the Auth Token.
2. A Memory Store, which holds profiles and conversation memory.
3. A Conversation Configuration bound to that store, with memory extraction on,
   capture rules for whichever senders are configured, and a status callback at
   `<domain>/webhook`.
   Set `TWILIO_PHONE_NUMBER` for SMS, `TWILIO_WHATSAPP_NUMBER` for WhatsApp, or
   both. At least one is required.

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
import os
import sys

import httpx
from dotenv import load_dotenv

CLASSIC_API = "https://api.twilio.com/2010-04-01"
MEMORY_API = "https://memory.twilio.com/v1/ControlPlane"
CONVERSATION_API = "https://conversations.twilio.com/v2/ControlPlane"

POLL_ATTEMPTS = 30
POLL_SECONDS = 2.0


def basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class ProvisionError(RuntimeError):
    pass


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
    client: httpx.AsyncClient, auth: str, name: str, known_id: str | None
) -> tuple[str, bool]:
    """Return the Memory Store id, creating one only if none exists.

    The second element is True when this call created it.
    """
    if known_id:
        return known_id, False

    existing = await find_by_display_name(client, auth, f"{MEMORY_API}/Stores", name)
    if existing:
        return existing, False

    return await create_memory_store(client, auth, name), True


async def ensure_conversation_configuration(
    client: httpx.AsyncClient,
    auth: str,
    name: str,
    known_id: str | None,
    **create_kwargs: object,
) -> tuple[str, bool]:
    """Return the Conversation Configuration id, creating one only if none exists.

    An existing configuration is returned as it stands; `sync_configuration`
    is what brings it in line with the current run.
    """
    if known_id:
        return known_id, False

    existing = await find_by_display_name(client, auth, f"{CONVERSATION_API}/Configurations", name)
    if existing:
        return existing, False

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
) -> list[str]:
    """Bring an existing configuration in line with this run, in place.

    Patching rather than rebuilding keeps the configuration id stable, so a
    changed tunnel domain does not mean editing `.env` again. Returns a
    description of what changed.
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

    if not patch:
        return []

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


def _capture_rules(address: str) -> list[dict[str, str]]:
    """Capture both directions of traffic for one agent address."""
    return [{"from": "*", "to": address}, {"from": address, "to": "*"}]


def build_channel_settings(
    phone_number: str | None, whatsapp_number: str | None
) -> dict[str, object]:
    """Channel settings for the senders that are configured."""
    settings: dict[str, object] = {}
    if phone_number:
        settings["SMS"] = {
            "statusTimeouts": {"inactive": 2, "closed": 3},
            "captureRules": _capture_rules(phone_number),
        }
    if whatsapp_number:
        settings["WHATSAPP"] = {
            "statusTimeouts": {"inactive": 2, "closed": 3},
            "captureRules": _capture_rules(whatsapp_number),
        }
    if not settings:
        raise ProvisionError("No senders configured; nothing to capture.")
    return settings


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
    phone_number = (os.environ.get("TWILIO_PHONE_NUMBER") or "").strip()
    whatsapp_number = (os.environ.get("TWILIO_WHATSAPP_NUMBER") or "").strip()
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

    async with httpx.AsyncClient(timeout=30.0) as client:
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

        if phone_number and not phone_number.startswith("+"):
            print(
                f"TWILIO_PHONE_NUMBER is {phone_number!r}; set it to an E.164 number "
                "(e.g. +15555550100), or leave it empty for WhatsApp only.",
                file=sys.stderr,
            )
            return 1
        if whatsapp_number and not whatsapp_number.startswith("whatsapp:+"):
            print(
                f"TWILIO_WHATSAPP_NUMBER is {whatsapp_number!r}; it must look like "
                "whatsapp:+14155238886.",
                file=sys.stderr,
            )
            return 1
        if not phone_number and not whatsapp_number:
            print(
                "Set TWILIO_PHONE_NUMBER, TWILIO_WHATSAPP_NUMBER, or both in .env. "
                "For sandbox testing, TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886.",
                file=sys.stderr,
            )
            return 1

        store_name = validate_display_name(f"{args.name}-memory")
        config_name = validate_display_name(args.name)
        webhook_url = f"https://{args.webhook_domain.rstrip('/')}/webhook"

        memory_store_id, store_created = await ensure_memory_store(
            client, auth, store_name, args.memory_store_id or known_store_id or None
        )
        print(
            f"{'Created' if store_created else 'Reusing'} Memory Store {memory_store_id}"
        )

        known_configuration_id = (
            os.environ.get("TWILIO_CONVERSATION_CONFIGURATION_ID") or ""
        ).strip() or None

        channel_settings = build_channel_settings(phone_number or None, whatsapp_number or None)

        configuration_id, configuration_created = await ensure_conversation_configuration(
            client,
            auth,
            config_name,
            known_configuration_id,
            memory_store_id=memory_store_id,
            webhook_url=webhook_url,
            channel_settings=channel_settings,
        )
        print(
            f"{'Created' if configuration_created else 'Reusing'} "
            f"Conversation Configuration {configuration_id}"
        )

        if not configuration_created:
            changes = await sync_configuration(
                client,
                auth,
                configuration_id,
                webhook_url=webhook_url,
                channel_settings=channel_settings,
            )
            for change in changes:
                print(f"  Updated: {change}")

    print("\nAdd these to .env, so a re-run reuses them instead of looking them up:\n")
    print(f"TWILIO_MEMORY_STORE_ID={memory_store_id}")
    print(f"TWILIO_CONVERSATION_CONFIGURATION_ID={configuration_id}")
    if not have_key:
        print("\nAnd the API key printed above, if you have not already:\n")
        print(f"TWILIO_API_KEY={api_key}")
        print(f"TWILIO_API_SECRET={api_secret}")
    print(
        "Senders captured: "
        + ", ".join(filter(None, (phone_number or None, whatsapp_number or None)))
    )
    print(f"Webhook the configuration will call: {webhook_url}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except ProvisionError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
