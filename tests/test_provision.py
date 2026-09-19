"""Provisioning tests, over a fake transport rather than the Twilio API.

The behavior worth protecting is that a second run creates nothing. These
tests assert on the requests the script issues, so a regression that starts
re-creating a Memory Store or re-patching an unchanged configuration fails
here rather than on someone's account.
"""

import httpx
import provision
import pytest
from provision import (
    CONVERSATION_API,
    MEMORY_API,
    MESSAGING_API,
    ProvisionError,
    _items,
    _routing,
    build_channel_settings,
    ensure_conversation_configuration,
    ensure_memory_store,
    fetch_senders,
    find_by_display_name,
    poll_operation,
    refuse_mutation,
    sync_configuration,
    validate_display_name,
)

AUTH = "Basic stub"
SERVICE_SID = "MG" + "0" * 32


class Recorder:
    """Answers requests from a routing table and records what was asked."""

    def __init__(self, routes: dict[tuple[str, str], dict | tuple[int, dict]]) -> None:
        self.routes = routes
        self.seen: list[tuple[str, str]] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, str(request.url))
        self.seen.append(key)
        if key not in self.routes:
            raise AssertionError(f"unexpected request {key[0]} {key[1]}")
        answer = self.routes[key]
        status, body = answer if isinstance(answer, tuple) else (200, answer)
        return httpx.Response(status, json=body)

    def methods(self) -> list[str]:
        return [method for method, _ in self.seen]


# --- reuse: the second run must not create anything ---


async def test_a_known_store_id_is_reused_without_any_request():
    recorder = Recorder({})

    async with recorder.client() as client:
        store_id, created = await ensure_memory_store(client, AUTH, "agent", "mem_store_known")

    assert (store_id, created) == ("mem_store_known", False)
    assert recorder.seen == []


async def test_a_store_matching_the_display_name_is_reused_rather_than_created():
    recorder = Recorder(
        {
            ("GET", f"{MEMORY_API}/Stores"): {
                "meta": {"key": "stores"},
                "stores": ["mem_store_existing"],
            },
            ("GET", f"{MEMORY_API}/Stores/mem_store_existing"): {
                "id": "mem_store_existing",
                "displayName": "agent",
            },
        }
    )

    async with recorder.client() as client:
        store_id, created = await ensure_memory_store(client, AUTH, "agent", None)

    assert (store_id, created) == ("mem_store_existing", False)
    assert "POST" not in recorder.methods()


async def test_a_known_configuration_id_is_reused_without_any_request():
    recorder = Recorder({})

    async with recorder.client() as client:
        configuration_id, created = await ensure_conversation_configuration(
            client, AUTH, "agent", "conv_configuration_known"
        )

    assert (configuration_id, created) == ("conv_configuration_known", False)
    assert recorder.seen == []


async def test_a_collection_of_bare_ids_is_expanded_to_find_the_display_name():
    """Stores list as id strings; the display name is only on the detail
    representation, so a match needs a second request per entry."""
    recorder = Recorder(
        {
            ("GET", f"{MEMORY_API}/Stores"): {
                "meta": {"key": "stores"},
                "stores": ["mem_store_other", "mem_store_wanted"],
            },
            ("GET", f"{MEMORY_API}/Stores/mem_store_other"): {
                "id": "mem_store_other",
                "displayName": "something-else",
            },
            ("GET", f"{MEMORY_API}/Stores/mem_store_wanted"): {
                "id": "mem_store_wanted",
                "displayName": "agent",
            },
        }
    )

    async with recorder.client() as client:
        found = await find_by_display_name(client, AUTH, f"{MEMORY_API}/Stores", "agent")

    assert found == "mem_store_wanted"


async def test_a_display_name_that_matches_nothing_returns_none():
    recorder = Recorder(
        {("GET", f"{CONVERSATION_API}/Configurations"): {"configurations": []}}
    )

    async with recorder.client() as client:
        assert (
            await find_by_display_name(
                client, AUTH, f"{CONVERSATION_API}/Configurations", "agent"
            )
            is None
        )


# --- sync: patch only what actually differs ---


def _configuration(**overrides: object) -> dict:
    base = {
        "id": "conv_configuration_1",
        "statusCallbacks": [{"url": "https://host/webhook", "method": "POST"}],
        "memoryExtractionEnabled": True,
        "channelSettings": {
            "WHATSAPP": {
                "statusTimeouts": {"inactive": 2, "closed": 3},
                "captureRules": [
                    {"from": "*", "to": "+1", "metadata": {}},
                    {"from": "+1", "to": "*", "metadata": {}},
                ],
            }
        },
    }
    return {**base, **overrides}


async def _sync(
    current: dict, *, webhook_url: str, channel_settings: dict, dry_run: bool = False
) -> tuple:
    routes: dict = {("GET", f"{CONVERSATION_API}/Configurations/conv_configuration_1"): current}
    routes[("PATCH", f"{CONVERSATION_API}/Configurations/conv_configuration_1")] = (200, {})
    recorder = Recorder(routes)
    async with recorder.client() as client:
        changes = await sync_configuration(
            client,
            AUTH,
            "conv_configuration_1",
            webhook_url=webhook_url,
            channel_settings=channel_settings,
            dry_run=dry_run,
        )
    return changes, recorder


async def test_an_unchanged_configuration_is_not_patched():
    changes, recorder = await _sync(
        _configuration(),
        webhook_url="https://host/webhook",
        channel_settings=build_channel_settings({"WHATSAPP": ["+1"]}),
    )

    assert changes == []
    assert "PATCH" not in recorder.methods()


async def test_a_changed_webhook_domain_patches_the_callback():
    changes, recorder = await _sync(
        _configuration(),
        webhook_url="https://new-host/webhook",
        channel_settings=build_channel_settings({"WHATSAPP": ["+1"]}),
    )

    assert "PATCH" in recorder.methods()
    assert any("status callback" in change for change in changes)


async def test_memory_extraction_is_turned_on_when_it_is_off():
    changes, _ = await _sync(
        _configuration(memoryExtractionEnabled=False),
        webhook_url="https://host/webhook",
        channel_settings=build_channel_settings({"WHATSAPP": ["+1"]}),
    )

    assert changes == ["memory extraction on"]


async def test_a_changed_sender_number_patches_the_capture_rules():
    """The channel set is still {"WHATSAPP"}, so only a value-level comparison
    notices that the rules point at the previous number."""
    changes, recorder = await _sync(
        _configuration(),
        webhook_url="https://host/webhook",
        channel_settings=build_channel_settings({"WHATSAPP": ["+2"]}),
    )

    assert "PATCH" in recorder.methods()
    assert any("channels" in change for change in changes)


def test_server_supplied_fields_do_not_count_as_a_difference():
    """`statusTimeouts` and `metadata` come back from the server whether or not
    the script sent them, so comparing them would patch on every run."""
    ours = build_channel_settings({"WHATSAPP": ["+1"]})
    theirs = _configuration()["channelSettings"]

    assert _routing(ours) == _routing(theirs)


# --- operation polling ---


async def test_polling_returns_the_result_of_a_completed_operation():
    recorder = Recorder(
        {("GET", "https://host/op"): {"status": "COMPLETED", "result": {"id": "made_1"}}}
    )

    async with recorder.client() as client:
        assert await poll_operation(client, "https://host/op", AUTH) == {"id": "made_1"}


async def test_polling_raises_with_the_error_from_a_failed_operation():
    recorder = Recorder({("GET", "https://host/op"): {"status": "FAILED", "error": "no capacity"}})

    async with recorder.client() as client:
        with pytest.raises(ProvisionError, match="no capacity"):
            await poll_operation(client, "https://host/op", AUTH)


async def test_polling_gives_up_rather_than_hanging(monkeypatch):
    monkeypatch.setattr(provision, "POLL_ATTEMPTS", 2)
    monkeypatch.setattr(provision, "POLL_SECONDS", 0)
    recorder = Recorder({("GET", "https://host/op"): {"status": "IN_PROGRESS"}})

    async with recorder.client() as client:
        with pytest.raises(ProvisionError, match="did not complete"):
            await poll_operation(client, "https://host/op", AUTH)

    assert recorder.methods() == ["GET", "GET"]


async def test_an_http_error_is_reported_with_its_status_and_body():
    recorder = Recorder({("GET", "https://host/op"): (403, {"message": "not authorized"})})

    async with recorder.client() as client:
        with pytest.raises(ProvisionError, match="HTTP 403"):
            await poll_operation(client, "https://host/op", AUTH)


# --- senders read off the Messaging Service ---


def _senders_url(subresource: str, page: str = "") -> str:
    return f"{MESSAGING_API}/Services/{SERVICE_SID}/{subresource}?PageSize=1000{page}"


def _empty(key: str) -> dict:
    return {"meta": {"key": key}, key: []}


def _sender_routes(**bodies: dict) -> dict:
    """Routes for every sender subresource, empty unless a body is given."""
    defaults = {
        "PhoneNumbers": _empty("phone_numbers"),
        "ShortCodes": _empty("short_codes"),
        "ChannelSenders": _empty("channel_senders"),
    }
    return {
        ("GET", _senders_url(subresource)): bodies.get(subresource, default)
        for subresource, default in defaults.items()
    }


async def test_senders_are_grouped_by_the_channel_they_serve():
    """The service is the only place a sender is configured, so every
    subresource that can carry a conversation is read and sorted by channel."""
    recorder = Recorder(
        _sender_routes(
            PhoneNumbers={
                "meta": {"key": "phone_numbers"},
                "phone_numbers": [
                    {"sid": "PN1", "phone_number": "+15550100"},
                    {"sid": "PN2", "phone_number": "+15550101"},
                ],
            },
            ShortCodes={
                "meta": {"key": "short_codes"},
                "short_codes": [{"sid": "SC1", "short_code": "12345"}],
            },
            ChannelSenders={
                "meta": {"key": "channel_senders"},
                "channel_senders": [{"sid": "XE1", "sender_id": "whatsapp:+15550199"}],
            },
        )
    )

    async with recorder.client() as client:
        assert await fetch_senders(client, AUTH, SERVICE_SID) == {
            "SMS": ["+15550100", "+15550101", "12345"],
            "WHATSAPP": ["+15550199"],
        }


async def test_a_whatsapp_sender_loses_its_channel_prefix():
    """Conversation Orchestrator describes WhatsApp traffic in E.164, so a rule
    written as `whatsapp:+1...` would not match it."""
    recorder = Recorder(
        _sender_routes(
            ChannelSenders={
                "meta": {"key": "channel_senders"},
                "channel_senders": [{"sid": "XE1", "sender_id": "whatsapp:+15550199"}],
            }
        )
    )

    async with recorder.client() as client:
        assert await fetch_senders(client, AUTH, SERVICE_SID) == {"WHATSAPP": ["+15550199"]}


async def test_a_service_with_no_senders_reads_as_empty():
    """Not an error here — `main` is what refuses to provision on it, with a
    message naming the Console page to fix it on."""
    recorder = Recorder(_sender_routes())

    async with recorder.client() as client:
        assert await fetch_senders(client, AUTH, SERVICE_SID) == {}


async def test_a_paged_subresource_is_followed_to_the_end():
    """A pool holds up to 400 numbers by default, so one page is the common
    case but not the guaranteed one."""
    second = _senders_url("PhoneNumbers", "&Page=1")
    routes = _sender_routes(
        PhoneNumbers={
            "meta": {"key": "phone_numbers", "next_page_url": second},
            "phone_numbers": [{"phone_number": "+15550100"}],
        }
    )
    routes[("GET", second)] = {
        "meta": {"key": "phone_numbers"},
        "phone_numbers": [{"phone_number": "+15550101"}],
    }
    recorder = Recorder(routes)

    async with recorder.client() as client:
        assert await fetch_senders(client, AUTH, SERVICE_SID) == {
            "SMS": ["+15550100", "+15550101"]
        }


async def test_a_sender_whose_address_cannot_be_read_is_an_error():
    """ChannelSenders has no reference page, so the field its address lands in
    is a guess. A sender silently skipped is one nobody can reach and nothing
    that says so, which is worse than failing the run."""
    recorder = Recorder(
        _sender_routes(
            ChannelSenders={
                "meta": {"key": "channel_senders"},
                "channel_senders": [{"sid": "XE1", "some_unexpected_field": "whatsapp:+1"}],
            }
        )
    )

    async with recorder.client() as client:
        with pytest.raises(ProvisionError, match="has no address in any of"):
            await fetch_senders(client, AUTH, SERVICE_SID)


async def test_a_sender_read_that_fails_is_reported_rather_than_treated_as_empty():
    recorder = Recorder(
        {("GET", _senders_url("PhoneNumbers")): (404, {"message": "not found"})}
    )

    async with recorder.client() as client:
        with pytest.raises(ProvisionError, match="PhoneNumbers read"):
            await fetch_senders(client, AUTH, SERVICE_SID)


# --- dry run: reads only ---


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
async def test_a_dry_run_refuses_every_write(method):
    """The dry-run branches are what report instead of acting; this is what
    makes that a guarantee, so a write that slips past one cannot reach a live
    account."""
    request = httpx.Request(method, f"{CONVERSATION_API}/Configurations")

    with pytest.raises(ProvisionError, match="--dry-run tried to"):
        await refuse_mutation(request)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_a_dry_run_allows_reads(method):
    assert await refuse_mutation(httpx.Request(method, f"{MEMORY_API}/Stores")) is None


async def test_a_dry_run_reports_the_changes_it_does_not_make():
    changes, recorder = await _sync(
        _configuration(memoryExtractionEnabled=False),
        webhook_url="https://new-host/webhook",
        channel_settings=build_channel_settings({"WHATSAPP": ["+2"]}),
        dry_run=True,
    )

    assert "PATCH" not in recorder.methods()
    assert any("status callback" in change for change in changes)
    assert "memory extraction on" in changes
    assert any("channels" in change for change in changes)


async def test_a_dry_run_creates_neither_resource():
    """With nothing on the account to reuse, both would be created — and on a
    dry run neither is, so nothing POSTs."""
    recorder = Recorder(
        {
            ("GET", f"{MEMORY_API}/Stores"): {"meta": {"key": "stores"}, "stores": []},
            ("GET", f"{CONVERSATION_API}/Configurations"): {"configurations": []},
        }
    )

    async with recorder.client() as client:
        store_id, store_created = await ensure_memory_store(
            client, AUTH, "agent", None, dry_run=True
        )
        config_id, config_created = await ensure_conversation_configuration(
            client, AUTH, "agent", None, dry_run=True
        )

    assert (store_id, store_created) == (provision.WOULD_CREATE, True)
    assert (config_id, config_created) == (provision.WOULD_CREATE, True)
    assert recorder.methods() == ["GET", "GET"]


# --- pure helpers ---


def test_channel_settings_cover_only_the_channels_with_senders():
    assert set(build_channel_settings({"SMS": ["+15550100"]})) == {"SMS"}
    assert set(build_channel_settings({"WHATSAPP": ["+1"]})) == {"WHATSAPP"}
    assert set(build_channel_settings({"SMS": ["+15550100"], "WHATSAPP": ["+1"]})) == {
        "SMS",
        "WHATSAPP",
    }


def test_a_channel_whose_senders_are_empty_is_left_out():
    """Empty capture rules would capture nothing anyway; leaving the channel out
    keeps the configuration honest about what it serves."""
    assert set(build_channel_settings({"SMS": ["+15550100"], "WHATSAPP": []})) == {"SMS"}


def test_every_sender_on_a_channel_is_captured():
    """A contact can message any sender in the pool, so a rule for the first
    one alone would drop the rest on the floor."""
    rules = build_channel_settings({"SMS": ["+15550100", "+15550101"]})["SMS"]["captureRules"]

    assert rules == [
        {"from": "*", "to": "+15550100"},
        {"from": "+15550100", "to": "*"},
        {"from": "*", "to": "+15550101"},
        {"from": "+15550101", "to": "*"},
    ]


def test_no_senders_is_an_error_rather_than_an_empty_configuration():
    with pytest.raises(ProvisionError, match="No senders"):
        build_channel_settings({})


def test_capture_rules_cover_both_directions():
    rules = build_channel_settings({"WHATSAPP": ["+1"]})["WHATSAPP"]["captureRules"]

    assert rules == [{"from": "*", "to": "+1"}, {"from": "+1", "to": "*"}]


@pytest.mark.parametrize("name", ["a" * 33, "has spaces", "has/slash", "hasplus+"])
def test_display_names_twilio_would_reject_are_caught_locally(name):
    with pytest.raises(ProvisionError, match="Invalid display name"):
        validate_display_name(name)


def test_a_valid_display_name_passes_through():
    assert validate_display_name("curesyngap1-agent") == "curesyngap1-agent"


def test_items_reads_whichever_envelope_the_collection_uses():
    assert _items({"meta": {"key": "stores"}, "stores": ["a"]}) == ["a"]
    assert _items({"configurations": [{"id": "b"}]}) == [{"id": "b"}]
    assert _items({}) == []
