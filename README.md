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
| `id`     | Optional short name (letters, digits, `-`, `_`) that keeps this card's saved filters separate from other cards with identical settings on the same dashboard. |
| `area`   | Optional `{name, latitude, longitude, radius_miles, timezone}` for this card (see Choosing the area). |
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

1. `.\refresh-events.ps1` updates `data\events.json` and publishes it to `Z:\www\area-events-card\events.json` (see Hourly refresh below; `.\deploy.ps1` also copies `data\events.json` if present).
2. Use the YAML below ([examples/kpbs-feed-card.yaml](examples/kpbs-feed-card.yaml)). The feed now merges several sources, so the example title is generic:

```yaml
type: custom:area-events-card
title: San Diego Events
feed: /local/area-events-card/events.json
```

To switch from the demo card, replace the demo YAML (`demo: true` plus inline `events:`) with the YAML above. Demo mode is only active when a card has `demo: true` with its own inline `events`; the feed card never shows demo events.

Behavior: the feed is fetched on load and every 30 minutes with a unique query string and `cache: no-store`. Past events are hidden. If the feed cannot be loaded the card shows an error; if a later refresh fails it keeps showing the last data with a "stale" notice. The footer shows the build identifier and when the feed was generated. Events show image, date, venue/address, a description excerpt, category and a source link. Discovery events show **Verified** only when two or more independent publishers agree on title, start and venue (see Merging and verification), otherwise **Source corroborated** (one source's own signals agree; hover for the evidence) or **Unverified**.

The `feed` option accepts either `{ "events": [...] }` (discovery format) or a bare list. Events in the discovery format (`venue`, `address`, `categories`, `image_url`, `description`, `url`, `source`, `verification`) are also accepted in `events:` and `entity` attributes.

Checks: `npm test` (card behavior with a DOM stub) and `python -m unittest discover -s tests -t .`.

### Header and category filters

The sticky header shows the card title, **Updated <date time> (<age>)** taken from the feed's own `generated_at` (never the time the page loaded), the event count (`12 of 227 events` while filtered) and, when the feed lists them, the number of sources. A **Stale** badge appears when the feed is more than 3 hours old, **N sources failing** when sources report errors, and **Update failed** if the latest download failed (the last good data stays visible). No future refresh time is shown because the feed does not provide one. The header sticks while the card scrolls with the page; the card does not add a scroll area of its own.

**Filters** sit under the header. Open **Filters** to choose any number of categories (Music, Festivals, Food & Drink, Arts, Sports, Science, Technology, Family, Community, Outdoors, Education, History, Theater, Comedy, Markets, Other). The default is all events. An event belongs to every category whose keywords appear (as whole words) in its title, description or source-provided categories; **Other** holds events that match none. Filtering happens in the browser: no new requests, and the event tiles and images are not rebuilt.

Use **New category** to add your own with comma-separated keywords (with none, the category name itself is the keyword); custom categories can be edited and deleted. Choices are saved per card: in your Home Assistant user data (so they follow you across devices, as the NVR Card does) with the browser's local storage as a cache and fallback. The saved key combines the dashboard path with the card's `id`, or else a hash of its title, feed and entity. Give cards that are otherwise identical on one dashboard different `id` values, or they will share filters.

### Choosing the area (Home Assistant home / browser location)

Open **Filters** to see the **Area** panel. The card decides which area applies in this order:

1. an area **you chose in the card** ("Use Home Assistant home" or "Use my location"), saved per card (HA user data with a local cache, isolated by dashboard and card `id`, like the filters);
2. an `area` in the card configuration;
3. the **Home Assistant home location** (`hass.config`: latitude, longitude, time zone), read live. It is never hardcoded or copied into settings, and it is the home you configured in Home Assistant, not where you are now. A missing, zero or out-of-range home location counts as unavailable;
4. otherwise the panel says no location is available and offers the choices above.

**Browser location** is requested only when you click "Use my location", never on page load, and only if the browser supports it. Denial, timeout, an unusable position or an insecure (non-https, non-localhost) page produce a message and save nothing; no coordinates are substituted. A granted position is saved for that card only, with its reported accuracy and the browser's own time zone.

**What this does and does not do.** The area chosen in the card is for the card. **It does not change what the discovery service searches**, and the card says so. The service runs on a server and cannot read Home Assistant's or a browser's location, and it deliberately has no endpoint that accepts one (that would let anyone on the network redirect discovery). There is **no automatic, authenticated channel from a card or Home Assistant to the discovery service yet**; building one (for example an authenticated Home Assistant integration) is future work. Today the supported route is manual: the panel shows the discovery area the feed was produced for, whether your area is inside it, and the command to hand a location to the server:

```
python -m discovery.hint --source home_assistant --latitude 30.2672 --longitude -97.7431 --timezone America/Chicago   # Home Assistant home
python -m discovery.hint --source browser --latitude 51.5072 --longitude -0.1276 --accuracy-m 40                      # browser location
```

(In Docker: `docker compose exec discovery python -m discovery.hint --data-dir /data ...`.) The service resolves its own area by priority: an explicit `location` in `discovery.config.json`, then the Home Assistant hint, then the browser hint, otherwise it stops with "no location available" and keeps the last feed. Remove `location` from the configuration to let a hint take effect (hints use `default_radius_miles`, default 25). The hint lives in the data directory as `location-hint.json`, changing it starts a clean area (cached events are never mixed between areas), and the feed's `location.basis` says which rule applied (`configured`, `home_assistant` or `browser`).

## Event discovery (Python, standard library only)

`discovery/` collects public San Diego events from several free sources, merges duplicates, and writes `data/events.json` (the feed the card reads). It is separate from the card and from the future Home Assistant integration. No API keys, accounts, AI or browser automation.

Requires Python 3.9+ (tested on 3.14).

### Sources

Enabled sources live in the registry of [discovery.config.json](discovery.config.json) (list order = priority when merging fields; see Location, source registry and event locations below):

| Key | Source | Method | Notes |
|-----|--------|--------|-------|
| `kpbs` | KPBS events | listing HTML + calendar links + event pages | exact UTC times, venue, address, description, image |
| `sandiegoreader` | San Diego Reader | day listings + schema.org JSON-LD on event pages | `days` ahead; only path-style day pages are used (robots.txt disallows the search query pages) |
| `dosd` | DoSD | day listings + JSON-LD on event pages | `days` ahead; the site's generic ticketing blurb is not used as a description |
| `sandiego_gov` | City of San Diego special events | listing HTML + event pages | `pages` of 10; no images or machine-readable data; multi-day/unparseable dates are skipped |

Per source, the `max_details_per_run` option caps event-page requests each refresh; the rest are filled in by later refreshes (details are cached). Set `"enabled": false` to turn a source off, or change `retention_days` (how long cached events from a failing source are kept) and `verification.min_independent_publishers`.

All requests send a descriptive User-Agent, honor robots.txt, and wait 2 s between requests. A first refresh takes about 4 minutes; steady state is shorter.

### Running a refresh

```
python -m discovery                       # refresh data/events.json
python -m discovery --dry-run             # fetch and report without writing
python -m discovery --offline             # re-merge cached source data, no requests
python -m discovery --only kpbs dosd      # selected sources
python -m discovery --publish PATH        # also publish the validated feed to PATH
python -m unittest discover -s tests -t .
```

Exit code 0 = all sources refreshed, 2 = some failed (their previous events are kept), 1 = nothing refreshed or publishing failed (feed unchanged). `data/events.json` and `data/source-cache.json` are generated and not stored in Git.

### Location, source registry and event locations

Nothing about a place is built into the code. `discovery.config.json` holds the search area and the source registry; [examples/discovery.san-diego.json](examples/discovery.san-diego.json) (the shipped configuration) and [examples/discovery.austin.json](examples/discovery.austin.json) are complete examples, and any other area is just another file (`--config`, with its own `--data-dir`).

**Search area (`location`)**: exactly one of a name, a postal code or coordinates, plus optional details:

```json
"location": { "query": "Austin, TX", "country": "US", "region": "TX", "radius_miles": 25 }
"location": { "zip": "78701", "country": "US", "radius_miles": 25 }
"location": { "latitude": 32.7157, "longitude": -117.1611, "name": "San Diego, CA", "country": "US", "region": "CA", "timezone": "America/Los_Angeles", "radius_miles": 25 }
```

Coordinates need no geocoding. Names use OpenStreetMap Nominatim and ZIP codes use Zippopotam.us (free, no key, interchangeable providers in `discovery/location.py`; the identifying User-Agent, one request per second and on-disk caching required by Nominatim's policy are followed; (c) OpenStreetMap contributors, ODbL). Results are cached in `location-cache.json` for 180 days (and reused if a later lookup fails). A timezone is used only if configured or looked up from the coordinates (timeapi.io, cached); it is never inferred from a country or state, and without one event times keep the UTC offsets their sources give. A missing, invalid or unresolvable location is a clear error and the previous feed is kept (`location` may be left out when a Home Assistant or browser location hint exists; see Choosing the area).

**Source registry (`sources`)**: one entry per source.

```json
{ "id": "ut_austin", "name": "UT Austin Events", "adapter": "localist", "url": "https://calendar.utexas.edu",
  "enabled": true, "publisher": "utexas.edu",
  "coverage": { "type": "area", "name": "UT Austin campus", "latitude": 30.2849, "longitude": -97.7341, "radius_miles": 10 },
  "options": { "days": 14 } }
```

`adapter` picks the code (`kpbs`, `sandiegoreader`, `dosd`, `sandiego_gov`, `localist`; Localist can be registered many times). `id` is unique and keys the cache; `url` is the site read; `publisher` (default: the URL's domain) is what verification counts as independent. `coverage` says where the source has events: `area` (center and radius; the source runs only when that circle overlaps the search area), `global` (searchable anywhere; always runs and receives the search location) or `unknown`/omitted, which is **never selected**: unknown coverage is not assumed to cover everything. Disabled sources and invalid entries are reported; if no source applies, the run logs a coverage error and keeps the last valid feed. Cached events are discarded when the search area changes, so areas never mix.

**Event locations (`place`).** Every event carries a `place`, separate from event verification:

- `confirmed`: the source supplied event-specific coordinates (precision `venue`, confidence `high`). Inside the radius: kept; outside: dropped.
- `estimated`: no coordinates, but the event's own address names a city. The city is geocoded (cached, at most 10 new lookups per refresh) for an approximate position (precision `city`; confidence `medium` if the address also states a region or postal code, otherwise `low`, and the search area's region is then used as geocoding context). Estimates are always kept, labeled with their evidence, and flagged when the estimated position falls outside the radius.
- `unknown`: nothing defensible; venue, neighborhood and address text are preserved and no coordinates are ever invented. The search area's center is never used as an event position.

Duplicates keep the best-supported place with the other sources' evidence; sources that put an event more than 50 miles apart, or in different cities, are flagged `conflict` with confidence `low`. A verified event may have an estimated or unknown location. Limitations: the San Diego adapters still interpret their sites' local times as US Pacific time; estimates are city-level only (street addresses are not geocoded); and a city name can be ambiguous, which is why estimates are labeled.

### Localist calendars (Austin example)

`localist` is a reusable adapter for any [Localist](https://localist.com) calendar (universities, cities, libraries): every Localist site has a free, key-less JSON API (`/api/2/events`) with exact times, venue, address, image and usually the venue's coordinates. Register each site as its own source. Run Austin in its own folder so it never touches the San Diego feed or caches:

```
python -m discovery --config examples/discovery.austin.json --data-dir data/austin
```

Limitations: the only Austin source found is the UT Austin campus calendar (Do512 returns 403 to automated requests, which is respected), so the feed is mostly university events; the adapter reads at most 5 pages (500 events) per site.

### Merging and verification

Candidates are chosen cheaply: records are sorted by start time and only records from different sources starting within 30 minutes of each other are compared. A pair is the same event only when titles agree (ignoring punctuation, curly quotes, years, ordinals and words like "annual"; one title may be the other plus a lineup or tour name) **and** there is evidence beyond the title:

- the venue or street address agrees (venue names may differ in wording); or
- the descriptions largely repeat each other (60% of the shorter one's content words), which lets a multi-location event match even though listings give different venues; or
- the end times are identical and one listing names the other's venue/area (for example "Ocean Beach" in the title).

Start times must be equal, or within 30 minutes for door time vs show time when the venue agrees and neither source lists separate showtimes of that title (or when the end time is identical plus venue or description evidence). Generic titles ("Live Music", "Farmers Market") need the venue to agree. Same-source records never merge, a group never contains two records of one source, and every matched pair is recorded in `verification.match_reasons`. Merged events keep the longest description, the first available image (all image URLs in `images`), every source with its URL, retrieval time, evidence and its own start time, description or image when they differ, and all distinct `locations` when venues differ.

`verified` is `true` only when at least `min_independent_publishers` (default 2) *different publishers* agree on title, start and venue and nothing conflicts; several pages from one publisher count once. Otherwise `verification.status` is `corroborated` (the one source's own signals agree) or `unverified`. Limitation: two publishers can still repeat the same upstream listing (for example a venue's own calendar), so "Verified" means independent publishers agree, not that the organizer was contacted.

### Adding a source

Add a module under `discovery/sources/` providing `discover(fetcher, previous, details, limit, source_id, source_name, url, **options) -> (events, notes)`, register its adapter type in `ADAPTERS` in `discovery/sources/__init__.py`, and add a registry entry (id, name, adapter, url, coverage, options) to the configuration. Coverage and names are data, not code.

### Docker service (always-on host)

The same engine also runs as a standalone Docker Compose project (`area-events`) that refreshes about hourly and serves `GET /events.json` and `GET /health` read-only over HTTP. See [docs/docker-deployment.md](docs/docker-deployment.md) for the design, the safe SSH deployment procedure and rollback. Run `python -m discovery.service` for the same thing without Docker (`AREA_EVENTS_DATA_DIR` selects where data is kept).

### Home Assistant feed sync

A Home Assistant `shell_command` plus an automation can copy the Docker service's feed into `/config/www/area-events-card/events.json` every 30 minutes, validated and atomically, with no Windows machine involved. See [docs/home-assistant-sync.md](docs/home-assistant-sync.md) and the examples in [homeassistant/](homeassistant/).

### Hourly refresh on Windows (Task Scheduler)

`.\refresh-events.ps1` runs the refresh and publishes the feed straight to `Z:\www\area-events-card\events.json` (atomic replace, JSON validated first, previous feed untouched on any failure). It does not deploy the card; `.\deploy.ps1` remains the software deployment command and must have been run once so the `www\area-events-card` folder exists. A lock file prevents overlapping runs; output is appended to `data\refresh.log`.

```
.\refresh-events.ps1                 # refresh and publish
.\refresh-events.ps1 -DryRun         # fetch and report only
.\refresh-events.ps1 -NoPublish      # update data\events.json only
```

Nothing installs a scheduled task for you. To create one yourself (PowerShell, as your normal user; adjust the path):

```
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\Users\laird\Documents\Projects\area-events-card\refresh-events.ps1"' `
  -WorkingDirectory "C:\Users\laird\Documents\Projects\area-events-card"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 25)
Register-ScheduledTask -TaskName "Area Events Refresh" -Action $action -Trigger $trigger -Settings $settings
```

This registers the task to run as you, only while you are logged on, with no stored password. Remove it with `Unregister-ScheduledTask -TaskName "Area Events Refresh"`.

**Execution identity and the share.** The Home Assistant share needs credentials. A task that runs *only when you are logged on* reuses your session, so `Z:` normally works. A task set to "run whether user is logged on or not" has no mapped drives and no access to your session's saved share logon; it would need the share's credentials stored for that account (`cmdkey /add:<host> ...`, run as that account), which this project never does for you. The script therefore tries `Z:` first and then the same share by UNC path (`$HaConfigUnc` at the top of `refresh-events.ps1`, currently `\\192.168.1.48\config`).

**Troubleshooting.**
- Read `data\refresh.log`; each run logs the user, destination, per-source counts, errors and duration.
- "no Home Assistant destination reachable": run `Test-Path Z:\www\area-events-card` and `Test-Path \\192.168.1.48\config\www\area-events-card` as the task's user; if only the second works, the scheduled session lacks the mapped drive (the script will use the UNC path); if neither works, the share credentials are not available to that account.
- "another refresh is running": a previous run is still active, or crashed; a lock older than 30 minutes is ignored automatically.
- Exit code 2 means some sources failed and their previous events were kept; each source's `ok`, `error` and `last_success_at` are in the feed's `sources` block.
- "Python was not found": the task's user needs Python 3 on `PATH`.
- The card shows the old feed until the page reloads or its 30-minute refresh fires.

## Planned: Python integration (not implemented)

A separate Home Assistant custom integration (own repository/component, Python) is planned to supply the events. Intended shape:

- Config flow for choosing an area/location.
- A coordinator that fetches and verifies events, and exposes them as an entity with an `events` attribute in the format above.
- The card stays unchanged: it reads that entity via the `entity` option.

Nothing of this exists yet.

## License

MIT
