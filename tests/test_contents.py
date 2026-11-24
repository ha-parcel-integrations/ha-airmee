"""Tests for the parcel contents field and the update_contents action."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airmee.calendar import AirmeeDeliveriesCalendar
from custom_components.airmee.const import (
    CAPABILITIES,
    CONF_ACCESS_TOKEN,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    SOURCE_ACCOUNT,
)
from custom_components.airmee.contents import (
    CONTENTS_MAX_LEN,
    CONTENTS_STORE_MAX_CODES,
    ContentsStore,
    clean_contents,
    join_contents,
    normalize_code,
)
from custom_components.airmee.sensor import AirmeeNextDeliverySensor

from .payloads import active_sample

_TRACK_PATH = "custom_components.airmee.tracking.api.AirmeeApiClient.async_get_parcel"
_INBOX_PATH = (
    "custom_components.airmee.account.client.AirmeeAccountClient.async_get_deliveries"
)


async def _tracking(hass, codes=("AAA111",)) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"phone_number_hash": "hash01"},
        options={CONF_PARCELS: [{"tracking_code": c} for c in codes]},
    )
    entry.add_to_hass(hass)
    return entry


def _account(number: str = "a") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"account:{number}",
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_ACCESS_TOKEN: "acc",
            CONF_REFRESH_TOKEN: "ref",
        },
    )


async def _load(hass, *entries, inbox=None):
    with (
        patch(_TRACK_PATH, new=AsyncMock(return_value=active_sample())),
        patch(_INBOX_PATH, new=AsyncMock(return_value=inbox or [])),
    ):
        for entry in entries:
            entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entries[0].entry_id)
        await hass.async_block_till_done()


async def _call(hass, data, *, tracking_return=None, inbox=None):
    with (
        patch(_TRACK_PATH, new=AsyncMock(return_value=active_sample())),
        patch(_INBOX_PATH, new=AsyncMock(return_value=inbox or [])),
    ):
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
        await hass.async_block_till_done()


def _store(entry) -> ContentsStore:
    return entry.runtime_data.coordinator.contents_store


def test_clean_trims_collapses_and_caps():
    assert clean_contents("  Running \n shoes ") == "Running shoes"
    assert clean_contents("   ") is None
    assert clean_contents(None) is None
    assert len(clean_contents("x" * 500)) == CONTENTS_MAX_LEN
    assert clean_contents("None", frozenset({"none"})) is None


def test_join_drops_empties_and_repeats():
    assert join_contents(["Shoes", " ", "Shoes", "Hat"]) == "Shoes; Hat"
    assert join_contents([None, ""]) is None


def test_normalize_code():
    assert normalize_code(" ab1 ") == "AB1"


async def test_store_set_replace_clear_and_cap(hass):
    store = ContentsStore(hass, "e1")
    await store.async_load()
    assert store.async_set("a1", "Shoes") is True
    assert store.async_set("A1", "Shoes") is False
    assert store.async_set("a1", "Hat") is True
    assert store.get("A1") == "Hat"
    assert store.async_set("a1", "") is True
    assert store.get("a1") is None
    assert store.async_set("a1", None) is False
    for number in range(CONTENTS_STORE_MAX_CODES + 1):
        store.async_set(f"c{number}", "x")
    assert store.get("c0") is None
    assert store.get("c1") == "x"
    assert store.get(f"c{CONTENTS_STORE_MAX_CODES}") == "x"


async def test_store_loads_saved_values_and_tolerates_junk(hass, hass_storage):
    hass_storage[f"{DOMAIN}.contents.e1"] = {
        "version": 1,
        "key": f"{DOMAIN}.contents.e1",
        "data": {"by_code": {"a1": " Shoes ", "b2": "", "c3": 5}},
    }
    store = ContentsStore(hass, "e1")
    await store.async_load()
    assert store.get("A1") == "Shoes"
    assert store.get("b2") is None
    assert store.get("c3") is None
    hass_storage[f"{DOMAIN}.contents.e1"]["data"] = {"by_code": "junk"}
    await store.async_load()
    assert store.get("a1") is None


async def test_entry_without_a_store_works(hass):
    entry = await _tracking(hass)
    await _load(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.data[0]["contents"] is None


async def test_update_contents_set_replace_clear_on_single_entry(hass):
    entry = await _tracking(hass)
    await _load(hass, entry)
    coordinator = entry.runtime_data.coordinator

    await _call(hass, {"tracking_code": "aaa111", "contents": " Shoes "})
    assert coordinator.data[0]["contents"] == "Shoes"
    await _call(hass, {"tracking_code": "AAA111", "contents": "Hat"})
    assert _store(entry).get("AAA111") == "Hat"
    with patch(_TRACK_PATH, new=AsyncMock(return_value=active_sample())):
        await coordinator.async_refresh()
    assert coordinator.data[0]["contents"] == "Hat"
    await _call(hass, {"tracking_code": "AAA111", "contents": ""})
    assert _store(entry).get("AAA111") is None
    with patch(_TRACK_PATH, new=AsyncMock(return_value=active_sample())):
        await coordinator.async_refresh()
    assert coordinator.data[0]["contents"] is None
    await _call(hass, {"tracking_code": "AAA111"})
    assert _store(entry).get("AAA111") is None


async def test_unchanged_contents_writes_nothing_and_does_not_refresh(hass):
    entry = await _tracking(hass)
    await _load(hass, entry)
    coordinator = entry.runtime_data.coordinator
    await _call(hass, {"tracking_code": "AAA111", "contents": "Shoes"})
    with patch.object(coordinator, "async_request_refresh", new=AsyncMock()) as refresh:
        await _call(hass, {"tracking_code": "AAA111", "contents": "Shoes"})
        await _call(hass, {"tracking_code": "AAA111", "contents": "  "})
        assert refresh.await_count == 1


async def test_untracked_code_can_be_set(hass):
    entry = await _tracking(hass)
    await _load(hass, entry)
    await _call(hass, {"tracking_code": "zzz999", "contents": "Lamp"})
    assert _store(entry).get("ZZZ999") == "Lamp"


async def test_track_parcel_with_contents_and_untrack_drops_it(hass):
    entry = await _tracking(hass, codes=())
    await _load(hass, entry)
    with patch(_TRACK_PATH, new=AsyncMock(return_value=active_sample())):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {"tracking_code": "NEW123", "contents": "Lamp"},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert _store(entry).get("NEW123") == "Lamp"
        assert entry.runtime_data.coordinator.data[0]["contents"] == "Lamp"
        # tracked already: a changed value replaces, an unchanged one is a no-op
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {"tracking_code": "NEW123", "contents": "Desk"},
            blocking=True,
        )
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {"tracking_code": "NEW123", "contents": "Desk"},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert _store(entry).get("NEW123") == "Desk"
        await hass.services.async_call(
            DOMAIN, "untrack_parcel", {"tracking_code": "NEW123"}, blocking=True
        )
        await hass.async_block_till_done()
    assert _store(entry).get("NEW123") is None


async def test_track_parcel_without_contents_keeps_stored_value(hass):
    entry = await _tracking(hass, codes=())
    await _load(hass, entry)
    with patch(_TRACK_PATH, new=AsyncMock(return_value=active_sample())):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {"tracking_code": "NEW123"}, blocking=True
        )
        await hass.async_block_till_done()
    assert _store(entry).get("NEW123") is None


async def test_track_parcel_with_contents_needs_a_loaded_entry(hass):
    entry = await _tracking(hass, codes=())
    await _load(hass, entry)
    with patch(_TRACK_PATH, new=AsyncMock(return_value=active_sample())):
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                "track_parcel",
                {"tracking_code": "NEW123", "contents": "Lamp"},
                blocking=True,
            )
        await hass.services.async_call(
            DOMAIN, "untrack_parcel", {"tracking_code": "NEW123"}, blocking=True
        )


async def test_remove_entry_deletes_the_store(hass):
    entry = await _tracking(hass)
    await _load(hass, entry)
    with patch.object(ContentsStore, "async_remove", new=AsyncMock()) as remove:
        await hass.config_entries.async_remove(entry.entry_id)
    remove.assert_awaited_once()


async def test_hybrid_routing_targets_entries_that_list_the_code(hass):
    tracking = await _tracking(hass, codes=("AAA111",))
    account = _account()
    await _load(hass, tracking, account, inbox=[active_sample(7)])
    await hass.async_block_till_done()
    assert account.state is ConfigEntryState.LOADED

    await _call(hass, {"tracking_code": "AAA111", "contents": "Shoes"})
    assert _store(tracking).get("AAA111") == "Shoes"
    assert _store(account).get("AAA111") is None

    await _call(
        hass, {"tracking_code": "7", "contents": "Hat"}, inbox=[active_sample(7)]
    )
    assert _store(account).get("7") == "Hat"
    assert _store(tracking).get("7") is None
    assert account.runtime_data.coordinator.data[0]["contents"] == "Hat"

    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, {"tracking_code": "UNKNOWN", "contents": "Lamp"})
    assert err.value.translation_key == "update_contents_entry_required"

    await _call(
        hass,
        {
            "tracking_code": "UNKNOWN",
            "contents": "Lamp",
            "config_entry_id": account.entry_id,
        },
    )
    assert _store(account).get("UNKNOWN") == "Lamp"
    assert _store(tracking).get("UNKNOWN") is None

    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            {"tracking_code": "AAA111", "contents": "x", "config_entry_id": "nope"},
        )
    with pytest.raises(ServiceValidationError):
        await _call(hass, {"tracking_code": "  ", "contents": "x"})


async def test_code_in_two_entries_updates_both(hass):
    tracking = await _tracking(hass, codes=("7",))
    account = _account()
    await _load(hass, tracking, account, inbox=[active_sample(7)])
    await _call(
        hass, {"tracking_code": "7", "contents": "Hat"}, inbox=[active_sample(7)]
    )
    assert _store(tracking).get("7") == "Hat"
    assert _store(account).get("7") == "Hat"


async def test_account_only_setup_has_update_contents_only(hass):
    account = _account()
    await _load(hass, account, inbox=[active_sample(7)])
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")
    assert not hass.services.has_service(DOMAIN, "untrack_parcel")
    await _call(
        hass, {"tracking_code": "7", "contents": "Hat"}, inbox=[active_sample(7)]
    )
    assert account.runtime_data.coordinator.data[0]["contents"] == "Hat"


async def test_update_contents_without_loaded_entry_raises(hass):
    account = _account()
    await _load(hass, account)
    await hass.config_entries.async_unload(account.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_not_set_up_raises(hass):
    from custom_components.airmee.services import async_setup_services

    async_setup_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "update_contents",
            {"tracking_code": "A", "contents": "x"},
            blocking=True,
        )


async def test_unload_keeps_services_while_others_need_them(hass):
    tracking = await _tracking(hass)
    account = _account()
    await _load(hass, tracking, account)
    await hass.config_entries.async_unload(tracking.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")
    await hass.config_entries.async_unload(account.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, "update_contents")


def test_contents_is_a_known_but_not_claimed_capability():
    assert "contents" in KNOWN_CAPABILITIES
    assert "contents" not in CAPABILITIES
    assert KNOWN_CAPABILITIES - {"contents"} >= CAPABILITIES


def _parcel(contents):
    return {
        "barcode": "B1",
        "sender": "Example Sender",
        "contents": contents,
        "status": "out_for_delivery",
        "planned_from": "2099-01-01T10:00:00Z",
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": None,
    }


def test_calendar_title_prefers_contents():
    coordinator = MagicMock()
    entry = MagicMock()
    entry.entry_id = "e1"
    coordinator.data = [_parcel("Running shoes")]
    assert AirmeeDeliveriesCalendar(coordinator, entry).event.summary == (
        "Running shoes"
    )
    coordinator.data = [_parcel(None)]
    assert AirmeeDeliveriesCalendar(coordinator, entry).event.summary == (
        "Example Sender"
    )


def test_next_delivery_attribute_carries_contents():
    coordinator = MagicMock()
    entry = MagicMock()
    entry.entry_id = "e1"
    coordinator.data = [_parcel("Running shoes")]
    sensor = AirmeeNextDeliverySensor(coordinator, entry)
    assert sensor.extra_state_attributes["contents"] == "Running shoes"
