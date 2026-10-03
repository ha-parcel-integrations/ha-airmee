# Airmee Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-airmee.svg)](https://github.com/ha-parcel-integrations/ha-airmee/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-airmee/total.svg)](https://github.com/ha-parcel-integrations/ha-airmee/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Airmee](https://airmee.com) parcels (Sweden). Two ways to follow them, chosen when you add the integration:

- **Account** — log in with your phone number (Airmee texts you a code). Your parcels are discovered automatically, no per-parcel input.
- **Tracking links** — add the token from each Airmee tracking link yourself, together with your phone number hash.

> **Pre-release (0.9.0).** Built from the Airmee app's data model and not yet confirmed against a real parcel. Anything the integration cannot read or map is logged once as a warning with a link to report it — please do.

Part of the [ha-parcel-integrations](https://ha-parcel-integrations.github.io/) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Account** mode: log in with an SMS code and every Airmee parcel on your number appears by itself
- **Tracking links** mode: track any number of Airmee parcels by the token in their tracking link
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), the carrier's own status text and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, recently delivered parcels
- Read-only **Deliveries** calendar of your active parcels
- `airmee.track_parcel` / `airmee.untrack_parcel` services (tracking links mode), so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2024.12 or newer
- **Account** mode: the phone number you use with Airmee, able to receive an SMS
- **Tracking links** mode: the token from an Airmee tracking link, and your
  phone number hash — the value Airmee's tracking page sends alongside your
  link (the `phone_number_hash` parameter in your browser's developer tools
  while the tracking page loads)

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-airmee` as an **Integration**.
3. Install **Airmee** and restart Home Assistant.

### Manual

Copy `custom_components/airmee` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → Airmee** and choose a source. You can add both.

**Account.** Enter your country code and phone number, then the code Airmee texts you. Only the resulting login tokens are stored — never your phone number. If Airmee later rejects the login, Home Assistant asks you to repeat the SMS step (use the same phone number). Account mode has no parcel list to manage: parcels come and go with your Airmee inbox.

**Tracking links.** Enter your phone number hash; it is kept as protected entry data and can be replaced from the entry's **Reconfigure** menu. Then add parcels via the integration's **Configure** dialog, the [`airmee.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). A parcel is added by the token in its tracking link. One tracking entry holds one phone number hash; Airmee only answers a link when it comes with the hash it belongs to, so a parcel for a different phone number needs its own hash.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels (tracking links only) | Add / remove | — | Manage the tracked tracking-link tokens. Changes apply immediately, no restart. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule with nothing to configure.

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence to
what your tracked parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 15 minutes)** — while any tracked parcel is out for delivery
  today, starting an hour before its delivery window opens (or immediately if
  no window is known yet).
- **Normal (every 45 minutes)** — for anything else still on its way.
- **Fully paused** — once every tracked parcel has been delivered, or nothing
  is tracked at all, polling stops until you add a parcel back (adding one
  always triggers an immediate check, regardless of the pause).
- A small, fixed per-hub offset is added on top, so not every Airmee
  hub out there polls at exactly the same second.

Airmee's delivery window is not read yet, so nothing is ever scheduled by
time: a parcel that is out for delivery is always polled on the 15-minute
schedule. Account mode never pauses — the inbox poll is also how new parcels
are found — and falls back to the 45-minute schedule when nothing is out for
delivery. Tracking links mode pauses as described above.


## Removal

Standard HA removal applies: **Settings → Devices & Services → Airmee → ⋮ → Delete**. Nothing is stored on Airmee's side. Deleting an account entry does not log you out of the Airmee app.

## Sensors

| Entity | Description |
|---|---|
| `sensor.airmee_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.airmee_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.airmee_next_delivery` | Earliest expected delivery moment across all active parcels. Airmee's delivery-window fields are not yet confirmed, so this stays `unknown` until a real parcel settles them (see Pre-release) |
| `sensor.airmee_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.airmee_last_successful_update` | Diagnostic: when Airmee was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `registered` | Announced to Airmee, not yet with a courier |
| `in_transit` | A courier has been assigned or has collected it |
| `out_for_delivery` | On its way to you |
| `at_pickup_point` | Ready for you to collect |
| `delivered` | Delivery confirmed |
| `problem` | Postponed (customer unreachable or another reason) or cancelled |
| `unknown` | Not yet reported, or a status we have not mapped yet |

`returning` is not used: Airmee reports no return leg.

Airmee's own status name is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the Airmee device):

| Event | When |
|---|---|
| `airmee_parcel_registered` | A new parcel appears in the active list |
| `airmee_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `airmee_parcel_delivered` | A parcel is delivered |
| `airmee_parcel_delivery_time_changed` | The expected delivery window changes |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `airmee.track_parcel` | `tracking_code` | Start tracking a parcel |
| `airmee.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.airmee: debug
```

## Troubleshooting

- **A parcel shows `unknown` (tracking links)** — Airmee answered with nothing for that link: the token or the phone number hash is wrong, or the tracker has expired. Check both; a rejected hash starts a re-enter prompt.
- **Home Assistant asks to log in again (account)** — Airmee rejected the stored login. Repeat the SMS step with the same phone number.
- **A status logs "Unrecognised Airmee status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-airmee/issues/new) with the logged line so the mapping can be extended.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by Airmee, Home Assistant, or any other third party referenced in this project. Please don't contact Airmee for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or API key where required. These may change or be withdrawn without notice and may be subject to Airmee's terms. Data is sent only to Airmee's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

Tracking links mode uses the same tracking service as Airmee's consumer tracking page; account mode uses the same sign-in and parcel list as the Airmee app, with your own login.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
