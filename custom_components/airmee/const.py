"""Constants for the Airmee parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "airmee"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping a carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Claimed only where normalize_parcel() fills the field from a real value.
# ``delivery_window`` is not claimed: the epoch unit of the dropoff window has
# not been seen on the wire, so planned_from / planned_to stay ``None``.
# ``weight``, ``dimensions`` and ``pickup_point`` have no confirmed source field.
# The account inbox has no tracking token to build a ``url`` from.
CAPABILITIES_BY_VARIANT = {
    "Tracking": frozenset({"url", "history"}),
    "Account": frozenset({"history"}),
}

# Fields not confirmed yet — the docs site shows them as "awaiting data".
# Move a field into the declaration above once a real parcel shows it.
PENDING_CAPABILITIES_BY_VARIANT = {
    "Tracking": frozenset({"delivery_window", "pickup_point"}),
    "Account": frozenset({"delivery_window", "pickup_point"}),
}
# Aliased to the tracking source, as the flat shape the parcel tests check.
CAPABILITIES = CAPABILITIES_BY_VARIANT["Tracking"]

# ``TRACKING_API_URL`` is the consumer tracker's JSON route. It takes two query
# parameters: ``tracking_url`` (the opaque token from the recipient's tracking
# link) and ``phone_number_hash`` (a per-recipient value the tracker page sends
# alongside it). No API key or session. The tracker sends browser-shaped
# ``Origin`` / ``Referer`` headers and the client mirrors them. An unknown token
# is answered with HTTP 200 and an empty ``order_details`` list.
TRACKING_API_URL = "https://api.airmee.com/track/track_by_url"
TRACKING_ORIGIN = "https://tracking.airmee.com"
TRACKING_URL = "https://tracking.airmee.com/track/{tracking_code}"

# The customer app's backend. Phone-OTP login, raw-token ``Authorization``.
ACCOUNT_API_URL = "https://api.airmee.com/customer"

# The account surface was reconstructed from the app, never seen on the wire.
# Until a real login confirms it, every unexpected status or missing field is
# reported once with this link so a tester can hand back the shape.
NEW_BUG_URL = (
    "https://github.com/ha-parcel-integrations/ha-airmee/issues/new"
    "?template=bug_report.yml"
)

# The app itself waits 20s. aiohttp's default total is 300s, which would freeze
# a config-flow form for five minutes on a hung call.
REQUEST_TIMEOUT_SECONDS = 20

# An entry's source lives in ``entry.data``; an entry without it is a tracking
# entry. Account tokens and the recipient's ``phone_number_hash`` are secret
# entry data — never options, never logged, always redacted from diagnostics.
CONF_SOURCE = "source"
SOURCE_TRACKING = "tracking"
SOURCE_ACCOUNT = "account"
CONF_PHONE_NUMBER_HASH = "phone_number_hash"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_COUNTRY_CODE = "country_code"
CONF_PHONE_NUMBER = "phone_number"
CONF_OTP_CODE = "otp_code"

# Tracked parcels live in the config entry options as a list of
# ``{tracking_code}`` dicts — this carrier has no account or parcel feed, so the
# user enters the tracking-link tokens themselves. Kept as dicts so future per-parcel fields
# slot in without an options migration.
CONF_PARCELS = "parcels"
CONF_TRACKING_CODE = "tracking_code"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Dynamic, status-driven polling — unconditional across the suite, no
# user-facing interval option (see scaffold/CLAUDE.md's "Dynamic polling"
# section for the full algorithm and the reasoning behind it).
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one tracked,
# not-yet-delivered parcel is out_for_delivery within HOT_LOOKAHEAD_HOURS of
# its planned_from (or has no planned_from at all); mid = anything else still
# in flight (registered, in_transit, at_pickup_point, unknown, problem,
# returning).
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

# Per-parcel status history is opt-in and off by default, identical across the
# suite. Keep it off by default even when — as here — the timeline arrives in
# the same response and costs no extra request: it is a large attribute, and on
# carriers that need a second call per parcel the cost is real.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
