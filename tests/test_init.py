"""Tests for Airmee setup and unload."""
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airmee.const import (
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from custom_components.airmee.tracking.api import AirmeeApiError

from .payloads import ACTIVE_CODE
from .payloads import active_sample as _sample

OTHER_CODE = "EXAMPLE222222"


async def test_setup_and_unload(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"phone_number_hash": "hash01"},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.airmee.tracking.api.AirmeeApiClient.async_get_parcel",
        new=AsyncMock(return_value=_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    # The active parcel produced a per-parcel sensor and the summary sensor.
    incoming = hass.states.get("sensor.airmee_incoming_parcels")
    assert incoming is not None
    assert incoming.state == "1"

    # Services registered on setup...
    assert hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    # ...and removed on unload (single-instance integration).
    assert not hass.services.has_service(DOMAIN, "track_parcel")


async def test_setup_retries_when_first_refresh_fails(hass):
    """When the first data fetch fails, setup retries from the entry itself.

    The first refresh runs in __init__.py before platforms are forwarded, so a
    failure raises ConfigEntryNotReady from the entry setup (SETUP_RETRY) rather
    than — too late — from a forwarded platform.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"phone_number_hash": "hash01"},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.airmee.tracking.api.AirmeeApiClient.async_get_parcel",
        new=AsyncMock(side_effect=AirmeeApiError("Airmee unreachable")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"phone_number_hash": "hash01"},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=_sample())
    with patch("custom_components.airmee.tracking.api.AirmeeApiClient.async_get_parcel", new=mock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
        )

        # The user swaps the tracked token: the summary sensor spawns a new
        # per-parcel sensor and removes the stale one.
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: [{CONF_TRACKING_CODE: OTHER_CODE}]}
        )
        await hass.async_block_till_done()

        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{OTHER_CODE}"
        )
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
            )
            is None
        )


async def test_options_update_applies_live_without_reload(hass):
    """Adding a parcel via options refreshes the coordinator immediately."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"phone_number_hash": "hash01"},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=_sample())
    with patch("custom_components.airmee.tracking.api.AirmeeApiClient.async_get_parcel", new=mock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        mock.side_effect = lambda code: _sample(code)
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_PARCELS: [
                    {CONF_TRACKING_CODE: ACTIVE_CODE},
                    {CONF_TRACKING_CODE: OTHER_CODE},
                ],
            },
        )
        await hass.async_block_till_done()

    incoming = hass.states.get("sensor.airmee_incoming_parcels")
    assert incoming.state == "2"


async def test_unload_failure_is_reported(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"phone_number_hash": "hash01"},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.airmee.tracking.api.AirmeeApiClient.async_get_parcel",
        new=AsyncMock(return_value=_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        from custom_components.airmee import async_unload_entry

        with patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
        ):
            assert await async_unload_entry(hass, entry) is False
        assert hass.services.has_service(DOMAIN, "track_parcel")


async def test_stale_parcel_sensors_are_swept_at_setup(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"phone_number_hash": "hash01"},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_gone-token", config_entry=entry
    )
    with patch(
        "custom_components.airmee.tracking.api.AirmeeApiClient.async_get_parcel",
        new=AsyncMock(return_value=_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert registry.async_get(stale.entity_id) is None
