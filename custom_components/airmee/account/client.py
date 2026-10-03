"""Client for the Airmee customer account API (phone-OTP login, deliveries inbox).

Every response is the DTO itself, not an envelope. The ``Authorization``
header carries the raw token with no ``Bearer`` prefix; the pre-login and
refresh calls send the literal ``TEMP_TOKEN``. No token, OTP code or phone
number is ever written to a log or an exception message.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from ..const import ACCOUNT_API_URL, NEW_BUG_URL, REQUEST_TIMEOUT_SECONDS

_LOGGER = logging.getLogger(__name__)

TokenCallback = Callable[[dict[str, str]], Awaitable[None]]

_TEMP_TOKEN = "TEMP_TOKEN"
_TIMEOUT = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

# Reported shapes are deliberately keys-and-status only — a value here could be
# a token, an OTP code or a phone number.
_shape_warnings_logged: set[str] = set()


def _warn_shape(context: str, detail: str, payload: Any = None) -> None:
    """Warn once that the account API answered in an unconfirmed shape."""
    if context in _shape_warnings_logged:
        return
    _shape_warnings_logged.add(context)
    keys = sorted(payload) if isinstance(payload, dict) else None
    _LOGGER.warning(
        "Unexpected Airmee account response from %s: %s. This surface was built "
        "from the app and is still unconfirmed — please report it: %s%s",
        context,
        detail,
        NEW_BUG_URL,
        f"\n  top-level keys: {keys}" if keys else "",
    )


class AirmeeAccountError(Exception):
    """An unexpected account API response; the message never holds a secret."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store safe failure metadata."""
        super().__init__(f"Airmee account request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class AirmeeAccountOtpError(AirmeeAccountError):
    """The SMS code (or the login attempt it belongs to) was rejected."""


class AirmeeAccountAuthError(AirmeeAccountError):
    """The stored tokens are no longer usable; the OTP login must be redone."""


def _text(payload: Any, key: str, context: str) -> str:
    """Return a required top-level string field or raise without the body."""
    value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value:
        _warn_shape(f"{context}:{key}", f"no usable {key!r} field", payload)
        raise AirmeeAccountError(f"response has no {key}")
    return value


async def _json(response: aiohttp.ClientResponse, context: str) -> Any:
    """Read a JSON body, mapping a parse failure to a safe error."""
    try:
        # content_type=None: the backend is not strict about its JSON type.
        return await response.json(content_type=None)
    except ValueError as err:
        _warn_shape(
            f"{context}:body", f"HTTP {response.status} body was not JSON"
        )
        raise AirmeeAccountError("unparseable body") from err


def _retry_after(response: aiohttp.ClientResponse) -> float | None:
    """Parse a numeric ``Retry-After`` header, or ``None``."""
    header = response.headers.get("Retry-After")
    try:
        return float(header) if header else None
    except ValueError:
        return None


async def _post(
    session: aiohttp.ClientSession,
    path: str,
    body: dict[str, Any],
    token: str,
) -> Any:
    """POST a pre-login call; 4xx is reported as a rejected request."""
    async with session.post(
        f"{ACCOUNT_API_URL}/{path}",
        json=body,
        headers={"Authorization": token, "Content-Type": "application/json"},
        timeout=_TIMEOUT,
    ) as response:
        if response.status == 429:
            _warn_shape(f"{path}:429", "rate limited")
            raise AirmeeAccountError(
                "HTTP 429", status_code=429, retry_after=_retry_after(response)
            )
        if 400 <= response.status < 500:
            raise AirmeeAccountOtpError(
                f"HTTP {response.status}", status_code=response.status
            )
        if response.status != 200:
            _warn_shape(f"{path}:{response.status}", f"HTTP {response.status}")
            raise AirmeeAccountError(
                f"HTTP {response.status}", status_code=response.status
            )
        return await _json(response, path)


async def async_send_otp(
    session: aiohttp.ClientSession, country_code: str, phone_number: str
) -> tuple[str, str]:
    """Ask Airmee to text a code; return ``(temp_token, otp_hash_code)``."""
    payload = await _post(
        session,
        "register/sendOtp",
        {"country_code": country_code, "phone_number": phone_number},
        _TEMP_TOKEN,
    )
    context = "register/sendOtp"
    return _text(payload, "temp_token", context), _text(
        payload, "otp_hash_code", context
    )


async def async_verify_otp(
    session: aiohttp.ClientSession,
    *,
    country_code: str,
    phone_number: str,
    otp_code: str,
    otp_hash_code: str,
    temp_token: str,
) -> dict[str, str]:
    """Exchange the SMS code for the ``access_token`` / ``refresh_token`` pair."""
    payload = await _post(
        session,
        "register/verifyOtp",
        {
            "country_code": country_code,
            "phone_number": phone_number,
            "otp_code": otp_code,
            "otp_hash_code": otp_hash_code,
        },
        temp_token,
    )
    context = "register/verifyOtp"
    return {
        "access_token": _text(payload, "access_token", context),
        "refresh_token": _text(payload, "refresh_token", context),
    }


class AirmeeAccountClient:
    """Reads the logged-in customer's deliveries, refreshing tokens on a 401."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        access_token: str,
        refresh_token: str,
        token_callback: TokenCallback | None = None,
    ) -> None:
        """Hold the entry-owned token pair and the hook that persists rotations."""
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_callback = token_callback
        self._refresh_lock = asyncio.Lock()

    async def _refresh(self, stale_access_token: str) -> None:
        """Rotate both tokens once; concurrent callers share the result."""
        async with self._refresh_lock:
            if self._access_token != stale_access_token:
                return  # another request already rotated while we waited
            try:
                async with self._session.post(
                    f"{ACCOUNT_API_URL}/register/refreshToken",
                    json={"refresh_token": self._refresh_token},
                    headers={
                        "Authorization": _TEMP_TOKEN,
                        "Content-Type": "application/json",
                    },
                    timeout=_TIMEOUT,
                ) as response:
                    # Only an outright rejection means the stored pair is dead.
                    # A 429 or a 5xx is Airmee being unavailable: raising an
                    # auth error there would force the user through the whole
                    # SMS login again over a transient outage.
                    if response.status in (400, 401, 403):
                        raise AirmeeAccountAuthError(
                            f"refresh HTTP {response.status}",
                            status_code=response.status,
                        )
                    if response.status == 429:
                        raise AirmeeAccountError(
                            "refresh HTTP 429",
                            status_code=429,
                            retry_after=_retry_after(response),
                        )
                    if response.status != 200:
                        _warn_shape(
                            f"register/refreshToken:{response.status}",
                            f"HTTP {response.status}",
                        )
                        raise AirmeeAccountError(
                            f"refresh HTTP {response.status}",
                            status_code=response.status,
                        )
                    payload = await _json(response, "register/refreshToken")
                context = "register/refreshToken"
                tokens = {
                    "access_token": _text(payload, "access_token", context),
                    "refresh_token": _text(payload, "refresh_token", context),
                }
            except AirmeeAccountAuthError:
                raise
            except AirmeeAccountError as err:
                # A transient status stays retryable; only a 2xx we cannot read
                # means the pair itself is unusable and reauth is the way out.
                if err.status_code == 429 or (err.status_code or 0) >= 500:
                    raise
                raise AirmeeAccountAuthError("refresh returned no tokens") from err
            self._access_token = tokens["access_token"]
            self._refresh_token = tokens["refresh_token"]
            if self._token_callback:
                await self._token_callback(tokens)

    async def _get(self, path: str) -> Any:
        """GET with the access token, rotating and replaying once on a 401."""
        for attempt in (1, 2):
            token = self._access_token
            async with self._session.get(
                f"{ACCOUNT_API_URL}/{path}",
                headers={
                    "Authorization": token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=_TIMEOUT,
            ) as response:
                if response.status == 401:
                    if attempt == 2:
                        raise AirmeeAccountAuthError(
                            "token rejected after refresh", status_code=401
                        )
                elif response.status == 429:
                    raise AirmeeAccountError(
                        "HTTP 429", status_code=429, retry_after=_retry_after(response)
                    )
                elif response.status != 200:
                    _warn_shape(f"{path}:{response.status}", f"HTTP {response.status}")
                    raise AirmeeAccountError(
                        f"HTTP {response.status}", status_code=response.status
                    )
                else:
                    return await _json(response, path)
            # Only a first-attempt 401 falls out of the block above.
            await self._refresh(token)
        raise AirmeeAccountError("unreachable")  # pragma: no cover

    async def async_get_deliveries(self) -> list[dict[str, Any]]:
        """Return active and completed deliveries as one de-duplicated list."""
        orders: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in ("deliveries", "deliveries/completed"):
            payload = await self._get(path)
            if not isinstance(payload, list):
                _warn_shape(
                    f"{path}:shape",
                    f"expected a list of orders, got {type(payload).__name__}",
                    payload,
                )
                raise AirmeeAccountError(f"{path} is not a list")
            for order in payload:
                if not isinstance(order, dict):
                    continue
                key = str(order.get("id") or order.get("receipt_id") or "")
                if key and key in seen:
                    continue
                seen.add(key)
                orders.append(order)
        return orders
