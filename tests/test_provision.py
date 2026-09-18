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
    fetch_sender_pool,
    find_by_display_name,
    poll_operation,
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
                    {"from": "*", "to": "whatsapp:+1", "metadata": {}},
                    {"from": "whatsapp:+1", "to": "*", "metadata": {}},
                ],
            }
        },
    }
    return {**base, **overrides}


async def _sync(current: dict, *, webhook_url: str, channel_settings: dict) -> tuple:
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
        )
    return changes, recorder


async def test_an_unchanged_configuration_is_not_patched():
    changes, recorder = await _sync(
        _configuration(),
        webhook_url="https://host/webhook",
        channel_settings=build_channel_settings(None, "whatsapp:+1"),
    )

    assert changes == []
    assert "PATCH" not in recorder.methods()


async def test_a_changed_webhook_domain_patches_the_callback():
    changes, recorder = await _sync(
        _configuration(),
        webhook_url="https://new-host/webhook",
        channel_settings=build_channel_settings(None, "whatsapp:+1"),
    )

    assert "PATCH" in recorder.methods()
    assert any("status callback" in change for change in changes)


async def test_memory_extraction_is_turned_on_when_it_is_off():
    changes, _ = await _sync(
        _configuration(memoryExtractionEnabled=False),
        webhook_url="https://host/webhook",
        channel_settings=build_channel_settings(None, "whatsapp:+1"),
    )

    assert changes == ["memory extraction on"]


async def test_a_changed_sender_number_patches_the_capture_rules():
    """The channel set is still {"WHATSAPP"}, so only a value-level comparison
    notices that the rules point at the previous number."""
    changes, recorder = await _sync(
        _configuration(),
        webhook_url="https://host/webhook",
        channel_settings=build_channel_settings(None, "whatsapp:+2"),
    )

    assert "PATCH" in recorder.methods()
    assert any("channels" in change for change in changes)


def test_server_supplied_fields_do_not_count_as_a_difference():
    """`statusTimeouts` and `metadata` come back from the server whether or not
    the script sent them, so comparing them would patch on every run."""
    ours = build_channel_settings(None, "whatsapp:+1")
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


# --- the Messaging Service sender pool ---


def _pool_url(page: str = "") -> str:
    return f"{MESSAGING_API}/Services/{SERVICE_SID}/PhoneNumbers?PageSize=1000{page}"


async def test_the_sender_pool_is_read_as_e164_numbers():
    recorder = Recorder(
        {
            ("GET", _pool_url()): {
                "meta": {"key": "phone_numbers"},
                "phone_numbers": [
                    {"sid": "PN1", "phone_number": "+15550100"},
                    {"sid": "PN2", "phone_number": "+15550101"},
                ],
            }
        }
    )

    async with recorder.client() as client:
        assert await fetch_sender_pool(client, AUTH, SERVICE_SID) == ["+15550100", "+15550101"]


async def test_a_paged_sender_pool_is_followed_to_the_end():
    """A pool holds up to 400 numbers by default, so one page is the common
    case but not the guaranteed one."""
    second = _pool_url("&Page=1")
    recorder = Recorder(
        {
            ("GET", _pool_url()): {
                "meta": {"key": "phone_numbers", "next_page_url": second},
                "phone_numbers": [{"phone_number": "+15550100"}],
            },
            ("GET", second): {
                "meta": {"key": "phone_numbers"},
                "phone_numbers": [{"phone_number": "+15550101"}],
            },
        }
    )

    async with recorder.client() as client:
        assert await fetch_sender_pool(client, AUTH, SERVICE_SID) == ["+15550100", "+15550101"]


async def test_senders_that_cannot_hold_a_conversation_are_left_out():
    """A pool can also hold short codes and Alphanumeric Sender IDs, which this
    endpoint lists without a phone number."""
    recorder = Recorder(
        {
            ("GET", _pool_url()): {
                "meta": {"key": "phone_numbers"},
                "phone_numbers": [{"sid": "PN1", "phone_number": "+15550100"}, {"sid": "AS1"}],
            }
        }
    )

    async with recorder.client() as client:
        assert await fetch_sender_pool(client, AUTH, SERVICE_SID) == ["+15550100"]


async def test_a_pool_read_that_fails_is_reported_rather_than_treated_as_empty():
    recorder = Recorder({("GET", _pool_url()): (404, {"message": "not found"})})

    async with recorder.client() as client:
        with pytest.raises(ProvisionError, match="sender pool read"):
            await fetch_sender_pool(client, AUTH, SERVICE_SID)


# --- pure helpers ---


def test_channel_settings_cover_only_the_configured_senders():
    assert set(build_channel_settings(["+15550100"], None)) == {"SMS"}
    assert set(build_channel_settings(None, "whatsapp:+1")) == {"WHATSAPP"}
    assert set(build_channel_settings(["+15550100"], "whatsapp:+1")) == {"SMS", "WHATSAPP"}


def test_every_number_in_a_sender_pool_is_captured():
    """A family can text any number in a Messaging Service's pool, so a rule
    for the first one alone would drop the rest on the floor."""
    rules = build_channel_settings(["+15550100", "+15550101"], None)["SMS"]["captureRules"]

    assert rules == [
        {"from": "*", "to": "+15550100"},
        {"from": "+15550100", "to": "*"},
        {"from": "*", "to": "+15550101"},
        {"from": "+15550101", "to": "*"},
    ]


def test_no_senders_is_an_error_rather_than_an_empty_configuration():
    with pytest.raises(ProvisionError, match="No senders"):
        build_channel_settings(None, None)


def test_capture_rules_cover_both_directions():
    rules = build_channel_settings(None, "whatsapp:+1")["WHATSAPP"]["captureRules"]

    assert rules == [{"from": "*", "to": "whatsapp:+1"}, {"from": "whatsapp:+1", "to": "*"}]


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
