"""Coordinator for the tracking-link source.

Each tracking-link token the user entered is fetched individually and merged
into one list; the shared publishing lives in :mod:`..coordinator`.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import CONF_PARCELS, CONF_TRACKING_CODE
from ..coordinator import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    AirmeeBaseCoordinator,
)
from ..parcels import normalize_parcel
from .api import AirmeeApiClient, AirmeeApiError

_LOGGER = logging.getLogger(__name__)


class AirmeeCoordinator(AirmeeBaseCoordinator):
    """Polls each tracked token and publishes the canonical parcel lists."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AirmeeApiClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(hass, client, entry)
        # tracking_code -> last successful raw payload, so a transient fetch
        # failure or a not-found blip keeps the parcel visible instead of
        # dropping its sensor. Lives for the integration's lifetime.
        self._raw_cache: dict[str, dict] = {}
        # Consecutive 429 responses across all tracked parcels, for the
        # exponential backoff. Reset to 0 on any success.
        self._consecutive_429 = 0

    def _tracked(self) -> list[str]:
        """Return the configured tracking codes."""
        return [
            item[CONF_TRACKING_CODE]
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if item.get(CONF_TRACKING_CODE)
        ]

    async def _async_update_data(self) -> list[dict]:
        """Fetch every tracked parcel and split into active vs delivered."""
        codes = self._tracked()

        # Drop cache entries for parcels the user no longer follows, so the
        # cache stays bounded.
        tracked_codes = set(codes)
        self._raw_cache = {
            code: raw for code, raw in self._raw_cache.items() if code in tracked_codes
        }
        self._delivered_codes &= tracked_codes

        # A delivered parcel's payload can never change again, so it is
        # dropped from the fetch — not from ``codes``/the options list, which
        # stays untouched until the user removes it by hand.
        codes_to_fetch = [code for code in codes if code not in self._delivered_codes]

        results = await asyncio.gather(
            *(self._client.async_get_parcel(code) for code in codes_to_fetch),
            return_exceptions=True,
        )

        raws_by_code: dict[str, dict] = {}
        errors = 0
        retry_afters: list[float] = []
        saw_429 = False
        for code, result in zip(codes_to_fetch, results):
            if isinstance(result, BaseException):
                if not isinstance(result, (AirmeeApiError, aiohttp.ClientError)):
                    raise result
                if isinstance(result, AirmeeApiError) and result.is_auth_error:
                    raise ConfigEntryAuthFailed(
                        "Airmee rejected the phone number hash"
                    ) from result
                errors += 1
                if isinstance(result, AirmeeApiError) and result.status_code == 429:
                    saw_429 = True
                    if result.retry_after is not None:
                        retry_afters.append(result.retry_after)
                _LOGGER.warning("Airmee fetch failed: %s", result)
                cached = self._raw_cache.get(code)
                if cached is not None:
                    raws_by_code[code] = cached
                continue

            if result is None:
                # Unknown code, or not scanned yet. Keep prior data if we have
                # it, otherwise show a pending placeholder so the user still
                # sees the parcel they asked us to track.
                raws_by_code[code] = self._raw_cache.get(code) or {}
                continue

            self._raw_cache[code] = result
            raws_by_code[code] = result

        # Codes skipped from the fetch above (already confirmed delivered) —
        # re-add their cached payload so the delivered sensor keeps its data
        # until the retention filter drops it.
        for code in self._delivered_codes:
            cached = self._raw_cache.get(code)
            if cached is not None:
                raws_by_code[code] = cached

        if saw_429:
            # A 429 anywhere in this batch means the whole poll backs off —
            # one hot parcel's cadence must not keep hammering an endpoint
            # that just asked everyone to slow down.
            self._consecutive_429 += 1
            retry_after = (
                max(retry_afters)
                if retry_afters
                else min(
                    BACKOFF_BASE_SECONDS * 2**self._consecutive_429,
                    BACKOFF_CAP_SECONDS,
                )
            )
            raise UpdateFailed("Airmee rate-limited (429)", retry_after=retry_after)
        self._consecutive_429 = 0

        if codes_to_fetch and errors == len(codes_to_fetch) and not raws_by_code:
            raise UpdateFailed("Airmee unreachable for all tracked parcels")

        include_history = self._include_history
        entries = [
            (
                code,
                normalize_parcel(
                    raw, tracking_code=code, include_history=include_history
                ),
            )
            for code, raw in raws_by_code.items()
        ]
        # Rebuilt fresh from this cycle's data — a code whose payload just
        # flipped to delivered is skipped starting next cycle; one that
        # somehow un-delivers rejoins the fetch list automatically.
        self._delivered_codes = {code for code, parcel in entries if parcel["delivered"]}

        return self._publish(
            [parcel for _, parcel in entries],
            stamp_success=not codes_to_fetch or errors < len(codes_to_fetch),
        )
