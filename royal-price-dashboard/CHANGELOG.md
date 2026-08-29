# Changelog

## 0.6.0

- Package the App in the Home Assistant third-party repository layout with a
  root `repository.yaml` and one-click My Home Assistant repository link.
- Declare `amd64` and `aarch64` support through a generic GHCR
  multi-architecture image.
- Add release-gated Home Assistant builder workflows for native architecture
  builds, Cosign signing, and a versioned multi-architecture manifest. Pull
  requests and ordinary pushes build without publishing.
- Use a cold Home Assistant backup so the App is stopped while Supervisor copies
  JSON and SQLite state.
- Restrict the distributed App container's HTTP service to the Supervisor
  Ingress proxy and loopback clients while leaving ordinary source-only local
  runs unrestricted unless an allowlist is configured.
- Add local regression coverage for repository metadata, minimum privileges,
  Ingress client filtering, and restoring a copied cold-backup data directory.

## 0.5.2 (private patch)

- Default Changes to all catalog items so the everyday summary does not appear
  empty merely because no watched product has changed.
- Give **Since last visit** a minimum lookback to the start of yesterday in the
  browser's local time, preventing another visit today from immediately hiding
  useful recent changes.
- Add an **All changed items** option that shows each product with at least one
  recorded price or availability change once, ordered by its latest change and
  labeled with the change date.
- Fit all four catalog tabs across phone widths so **All** and **Changes** stay
  visible together without horizontal scrolling.

## 0.5.1 (private patch)

- Open **View history** from a Changes card in the expanded history dialog
  without leaving the Changes view or inheriting an unrelated catalog scroll
  position.
- Add Watch/Unwatch and Pin/Unpin actions directly to Changes cards, using the
  same explicit alert and shortlist semantics as the catalog.

## 0.5.0 (private release candidate)

- Refresh cruises with at least one explicit product watch every 12 hours and
  cruises with no watches every 24 hours by default. Pins do not select the
  faster schedule.
- Add `watched_refresh_interval_hours` and
  `unwatched_refresh_interval_hours` Home Assistant App options for global YAML
  overrides from 1 through 168 hours. Both are optional so an existing saved
  configuration can upgrade without first adding the new keys.
- Remove the per-cruise refresh selector from onboarding and report each
  cruise's effective watched or unwatched schedule through the state API.
- Retain the legacy `refresh_interval_hours` option as an optional unwatched
  fallback for upgrade compatibility.
- Add a **Changes** view that defaults to watched products, can include all
  products, and shows changes since that browser last visited the cruise.
- Show the most recent actual price-change date, amount, and resulting price on
  watched product cards without treating the initial history baseline as a
  change.
- Add mutually exclusive **Record low** and **Below average** badges to watched
  products with at least two saved available price points, with accessible
  comparison details and explicit recorded-price semantics.
- Make compact history charts selectable so they open in a larger, accessible
  dialog with clearer price-axis labels and narrow-screen scrolling.

## 0.4.1 (private release candidate)

- Expand the compact, searchable **Show description** disclosure from shore
  excursions to every public catalog category.
- Keep descriptions collapsed by default while preserving the existing plain
  text normalization, bounded storage, and category-neutral interface.
- Apply a base product's description to its separately coded variant rows, such
  as larger bottle deliveries and multi-device internet packages.
- Verify the common description field against all 15 categories returned for a
  current Wonder of the Seas public catalog, with no empty values or GraphQL
  errors in the probe.

## 0.4.0 (private release candidate)

- Add searchable shore-excursion descriptions from Royal's public catalog
  behind a compact per-item **Show description** disclosure.
- Preserve the exact checksum-pinned upstream file while a local adapter
  requests and safely normalizes the additional public description field.
- Add a 10-minute per-cruise refresh cooldown and surface the remaining wait in
  the dashboard.
- Back off failed upstream refreshes exponentially from 15 minutes to a six-hour
  maximum while preserving safe restart recovery for unfinished initial loads.
- Add public-release documentation, MIT licensing, privacy-hardened Docker
  exclusions, fixture provenance, and GitHub Actions checks for the backend,
  frontend, Python syntax, and container build.
- Add original App artwork plus sanitized mobile-onboarding and populated
  desktop screenshots made from curated test fixtures.

## 0.3.5

- Explain that the initial public-price catalog can take a few minutes to
  build and continues in the background if the user switches cruises or leaves
  the dashboard.
- Arrange the dashboard's four top actions in a compact two-column grid,
  including at phone widths.
- Make the alert-behavior guidance dismissible, remember that preference in
  the browser, and provide a compact control to show the guidance again.

## 0.3.4

- Decode bounded HTTP chunked request bodies forwarded by Home Assistant
  Ingress instead of treating any request without `Content-Length` as empty.
- Retain the existing one-megabyte body limit for both content-length and
  chunked framing, and reject invalid or oversized framing with user-visible
  validation errors.
- Version the frontend assets and client/backend markers for unambiguous live
  verification of this request-framing fix.

## 0.3.3

- Resolve the authoritative ship and cruise line from the public discovery ship
  ID before validating client metadata, so a stale or missing UI cruise-line
  value cannot reject a valid selected ship and sailing.
- Version the served frontend asset URLs and identify the client version in
  cruise-creation requests to avoid ambiguous Home Assistant WebView caching.
- Include only bounded, non-sensitive sailing-selection fields in App logs when
  a cruise-creation request is rejected, and report the running backend version
  from the health endpoint.

## 0.3.2

- Submit the selected ship's authoritative discovery cruise line instead of
  relying on a separate onboarding field that can be lost during UI rerenders.
- Recover a missing cruise line from the submitted canonical ship name before
  validating the ship ID and sailing, while continuing to reject conflicting
  or unsupported cruise lines.

## 0.3.1

- Submit the selected discovery ship ID and resolve it back to the canonical
  ship before validation, preventing a valid confirmed sailing from failing
  with `Choose a valid ship.`
- Add a confirmed **Remove cruise** action. Removal is blocked while that cruise
  is refreshing and permanently deletes only its catalog, pins, watches, and
  sailing-keyed price history.
- Removing the active cruise selects another saved cruise; removing the final
  cruise returns the dashboard to onboarding.
- Cruises with a known duration are marked complete on their calculated return
  date. Automatic and manual refreshes stop, while a prominent cleanup card
  offers to remove the cruise without deleting it automatically.
- Migrated cruises can recover a missing duration from their cached sailing
  description, so completion handling also applies without rewriting App data.
- Use Home Assistant's supported `with-contenv bashio` service launcher while
  retaining the unprivileged App process, and enforce LF line endings for its
  Linux shebang.

## 0.3.0

- First-run dashboard onboarding discovers Royal Caribbean or Celebrity ships
  and actual future sailings from the pinned public upstream browser.
- Multiple cruises have separate catalogs, pins, watches, refresh
  settings, notification settings, and sailing-keyed history.
- **Pinned** replaces **Hidden** as a shortlist: pinned products remain in
  **All**, and a cruise opens on **Pinned** whenever its shortlist is non-empty.
- Legacy hidden selections are not converted to pins; existing watch settings
  remain separate and are retained during single-cruise migration.
- Existing single-cruise `/data/catalog.json` and `/data/preferences.json` are
  copied into the cruise registry on first start; the legacy files are retained.
- Cruise switching and **Add cruise** are available directly in the dashboard.
- Pin `curl-cffi` so the upstream browser can use its supported Chrome TLS
  impersonation path; plain `requests` currently receives HTTP 403 for ship
  discovery from this network.

## 0.2.0

- Compact SQLite price and availability history for every catalog item.
- Change-only recording avoids duplicate points on unchanged daily refreshes.
- Lazy, responsive per-item history charts with current, low, and high prices.
- History is isolated by sailing and expires 30 days after the sailing date.

## 0.1.0

- Initial searchable catalog dashboard.
- Persistent Hide, Restore, Watch, Unwatch, and target-price controls.
- Watched-item-only Home Assistant persistent notifications.
- Watch-list YAML export for the existing personalized checker.
