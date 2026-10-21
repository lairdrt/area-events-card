# Architecture

## Component boundaries

| Component | Folder | Owns | Does not |
|---|---|---|---|
| Card | `src/` (built to `custom_components/area_events/frontend/`) | User-facing configuration and its visual editor; presentation (categories, interests, exclusions, date range, maximum, sorting, layout, images, filters, theme); the viewer's saved filter choices | Contact any server other than Home Assistant; hold a secret; read files |
| Integration | `custom_components/area_events/` | The config entry (app address + token); validating and normalizing card requests; coordinating identical requests; a cache of each profile's latest answer; the card-facing WebSocket command; serving the card's JavaScript | Discover events; know about adapters |
| App (engine) | `area_events/` | All discovery: adapters, normalization, de-duplication, verification, images and evidence, the persistent cache, scheduling | Know about dashboards or cards |

Data flows one way: card → `area_events/events` WebSocket command → integration hub → app HTTP API → engine. Nothing else connects them.

## Profiles

A card's settings split into two kinds. Settings that change *which events exist* form a **discovery profile**: `area` (or the Home Assistant home coordinates), `radius_miles`, `sources`. Everything else (categories, interests, exclusions, date range, maximum, sorting, layout) is presentation and is applied in the card, so it never causes a new discovery.

The integration builds the profile (adding the Home Assistant country only for ZIP codes, and the home location, name and time zone when the card has no `area`). The engine normalizes it (area text collapsed, coordinates rounded to four decimals, sources de-duplicated and sorted) and derives its id as a hash of the canonical form. Identical requests are therefore one profile, with one folder `/data/profiles/<id>/` holding its feed, per-source cache and geocoding cache. Different profiles share nothing and cannot overwrite each other.

## Card ↔ integration: `area_events/events`

Request (Home Assistant WebSocket, authenticated by the user's session):

```json
{"type": "area_events/events", "area": "Austin, TX", "radius_miles": 25, "sources": ["https://calendar.utexas.edu"], "since": "<generated_at of the events the card has>"}
```

Result:

```json
{"state": "pending | discovering | ready | error | unavailable", "message": null, "refreshing": false, "connected": true,
 "generated_at": "...", "location": {"display_name": "Austin, TX", "...": "..."}, "sources": {"ut_austin": {"ok": true, "events": 323}},
 "report": [{"url": "...", "status": "supported | unsupported | blocked | unavailable | no_events", "adapter": "...", "reason": "..."}],
 "events": [], "unchanged": true}
```

`events` is omitted and `unchanged` is true when `since` equals `generated_at`. The command never waits for discovery: it answers from the hub's cache, and the card polls (10 s while `pending`/`discovering`/`unavailable`/`refreshing`, 5 min otherwise, 30 s after a transient error). Errors use `invalid_profile`, `rejected`, `not_configured` and `unauthorized` codes with a message for the user.

## Integration ↔ app: HTTP API

All requests under `/api/v1` carry `Authorization: Bearer <token>`; five wrong tokens from one address in a minute are throttled. No CORS headers: the API is for the integration only.

**Authentication without typing.** On first start the app generates a random token and keeps it in `/data/token` (mode 600). When it runs under the Supervisor it announces itself through Supervisor discovery: it reads its own hostname (`/addons/self/info`) and posts `{service: area_events, config: {host, port, token}}` to `/discovery`, which the app is allowed to do because `config.yaml` lists `discovery: [area_events]` (no `hassio_api` is needed). Home Assistant Core receives it as a `hassio` config flow; the integration's `async_step_hassio` asks the user only to confirm, and later announcements (a restart, a new hostname) update the existing entry silently. The token never appears in a log, option or dashboard. An optional `token` app option (32+ characters) and the manual config-flow step remain for an app running elsewhere.

| Request | Meaning |
|---|---|
| `GET /health` | Liveness for the Supervisor watchdog; no authentication, reveals nothing. |
| `GET /api/v1/status` | Engine counters; also the integration's connection and token check. |
| `POST /api/v1/profiles` | Register a profile (idempotent) → its snapshot without events, including `id`. 422 for an invalid profile, 429 when too many different areas are in use (12 at most; profiles unused for ten minutes are evicted first). |
| `GET /api/v1/profiles/<id>[?since=...]` | The snapshot with events; 404 for an unknown profile. |

`state` is `ready` once a feed exists (with `error` set if the latest refresh failed and `refreshing` while one runs), `discovering` while the first run executes, `pending` while queued, `error` when the first run failed.

## Coordination and failure behaviour

- **Hub (integration).** One record per distinct profile. Concurrent identical requests share a record and one registration; repeated requests are answered from the record's cached snapshot. Registration is repeated every 15 minutes and whenever the app answers 404 (restart, clean-up). A profile nobody has asked for in 30 minutes stops being refreshed.
- **Coordinator (integration).** One `DataUpdateCoordinator` per config entry, not per card. Each update checks the app is reachable and refreshes the hub's active profiles one after another (15 s interval while any profile is busy, 5 minutes otherwise). A rejected token raises `ConfigEntryAuthFailed` (reauthentication); an unreachable app raises `UpdateFailed`, while cards keep showing the cached events with a notice.
- **Admission and clean-up.** A profile is identified only by its canonical form, so reloads, second dashboards, spelling variants and the editor preview of the same card all share one profile. At most 12 profiles exist; to admit a new one the engine evicts, in this order, profiles that never produced events (a half-typed area in the editor, a removed card), then ones with events that nobody has asked for in ten minutes. The profile being discovered is never evicted, profiles with events that are in use are never evicted, and when none can go the request is refused with HTTP 429, which the integration reports to the card as `capacity` (the card says so and retries in 30 seconds; it is never shown as "no events"). Queued searches run most-recently-requested first, searches nobody repeated for ten minutes are skipped, profiles that never produced events are deleted after an hour, and after a restart only recently requested ones are searched. The hub drops a profile that no card has asked for in 12 minutes (2 minutes if it has no events yet), and the card waits 1.5 s in the editor preview before searching so half-typed areas never become searches.
- **Engine (app).** One worker thread runs queued profiles sequentially; a scheduler tick queues the profiles that are older than the refresh interval (only for profiles requested in the last 7 days) and deletes those unused for 14 days. A failed run (no coverage, geocoding failure, all sources failing, a crashing adapter) leaves that profile's previous feed and cache untouched and records the reason; an error is retried after ten minutes. A restart reloads profiles from disk and re-queues those without a feed.

## Source websites

`sources` lists **additional** website addresses; they never replace the automatic sources (every registry source whose coverage overlaps the area). Resolution happens when the profile runs:

1. an address whose host matches a registered source (`area_events/discovery/sources.json`) uses that source's adapter, regardless of its declared coverage;
2. any other address is probed once as a Localist calendar (`/api/2/events`); hosts that do not resolve to public addresses are refused without any request;
3. otherwise the generic **web** adapter (`sources/web.py`) crawls it: `webparse.py` (HTML tree, JSON-LD, microdata, links, listing cards), `webfeeds.py` (ICS with simple recurrence, RSS/Atom) and `webtime.py` (ISO/RFC 822 dates; US zones without tzdata). The crawl is staged: structured data first (JSON-LD, microdata), then linked same-site feeds, then, only if those found nothing, `<time datetime>` listing blocks; a bounded number of same-site event pages complete or extend the result. The outcome of this probe is the status in the report;
4. an address nothing can read is reported with a status and reason: `unsupported` (no event data, a non-page content type, or a page built by JavaScript), `no_events`, `blocked` (robots.txt, HTTP 401/403/429, non-public address) or `unavailable` (network, 404, 5xx, too large).

Every request for a typed-in address goes through `SafeFetcher` (`safe.py`): public addresses only, connection pinned to the validated address, manual and re-validated redirects, RFC 9309 robots.txt, shared per-host spacing, size/time limits. The registry entries the engine creates for such addresses carry `options.safe`, which makes `registry.Source.discover` use that fetcher, including for user-entered Localist sites. Crawl results are cached for 15 minutes (`web.crawl`), so profiles sharing a website do not refetch it; the probe and the later discovery of the same run share one crawl.

The report entries are `{"url", "status": "supported | unsupported | blocked | unavailable | no_events", "adapter", "name", "id", "format", "found", "reason"}`; `id` names the source in the feed's `sources` block so the card can show how many events a website produced.

Each run reads the automatic sources first (plus websites that worked on an earlier run, so their events never drop out of the feed), publishing progressively; only then are addresses not yet known to work probed, and the ones that turn out readable are read in a second pass (`only=` those sources). Addresses that cannot be read appear in the report as warnings and add no run. A registered source named in `sources` is forced on (coverage `global`) in place of its automatic entry, so it is never scanned twice. A profile that differs from an existing one only in `sources` (an editor change) is **seeded** with that sibling's feed, report and source cache when it is registered: the card keeps showing the events already found, the feed is rebuilt offline for the new set of websites (removed ones disappear without fetching anything) and only genuinely new websites are read. A source with unknown coverage is never assumed to apply. Pacific-time handling exists only inside the San Diego adapters, which read local wall-clock times; Localist and web sources report UTC offsets, and a web event's wall-clock time without an offset takes the page's time zone (ICS `TZID`) or else the search area's, and is skipped when neither is known.

To support a new place with its own reader: add a Localist entry to `sources.json`, or write an adapter module (see `area_events/discovery/sources/__init__.py`) and register it with its coverage.

## The app

`area_events/` follows the Home Assistant app layout: `config.yaml` (aarch64 and amd64, `init: false`, watchdog on `/health`), a `Dockerfile` based on the Home Assistant base image with Python from Alpine and nothing from pip, `run.sh`, `DOCS.md`, `translations/en.yaml`. Options (`refresh_minutes`, `log_level`, optional `token`) arrive in `/data/options.json`; persistent data lives in `/data`; logs go to stdout and appear in the app's Log tab. The base image is `ghcr.io/home-assistant/base-python:3.14-alpine3.24` (multi-architecture, includes s6 and bashio), so building needs no package downloads. The repository root has `repository.yaml` so the repository can be added in the App store.

## Security

The token exists only in the app's data folder and the integration's config entry. The card never sees it, and the app's port is not published. The engine fetches only registered sources and public Localist calendars, honours `robots.txt`, spaces requests and accepts no paths or adapters from requests. Geocoding uses free key-less providers (Nominatim, Zippopotam.us, timeapi.io) with a disk cache.

## Home Assistant interfaces used, and how they were checked

Checked against the current developer documentation and the Home Assistant core, frontend and Supervisor sources (October 2026), not against a running instance:

| Interface | Use here | Finding |
|---|---|---|
| Manifest | `domain`, `name`, `version`, `documentation`, `issue_tracker`, `codeowners`, `config_flow`, `integration_type: service`, `iot_class: local_polling`, `dependencies: frontend, http, websocket_api` | Matches the custom-integration requirements. |
| Config flow | `async_step_user`, `async_step_hassio` + confirm, `async_step_reauth`; one unique id; `_abort_if_unique_id_configured(updates=...)`; `async_update_reload_and_abort(data_updates=...)`; `_get_reauth_entry` | Signatures confirmed in core. Discovery ends in a confirmation step as the docs require. |
| Supervisor discovery | `discovery:` in `config.yaml`; `POST /discovery`; `async_step_hassio(HassioServiceInfo)` | Any service name is accepted and becomes the flow's domain; the default app role may call `/discovery` and `/addons/self/info`. |
| Config entry lifecycle | `runtime_data`, `async_setup_entry`/`async_unload_entry`, `ConfigEntryNotReady`/`ConfigEntryAuthFailed` | As documented. Nothing is forwarded to platforms, so unloading has nothing to undo. |
| DataUpdateCoordinator | `config_entry=`, `async_config_entry_first_refresh`, `UpdateFailed`, a changing `update_interval` | **Corrected:** a coordinator without listeners never polls, so the entry attaches one listener; and changing the interval does not reschedule, so a new busy profile requests a refresh (`async_boost`). |
| WebSocket API | `websocket_command` above `async_response`, registered in `async_setup`, `send_result`/`send_error` | As documented. Not admin-only on purpose: any signed-in user may view events. Request/response rather than a subscription, since polling is cheap and cached. |
| Frontend loading | `async_register_static_paths(StaticPathConfig)`, `add_extra_js_url` | Loads the card as a module on every page; no Lovelace resource. Static paths are unauthenticated, which is fine for a file with no secrets. |
| Card editor | `getConfigForm()` schema rendered by Home Assistant's own form | **Corrected:** the custom editor element was replaced. `ha-form` expects its parent to hold the data and a custom element must load `ha-form` itself; `getConfigForm` has neither problem, and chip pickers (`select` with `multiple` and `custom_value`) give arrays directly. |
| App | `config.yaml` keys, `base-python` image, `run.sh` with `with-contenv bashio`, `/data`, `watchdog`, `ports: null` | Matches the tutorial and configuration reference. |

## Limitations and runtime checks

Verified here: engine logic, HTTP API and Supervisor announcement (unit tests, plus a local run that discovered 323 events for Austin through the real API in 26 seconds and reported an uncovered area, an unreadable website and a Localist calendar correctly), the integration's request, client and hub logic (unit tests with a fake app), the glue (config flow, setup, coordinator, WebSocket command) run against stand-ins that model the interfaces above, and the card (DOM-stub tests).

Not verified, because they need a running Home Assistant: that the real framework accepts the glue exactly as the stand-ins do, discovery arriving from a real Supervisor, `add_extra_js_url` loading the card in the browser, the card picker and form editor, and the app image build and start on a Raspberry Pi 4. The `#branch` suffix on the repository URL is assumed to work as for other repositories. The first runtime checks, in order: the integration loads after a restart; the app starts and logs `announced`; the discovered entry appears; a card shows events; the editor opens.

Known limits: registered sources currently cover San Diego, CA and the University of Texas at Austin, so most other areas report that no source applies; the first search for a new area takes a few minutes; very large numbers of different areas are refused (12 at most).
