"""Tests for the Airmee tracking API client."""
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.airmee.tracking.api import AirmeeApiClient, AirmeeApiError

from ..payloads import ACTIVE_CODE, HASH, active_sample

CODE = ACTIVE_CODE


def _session_returning(
    status: int, body: object = None, headers: dict | None = None
) -> MagicMock:
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


async def test_get_parcel_returns_first_order_and_sends_token_hash_and_headers():
    session = _session_returning(200, {"order_details": [active_sample()]})
    order = await AirmeeApiClient(session, HASH).async_get_parcel(CODE)

    assert order["id"] == 2002
    call = session.get.call_args
    assert call.args[0] == "https://api.airmee.com/track/track_by_url"
    assert call.kwargs["params"] == {
        "tracking_url": CODE,
        "phone_number_hash": HASH,
    }
    headers = call.kwargs["headers"]
    assert headers["Origin"] == "https://tracking.airmee.com"
    assert headers["Referer"] == "https://tracking.airmee.com/"
    assert headers["Accept"] == "application/json"
    # the secret never ends up in the URL string itself
    assert HASH not in call.args[0] and CODE not in call.args[0]


async def test_empty_order_details_is_none_and_warns_once_without_secrets(caplog):
    client = AirmeeApiClient(_session_returning(200, {"order_details": []}), HASH)
    with caplog.at_level("WARNING"):
        assert await client.async_get_parcel(CODE) is None
        assert await client.async_get_parcel(CODE) is None
    assert caplog.text.count("returned no order") == 1
    assert HASH not in caplog.text and CODE not in caplog.text


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_statuses_are_flagged(status):
    client = AirmeeApiClient(_session_returning(status, {}), HASH)
    with pytest.raises(AirmeeApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.is_auth_error
    assert err.value.status_code == status


async def test_server_error_is_not_an_auth_error():
    client = AirmeeApiClient(_session_returning(500, {}), HASH)
    with pytest.raises(AirmeeApiError) as err:
        await client.async_get_parcel(CODE)
    assert not err.value.is_auth_error


async def test_429_carries_numeric_retry_after():
    client = AirmeeApiClient(
        _session_returning(429, {}, {"Retry-After": "120"}), HASH
    )
    with pytest.raises(AirmeeApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after == 120.0


@pytest.mark.parametrize("headers", [{}, {"Retry-After": "Wed, 21 Oct 2026"}])
async def test_429_without_usable_retry_after(headers):
    client = AirmeeApiClient(_session_returning(429, {}, headers), HASH)
    with pytest.raises(AirmeeApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.retry_after is None


async def test_non_json_body_raises():
    client = AirmeeApiClient(_session_returning(200, "<html>"), HASH)
    with pytest.raises(AirmeeApiError):
        await client.async_get_parcel(CODE)


@pytest.mark.parametrize(
    "body",
    [["not", "a", "dict"], {}, {"order_details": None}, {"order_details": "x"}],
)
async def test_missing_or_misshapen_order_details_raises(body):
    client = AirmeeApiClient(_session_returning(200, body), HASH)
    with pytest.raises(AirmeeApiError):
        await client.async_get_parcel(CODE)


async def test_order_that_is_not_an_object_raises():
    client = AirmeeApiClient(_session_returning(200, {"order_details": ["x"]}), HASH)
    with pytest.raises(AirmeeApiError):
        await client.async_get_parcel(CODE)


async def test_network_error_is_left_alone():
    """ClientError propagates — DataUpdateCoordinator already wraps it."""
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await AirmeeApiClient(session, HASH).async_get_parcel(CODE)
