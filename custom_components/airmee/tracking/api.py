"""Airmee consumer tracking API client."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from ..const import REQUEST_TIMEOUT_SECONDS, TRACKING_API_URL, TRACKING_ORIGIN

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

_HEADERS = {
    "Accept": "application/json",
    "Origin": TRACKING_ORIGIN,
    "Referer": f"{TRACKING_ORIGIN}/",
}


class AirmeeApiError(Exception):
    """Raised when an Airmee API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"Airmee API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def is_auth_error(self) -> bool:
        """Whether the response says the phone number hash is not accepted."""
        return self.status_code in (401, 403)


class AirmeeApiClient:
    """Client for Airmee's consumer tracking endpoint.

    One GET per tracking token, carrying the recipient's ``phone_number_hash``.
    The answer is ``{"order_details": [ {...} ]}``; an unknown token (or one
    paired with the wrong hash) is an empty list.
    """

    def __init__(
        self, session: aiohttp.ClientSession, phone_number_hash: str
    ) -> None:
        """Initialise the client with an aiohttp session and the recipient hash."""
        self._session = session
        self._phone_number_hash = phone_number_hash
        self._warned_empty = False

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's order record.

        Returns the first ``order_details`` entry, or ``None`` when the list is
        empty (unknown token, wrong hash, or an expired tracker — Airmee does
        not say which). Anything else unexpected raises
        :class:`AirmeeApiError`; network errors propagate as
        ``aiohttp.ClientError``.
        """
        params = {
            "tracking_url": tracking_code,
            "phone_number_hash": self._phone_number_hash,
        }
        async with self._session.get(
            TRACKING_API_URL, params=params, headers=_HEADERS, timeout=_TIMEOUT
        ) as response:
            if response.status == 429:
                retry_after_header = response.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_header) if retry_after_header else None
                except ValueError:
                    retry_after = None  # an HTTP-date, not seconds; the caller's own backoff applies
                raise AirmeeApiError("HTTP 429", status_code=429, retry_after=retry_after)
            if response.status != 200:
                raise AirmeeApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            try:
                # content_type=None: consumer endpoints routinely serve JSON as
                # text/plain, and aiohttp would otherwise refuse to parse it.
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise AirmeeApiError(f"unparseable body ({err})") from err

        if not isinstance(payload, dict):
            raise AirmeeApiError("unexpected body (not a JSON object)")
        orders = payload.get("order_details")
        if not isinstance(orders, list):
            raise AirmeeApiError("response has no order_details list")
        if not orders:
            if not self._warned_empty:
                self._warned_empty = True
                _LOGGER.warning(
                    "Airmee returned no order for a tracked link. Check the "
                    "tracking link and the phone number hash (Configure the "
                    "integration to re-enter it); finished parcels also expire"
                )
            return None
        order = orders[0]
        if not isinstance(order, dict):
            raise AirmeeApiError("order_details entry is not an object")
        return order
