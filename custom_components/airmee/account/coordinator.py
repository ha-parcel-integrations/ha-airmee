"""Coordinator for the account inbox source."""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import MID_INTERVAL_MINUTES
from ..coordinator import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    AirmeeBaseCoordinator,
    _hottest_tier_minutes,
)
from ..parcels import normalize_parcel
from .client import (
    AirmeeAccountAuthError,
    AirmeeAccountClient,
    AirmeeAccountError,
)

_LOGGER = logging.getLogger(__name__)


class AirmeeAccountCoordinator(AirmeeBaseCoordinator):
    """Refreshes the whole account inbox; never suspends polling."""

    def __init__(
        self, hass: HomeAssistant, client: AirmeeAccountClient, entry: ConfigEntry
    ) -> None:
        """Initialise the inbox coordinator."""
        super().__init__(hass, client, entry)
        self._consecutive_429 = 0

    def _tier_for(self, active: list[dict], now: datetime) -> int | None:
        """Return a tier every cycle: the inbox is how new parcels are discovered."""
        return _hottest_tier_minutes(active, now) or MID_INTERVAL_MINUTES

    async def _async_update_data(self) -> list[dict]:
        """Fetch the inbox and publish it."""
        try:
            orders = await self._client.async_get_deliveries()
        except AirmeeAccountAuthError as err:
            raise ConfigEntryAuthFailed("Airmee login expired") from err
        except AirmeeAccountError as err:
            if err.status_code == 429:
                self._consecutive_429 += 1
                retry_after = err.retry_after or min(
                    BACKOFF_BASE_SECONDS * 2**self._consecutive_429,
                    BACKOFF_CAP_SECONDS,
                )
                raise UpdateFailed(
                    "Airmee rate-limited (429)", retry_after=retry_after
                ) from err
            raise UpdateFailed(str(err)) from err
        self._consecutive_429 = 0

        include_history = self._include_history
        parcels = [
            normalize_parcel(order, include_history=include_history)
            for order in orders
        ]
        # An order without any usable id has no stable key for events or its
        # sensor, so it is skipped rather than published under a guess.
        parcels = [parcel for parcel in parcels if parcel["barcode"]]
        return self._publish(parcels, stamp_success=True)
