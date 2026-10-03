"""Diagnostics support for the Airmee parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import REDACTED

from . import AirmeeConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address, a parcel or a credential. Over-redacting is
# cheap; under-redacting leaks a user's home address into a GitHub thread.
# Keys are kept; only values are replaced. ``raw`` itself is never trimmed —
# this is the only place privacy is applied.
TO_REDACT = {
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "url",
    # credentials and login material
    "phone_number_hash",
    "access_token",
    "refresh_token",
    "temp_token",
    "otp_hash_code",
    "otp_code",
    "country_code",
    "phone_number",
    # order identity and free text that can name a person
    "id",
    "receipt_id",
    "pin",
    "qrcode",
    "extra_information",
    "customer_profile",
}

# Any key containing one of these ``_``-separated words is redacted too, so a
# payload field we have not seen (``dropoff_place_address``, ``courier_full_name``,
# ``courier_lat``) is covered without being listed.
SENSITIVE_WORDS = {
    "name",
    "address",
    "phone",
    "email",
    "lat",
    "lng",
    "lon",
    "latitude",
    "longitude",
    "coordinates",
    "location",
    "street",
    "city",
    "zip",
    "postcode",
    "pin",
    "qr",
    "qrcode",
    "token",
    "hash",
    "url",
    "signature",
}


def _sensitive(key: str) -> bool:
    """Whether a payload key must have its value replaced."""
    lowered = key.lower()
    return lowered in TO_REDACT or bool(SENSITIVE_WORDS & set(lowered.split("_")))


def redact(data: Any) -> Any:
    """Return ``data`` with sensitive values replaced and every key preserved."""
    if isinstance(data, dict):
        return {
            key: REDACTED if _sensitive(str(key)) else redact(value)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact(item) for item in data]
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AirmeeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Airmee config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_data": redact(dict(entry.data)),
        "entry_options": redact(dict(entry.options)),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
            "skipped_from_fetch": len(coordinator.delivered_codes),
        },
        "polling": {
            "tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "suspended": coordinator.update_interval is None,
        },
        "incoming": redact(coordinator.data or []),
        "delivered": redact(coordinator.delivered or []),
    }
