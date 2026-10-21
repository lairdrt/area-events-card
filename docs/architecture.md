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
 "report": [{"url": "...", "status": "supported | unsupported", "adapter": "...", "reason": "..."}],
 "events": [], "unchanged": true}
```

`events` is omitted and `unchanged` is true when `since` equals `generated_at`. The command never waits for discovery: it answers from the hub's cache, and the card polls (10 s while `pending`/`discovering`/`unavailable`/`refreshing`, 5 min otherwise, 30 s after a transient error). Errors use `invalid_profile`, `rejected`, `not_configured` and `unauthorized` codes with a message for the user.

## Integration ↔ app: HTTP API

All requests under `/api/v1` carry `Authorization: Bearer <token>`; five wrong tokens from one address in a minute are throttled. No CORS headers: the API is for the integration only.

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
- **Engine (app).** One worker thread runs queued profiles sequentially; a scheduler tick queues the profiles that are older than the refresh interval (only for profiles requested in the last 7 days) and deletes those unused for 14 days. A failed run (no coverage, geocoding failure, all sources failing, a crashing adapter) leaves that profile's previous feed and cache untouched and records the reason; an error is retried after ten minutes. A restart reloads profiles from disk and re-queues those without a feed.

## Source websites

`sources` lists website addresses. Resolution happens when the profile runs:

1. an address whose host matches a registered source (`area_events/discovery/sources.json`) uses that source's adapter, regardless of its declared coverage;
2. any other address is probed once as a Localist calendar (`/api/2/events`); hosts that do not resolve to public addresses are refused without any request;
3. otherwise it is reported as unsupported with the reason.

If none of the named addresses is supported the profile fails with that explanation instead of silently using other sources. Without `sources`, registered sources are chosen by the coverage each declares; a source with unknown coverage is never assumed to apply. Pacific-time handling exists only inside the San Diego adapters, which read local wall-clock times; Localist reports UTC offsets.

To support a new place: add a Localist entry to `sources.json`, or write an adapter module (see `area_events/discovery/sources/__init__.py`) and register it with its coverage.

## The app

`area_events/` follows the Home Assistant app layout: `config.yaml` (aarch64 and amd64, `init: false`, watchdog on `/health`), a `Dockerfile` based on the Home Assistant base image with Python from Alpine and nothing from pip, `run.sh`, `DOCS.md`, `translations/en.yaml`. Options (`token`, `refresh_minutes`, `log_level`) arrive in `/data/options.json`; persistent data lives in `/data`; logs go to stdout and appear in the app's Log tab. The app refuses to start without a token of at least 32 characters. The repository root has `repository.yaml` so the repository can be added in the App store.

## Security

The token exists only in the app's options and the integration's config entry. The card never sees it. The engine fetches only registered sources and public Localist calendars, honours `robots.txt`, spaces requests and accepts no paths or adapters from requests. Geocoding uses free key-less providers (Nominatim, Zippopotam.us, timeapi.io) with a disk cache.

## Limitations and runtime checks

Verified here: engine logic and HTTP API (unit tests, plus a local run that discovered 323 events for Austin through the real API in 26 seconds, reported an uncovered area, an unreadable website and a Localist calendar correctly), the integration's request, client and hub logic (unit tests with a fake app), and the card (DOM-stub tests).

Not verified, because they need a running Home Assistant: the config flow, the coordinator, the WebSocket registration, static-path and `add_extra_js_url` loading of the card, the ha-form editor, and the app image build and start under the Supervisor on a Raspberry Pi 4. Home Assistant API usage was written against the current developer documentation (config entries with `runtime_data`, `DataUpdateCoordinator`, `async_register_static_paths`) but not executed. The first runtime checks, in order: the app starts and logs `listening`; the integration connects; a card shows events; the editor opens.

Known limits: registered sources currently cover San Diego, CA and the University of Texas at Austin, so most other areas report that no source applies; the first search for a new area takes a few minutes; discovery state lives per profile, so very large numbers of different areas are refused (12 at most).
