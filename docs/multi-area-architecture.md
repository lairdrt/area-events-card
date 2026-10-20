# Multi-area architecture: choosing an area in the card and getting its events from the Cubi

Status: **design only, nothing here is implemented.** Baseline: `b13ef40`.

## 1. Recommendation

1. **Make the Cubi service multi-area first.** It gets an authenticated `/api/v1` that creates, lists, refreshes and serves *areas*. Each area has its own definition, feed, caches, lock and status, all derived from the existing `refresh()` code. This part is needed whatever Home Assistant does, and it is where the security rules live.
2. **Never let a browser or card talk to the Cubi.** Home Assistant (server side) is the only client of the new API, using one shared secret that stays in Home Assistant. Browsers authenticate to Home Assistant with their normal session, so no token is ever placed in a card, a dashboard or `localStorage`.
3. **Reach Cubi from Home Assistant in two steps, behind one small card-side interface (`AreaClient`):**
   - **Step 1 (spike, no custom code in HA):** `rest_command` + scripts that return response data. The card calls them with the WebSocket `call_service` command and `return_response`. This validates the whole path with YAML only.
   - **Step 2 (target, only if step 1 shows a real limit):** a thin custom integration `area_events` (config flow, authenticated WebSocket commands, authenticated HTTP view for the feed). It adds what YAML cannot: admin-only mutations, per-user quotas and a status subscription.
4. **Sources are operator-defined on the Cubi, not by card users.** Home Assistant users choose *where* (name / ZIP / coordinates / radius); the Cubi picks sources from its own registry by coverage overlap. This removes the largest risk (a user making the Cubi fetch arbitrary URLs).

Main trade-off: step 1 is nearly free but lets any logged-in Home Assistant user call the scripts (the Cubi's quotas are the backstop); step 2 closes that but adds Python to maintain on HA OS.

## 2. What exists today (inspected)

- **Card:** per-card persistent preferences (HA user data `frontend/get_user_data`/`set_user_data` + localStorage, key = dashboard path + card `id` or config hash). An **Area** panel resolves *saved choice → card `area` → HA home (`hass.config`) → unavailable*, requests browser geolocation only on click, and states plainly that the choice is **not** sent to discovery. Feed comes from `feed:` (`/local/area-events-card/events.json`).
- **HA sync:** `shell_command` + automation copy the Cubi's `/events.json` to `/config/www/...` every 30 minutes (validated, atomic). `/local` is served without authentication.
- **Cubi service** (`discovery/service.py`): one `ThreadingHTTPServer` (`GET /events.json`, `/health`, CORS `*`, read-only), one scheduler thread, one data dir `/data`, one `Fetcher` (2 s between requests per host, robots.txt), container limits 0.5 CPU / 256 MB, port bound to `192.168.1.88:8765`, no authentication.
- **Discovery:** `refresh(config, options, cache_path, output, lock_path, location_cache_path, ...)` is already parameterized by paths, so a second area is "the same function with a different directory and `location`". Area change resets cached events (`_location_key`), `--data-dir` isolates runs, `resolve_area` knows configured / HA hint / browser hint, `registry.select_sources` chooses sources by coverage, events carry `place` (confirmed/estimated/unknown).
- **Gap the earlier work documented:** nothing authenticated carries a card's area to the service.

## 3. Options evaluated

| | Approach | Verdict |
|---|---|---|
| **A** | HA custom integration: config flow stores Cubi URL + key; WebSocket commands (`@websocket_api.websocket_command`, `require_admin` for mutations, per-user checks via `connection.user`) registered with `async_register_command` ([upstream docs](https://contributors.home-assistant.io/frontend/extending/websocket-api/)); optional `HomeAssistantView` with `requires_auth` for the feed; an HA-side `aiohttp` client to the Cubi | **Target.** Strongest boundary and best UX; most code. |
| **B** | Plain REST proxy in HA (a `HomeAssistantView` only, no WebSocket) | Same code burden as A with less structure (no admin decorator, no subscriptions). Use only as A's *data* path, not as the whole design. |
| **C** | Existing HA mechanisms only: `rest_command` (templated URL/payload, `!secret` headers, returns `status`/`content`/`headers` via `response_variable`, default timeout 10 s; [docs](https://www.home-assistant.io/integrations/rest_command/)) wrapped in scripts; card calls them through the WebSocket `call_service` command with `return_response` ([WebSocket API](https://developers.home-assistant.io/docs/api/websocket)) | **Step 1 / spike.** Zero custom code, key lives in `secrets.yaml`. Limits: no admin-only gating for scripts, response-size limits undocumented, no push status. |
| D | Card fetches Cubi directly (CORS `*` or a token in the card config) | **Rejected.** Token visible to every dashboard user; needs https/mixed-content workarounds; defeats "use HA's session". |
| E | Reverse proxy exposing the Cubi API to the LAN/Internet | **Rejected.** Replaces HA auth with nothing unless a second auth system is built. |
| F | MQTT: HA publishes area requests, Cubi subscribes | **Rejected for now.** Adds a broker dependency and an asynchronous protocol to solve a request/response problem; revisit only if the Cubi must never accept inbound connections. |
| G | HA add-on / ingress | **Not applicable.** The service runs on a separate host, not under the Supervisor. |
| H | Keep static feeds per area (extend the 30-minute copy) | **Rejected as the control plane** (no way to create areas) and **as the data plane** for per-user areas because `/local` is unauthenticated and the feed contains the area's coordinates. Fine for the one legacy default area. |

## 4. Architecture

```
 Browser (HA user session)                Home Assistant (RPi4)                    Cubi (Docker)
 ┌────────────────────┐   HA auth         ┌─────────────────────────┐  shared key   ┌──────────────────────────┐
 │ Area Events Card   │ ───────────────►  │ step 1: scripts +       │ ───────────►  │ /api/v1  (Bearer key)    │
 │  prefs: area spec  │  call_service /   │         rest_command    │  LAN, HA only │  areas, feeds, refresh   │
 │  (HA user data)    │  WebSocket        │ step 2: integration     │               │ scheduler + job queue    │
 └────────────────────┘                   │         area_events     │               │ /data/areas/<id>/…       │
        never talks to Cubi               └─────────────────────────┘               │ /events.json (legacy,    │
                                                                                     │   read-only, default)    │
                                                       Cubi ──► public event sites, Nominatim (no credentials)
```

Trust boundaries: (1) browser ↔ HA: HA authentication and user identity; (2) HA ↔ Cubi: a single shared secret over the LAN; (3) Cubi ↔ Internet: outbound only, no credentials, robots.txt and rate limits.

### Control and data flow

1. **Select.** The user picks a saved name, types a city/ZIP/coordinates + radius, chooses "Home Assistant home" (HA coordinates from `hass.config`, no browser involved) or clicks "Use my location" (explicit click; coordinates **rounded to 2 decimals, about 1 km** before leaving the browser). The card stores the **area spec** in its existing preferences.
2. **Ensure.** The card asks HA to create/ensure that area. HA forwards `POST /api/v1/areas`; the Cubi validates, computes the area id, and returns `{id, status}` (idempotent: the same spec is the same area for every card and user).
3. **Read.** The card requests the feed for `id`. HA fetches `GET /api/v1/areas/{id}/events` (with `If-None-Match`) and returns it; while the first run is still queued it returns `status: queued` and the card shows its existing "loading"/stale states.
4. **Refresh.** Hourly by the Cubi scheduler per area (staggered); a user "refresh now" enqueues at most one job per area per 15 minutes.
5. **Failure.** Each area has its own `ok/degraded/failed/queued/running` status, last success time and error. The previous valid feed of that area is always served; the card's stale/failed header badges already exist.

## 5. Cubi API (new, `/api/v1`)

| Request | Purpose | Notes |
|---|---|---|
| `GET /api/v1/health` | service + queue state | for the HA config flow |
| `GET /api/v1/areas` | list areas (definitions + status, no events) | |
| `POST /api/v1/areas` | ensure an area `{name?, location:{query \| zip+country \| latitude+longitude, country?, region?, timezone?, radius_miles}}` | `200` existing / `201` created / `422` invalid / `429` quota |
| `GET /api/v1/areas/{id}` | definition + status | |
| `GET /api/v1/areas/{id}/events` | the area's feed (same document as today) | `ETag`/`304`, `Cache-Control: private`, 503 until first success |
| `POST /api/v1/areas/{id}/refresh` | enqueue a refresh | `202`, or `429` + `Retry-After` |
| `DELETE /api/v1/areas/{id}` | remove | admin path only; unused areas also expire |

No endpoint accepts a source URL, an adapter name, or a file path. `/events.json` and `/health` stay as they are (read-only, unauthenticated, default area) until the HA bridge replaces the static sync; then `AREA_EVENTS_PUBLIC_FEED=0` can turn them off.

### Authentication, validation, abuse limits

- **Key:** 32+ random bytes from a Docker secret file (`AREA_EVENTS_API_KEY_FILE`), `Authorization: Bearer`, compared with `hmac.compare_digest`; a second key slot allows rotation without downtime. Never in logs, feeds, cards or `localStorage`.
- **Uniform failure:** `401` with no detail; more than 5 failures per minute per source address returns `429` for 60 s.
- **Validation:** request body ≤ 4 KB; strict schema with unknown fields rejected, reusing the existing `parse_spec` (coordinate ranges, country/timezone formats) and registry validators; names ≤ 60 characters; radius capped by a server setting (default 100 mi); server computes the id, so a client cannot choose a path (`^a_[0-9a-f]{12}$` only).
- **Cost control:** at most 8 areas (configurable); one worker (no concurrent discovery jobs); queue ≤ 16; ≤ 20 uncached geocode lookups per hour (Nominatim policy: 1 request/second, cached; the location cache becomes shared so a city is looked up once for all areas); per-host 2 s limiter shared across areas; a short HTTP response cache (10 minutes, public pages only) so overlapping areas do not refetch the same pages; areas not read for 14 days stop refreshing and are deleted after 30.
- **SSRF:** impossible by construction because users cannot supply URLs. "User-configured sources" means *operator-edited registry entries on the Cubi*. If HA-side source editing is ever wanted, it must be admin-only, adapter-type allowlisted (e.g. Localist), host-allowlisted, and must refuse private/loopback addresses after DNS resolution.
- **LAN exposure:** keep the published port bound to the Cubi's LAN address and, if the Cubi has a firewall, allow the HA host only (not changed by this design; to be decided at deployment). The key crosses the LAN in cleartext; accept for a home LAN, or terminate TLS in front of the service (open question). The new API sends **no CORS headers**, since only HA calls it.
- **Privacy:** areas contain coordinates and names; they are returned only to authenticated callers. The legacy public feed already exposes its own area.

## 6. Area, feed and cache isolation

- **Identity:** `area_id = "a_" + sha256(canonical spec)[:12]` over `{kind, query|zip|rounded lat/lon, country, radius}`. Same location + radius = same area (shared by cards/users, no duplicate work); anything different is a different area. Names are labels stored with the definition, not part of the id.
- **Layout:** `/data/areas.json` (definitions, labels, last-read times) and `/data/areas/<id>/{events.json, source-cache.json, status.json, .refresh.lock}`. The existing top-level files become the reserved area `default`, so today's feed path, volume and rollback are untouched. Caches that are *not* area-specific and safe to share: geocode cache and the public-page response cache.
- **Execution:** the scheduler becomes "next due area" over the same `refresh()` call with that area's paths and `location` substituted into the base config. No new discovery logic.
- **No leakage:** every area-dependent value (events, `place` classification, distances, `_location_key`) is derived and stored inside that area's directory; nothing area-specific is held in module globals or shared caches. Required tests: two areas with overlapping sources never share events; one area failing leaves the other's feed, status and schedule unchanged; deleting an area leaves others intact; the `default` feed is byte-identical before/after enabling the API.
- **Independent states:** per-area status, error, next run; the service-level health reports counts (areas ok/failed) without hiding per-area errors.

## 7. Home Assistant side

**Step 1 (YAML only).** `rest_command` entries (one per verb: list, ensure, feed, refresh) whose URL/payload are templated from validated script fields and whose `Authorization` header uses `!secret`. Scripts check the area id format, call the command, and return the result with `stop` + `response_variable`. The card calls `{type: "call_service", domain: "script", service: "...", return_response: true}`. Everything is installable the way the current sync was.

**Step 2 (integration).** `custom_components/area_events`: config flow (Cubi URL + key, validated against `/api/v1/health`), WebSocket commands `area_events/list|ensure|refresh` (mutations beyond personal areas `require_admin`; per-user quotas via `connection.user.id`), a background coordinator that polls area status, and a `requires_auth` HTTP view that streams the feed with `ETag` so large payloads do not travel as WebSocket frames. It replaces the 30-minute `shell_command` sync. Packaging: HACS custom repository; HACS accepts exactly one integration per repo ([upstream](https://hacs.xyz/docs/publish/integration)), so the integration and the card live in separate repositories unless the integration serves the card file itself.

**Card.** A small `AreaClient` interface (`ensure`, `feed`, `refresh`, `status`) with three backends selected by config: `static` (today's `feed:`, the default and the fallback), `script` (step 1), `integration` (step 2). Cards without the new option behave exactly as now.

### Persistence and migration

- The card's preferences key and HA user-data storage are **reused unchanged**; the change is additive. Prefs keep the **area spec** (the user's choice) and cache the derived `area_id`; if the Cubi no longer knows the id (404) the card re-ensures it from the stored spec, so Cubi data loss cannot lose a choice.
- Existing values map directly: `{kind:"ha_home"}` → coordinates read live from `hass.config`; `{kind:"browser", lat, lon}` → rounded and ensured; YAML `area` → ensured. Unknown fields are ignored by older card versions, so rollback of the card is safe.
- **Named saved areas** live on the Cubi (labels on definitions), so they are shared by the household and survive HA restores; which one a card uses is per-card preference.
- Multiple cards = multiple independent specs (San Diego and Austin simultaneously), each with its own `id`-scoped key; nothing global is switched.

## 8. Resources

- **Cubi:** one worker keeps concurrency at today's level. A cold San Diego run is about 100+ requests (2.5–5 min, mostly waiting on the 2 s limiter); warm runs reuse cached details (about 2 min). Eight areas hourly is within one worker's capacity only because of staggering and the response cache; the worst case (all cold) is 8 × 5 = 40 min of an hour, so the area cap and queue cap are real limits. Disk ≈ 2–3 MB per area. Memory should match today's process plus the job queue; **measure peak RSS** before raising the 256 MB limit.
- **RPi4:** step 1 moves ~0.5 MB per feed through a service call; step 2 streams it. Do **not** put events in entity attributes or the recorder database. Keep at most a few feeds cached in memory.

## 9. Failure and rollback

- Cubi API down/unreachable: HA returns the last feed it has (or the card keeps what it already shows) with the existing stale/error badges; the legacy static feed keeps working for the default area.
- New area failing (geocode, no covering source, all sources down): that area reports `failed` with the reason; others are unaffected; no data from another area is substituted.
- Rollout is flag-gated at each layer: the Cubi API is off unless a key is configured; cards opt in per card; the static sync stays until the end. Rollback = remove the key (API off), switch cards back to `static`, delete `/data/areas/`. The `default` area and image rollback procedure are unchanged.

## 10. Limitations of the platform

- **HA OS:** custom integrations are allowed (`/config/custom_components`), but they run inside HA Core with its Python and release cadence; breaking changes must be tracked. No shell or extra daemons on the host.
- **Scripts as an API (step 1):** any authenticated HA user can call them; script responses and WebSocket frame sizes are not documented as bounded; there is no server push.
- **Frontend APIs:** `hass.callWS` is documented; `hass.fetchWithAuth` for an authenticated HTTP view is used by the frontend but not documented as a stable custom-card API (verify before depending on it).
- **HACS:** one integration per repository; frontend resources and integrations are different repository types.
- **Browser geolocation:** needs https or localhost; over plain-http HA it will not work, so "HA home" is the dependable default.
- **Data availability, not plumbing, limits value:** only San Diego (four sources) and UT Austin (one Localist site) exist. A new city yields events only if an operator registers sources covering it.

## 11. Minimal milestones (each independently testable)

1. **Cubi: area store + per-area runs** (no HTTP): `areas.json`, `/data/areas/<id>/`, scheduler picks the next due area, `default` unchanged. Tests: isolation, failure independence, 207-event baseline byte-identical.
2. **Cubi: authenticated read API** (`health`, `areas`, `areas/{id}`, `events` with ETag): key handling, throttling, no CORS. Tests: 401/429 paths, key rotation, large/invalid requests.
3. **Cubi: ensure/refresh/delete + quotas:** validation, id derivation, geocode/refresh budgets, area cap, expiry. Tests: quota exhaustion, invalid specs, duplicate specs, coverage-error reporting.
4. **Deploy the API on the Cubi (flag off by default):** key secret, firewall decision, rollback rehearsal.
5. **HA spike (step 1):** `rest_command` + scripts, exercised from Developer Tools with `return_response`; record payload sizes, latency, behaviour as a non-admin user. **Decision gate:** keep step 1, or build step 2.
6. **Card `AreaClient` + migration:** `static` and `script` backends, prefs additive change, mismatch notice replaced by real status. Tests with stubbed clients: two cards (San Diego, Austin) independent; 404 re-ensure; older prefs load.
7. **(Conditional) Integration `area_events`:** config flow, admin/user WebSocket commands, authenticated feed view, coordinator; HACS custom repository.
8. **Retire the static sync** for non-default areas; optionally turn off the public feed.

## 12. Open questions needing runtime validation

1. Does a script's `stop`/`response_variable` return through WebSocket `call_service` with `return_response` on the installed HA version, and what payload size and latency does a ~0.5 MB feed show (script, WebSocket frame, `rest_command`)?
2. Can a non-admin HA user call the scripts (expected yes), and is that acceptable given the Cubi quotas?
3. Is `hass.fetchWithAuth` available and stable enough for an authenticated feed view, or should large feeds be chunked over WebSocket?
4. Which firewall tool exists on the Cubi, and is restricting port 8765 to the HA host acceptable? Is cleartext on the LAN acceptable or is a TLS front needed?
5. Peak memory/CPU of a Cubi worker with 4–8 areas; is 256 MB still enough?
6. Nominatim usage under many user-chosen cities: is the shared cache plus an hourly budget enough, or is a second free provider needed?
7. Per-user versus household-wide named areas and who may delete them.
8. Repository layout: separate integration and card repositories, or an integration that serves the card file.
9. Which additional cities matter, and which sources exist for them (the real constraint on usefulness).
