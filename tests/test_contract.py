"""The parcel contract's checks, run over every normalised sample."""

from datetime import datetime

import pytest
from homeassistant.util import dt as dt_util

from custom_components.airmee.const import ParcelStatus
from custom_components.airmee.parcels import (
    normalize_parcel,
    parse_iso,
    planned_window,
)

from .payloads import active_sample, delivered_sample, event

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def _at_pickup_point() -> dict:
    sample = active_sample()
    sample["new_timeline"] = [
        event("REGISTERED", 1_777_400_000),
        event("READY_FOR_CUSTOMER_PICKUP", 1_777_459_000),
    ]
    return sample


def _collected() -> dict:
    sample = delivered_sample()
    sample["new_timeline"] = [
        event("REGISTERED", 1_777_400_000),
        event("READY_FOR_CUSTOMER_PICKUP", 1_777_459_000),
        event("DROPOFF_CONFIRMED", 1_777_464_000),
    ]
    return sample


def _collected_then_amended() -> dict:
    sample = _collected()
    sample["new_timeline"].append(event("EDIT", 1_777_470_000))
    return sample


def _returning() -> dict:
    return {"courier_status_formatted": "Returned", "dropoff_eta": 1_777_463_000}


def _problem() -> dict:
    sample = active_sample()
    sample["new_timeline"][-1] = event("POSTPONED_OTHER", 1_777_459_000)
    return sample


def _moment_only() -> dict:
    sample = active_sample()
    sample["dropoff_earliest_time"] = sample["dropoff_latest_time"] = None
    return sample


SAMPLES = {
    "delivered": delivered_sample,
    "active": active_sample,
    "at_pickup_point": _at_pickup_point,
    "collected": _collected,
    "collected_then_amended": _collected_then_amended,
    "returning": _returning,
    "problem": _problem,
    "moment_only": _moment_only,
    "bare": lambda: {"id": 1},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param](), tracking_code="tok")


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_collected_pickup_parcel_stays_pickup():
    parcel = normalize_parcel(_collected())
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["delivered_at"] == "2026-04-29T12:00:00+00:00"


def test_an_amendment_after_collection_keeps_pickup_and_delivery_time():
    parcel = normalize_parcel(_collected_then_amended())
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["delivered_at"] == "2026-04-29T12:00:00+00:00"


def test_a_home_delivery_is_not_pickup():
    assert normalize_parcel(delivered_sample())["pickup"] is False


def test_a_return_is_not_delivered():
    parcel = normalize_parcel(_returning())
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False


def test_a_moment_ends_at_the_end_of_that_local_day(stockholm):
    parcel = normalize_parcel(_moment_only())
    start = parse_iso(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert end.date() == dt_util.as_local(start).date()
    assert parcel["planned_to"] == "2026-04-29T23:59:59+02:00"


def test_a_date_covers_the_whole_day(stockholm):
    start, end = planned_window("2099-01-01", None, zone=stockholm)
    start, end = datetime.fromisoformat(start), datetime.fromisoformat(end)
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert start.date() == end.date()
