"""Tests for the Airmee config and options flow."""
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airmee.account.client import (
    AirmeeAccountError,
    AirmeeAccountOtpError,
)
from custom_components.airmee.config_flow import (
    _account_unique_id,
    normalize_tracking_code,
    valid_tracking_code,
)
from custom_components.airmee.const import (
    CONF_ACCESS_TOKEN,
    CONF_COUNTRY_CODE,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_OTP_CODE,
    CONF_PARCELS,
    CONF_PHONE_NUMBER,
    CONF_PHONE_NUMBER_HASH,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)

SEND = "custom_components.airmee.config_flow.async_send_otp"
VERIFY = "custom_components.airmee.config_flow.async_verify_otp"
TOKENS = {"access_token": "acc-1", "refresh_token": "ref-1"}


@pytest.fixture
def no_setup():
    """Reload-after-update must not build a real client."""
    with patch("custom_components.airmee.async_setup_entry", return_value=True):
        yield


def test_normalize_tracking_code_only_trims():
    assert normalize_tracking_code("  AbC-12 x \n") == "AbC-12 x"
    assert normalize_tracking_code("") == ""
    assert normalize_tracking_code(None) == ""


def test_valid_tracking_code_accepts_any_non_empty_code():
    assert valid_tracking_code("abc")
    assert valid_tracking_code("A" * 31)
    assert not valid_tracking_code("")


# ---------------------------------------------------------------------------
# setup menu and tracking source
# ---------------------------------------------------------------------------


async def test_user_step_offers_both_sources(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "menu"
    assert result["menu_options"] == [SOURCE_ACCOUNT, SOURCE_TRACKING]


async def _start(hass, choice: str):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": choice}
    )


async def test_tracking_flow_stores_the_hash_as_entry_data(hass):
    result = await _start(hass, SOURCE_TRACKING)
    assert result["step_id"] == SOURCE_TRACKING
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PHONE_NUMBER_HASH: "  abc123 "}
    )
    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_SOURCE: SOURCE_TRACKING,
        CONF_PHONE_NUMBER_HASH: "abc123",
    }
    assert result["options"][CONF_PARCELS] == []
    assert CONF_PHONE_NUMBER_HASH not in result["options"]


async def test_tracking_flow_rejects_an_empty_hash(hass):
    result = await _start(hass, SOURCE_TRACKING)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PHONE_NUMBER_HASH: "   "}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_phone_number_hash"}


async def test_second_tracking_hub_is_rejected(hass):
    MockConfigEntry(domain=DOMAIN, unique_id=SOURCE_TRACKING).add_to_hass(hass)
    result = await _start(hass, SOURCE_TRACKING)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PHONE_NUMBER_HASH: "abc123"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_tracking_reconfigure_replaces_the_hash(hass, no_setup):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PHONE_NUMBER_HASH: "newhash"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PHONE_NUMBER_HASH] == "newhash"
    assert entry.data[CONF_SOURCE] == SOURCE_TRACKING


async def test_tracking_reauth_asks_for_the_hash_again(hass, no_setup):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PHONE_NUMBER_HASH: "   "}
    )
    assert result["errors"] == {"base": "invalid_phone_number_hash"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PHONE_NUMBER_HASH: "fresh"}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PHONE_NUMBER_HASH] == "fresh"


# ---------------------------------------------------------------------------
# account source: phone -> OTP -> tokens
# ---------------------------------------------------------------------------

PHONE = {CONF_COUNTRY_CODE: " +46 ", CONF_PHONE_NUMBER: " 700000000 "}


async def test_account_flow_sends_otp_verifies_and_stores_only_tokens(hass):
    result = await _start(hass, SOURCE_ACCOUNT)
    assert result["step_id"] == SOURCE_ACCOUNT
    with patch(SEND, new=AsyncMock(return_value=("temp-1", "otphash-1"))) as send:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], PHONE
        )
    send.assert_awaited_once()
    assert send.await_args.args[1:] == ("+46", "700000000")
    assert result["step_id"] == "otp"

    with patch(VERIFY, new=AsyncMock(return_value=TOKENS)) as verify:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_OTP_CODE: " 123456 "}
        )
    assert verify.await_args.kwargs == {
        "country_code": "+46",
        "phone_number": "700000000",
        "otp_code": "123456",
        "otp_hash_code": "otphash-1",
        "temp_token": "temp-1",
    }
    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_SOURCE: SOURCE_ACCOUNT,
        CONF_ACCESS_TOKEN: "acc-1",
        CONF_REFRESH_TOKEN: "ref-1",
    }
    # nothing about the login attempt or the number is persisted
    assert "700000000" not in repr(result["data"]) + repr(result["options"])
    assert CONF_PARCELS not in result["options"]


async def test_account_flow_blank_phone_is_rejected_without_a_request(hass):
    result = await _start(hass, SOURCE_ACCOUNT)
    with patch(SEND, new=AsyncMock()) as send:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_COUNTRY_CODE: " ", CONF_PHONE_NUMBER: "1"}
        )
    send.assert_not_awaited()
    assert result["errors"] == {"base": "invalid_phone_number"}


async def test_account_flow_send_errors(hass):
    result = await _start(hass, SOURCE_ACCOUNT)
    for error, key in (
        (AirmeeAccountOtpError("x"), "invalid_phone_number"),
        (AirmeeAccountError("x"), "cannot_connect"),
        (aiohttp.ClientError(), "cannot_connect"),
    ):
        with patch(SEND, new=AsyncMock(side_effect=error)):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], PHONE
            )
        assert result["step_id"] == SOURCE_ACCOUNT
        assert result["errors"] == {"base": key}


async def _to_otp(hass):
    result = await _start(hass, SOURCE_ACCOUNT)
    with patch(SEND, new=AsyncMock(return_value=("temp-1", "otphash-1"))):
        return await hass.config_entries.flow.async_configure(result["flow_id"], PHONE)


async def test_account_flow_verify_errors_keep_the_login_attempt(hass):
    result = await _to_otp(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTP_CODE: "  "}
    )
    assert result["errors"] == {"base": "invalid_otp"}
    for error, key in (
        (AirmeeAccountOtpError("x"), "invalid_otp"),
        (AirmeeAccountError("x"), "cannot_connect"),
        (aiohttp.ClientError(), "cannot_connect"),
    ):
        with patch(VERIFY, new=AsyncMock(side_effect=error)):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_OTP_CODE: "000000"}
            )
        assert result["step_id"] == "otp"
        assert result["errors"] == {"base": key}
    # a correct retry still works against the same temp token
    with patch(VERIFY, new=AsyncMock(return_value=TOKENS)) as verify:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_OTP_CODE: "123456"}
        )
    assert result["type"] == "create_entry"
    assert verify.await_args.kwargs["temp_token"] == "temp-1"


async def test_same_account_cannot_be_added_twice(hass):
    unique_id = _account_unique_id("+46", "700000000")
    MockConfigEntry(domain=DOMAIN, unique_id=unique_id).add_to_hass(hass)
    result = await _to_otp(hass)
    with patch(VERIFY, new=AsyncMock(return_value=TOKENS)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_OTP_CODE: "123456"}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


def _account_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=_account_unique_id("+46", "700000000"),
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_ACCESS_TOKEN: "old-acc",
            CONF_REFRESH_TOKEN: "old-ref",
        },
    )


async def test_account_reauth_reruns_otp_and_replaces_both_tokens(hass, no_setup):
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == SOURCE_ACCOUNT
    with patch(SEND, new=AsyncMock(return_value=("temp-2", "otphash-2"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], PHONE
        )
    with patch(VERIFY, new=AsyncMock(return_value=TOKENS)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_OTP_CODE: "123456"}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_ACCESS_TOKEN] == "acc-1"
    assert entry.data[CONF_REFRESH_TOKEN] == "ref-1"


async def test_account_reauth_with_a_different_phone_is_refused(hass, no_setup):
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(SEND, new=AsyncMock(return_value=("t", "o"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_COUNTRY_CODE: "+31", CONF_PHONE_NUMBER: "600000000"},
        )
    with patch(VERIFY, new=AsyncMock(return_value=TOKENS)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_OTP_CODE: "123456"}
        )
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_ACCESS_TOKEN] == "old-acc"


async def test_account_reconfigure_reruns_otp(hass, no_setup):
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == SOURCE_ACCOUNT
    with patch(SEND, new=AsyncMock(return_value=("t", "o"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], PHONE
        )
    with patch(VERIFY, new=AsyncMock(return_value=TOKENS)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_OTP_CODE: "123456"}
        )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_ACCESS_TOKEN] == "acc-1"


# ---------------------------------------------------------------------------
# options flow
# ---------------------------------------------------------------------------


def _hub(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_SOURCE: SOURCE_TRACKING, CONF_PHONE_NUMBER_HASH: "hash01"},
        options={CONF_PARCELS: parcels},
    )


def _settings_input(*, history=False, filter_type="days", amount=7) -> dict:
    return {
        CONF_DELIVERED_FILTER_TYPE: filter_type,
        CONF_DELIVERED_FILTER_AMOUNT: amount,
        CONF_INCLUDE_HISTORY: history,
    }


async def _open_options_step(hass, entry, step_id: str):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["menu_options"] == ["parcels", "settings"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


async def test_account_options_menu_has_no_parcels_step(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == ["settings"]


async def test_entry_without_source_is_a_tracking_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="legacy")
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == ["parcels", "settings"]


async def test_options_add_parcel_keeps_case_and_trims(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["  aBc-123 "]}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "aBc-123"}]


async def test_options_de_duplicates_and_drops_blanks(hass):
    entry = _hub([{CONF_TRACKING_CODE: "tok1"}])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["tok1", " tok1", "", "tok2"]}
    )
    assert result["data"][CONF_PARCELS] == [
        {CONF_TRACKING_CODE: "tok1"},
        {CONF_TRACKING_CODE: "tok2"},
    ]


async def test_options_remove_and_clear(hass):
    entry = _hub([{CONF_TRACKING_CODE: "tok1"}, {CONF_TRACKING_CODE: "tok2"}])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["tok2"]}
    )
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "tok2"}]
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": []}
    )
    assert result["data"][CONF_PARCELS] == []


async def test_options_changes_history_and_delivered(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _settings_input(history=True, filter_type="parcels", amount=5),
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_INCLUDE_HISTORY] is True
    assert result["data"][CONF_DELIVERED_FILTER_TYPE] == "parcels"
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 5
    assert result["data"][CONF_PARCELS] == []


@pytest.mark.parametrize(
    ("country_code", "phone_number"),
    [
        ("+46", "701234567"),
        ("46", "701234567"),
        ("0046", "701234567"),
        ("+46", "0701234567"),
        ("  +46 ", "070 123 45 67"),
    ],
)
def test_account_unique_id_survives_how_the_number_is_typed(country_code, phone_number):
    """Reauth must not abort with wrong_account over +46 vs 0046 vs a trunk zero.

    A mismatch here locks the user out of their own entry: reauth refuses the
    account and the only way back is deleting and re-adding it.
    """
    assert _account_unique_id(country_code, phone_number) == _account_unique_id(
        "+46", "701234567"
    )


def test_account_unique_id_still_separates_real_accounts():
    assert _account_unique_id("+46", "701234567") != _account_unique_id(
        "+46", "701234568"
    )
    assert _account_unique_id("+46", "701234567") != _account_unique_id(
        "+47", "701234567"
    )
