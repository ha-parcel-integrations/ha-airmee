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
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

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
}

# ``EDIT`` records that the order was amended, so it says nothing about where
# the parcel is. It must not become the parcel's status, and it must not warn
# either — it is a known value, just not a stage.
_NON_STATUS_CODES = frozenset({"EDIT"})

# Fallback for when the tracker omits the raw enum and only sends the localised
# ``courier_status_formatted`` text. Only unambiguous strings are mapped.
_FORMATTED_STATUS_MAP: dict[str, ParcelStatus] = {
    "delivered": ParcelStatus.DELIVERED,
    "out for delivery": ParcelStatus.OUT_FOR_DELIVERY,
    "picked up": ParcelStatus.IN_TRANSIT,
    "your product has been picked up": ParcelStatus.OUT_FOR_DELIVERY,
    "courier on route to pickup location": ParcelStatus.IN_TRANSIT,
    "unassigned": ParcelStatus.REGISTERED,
    "scanned": ParcelStatus.REGISTERED,
    "failed": ParcelStatus.PROBLEM,
    "returned": ParcelStatus.RETURNING,
}

# The dropoff window fields are Unix seconds sent as decimal strings (confirmed
# on the account route).
PLANNED_TIMES_VALIDATED = True

# Plausible Unix-seconds range (2015-01-01 .. 2100-01-01). Anything outside it
# (milliseconds, a stray zero) is refused rather than guessed at.
_EPOCH_MIN = 1_420_070_400
_EPOCH_MAX = 4_102_444_800
_EPOCH_MAX_DIGITS = 20

# The enum map is read off the app's own status list, not off a parcel anyone
# has tracked, so every mapping is a guess. Flip this to ``True`` once a real
# parcel has walked the stages, and the confirmation warnings stop.
STATUS_MAP_VALIDATED = False

# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()
_unconfirmed_mappings_logged: set[str] = set()


def _warn_unconfirmed_mapping(code: str, mapped: ParcelStatus) -> None:
    """Ask the user to confirm a mapping we have never seen on a parcel."""
    if STATUS_MAP_VALIDATED:
        return
    key = code.strip().casefold()
    if key in _unconfirmed_mappings_logged:
        return
    _unconfirmed_mappings_logged.add(key)
    _LOGGER.warning(
        "Airmee reported '%s', which this integration reads as '%s'. That "
        "mapping comes from the app's own status list, not from a parcel "
        "anyone has tracked — please confirm it looks right: %s",
        code.strip(),
        mapped,
        NEW_ISSUE_URL,
    )


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
    if str(code).strip().upper() in _NON_STATUS_CODES:
        return ParcelStatus.UNKNOWN
    mapped = _lookup(code)
    if mapped is not None:
        _warn_unconfirmed_mapping(str(code), mapped)
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
    if str(code).strip().upper() in _NON_STATUS_CODES:
        return None
    mapped = _lookup(code)
    if mapped is not None:
        _warn_unconfirmed_mapping(str(code), mapped)
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

    The field is a number or a string of decimal digits. Anything else, and
    numbers outside the plausible Unix-seconds range, are refused (``None``)
    with a one-shot warning naming the field, never the value.
    """
    if value is None:
        return None
    number = value
    if isinstance(value, str):
        text = value.strip()
        # Length-capped: int() on a very long digit string raises instead of
        # converting, and every shape this cannot read has to come back None.
        if len(text) <= _EPOCH_MAX_DIGITS and text.isascii() and text.isdigit():
            number = int(text)
    if isinstance(number, (int, float)) and not isinstance(number, bool):
        if _EPOCH_MIN <= number <= _EPOCH_MAX:
            return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
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


def _status_from_history(raw: dict) -> ParcelStatus | None:
    """Return the newest earlier timeline event that does map, else ``None``.

    A parcel whose newest event carries a word we have never seen is almost
    never a parcel in an unknown state — its previous events still say where
    it is.
    """
    timeline = raw.get("new_timeline")
    if not isinstance(timeline, list):
        return None
    for event in reversed(timeline[:-1]):
        if not isinstance(event, dict):
            continue
        code = event.get("courier_status")
        if not code or str(code).strip().upper() in _NON_STATUS_CODES:
            continue
        mapped = _lookup(str(code))
        if mapped is not None and mapped is not ParcelStatus.UNKNOWN:
            return mapped
    return None


def _current_status(raw: dict) -> tuple[str | None, ParcelStatus]:
    """Return ``(raw_status, status)`` from the newest timeline event.

    The raw ``courier_status`` enum is the source of record; the formatted
    display text only stands in when the enum is absent. An unmapped newest
    event falls back to the newest earlier event that does map, and only then
    to ``unknown`` — the warning fires either way.
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
    status = map_parcel_status(raw_status)
    if status is ParcelStatus.UNKNOWN:
        status = _status_from_history(raw) or ParcelStatus.UNKNOWN
    return raw_status, status


# ``raw`` ends up in sensor attributes and recorder-stored events, so a field
# only reaches it from this list. The order also holds names, addresses,
# coordinates, delivery photos, door and intercom codes, a proxy's personal
# number and the codes that claim the parcel, and it has never been seen on a
# real parcel, so an unknown field has to stay out by default. A field earns its
# place by being about the parcel or its delivery and useful in an automation.
PUBLISHED_RAW_FIELDS = frozenset(
    {
        "courier_status_formatted",
        "dropoff_earliest_time",
        "dropoff_latest_time",
        "dropoff_eta",
        "product_name",
        "is_large",
        "cold_delivery",
        "can_leave_in_front_of_door",
        "leave_in_front_of_door",
        "should_ring",
    }
)


def published_raw(raw: dict) -> dict:
    """Return the publishable subset of an order record."""
    return {key: value for key, value in raw.items() if key in PUBLISHED_RAW_FIELDS}


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED}
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


def normalize_parcel(
    raw: dict, *, tracking_code: str | None = None, include_history: bool = False
) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    ``tracking_code`` is the user's tracking-link token; it stands in as the
    barcode because no stable order reference has been confirmed on that route.
    The account inbox has no token, so it uses the order's ``id``.
    ``raw`` is :func:`published_raw` of the order; the whole record stays on the
    coordinator for diagnostics. ``receiver``, ``weight``, ``dimensions`` and
    ``pickup_point`` are
    ``None`` until a populated response establishes their shape.
    """
    raw_status, status = _current_status(raw)
    delivered = status is ParcelStatus.DELIVERED

    timeline = raw.get("new_timeline")
    events = (
        [event for event in timeline if isinstance(event, dict)]
        if isinstance(timeline, list)
        else []
    )
    event_statuses = [map_event_status(event.get("courier_status")) for event in events]
    delivered_at = None
    if delivered:
        delivery_event = next(
            (
                event
                for event, event_status in zip(
                    reversed(events), reversed(event_statuses)
                )
                if event_status is ParcelStatus.DELIVERED
            ),
            None,
        )
        if delivery_event is not None:
            delivered_at = to_iso_timestamp(
                delivery_event.get("time_of_event"), "time_of_event"
            )
        if delivered_at is None:
            delivered_at = to_iso_timestamp(raw.get("dropoff_eta"), "dropoff_eta")

    planned_from = planned_to = None
    if PLANNED_TIMES_VALIDATED:
        planned_from, planned_to = planned_window(
            to_iso_timestamp(raw.get("dropoff_earliest_time"), "dropoff_earliest_time")
            or to_iso_timestamp(raw.get("dropoff_eta"), "dropoff_eta"),
            to_iso_timestamp(raw.get("dropoff_latest_time"), "dropoff_latest_time"),
            zone=dt_util.get_default_time_zone(),
        )
    planned_from, planned_to = planned_for_status(status, planned_from, planned_to)
    pickup = is_pickup(None, status, event_statuses)

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
        "pickup": pickup,
        "pickup_point": None,
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": build_history(timeline) if include_history else None,
        "raw": published_raw(raw),
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
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
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
