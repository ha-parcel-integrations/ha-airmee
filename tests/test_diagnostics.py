"""Tests for Airmee diagnostics."""

from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.airmee.diagnostics import (
    async_get_config_entry_diagnostics,
    redact,
)
from custom_components.airmee.parcels import published_raw

from .payloads import active_sample

REDACTED = "**REDACTED**"


def test_redact_keeps_every_key_and_replaces_sensitive_values():
    raw = active_sample()
    raw.update(
        {
            "tracking_url": "https://tracking.airmee.com/track/tok",
            "courier_lat": 59.3,
            "courier_lng": 18.0,
            "receiver_phone": "+46700000000",
            "weird_new_address_line": "x",
            "door_code": "1234",
            "door_access": "x",
            "intercom_number": "x",
            "floor": "3",
            "type_of_building": "x",
            "access_via_courtyard": True,
            "proxy_personal_number": {"proxy_personal_number": "x"},
            "courier_locations": ["x"],
            "photos_array": ["x"],
            "locker_box_mark": "x",
            "dropoff_feedback": "x",
            "return_reason": "x",
        }
    )
    result = redact(raw)

    assert set(result) == set(raw)
    for key in (
        "id",
        "receipt_id",
        "sender_name",
        "receiver_name",
        "dropoff_place_address",
        "courier_full_name",
        "pin",
        "qrcode",
        "tracking_url",
        "courier_lat",
        "courier_lng",
        "receiver_phone",
        "weird_new_address_line",
        "door_code",
        "door_access",
        "intercom_number",
        "floor",
        "type_of_building",
        "access_via_courtyard",
        "proxy_personal_number",
        "courier_locations",
        "photos_array",
        "locker_box_mark",
        "dropoff_feedback",
        "return_reason",
    ):
        assert result[key] == REDACTED, key
    # the status vocabulary and the times are what a bug report needs
    assert result["courier_status_formatted"] == "Out for delivery"
    assert result["dropoff_latest_time"] == raw["dropoff_latest_time"]
    assert result["new_timeline"][0]["courier_status"] == "REGISTERED"
    assert (
        result["new_timeline"][0]["time_of_event"]
        == raw["new_timeline"][0]["time_of_event"]
    )


def test_redact_walks_lists_and_nested_dicts():
    result = redact(
        {"parcels": [{"tracking_code": "tok", "n": {"city": "X", "ok": 1}}]}
    )
    assert result == {
        "parcels": [{"tracking_code": REDACTED, "n": {"city": REDACTED, "ok": 1}}]
    }


def _entry(data: dict) -> MagicMock:
    entry = MagicMock()
    entry.data = data
    entry.options = {"parcels": [{"tracking_code": "tok"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "tok",
            "sender": "Example Shop",
            "receiver": "Jane Doe",
            "url": "https://tracking.airmee.com/track/tok",
            "status": "out_for_delivery",
            "raw": published_raw(active_sample()),
        }
    ]
    entry.runtime_data.coordinator.orders = [active_sample()]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()
    return entry


async def test_diagnostics_redacts_credentials_and_payload_pii(hass):
    entry = _entry(
        {
            "source": "account",
            "access_token": "acc",
            "refresh_token": "ref",
            "phone_number_hash": "hash01",
        }
    )
    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["entry_data"] == {
        "source": "account",
        "access_token": REDACTED,
        "refresh_token": REDACTED,
        "phone_number_hash": REDACTED,
    }
    assert result["entry_options"]["parcels"][0]["tracking_code"] == REDACTED
    incoming = result["incoming"][0]
    assert incoming["barcode"] == REDACTED
    assert incoming["receiver"] == REDACTED
    assert incoming["url"] == REDACTED
    assert "pin" not in incoming["raw"]
    assert "receiver_name" not in incoming["raw"]
    assert result["orders"][0]["pin"] == REDACTED
    assert result["orders"][0]["receiver_name"] == REDACTED
    assert incoming["status"] == "out_for_delivery"
    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "skipped_from_fetch": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    text = repr(result)
    for secret in ("acc", "ref", "hash01", "Jane Doe", "1 Example Street", "1234"):
        assert f"'{secret}'" not in text


async def test_diagnostics_reports_suspended_polling(hass):
    entry = _entry({})
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }
