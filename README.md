# Area Events Card

A vanilla JavaScript custom Lovelace card for Home Assistant that shows local area events as a responsive, theme-aware grid. Each event shows an image (placeholder if none), category, date, location and a verification indicator. The footer shows the build identifier.

**Status:** initial foundation (v0.1.0). The card renders events supplied to it; there is no event discovery yet.

## Card configuration

Registers as `custom:area-events-card`.

| Option   | Description |
|----------|-------------|
| `title`  | Optional header text. |
| `entity` | Entity whose `events` attribute is a list of events. |
| `events` | Inline list of events (takes precedence over `entity`). |
| `feed`   | URL of a JSON feed (`/local/area-events-card/events.json`). |
| `demo`   | If `true`, shows a "demonstration data" banner. |

Event fields: `title`, `start` (ISO 8601), `location`, `category`, `image` (URL, optional), `verified` (boolean).

The production card contains no sample data. A fictional demo configuration is in [examples/demo-card.yaml](examples/demo-card.yaml).

## Build

Requires Node.js 18+; no dependencies.

```
npm run build
```

Copies `src/area-events-card.js` to `dist/area-events-card.js` (generated, not stored in Git). The source keeps a `__AREA_EVENTS_CARD_BUILD__` placeholder, which the deploy script replaces in the deployed copy only. The identifier looks like `AREA-EVENTS <git short hash>-<file hash>` and is shown in the card footer and logged to the browser console.

## Local installation in Home Assistant

This uses the same mechanism as the sibling `temperature-card` project: a small `loader.js` is the only registered resource, and it imports the card with a fresh `?ts=` timestamp on every page load, so a redeployed card is never served from the browser cache.

One-time setup:

1. Map the Home Assistant `config` share (Samba) as drive `Z:`, or edit `$HaConfigShare` at the top of [deploy.ps1](deploy.ps1).
2. Run `.\deploy.ps1` (below). It creates `www\area-events-card` on the share if `www` exists.
3. Add the dashboard resource: **Settings > Dashboards > ⋮ > Resources > Add resource** (turn on Advanced Mode in your user profile if missing).
   - URL: `/local/area-events-card/loader.js`
   - Type: **JavaScript module**
   - Register only `loader.js`, never `area-events-card.js`.
4. Edit a dashboard > **Add card > Manual**, paste [examples/demo-card.yaml](examples/demo-card.yaml).

Deploy (build, stamp, copy, verify):

```
.\deploy.ps1
```

The script builds the card, replaces the placeholder, writes `area-events-card.js` and `loader.js` to `Z:\www\area-events-card\`, and verifies file sizes and that the build identifier is present in the deployed file. After deploying, reload the dashboard page and confirm the footer shows the identifier the script printed. The resource itself never needs to change.

Testing: confirm the demo banner, three event tiles, placeholder images, verified/unverified indicators and the build identifier in the footer. Check a light and a dark theme and a narrow (phone) width.

### Showing real events

The card can read the feed produced by the discovery tool, so no event data is embedded in the card:

1. `python -m discovery` (updates `data/events.json`).
2. `.\deploy.ps1` (also copies `data\events.json` to `Z:\www\area-events-card\events.json`).
3. Use [examples/kpbs-feed-card.yaml](examples/kpbs-feed-card.yaml):

```yaml
type: custom:area-events-card
title: San Diego Events
feed: /local/area-events-card/events.json
```

To switch from the demo card, replace the demo YAML (`demo: true` plus inline `events:`) with the YAML above. Demo mode is only active when a card has `demo: true` with its own inline `events`; the feed card never shows demo events.

Behavior: the feed is fetched on load and every 30 minutes with a unique query string and `cache: no-store`. Past events are hidden. If the feed cannot be loaded the card shows an error; if a later refresh fails it keeps showing the last data with a "stale" notice. The footer shows the build identifier and when the feed was generated. Events show image, date, venue/address, a description excerpt, category and a source link. Discovery events show **Source corroborated** (the source's own signals agree; hover for the evidence) or **Unverified**. **Verified** appears only for events whose `verified` is `true`, which nothing in the discovery tool sets.

The `feed` option accepts either `{ "events": [...] }` (discovery format) or a bare list. Events in the discovery format (`venue`, `address`, `categories`, `image_url`, `description`, `url`, `source`, `verification`) are also accepted in `events:` and `entity` attributes.

Checks: `npm test` (card behavior with a DOM stub) and `python -m unittest discover -s tests -t .`.

## Event discovery proof of concept (Python, standard library only)

`discovery/` fetches public San Diego events from one source (KPBS, https://www.kpbs.org/events/all) and writes `data/events.json`. It is separate from the card and from the future Home Assistant integration. No API keys, accounts, AI or browser automation.

Requires Python 3.9+ (tested on 3.14).

```
python -m discovery               # refresh data/events.json (first run ~75 s)
python -m discovery --dry-run     # fetch and report without writing
python -m discovery --no-details  # listing only, skip per-event page requests
python -m discovery --limit 3     # first 3 events, for testing
python -m unittest discover -s tests -t .
```

Exit code 0 = refreshed, 2 = partially refreshed, 1 = nothing refreshed. If the source is unreachable, blocked by robots.txt or yields no events, the existing `data/events.json` is left untouched. Writes are atomic.

**How it works.** KPBS publishes no JSON-LD, feed or documented API, so the adapter reads its server-rendered HTML (`discovery/sources/kpbs.py`). Exact start/end times come from the "add to calendar" links (UTC) and are converted to `America/Los_Angeles` with built-in US daylight-saving rules (no `tzdata` needed). Each event page adds venue, address, description and a large image. Recurring "ongoing" exhibits are skipped, and past events are dropped. Requests send a descriptive User-Agent, honor robots.txt, and wait 2 s between requests; event pages already stored are not re-fetched.

**Verification policy.** `verified` is always `false` in this proof of concept. Each event instead carries `verification.evidence` (for example: URL date matches the calendar start; listing text matches the UTC start in Pacific time; the event page's own ICS start agrees; `og:url` equals the listing URL), `verification.missing` (fields not found, never invented), and a `status` of `corroborated` (the source's own signals agree) or `unverified`. Real verification needs an independent second source.

**Adding a source.** Add a module under `discovery/sources/` that provides `NAME` and `discover(fetcher, previous, details, limit) -> (events, notes)`, then list it in `SOURCES` in `discovery/__main__.py`.

## Planned: Python integration (not implemented)

A separate Home Assistant custom integration (own repository/component, Python) is planned to supply the events. Intended shape:

- Config flow for choosing an area/location.
- A coordinator that fetches and verifies events, and exposes them as an entity with an `events` attribute in the format above.
- The card stays unchanged: it reads that entity via the `entity` option.

Nothing of this exists yet.

## License

MIT
