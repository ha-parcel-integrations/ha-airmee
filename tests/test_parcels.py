"""Tests for the pure parcel-mapping helpers (no Home Assistant instance)."""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airmee import parcels as parcels_module
from custom_components.airmee.const import (
    CAPABILITIES,
    CAPABILITIES_BY_VARIANT,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.airmee.parcels import (
    apply_delivered_filter,
    build_history,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
    tracking_url,
)

from .payloads import active_sample, delivered_sample, event

EPOCH = 1_777_464_000  # 2026-04-29T12:00:00+00:00

# ---------------------------------------------------------------------------
# status map: the closed CourierStatus enum
# ---------------------------------------------------------------------------

ENUM_MAP = {
    "REGISTERED": ParcelStatus.REGISTERED,
    "WAITING": ParcelStatus.REGISTERED,
    "UNASSIGNED": ParcelStatus.REGISTERED,
    "SCAN": ParcelStatus.REGISTERED,
    "ASSIGNED": ParcelStatus.IN_TRANSIT,
    "ON_WAY_TO_PICKUP": ParcelStatus.IN_TRANSIT,
    "PICKUP_CONFIRMED": ParcelStatus.IN_TRANSIT,
    "ON_WAY_TO_DROPOFF": ParcelStatus.OUT_FOR_DELIVERY,
    "READY_FOR_CUSTOMER_PICKUP": ParcelStatus.AT_PICKUP_POINT,
    "DROPOFF_CONFIRMED": ParcelStatus.DELIVERED,
    "POST_DROPOFF_CONFIRMED": ParcelStatus.DELIVERED,
    "POSTPONED_UNREACHABLE_CUSTOMER": ParcelStatus.PROBLEM,
    "POSTPONED_OTHER": ParcelStatus.PROBLEM,
    "CANCELLED": ParcelStatus.PROBLEM,
    "EDIT": ParcelStatus.UNKNOWN,
}


@pytest.mark.parametrize("code,expected", list(ENUM_MAP.items()))
def test_every_courier_status_maps(code, expected):
    assert map_parcel_status(code) == expected


def test_enum_map_is_closed_at_fifteen_values():
    assert len(ENUM_MAP) == 15


def test_only_the_two_dropoff_confirmations_are_delivered():
    delivered = {c for c, s in ENUM_MAP.items() if s is ParcelStatus.DELIVERED}
    assert delivered == {"DROPOFF_CONFIRMED", "POST_DROPOFF_CONFIRMED"}


def test_status_lookup_ignores_case_and_whitespace():
    assert map_parcel_status(" dropoff_confirmed ") == ParcelStatus.DELIVERED


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Delivered", ParcelStatus.DELIVERED),
        ("Out for delivery", ParcelStatus.OUT_FOR_DELIVERY),
        ("Picked up", ParcelStatus.IN_TRANSIT),
        ("Unassigned", ParcelStatus.REGISTERED),
        ("Scanned", ParcelStatus.REGISTERED),
        ("Failed", ParcelStatus.PROBLEM),
        ("Returned", ParcelStatus.RETURNING),
    ],
)
def test_formatted_text_fallback(text, expected):
    assert map_parcel_status(text) == expected


def test_map_parcel_status_missing_is_unknown_silently(caplog):
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN
    assert "Unrecognised" not in caplog.text


def test_map_event_status_missing_and_unmapped_are_none():
    assert map_event_status(None) is None
    assert map_event_status("SOMETHING_NEW") is None


def test_unmapped_status_warns_only_once(caplog):
    with caplog.at_level("WARNING"):
        assert map_parcel_status("SOMETHING_NEW") == ParcelStatus.UNKNOWN
        assert map_parcel_status("SOMETHING_NEW") == ParcelStatus.UNKNOWN
    assert caplog.text.count("Unrecognised Airmee status") == 1
    assert "issues/new?template=unrecognised_status.yml" in caplog.text


# ---------------------------------------------------------------------------
# time parsing: Unix UTC seconds only
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z") == datetime(
        2026, 4, 29, 13, 12, 42, tzinfo=timezone.utc
    )
    assert parse_iso("2026-04-29T13:12:42").tzinfo is not None
    assert parse_iso("nonsense") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_converts_epoch_seconds():
    assert to_iso_timestamp(EPOCH) == "2026-04-29T12:00:00+00:00"
    assert to_iso_timestamp(float(EPOCH)) == "2026-04-29T12:00:00+00:00"
    assert to_iso_timestamp(None) is None


@pytest.mark.parametrize("value", [EPOCH * 1000, 0, "2026-04-29", True, [1]])
def test_to_iso_timestamp_refuses_other_shapes_and_warns_once(value, caplog):
    parcels_module._unparseable_times_logged.clear()
    with caplog.at_level("WARNING"):
        assert to_iso_timestamp(value, "dropoff_eta") is None
        assert to_iso_timestamp(value, "dropoff_eta") is None
    assert caplog.text.count("cannot read") == 1
    assert "dropoff_eta" in caplog.text


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def test_build_history_orders_oldest_to_newest():
    events = [event("DROPOFF_CONFIRMED", EPOCH), event("REGISTERED", EPOCH - 5000)]
    history = build_history(events)
    assert [h["raw_status"] for h in history] == ["REGISTERED", "DROPOFF_CONFIRMED"]
    assert history[0]["status"] == ParcelStatus.REGISTERED
    assert set(history[0]) == {"timestamp", "status", "raw_status"}


def test_build_history_caps_to_max_events():
    events = [event("SCAN", EPOCH + i) for i in range(30)]
    assert len(build_history(events)) == 20
    assert len(build_history(events, max_events=3)) == 3


def test_build_history_skips_malformed_and_untimed_events():
    events = [None, "x", {"courier_status": "SCAN"}, event("SCAN", EPOCH)]
    assert len(build_history(events)) == 1
    assert build_history(None) == []


def test_build_history_unmapped_status_stays_none():
    history = build_history([event("BRAND_NEW", EPOCH)])
    assert history[0]["status"] is None
    assert history[0]["raw_status"] == "BRAND_NEW"


def test_build_history_keeps_unparseable_timestamp_out():
    assert build_history([event("SCAN", EPOCH * 1000)]) == []


# ---------------------------------------------------------------------------
# normalize_parcel
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
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


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    for capabilities in CAPABILITIES_BY_VARIANT.values():
        assert capabilities <= KNOWN_CAPABILITIES
    assert CAPABILITIES == CAPABILITIES_BY_VARIANT["Tracking"]


def test_capabilities_match_what_normalize_parcel_actually_returns():
    delivered = normalize_parcel(delivered_sample(), tracking_code="tok")
    active = normalize_parcel(active_sample(), tracking_code="tok")
    with_history = normalize_parcel(
        delivered_sample(), tracking_code="tok", include_history=True
    )

    if "weight" in CAPABILITIES:
        assert delivered["weight"] is not None
    if "dimensions" in CAPABILITIES:
        assert delivered["dimensions"] is not None
    if "delivery_window" in CAPABILITIES:
        assert active["planned_from"] is not None or active["planned_to"] is not None
    if "pickup_point" in CAPABILITIES:
        assert delivered["pickup_point"] is not None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "history" in CAPABILITIES:
        assert with_history["history"] is not None
    # What is not claimed must really be empty.
    assert delivered["weight"] is None and delivered["dimensions"] is None
    assert active["planned_from"] is None and active["planned_to"] is None
    assert delivered["pickup_point"] is None and delivered["receiver"] is None


def test_account_variant_has_no_url():
    parcel = normalize_parcel(active_sample())
    assert parcel["url"] is None
    assert "url" not in CAPABILITIES_BY_VARIANT["Account"]


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample(), tracking_code="tok")
    assert parcel["carrier"] == "Airmee"
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "DROPOFF_CONFIRMED"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T12:00:00+00:00"
    assert parcel["planned_from"] is None and parcel["planned_to"] is None
    assert parcel["sender"] == "Example Shop"
    assert parcel["history"] is None


def test_url_is_the_reconstructed_consumer_link_without_the_hash():
    parcel = normalize_parcel(delivered_sample(), tracking_code="tok")
    assert parcel["url"] == "https://tracking.airmee.com/track/tok"
    assert tracking_url(None) is None


def test_post_dropoff_confirmed_is_delivered():
    raw = delivered_sample()
    raw["new_timeline"][-1]["courier_status"] = "POST_DROPOFF_CONFIRMED"
    assert normalize_parcel(raw)["delivered"] is True


def test_active_parcel_is_not_delivered():
    parcel = normalize_parcel(active_sample(), tracking_code="tok")
    assert parcel["status"] is ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_unconfirmed_dropoff_window_is_not_read(monkeypatch):
    """The ETA fields stay unread until a real response confirms their unit."""
    assert normalize_parcel(active_sample())["planned_from"] is None


def test_validated_dropoff_window_is_read(monkeypatch):
    monkeypatch.setattr(parcels_module, "PLANNED_TIMES_VALIDATED", True)
    parcel = normalize_parcel(active_sample())
    assert parcel["planned_from"] == "2026-04-29T10:53:20+00:00"
    assert parcel["planned_to"] == "2026-04-29T12:53:20+00:00"


def test_validated_window_falls_back_to_eta_and_collapses_equal_ends(monkeypatch):
    monkeypatch.setattr(parcels_module, "PLANNED_TIMES_VALIDATED", True)
    raw = active_sample()
    raw["dropoff_earliest_time"] = None
    parcel = normalize_parcel(raw)
    assert parcel["planned_from"] == "2026-04-29T11:43:20+00:00"
    raw["dropoff_earliest_time"] = raw["dropoff_latest_time"] = 1_777_460_000
    assert normalize_parcel(raw)["planned_to"] is None


def test_validated_window_is_cleared_once_delivered(monkeypatch):
    monkeypatch.setattr(parcels_module, "PLANNED_TIMES_VALIDATED", True)
    parcel = normalize_parcel(delivered_sample())
    assert parcel["planned_from"] is None and parcel["planned_to"] is None


def test_formatted_status_is_used_when_the_raw_enum_is_absent():
    raw = {"courier_status_formatted": "Out for delivery"}
    parcel = normalize_parcel(raw)
    assert parcel["status"] is ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["raw_status"] == "Out for delivery"


def test_enum_beats_formatted_text():
    raw = {
        "courier_status_formatted": "Delivered",
        "new_timeline": [event("ON_WAY_TO_DROPOFF", EPOCH)],
    }
    assert normalize_parcel(raw)["status"] is ParcelStatus.OUT_FOR_DELIVERY


def test_unknown_enum_value_is_unknown_not_delivered():
    raw = {"new_timeline": [event("TELEPORTED", EPOCH)]}
    parcel = normalize_parcel(raw)
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False


def test_pickup_flag_follows_the_status():
    raw = {"new_timeline": [event("READY_FOR_CUSTOMER_PICKUP", EPOCH)]}
    parcel = normalize_parcel(raw)
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True


def test_delivered_without_readable_time_keeps_delivered():
    raw = {"new_timeline": [event("DROPOFF_CONFIRMED", EPOCH * 1000)]}
    parcel = normalize_parcel(raw)
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] is None


def test_normalize_history_is_opt_in():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert [h["raw_status"] for h in parcel["history"]][-1] == "DROPOFF_CONFIRMED"


def test_normalize_empty_record_is_a_pending_placeholder(caplog):
    parcels_module._schema_drift_logged = False
    with caplog.at_level("WARNING"):
        parcel = normalize_parcel({}, tracking_code="tok")
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["barcode"] == "tok"
    assert parcel["raw_status"] is None
    assert "format may have changed" not in caplog.text


def test_schema_drift_warns_once_with_keys_only(caplog):
    parcels_module._schema_drift_logged = False
    raw = {"something_new": "secret value"}
    with caplog.at_level("WARNING"):
        normalize_parcel(raw)
        normalize_parcel(raw)
    assert caplog.text.count("format may have changed") == 1
    assert "something_new" in caplog.text
    assert "secret value" not in caplog.text


def test_barcode_is_the_token_else_id_else_receipt_id():
    raw = delivered_sample()
    assert normalize_parcel(raw, tracking_code="tok")["barcode"] == "tok"
    assert normalize_parcel(raw)["barcode"] == "1001"
    del raw["id"]
    assert normalize_parcel(raw)["barcode"] == "R1001"
    del raw["receipt_id"]
    assert normalize_parcel(raw)["barcode"] is None


def test_normalize_keeps_the_full_raw_record_untrimmed():
    raw = delivered_sample()
    parcel = normalize_parcel(raw)
    assert parcel["raw"] is raw
    assert parcel["raw"]["pin"] == "1234"
    assert parcel["raw"]["receiver_name"] == "Jane Doe"


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels
