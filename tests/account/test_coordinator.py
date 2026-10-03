"""Tests for the account inbox coordinator and account-entry setup."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airmee.account.client import (
    AirmeeAccountAuthError,
    AirmeeAccountError,
)
from custom_components.airmee.account.coordinator import AirmeeAccountCoordinator
from custom_components.airmee.const import (
    CONF_ACCESS_TOKEN,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    DOMAIN,
    MID_INTERVAL_MINUTES,
    SOURCE_ACCOUNT,
    ParcelStatus,
)

from ..payloads import active_sample, delivered_sample


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:abc",
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_ACCESS_TOKEN: "acc",
            CONF_REFRESH_TOKEN: "ref",
        },
        options={CONF_DELIVERED_FILTER_TYPE: "parcels", CONF_DELIVERED_FILTER_AMOUNT: 50},
    )


async def test_inbox_is_split_into_active_and_delivered(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_deliveries.return_value = [active_sample(1), delivered_sample(2)]
    coordinator = AirmeeAccountCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert [p["barcode"] for p in data] == ["1"]
    assert [p["barcode"] for p in coordinator.delivered] == ["2"]
    assert data[0]["status"] is ParcelStatus.OUT_FOR_DELIVERY
    assert data[0]["url"] is None
    assert coordinator.delivered_codes == set()
    assert coordinator.last_success_time is not None


async def test_orders_without_any_id_are_skipped(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    nameless = active_sample()
    del nameless["id"], nameless["receipt_id"]
    client = AsyncMock()
    client.async_get_deliveries.return_value = [nameless, active_sample(5)]
    coordinator = AirmeeAccountCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()
    assert [p["barcode"] for p in data] == ["5"]


async def test_history_option_is_honoured(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "include_history": True}
    )
    client = AsyncMock()
    client.async_get_deliveries.return_value = [active_sample(1)]
    data = await AirmeeAccountCoordinator(hass, client, entry)._async_update_data()
    assert len(data[0]["history"]) == 3


async def test_polling_never_suspends_even_with_an_empty_inbox(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_deliveries.return_value = []
    coordinator = AirmeeAccountCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes == MID_INTERVAL_MINUTES
    assert coordinator.update_interval is not None


async def test_status_change_and_registration_events(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = AirmeeAccountCoordinator(hass, client, entry)
    fired = []
    for kind in ("registered", "status_changed", "delivered"):
        hass.bus.async_listen(f"{DOMAIN}_parcel_{kind}", lambda e: fired.append(e))

    client.async_get_deliveries.return_value = [active_sample(1)]
    await coordinator._async_update_data()  # first refresh: suppressed
    client.async_get_deliveries.return_value = [delivered_sample(1), active_sample(2)]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert sorted(e.event_type for e in fired) == [
        f"{DOMAIN}_parcel_delivered",
        f"{DOMAIN}_parcel_registered",
    ]


async def test_expired_login_raises_reauth(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_deliveries.side_effect = AirmeeAccountAuthError("expired")
    with pytest.raises(ConfigEntryAuthFailed):
        await AirmeeAccountCoordinator(hass, client, entry)._async_update_data()


async def test_other_errors_are_update_failures(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_deliveries.side_effect = AirmeeAccountError("HTTP 503", status_code=503)
    with pytest.raises(UpdateFailed):
        await AirmeeAccountCoordinator(hass, client, entry)._async_update_data()


async def test_429_backs_off_with_retry_after_then_exponentially(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = AirmeeAccountCoordinator(hass, client, entry)

    client.async_get_deliveries.side_effect = AirmeeAccountError(
        "HTTP 429", status_code=429, retry_after=90.0
    )
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after == 90.0

    client.async_get_deliveries.side_effect = AirmeeAccountError(
        "HTTP 429", status_code=429
    )
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after == 60 * 2**2

    client.async_get_deliveries.side_effect = None
    client.async_get_deliveries.return_value = []
    await coordinator._async_update_data()
    assert coordinator._consecutive_429 == 0


# ---------------------------------------------------------------------------
# entry setup
# ---------------------------------------------------------------------------


async def test_account_entry_sets_up_without_services_and_persists_rotated_tokens(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.airmee.account.client.AirmeeAccountClient.async_get_deliveries",
        new=AsyncMock(return_value=[active_sample(1)]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert hass.states.get("sensor.airmee_incoming_parcels").state == "1"
        assert not hass.services.has_service(DOMAIN, "track_parcel")

        # A token rotation inside the client is written back to the entry.
        client = entry.runtime_data.client
        await client._token_callback({"access_token": "n-acc", "refresh_token": "n-ref"})
        assert entry.data[CONF_ACCESS_TOKEN] == "n-acc"
        assert entry.data[CONF_REFRESH_TOKEN] == "n-ref"
        assert entry.data[CONF_SOURCE] == SOURCE_ACCOUNT

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_rejected_login_at_setup_starts_reauth(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.airmee.account.client.AirmeeAccountClient.async_get_deliveries",
        new=AsyncMock(side_effect=AirmeeAccountAuthError("expired")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_services_survive_an_account_entry_unloading(hass):
    """The shared services belong to tracking entries, not to account ones."""
    tracking = MockConfigEntry(
        domain=DOMAIN,
        unique_id="tracking",
        data={CONF_SOURCE: "tracking", "phone_number_hash": "h"},
        options={"parcels": []},
    )
    account = _entry()
    tracking.add_to_hass(hass)
    account.add_to_hass(hass)
    with patch(
        "custom_components.airmee.account.client.AirmeeAccountClient.async_get_deliveries",
        new=AsyncMock(return_value=[]),
    ):
        assert await hass.config_entries.async_setup(tracking.entry_id)
        await hass.async_block_till_done()
        assert hass.services.has_service(DOMAIN, "track_parcel")

        assert await hass.config_entries.async_unload(account.entry_id)
        await hass.async_block_till_done()
        assert hass.services.has_service(DOMAIN, "track_parcel")

        assert await hass.config_entries.async_unload(tracking.entry_id)
        await hass.async_block_till_done()
        assert not hass.services.has_service(DOMAIN, "track_parcel")


async def test_a_second_tracking_entry_keeps_services_until_the_last_unloads(hass):
    def make(unique_id):
        return MockConfigEntry(
            domain=DOMAIN,
            unique_id=unique_id,
            data={CONF_SOURCE: "tracking", "phone_number_hash": "h"},
            options={"parcels": []},
        )

    first, second = make("tracking"), make("tracking-2")
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()
    assert second.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(second.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, "track_parcel")
