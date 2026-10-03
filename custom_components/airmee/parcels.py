"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

The carrier-specific parts are :data:`_STATUS_MAP`, :data:`_FORMATTED_STATUS_MAP`
and :func:`normalize_parcel`; the rest is suite-wide machinery.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-airmee/issues/new"
    "?template=unrecognised_status.yml"
)

# The app's closed ``CourierStatus`` enum, carried per ``new_timeline`` event.
_STATUS_MAP: dict[str, ParcelStatus] = {
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

# Fallback for when the tracker omits the raw enum and only sends the localised
# ``courier_status_formatted`` text. Only unambiguous strings are mapped.
_FORMATTED_STATUS_MAP: dict[str, ParcelStatus] = {
    "delivered": ParcelStatus.DELIVERED,
    "out for delivery": ParcelStatus.OUT_FOR_DELIVERY,
    "picked up": ParcelStatus.IN_TRANSIT,
    "unassigned": ParcelStatus.REGISTERED,
    "scanned": ParcelStatus.REGISTERED,
    "failed": ParcelStatus.PROBLEM,
    "returned": ParcelStatus.RETURNING,
}

# Window times are not read until an ETA field has been seen on the wire with a
# known epoch unit and timezone; the capability is not claimed either.
PLANNED_TIMES_VALIDATED = False

# Plausible Unix-seconds range (2015-01-01 .. 2100-01-01). Anything outside it
# (milliseconds, a stray zero) is refused rather than guessed at.
_EPOCH_MIN = 1_420_070_400
_EPOCH_MAX = 4_102_444_800

# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised Airmee status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def _lookup(code: str) -> ParcelStatus | None:
    """Look a status up in the enum map, then the formatted-text map."""
    key = str(code).strip()
    mapped = _STATUS_MAP.get(key.upper())
    if mapped is not None:
        return mapped
    return _FORMATTED_STATUS_MAP.get(key.casefold())


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a carrier status code to a canonical :class:`ParcelStatus`.

    Accepts the raw ``CourierStatus`` enum name or, as a fallback, the
    formatted display text. ``None`` reports ``unknown`` silently; an
    unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = _lookup(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's status code to a canonical status, or ``None``.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warn once, reusing the parcel-status one-shot set.
    """
    if not code:
        return None
    mapped = _lookup(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


_unparseable_times_logged: set[str] = set()


def to_iso_timestamp(value: Any, field: str = "timestamp") -> str | None:
    """Return an ISO 8601 UTC string for an Airmee epoch-seconds field.

    Numbers outside the plausible Unix-seconds range are refused (``None``)
    with a one-shot warning naming the field, never the value.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if _EPOCH_MIN <= value <= _EPOCH_MAX:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    if field not in _unparseable_times_logged:
        _unparseable_times_logged.add(field)
        _LOGGER.warning(
            "Airmee sent a time we cannot read (field %s, type %s) — help us "
            "check the format. Open an issue: %s",
            field,
            type(value).__name__,
            NEW_ISSUE_URL,
        )
    return None


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the carrier's event list.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` is the carrier's own text, or
    its event code when the API has no human-readable text. Sorted oldest →
    newest and capped to the most recent ``max_events``.
    """
    entries: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        timestamp = to_iso_timestamp(event.get("time_of_event"), "time_of_event")
        if not timestamp:
            continue
        code = event.get("courier_status")
        entries.append(
            {
                "timestamp": timestamp,
                "status": map_event_status(code),
                "raw_status": code,
            }
        )
    # Every timestamp is the same-offset ISO string from to_iso_timestamp, so
    # string order is chronological order.
    ordered = sorted(entries, key=lambda item: item["timestamp"])
    return ordered[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


_schema_drift_logged = False


def _order_reference(raw: dict) -> str | None:
    """Return the order's own ``id`` (else ``receipt_id``) as a string."""
    for key in ("id", "receipt_id"):
        value = raw.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _current_status(raw: dict) -> tuple[str | None, ParcelStatus]:
    """Return ``(raw_status, status)`` from the newest timeline event.

    The raw ``courier_status`` enum is the source of record; the formatted
    display text only stands in when the enum is absent.
    """
    global _schema_drift_logged
    timeline = raw.get("new_timeline")
    latest = timeline[-1] if isinstance(timeline, list) and timeline else None
    enum_name = latest.get("courier_status") if isinstance(latest, dict) else None
    formatted = raw.get("courier_status_formatted")
    raw_status = enum_name or formatted or None
    if raw_status is None and raw and not _schema_drift_logged:
        _schema_drift_logged = True
        _LOGGER.warning(
            "Airmee order has neither new_timeline nor courier_status_formatted "
            "— the response format may have changed. Open an issue and paste "
            "this line: %s\n  keys=%s",
            NEW_ISSUE_URL,
            sorted(raw),
        )
    return raw_status, map_parcel_status(raw_status)


def normalize_parcel(
    raw: dict, *, tracking_code: str | None = None, include_history: bool = False
) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    ``tracking_code`` is the user's tracking-link token; it stands in as the
    barcode because no stable order reference has been confirmed on that route.
    The account inbox has no token, so it uses the order's ``id``.
    ``raw`` is the whole order record, untrimmed — redaction is a diagnostics
    concern. ``receiver``, ``weight``, ``dimensions`` and ``pickup_point`` are
    ``None`` until a populated response establishes their shape.
    """
    raw_status, status = _current_status(raw)
    delivered = status is ParcelStatus.DELIVERED

    timeline = raw.get("new_timeline")
    latest = timeline[-1] if isinstance(timeline, list) and timeline else {}
    delivered_at = (
        to_iso_timestamp(latest.get("time_of_event"), "time_of_event")
        if delivered and isinstance(latest, dict)
        else None
    )

    planned_from = planned_to = None
    if PLANNED_TIMES_VALIDATED and not delivered:
        planned_from = to_iso_timestamp(
            raw.get("dropoff_earliest_time"), "dropoff_earliest_time"
        )
        planned_to = to_iso_timestamp(
            raw.get("dropoff_latest_time"), "dropoff_latest_time"
        )
        if planned_from is None:
            planned_from = to_iso_timestamp(raw.get("dropoff_eta"), "dropoff_eta")
        if planned_from and planned_to and parse_iso(planned_to) == parse_iso(planned_from):
            planned_to = None

    return {
        "carrier": "Airmee",
        "barcode": tracking_code or _order_reference(raw),
        "sender": raw.get("sender_name") or None,
        "receiver": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": None,
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": build_history(timeline) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
