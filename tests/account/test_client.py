"""Tests for the Airmee account client: OTP login, auth header, token rotation."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.airmee.account.client import (
    AirmeeAccountAuthError,
    AirmeeAccountClient,
    AirmeeAccountError,
    AirmeeAccountOtpError,
    async_send_otp,
    async_verify_otp,
)


def _response(status: int, body=None, headers=None) -> MagicMock:
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
    return ctx


def _session(*, get=(), post=()) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(side_effect=list(get))
    session.post = MagicMock(side_effect=list(post))
    return session


def _client(session, callback=None) -> AirmeeAccountClient:
    return AirmeeAccountClient(
        session, access_token="acc-0", refresh_token="ref-0", token_callback=callback
    )


# ---------------------------------------------------------------------------
# OTP login
# ---------------------------------------------------------------------------


async def test_send_otp_uses_the_literal_temp_token_header_and_reads_top_level():
    session = _session(post=[_response(200, {"temp_token": "t", "otp_hash_code": "o"})])
    result = await async_send_otp(session, "+46", "700000000")

    assert result == ("t", "o")
    call = session.post.call_args
    assert call.args[0] == "https://api.airmee.com/customer/register/sendOtp"
    assert call.kwargs["json"] == {"country_code": "+46", "phone_number": "700000000"}
    assert call.kwargs["headers"]["Authorization"] == "TEMP_TOKEN"
    assert call.kwargs["headers"]["Content-Type"] == "application/json"


async def test_verify_otp_sends_the_temp_token_and_returns_both_tokens():
    session = _session(
        post=[
            _response(
                200,
                {"access_token": "a", "refresh_token": "r", "customer_profile": {}},
            )
        ]
    )
    tokens = await async_verify_otp(
        session,
        country_code="+46",
        phone_number="700000000",
        otp_code="123456",
        otp_hash_code="o",
        temp_token="t",
    )
    assert tokens == {"access_token": "a", "refresh_token": "r"}
    call = session.post.call_args
    assert call.args[0].endswith("/register/verifyOtp")
    assert call.kwargs["json"] == {
        "country_code": "+46",
        "phone_number": "700000000",
        "otp_code": "123456",
        "otp_hash_code": "o",
    }
    # raw token, no Bearer prefix
    assert call.kwargs["headers"]["Authorization"] == "t"


async def test_pre_login_4xx_is_a_rejected_request():
    for status in (400, 401, 403, 422):
        session = _session(post=[_response(status, {})])
        with pytest.raises(AirmeeAccountOtpError) as err:
            await async_send_otp(session, "+46", "1")
        assert err.value.status_code == status


async def test_pre_login_429_carries_retry_after():
    session = _session(post=[_response(429, {}, {"Retry-After": "30"})])
    with pytest.raises(AirmeeAccountError) as err:
        await async_send_otp(session, "+46", "1")
    assert not isinstance(err.value, AirmeeAccountOtpError)
    assert err.value.retry_after == 30.0


async def test_pre_login_server_error_and_bad_bodies():
    with pytest.raises(AirmeeAccountError):
        await async_send_otp(_session(post=[_response(500, {})]), "+46", "1")
    with pytest.raises(AirmeeAccountError, match="unparseable"):
        await async_send_otp(_session(post=[_response(200, "<html>")]), "+46", "1")
    for body in ({}, [], {"temp_token": "t"}, {"temp_token": "", "otp_hash_code": "o"}):
        with pytest.raises(AirmeeAccountError, match="response has no"):
            await async_send_otp(_session(post=[_response(200, body)]), "+46", "1")


async def test_errors_never_contain_the_secret_inputs():
    session = _session(post=[_response(400, {"message": "bad otp 999999 for 700000000"})])
    with pytest.raises(AirmeeAccountOtpError) as err:
        await async_verify_otp(
            session,
            country_code="+46",
            phone_number="700000000",
            otp_code="999999",
            otp_hash_code="o",
            temp_token="tt",
        )
    text = str(err.value)
    assert "999999" not in text and "700000000" not in text and "tt" not in text


# ---------------------------------------------------------------------------
# deliveries
# ---------------------------------------------------------------------------


async def test_deliveries_send_the_raw_access_token_and_merge_both_lists():
    session = _session(
        get=[
            _response(200, [{"id": 1}, {"id": 2}, "junk"]),
            _response(200, [{"id": 2}, {"id": 3}, {"x": 1}]),
        ]
    )
    orders = await _client(session).async_get_deliveries()

    assert [o.get("id") for o in orders] == [1, 2, 3, None]
    urls = [c.args[0] for c in session.get.call_args_list]
    assert urls == [
        "https://api.airmee.com/customer/deliveries",
        "https://api.airmee.com/customer/deliveries/completed",
    ]
    for call in session.get.call_args_list:
        assert call.kwargs["headers"]["Authorization"] == "acc-0"
        assert call.kwargs["headers"]["Content-Type"] == "application/json"


async def test_non_list_inbox_is_an_error():
    session = _session(get=[_response(200, {"data": []})])
    with pytest.raises(AirmeeAccountError, match="not a list"):
        await _client(session).async_get_deliveries()


async def test_429_and_5xx_on_the_inbox():
    with pytest.raises(AirmeeAccountError) as err:
        await _client(
            _session(get=[_response(429, {}, {"Retry-After": "bad"})])
        ).async_get_deliveries()
    assert err.value.status_code == 429 and err.value.retry_after is None
    with pytest.raises(AirmeeAccountError) as err:
        await _client(_session(get=[_response(503, {})])).async_get_deliveries()
    assert err.value.status_code == 503


async def test_network_errors_propagate():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await _client(session).async_get_deliveries()


# ---------------------------------------------------------------------------
# 401 -> refresh -> replay
# ---------------------------------------------------------------------------


async def test_401_rotates_both_tokens_persists_them_and_replays_once():
    callback = AsyncMock()
    session = _session(
        get=[
            _response(401, {}),
            _response(200, [{"id": 1}]),
            _response(200, []),
        ],
        post=[_response(200, {"access_token": "acc-1", "refresh_token": "ref-1"})],
    )
    client = _client(session, callback)

    orders = await client.async_get_deliveries()

    assert orders == [{"id": 1}]
    refresh = session.post.call_args
    assert refresh.args[0] == "https://api.airmee.com/customer/register/refreshToken"
    assert refresh.kwargs["json"] == {"refresh_token": "ref-0"}
    assert refresh.kwargs["headers"]["Authorization"] == "TEMP_TOKEN"
    callback.assert_awaited_once_with(
        {"access_token": "acc-1", "refresh_token": "ref-1"}
    )
    headers = [c.kwargs["headers"]["Authorization"] for c in session.get.call_args_list]
    assert headers == ["acc-0", "acc-1", "acc-1"]


async def test_second_401_after_refresh_is_an_auth_error():
    session = _session(
        get=[_response(401, {}), _response(401, {})],
        post=[_response(200, {"access_token": "a", "refresh_token": "r"})],
    )
    with pytest.raises(AirmeeAccountAuthError):
        await _client(session).async_get_deliveries()


@pytest.mark.parametrize(
    "refresh",
    [
        _response(401, {}),
        _response(400, {}),
        _response(403, {}),
        _response(200, {}),
        _response(200, {"access_token": "a"}),
        _response(200, "<html>"),
    ],
)
async def test_failed_refresh_is_an_auth_error_and_keeps_the_old_tokens(refresh):
    callback = AsyncMock()
    session = _session(get=[_response(401, {})], post=[refresh])
    client = _client(session, callback)
    with pytest.raises(AirmeeAccountAuthError):
        await client.async_get_deliveries()
    callback.assert_not_awaited()
    assert client._access_token == "acc-0" and client._refresh_token == "ref-0"


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_a_transient_refresh_failure_is_retryable_not_a_reauth(status):
    """An outage during a refresh must not cost the user the whole SMS login."""
    callback = AsyncMock()
    session = _session(get=[_response(401, {})], post=[_response(status, {})])
    client = _client(session, callback)
    with pytest.raises(AirmeeAccountError) as err:
        await client.async_get_deliveries()
    assert not isinstance(err.value, AirmeeAccountAuthError)
    assert err.value.status_code == status
    callback.assert_not_awaited()
    assert client._access_token == "acc-0" and client._refresh_token == "ref-0"


async def test_refresh_without_a_callback_still_rotates():
    session = _session(
        get=[_response(401, {}), _response(200, []), _response(200, [])],
        post=[_response(200, {"access_token": "a1", "refresh_token": "r1"})],
    )
    client = _client(session)
    await client.async_get_deliveries()
    assert client._access_token == "a1"


async def test_concurrent_401s_share_one_refresh():
    release = asyncio.Event()

    async def slow_post_enter():
        await release.wait()
        return _response(200, {"access_token": "acc-1", "refresh_token": "ref-1"}).__aenter__.return_value

    refresh_ctx = MagicMock()
    refresh_ctx.__aenter__ = AsyncMock(side_effect=slow_post_enter)
    refresh_ctx.__aexit__ = AsyncMock(return_value=False)

    def get_side_effect(url, headers, timeout=None):
        token = headers["Authorization"]
        if token == "acc-0":
            return _response(401, {})
        return _response(200, [])

    session = MagicMock()
    session.get = MagicMock(side_effect=get_side_effect)
    session.post = MagicMock(return_value=refresh_ctx)
    client = _client(session)

    first = asyncio.create_task(client._get("deliveries"))
    second = asyncio.create_task(client._get("deliveries"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release.set()
    assert await first == [] and await second == []
    assert session.post.call_count == 1
