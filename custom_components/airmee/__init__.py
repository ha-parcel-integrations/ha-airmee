"""Airmee parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account.client import AirmeeAccountClient
from .account.coordinator import AirmeeAccountCoordinator
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_PHONE_NUMBER_HASH,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    DOMAIN,
    PLATFORMS,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .services import async_setup_services, async_unload_services
from .tracking.api import AirmeeApiClient
from .tracking.coordinator import AirmeeCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class AirmeeData:
    """Runtime data attached to the Airmee config entry."""

    client: AirmeeApiClient | AirmeeAccountClient
    coordinator: AirmeeCoordinator | AirmeeAccountCoordinator


type AirmeeConfigEntry = ConfigEntry[AirmeeData]


def is_account_entry(entry: ConfigEntry) -> bool:
    """Whether ``entry`` is an account (OTP login) entry; default is tracking."""
    return entry.data.get(CONF_SOURCE, SOURCE_TRACKING) == SOURCE_ACCOUNT


async def async_setup_entry(hass: HomeAssistant, entry: AirmeeConfigEntry) -> bool:
    """Set up Airmee from a config entry."""
    if is_account_entry(entry):

        async def async_store_tokens(tokens: dict[str, str]) -> None:
            # Both tokens rotate on every refresh; persist the pair together.
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_ACCESS_TOKEN: tokens[CONF_ACCESS_TOKEN],
                    CONF_REFRESH_TOKEN: tokens[CONF_REFRESH_TOKEN],
                },
            )

        client = AirmeeAccountClient(
            async_get_clientsession(hass),
            access_token=entry.data[CONF_ACCESS_TOKEN],
            refresh_token=entry.data[CONF_REFRESH_TOKEN],
            token_callback=async_store_tokens,
        )
        coordinator: AirmeeCoordinator | AirmeeAccountCoordinator = (
            AirmeeAccountCoordinator(hass, client, entry)
        )
    else:
        # The recipient's hash rides along on every tracking request.
        client = AirmeeApiClient(
            async_get_clientsession(hass), entry.data[CONF_PHONE_NUMBER_HASH]
        )
        coordinator = AirmeeCoordinator(hass, client, entry)

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to catch
    # cleanly (it logs a warning and half-sets-up the entry); doing the first
    # refresh here lets a transient failure fail the whole entry so HA retries
    # it with backoff.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = AirmeeData(client=client, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Apply option changes (added/removed parcels, history) live via a
    # coordinator refresh — no reload — so per-parcel sensors appear and
    # disappear immediately. This is also the resume path after polling fully
    # suspended: adding a parcel back triggers this refresh, which recomputes
    # the tier and re-arms scheduling.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    if not is_account_entry(entry):
        async_setup_services(hass)

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: AirmeeConfigEntry
) -> None:
    """Apply changed options by refreshing the coordinator."""
    await entry.runtime_data.coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: AirmeeConfigEntry) -> bool:
    """Unload the Airmee config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    # The services belong to tracking entries only: drop them when the last
    # loaded one goes.
    others_loaded = any(
        other.entry_id != entry.entry_id
        and other.state is ConfigEntryState.LOADED
        and not is_account_entry(other)
        for other in hass.config_entries.async_entries(DOMAIN)
    )
    if not is_account_entry(entry) and not others_loaded:
        async_unload_services(hass)
    return True
