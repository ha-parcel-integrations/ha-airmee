"""Sample Airmee order records shared by the test modules.

Built from the app's order model, not from a captured parcel: every value is
made up. Epoch values are Unix seconds.
"""
from __future__ import annotations

ACTIVE_CODE = "tok-active"
DELIVERED_CODE = "tok-delivered"
OTHER_CODE = "tok-other"
HASH = "hash01"


def event(courier_status: str, time_of_event: int, extra: str | None = None) -> dict:
    """One entry of ``new_timeline``."""
    return {
        "time_of_event": time_of_event,
        "courier_status": courier_status,
        "extra_information": extra,
    }


def delivered_sample(order_id: int = 1001) -> dict:
    """A delivered order."""
    return {
        "id": order_id,
        "receipt_id": f"R{order_id}",
        "sender_name": "Example Shop",
        "receiver_name": "Jane Doe",
        "dropoff_place_address": "1 Example Street",
        "courier_full_name": "Sam Courier",
        "courier_status_formatted": "Delivered",
        "dropoff_earliest_time": 1_777_460_000,
        "dropoff_latest_time": 1_777_467_200,
        "dropoff_eta": 1_777_463_000,
        "pin": "1234",
        "qrcode": "qr-data",
        "new_timeline": [
            event("REGISTERED", 1_777_400_000),
            event("PICKUP_CONFIRMED", 1_777_430_000),
            event("ON_WAY_TO_DROPOFF", 1_777_459_000),
            event("DROPOFF_CONFIRMED", 1_777_464_000),
        ],
    }


def active_sample(order_id: int = 2002) -> dict:
    """An out-for-delivery order."""
    sample = delivered_sample(order_id)
    sample["courier_status_formatted"] = "Out for delivery"
    sample["new_timeline"] = sample["new_timeline"][:3]
    return sample
