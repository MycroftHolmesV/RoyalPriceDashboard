# Royal Price Dashboard

An experimental Home Assistant App that turns the public catalogs produced by
`BrowseRoyalCaribbeanPrice.py` into a searchable, multi-cruise Ingress
dashboard.

> [!IMPORTANT]
> This community project is not affiliated with, endorsed by, or supported by
> Royal Caribbean Group, Royal Caribbean International, Celebrity Cruises, or
> Home Assistant. It uses public prices, which can differ from signed-in,
> reservation-specific offers.

The App is packaged for this independent third-party Home Assistant repository
with signed, versioned `amd64` and `aarch64` images. It does not require HACS,
jdeath's Home Assistant repository, the separate personalized price-checker
App, or a cruise-line account. See the [repository installation
guide](../README.md) for the current release status and setup steps.

On first run, **Add cruise** guides you through Royal Caribbean or Celebrity,
live public ship discovery, an actual future sailing, currency, and notification
settings. Additional cruises can be added and switched from
the dashboard; no cruise-line account or reservation is required. A confirmed
**Remove cruise** action deletes only that sailing's App-private catalog,
preferences, and price history.

When a cruise reaches its calculated return date, the dashboard marks it
complete and stops price refreshes. Nothing removes the cruise automatically:
its catalog, pins, and watches stay available, while price history remains
subject to the existing retention period, until you choose
**Remove completed cruise**.

It deliberately keeps four concepts separate:

- **All** includes every catalog item and never implies notifications.
- **Pinned** items form a per-cruise shortlist while remaining in **All**.
- **History** records price and availability changes for every item, whether it
  is pinned, unpinned, watched, or unwatched.
- **Watched** items have an explicit target price and are the only items that
  can create a Home Assistant persistent notification.

The **Changes** view answers the everyday "what changed since I last opened
this dashboard?" question. It defaults to all products, can be narrowed to
watched products, and remembers the prior visit separately for each cruise in
the current browser. A visit timestamp is never allowed to make the lookback
newer than the start of yesterday in the browser's local time. **All changed
items** instead shows each product with a recorded change once, using its latest
change and date. The first visit shows the latest recorded changes.
Watched product cards also show the date and amount of their most recent actual
price change.

Watched products with enough history can receive a factual price badge.
**Record low** means the current price matches the lowest saved available price
and at least one saved price was higher. Otherwise, **Below average** means the
current price is below that product's arithmetic mean across its saved
available price points. At least two price points are required. Because history
stores the initial baseline and later changes without duplicating unchanged
refreshes, this is a recorded-price average rather than a time-weighted average.

Each item's compact history chart remains quick to scan. Select the chart to
open a much larger view with more readable price labels; narrow screens can
scroll across the expanded chart.

Product rows can reveal the public catalog description through a compact
**Show description** control. Descriptions remain collapsed by default, are
included in catalog searches, and are normalized to plain text before they
reach the browser.

The existing Royal Caribbean Price Check App remains independent and untouched.

## Screenshots

Clean first-run setup (mobile viewport):

![Royal Price Dashboard first-run cruise setup](../screenshots/onboarding-mobile.jpg)

Curated public-price catalog (desktop viewport):

![Royal Price Dashboard populated catalog](../screenshots/dashboard-desktop.jpg)

Both screenshots use the repository's curated test fixtures, not a deployed
App or reservation. See [the screenshot notes](../screenshots/README.md).

## What it stores

Each cruise has a separate catalog, pin list, watch targets, and notification
setting. Compact SQLite history is keyed by ship, ISO sailing
date, currency, and product. All of that stays in the Home Assistant App's
private `/data` volume. It is not part of this source repository or Docker build
context.

No Royal Caribbean or Celebrity login is requested or stored. Exporting watches
produces a YAML snippet in the browser; it does not modify the separate price
checker automatically.

## Automatic refresh policy

A cruise with one or more explicitly watched products refreshes every 12 hours
by default. A cruise with no watches refreshes every 24 hours. Pins do not
select the faster schedule because pinning cannot generate an alert. Adding the
first watch or removing the last watch changes the schedule automatically from
the most recent successful catalog timestamp.

The Home Assistant App configuration accepts global YAML overrides. The
dashboard does not expose per-cruise cadence controls:

```yaml
watched_refresh_interval_hours: 12
unwatched_refresh_interval_hours: 24
```

Both optional values accept whole hours from 1 through 168 and are read when
the App starts. When they are omitted, the backend supplies the 12-hour and
24-hour defaults. The optional legacy `refresh_interval_hours` setting remains
accepted as an unwatched fallback so an existing installation can upgrade
without an invalid-option warning.

## Security boundary

- No host network, filesystem mappings, Docker API, Supervisor API, privileged
  mode, or full access.
- Home Assistant API access is used only for persistent notifications.
- Preferences and cached catalog data live in cruise-specific directories in
  the App's private `/data` volume.
- Compact per-sailing history lives in a private SQLite database. Unchanged
  refreshes do not create duplicate points, and a sailing's records expire 30
  days after its sailing date.
- The upstream browser is pinned to commit
  `bf5212c26576d468a6af2043565ece2d01f8b503` and verified by SHA-256 during
  the image build.
- `curl-cffi` is pinned alongside `requests` so the upstream browser can use
  its supported TLS-impersonation path for public endpoints that reject plain
  HTTP clients.

See the [security policy](../SECURITY.md) for reporting guidance and the remaining
hardening gates.

## Upstream relationship

The catalog browser comes from
[`jdeath/CheckRoyalCaribbeanPrice`](https://github.com/jdeath/CheckRoyalCaribbeanPrice).
Its MIT license is preserved in [upstream-LICENSE](upstream-LICENSE). The image
build downloads one pinned commit and fails if its SHA-256 checksum changes.
The verified upstream file stays unchanged in the image. A small local adapter
loads that exact file, requests the public product-description field across all
catalog categories, and adds machine-readable description markers to the
existing terminal output.

As of 2026-08-27, the pinned browser file is byte-for-byte identical to the
version on upstream `main`. Royal Price Dashboard still parses the upstream
terminal-oriented output; a structured JSON contract is the preferred future
integration.

## Upgrade compatibility

When a pre-0.3 installation has legacy `ship` and `sail_date` App options, the
dashboard creates a cruise registry and copies the existing root catalog and
watch preferences into that cruise's directory. Legacy hidden selections are
not converted to pins, so the initial pinned shortlist is empty. The original
files are deliberately left in place. The shared SQLite history already keys
records by ship, ISO sailing date, and currency, so prior history remains
available without a rewrite.

## Development

The App backend uses only the Python standard library at runtime; the container
also installs the dependencies required by the upstream browser. Run the local
checks with:

```text
python -m unittest discover -s tests -v
python -m py_compile server.py upstream_adapter.py tests/test_server.py
node --check static/app.js
```

Pull requests and main-branch pushes build both declared architectures without
publishing them. A matching GitHub release is the only event that can publish
signed images and the generic multi-architecture manifest.

Licensed under the [MIT License](../LICENSE). Contributions are welcome after
reading [CONTRIBUTING.md](../CONTRIBUTING.md). Artwork provenance and the exact
generation prompts are recorded in [ARTWORK.md](../ARTWORK.md).
