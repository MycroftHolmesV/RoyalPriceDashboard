# Royal Price Dashboard

The dashboard appears as **Royal Prices** in the Home Assistant sidebar.

## Cruises

- A clean installation opens with **Add cruise**. Choose Royal Caribbean or
  Celebrity, search the discovered ship list, select a real future sailing, and
  confirm currency and refresh behavior.
- Use the **Viewing cruise** menu to switch cruises. Each cruise has its own
  catalog, pins, watches, and settings.
- **Add cruise** starts the same guided flow for another sailing and creates its
  initial catalog and history baseline in the background.
- The first public-price catalog can take a few minutes. **Building catalog…**
  means the server is still working; switching cruises, closing the page, or
  force-closing the mobile app does not cancel that background job. Reopen the
  dashboard later to see its result.
- **Remove cruise** asks for confirmation, then permanently deletes that
  sailing's catalog, pins, watches, and saved price history. A cruise cannot be
  removed while its price refresh is running. Removing the final cruise returns
  to first-run setup.
- For cruises with a known duration, the return date is the sailing date plus
  the number of nights. On that date the cruise is labeled **Completed**,
  automatic and manual price refreshes stop, and the dashboard offers
  **Remove completed cruise**. Keeping it preserves the cruise until you choose
  to remove it.
- Browsing and switching cruises are silent. Enabling notifications for a
  cruise still does nothing until an individual product is explicitly watched.

## Controls

- **Watch** stores the item's current public price as its initial target.
  Notifications occur only if a later refresh is below that target.
- Edit the target beside a watched item to choose a specific buy price.
- **Pin** adds an item to that cruise's shortlist without removing it from
  **All**. Use the **Pinned** tab to focus on the shortlist; **Unpin** removes it.
- If a cruise has pinned items, its dashboard opens on **Pinned**. Otherwise it
  opens on **All**. Choosing a tab manually is respected while that cruise stays
  open.
- **History** opens an on-demand chart for that item. The App records an initial
  baseline and then only price or availability changes for every catalog item,
  including pinned and unwatched items.
- **Export watches** copies a `watchList` YAML block suitable for the separate
  Royal Caribbean Price Check App once the reservation appears in the account.
- **Refresh prices** starts a background catalog refresh. Automatic refreshes
  run at the configured interval until the cruise's return date.
- After any refresh starts, that cruise waits at least 10 minutes before another
  manual or scheduled attempt. A failed upstream request uses exponential retry
  delays beginning at 15 minutes and capped at six hours, so an outage cannot
  create a request storm. The refresh button shows the remaining wait.

Prices are public Cruise Planner prices and may differ from logged-in,
passenger-specific offers. The source browser may not return prices for every
multi-device or larger-size variant.

History is separated by ship, ISO sailing date, and currency. It is retained
until 30 days after the sailing date, then removed automatically. Existing
single-cruise data is copied into the multi-cruise layout on upgrade without
deleting the legacy catalog or preferences files. Existing watches are retained,
but legacy hidden selections do not become pins.

## Restarts, backups, and recovery

- Catalog writes and preferences use atomic file replacement. If Home Assistant
  or the mobile app closes during the first catalog build, reopen the dashboard.
  A running App continues the work; an App/container restart safely retries an
  unfinished initial build because no completed catalog was recorded.
- Refresh cooldowns and failure counters are intentionally process-local. A
  container restart clears them, allowing recovery from a stuck process. Normal
  successful-refresh scheduling still comes from the catalog's saved timestamp.
- Home Assistant App backups include the private `/data` volume. Create a backup
  before an upgrade or removal, and verify that the App is included before
  relying on it for recovery.
- Removing a cruise is intentionally destructive for that sailing. It deletes
  the cruise's catalog, preferences, and sailing-keyed history after
  confirmation; it does not affect another cruise or the separate price-checker
  App.

## Troubleshooting

- If **Building catalog…** remains for more than several minutes, leave the page
  open or return later, then inspect the App log. Public upstream endpoints can
  be slow or temporarily reject requests.
- If a refresh fails, keep the App running and let the displayed retry delay
  expire. Repeated tapping is intentionally blocked.
- If the dashboard and backend versions appear mixed after an update, force
  close and reopen the Home Assistant mobile app, then check `/health` through
  the App's Ingress session. Frontend assets include the release version to
  reduce WebView caching ambiguity.
- Do not post Home Assistant tokens, App logs containing private context, or the
  App's `/data` directory in a public issue. Follow [SECURITY.md](SECURITY.md)
  for private reporting guidance.
