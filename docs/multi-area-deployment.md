# Multi-area mode: installation, security, troubleshooting, rollback

Multi-area mode lets each Area Events Card show events for its own place (city, ZIP code, coordinates, the Home Assistant home location, or the browser location) and radius. Choosing an area in a card asks the discovery server to discover events there; the server keeps every area's feed and caches separate. The design and the reasons for it are in [multi-area-architecture.md](multi-area-architecture.md). Nothing here is required to keep the existing single-area setup working: with no API key the server behaves exactly as before.

```
card (browser)  --HA session-->  Home Assistant scripts  --Bearer key (LAN)-->  discovery service /api/v1
                                   (key in secrets.yaml)                          areas, feeds, scheduler
```

The browser never contacts the server and never holds a key. Home Assistant scripts run with the signed-in user's own session and add the server key from `secrets.yaml`.

## What is where

| Piece | Files |
|---|---|
| Discovery service (any host with Docker, or plain Python 3.9+) | `discovery/` (`api.py`, `areamgr.py`, `areas.py`, `service.py`), `discovery.config.json`, `compose.yaml` |
| Home Assistant scripts | [homeassistant/area_events.example.yaml](../homeassistant/area_events.example.yaml) |
| Card | `dist/area-events-card.js` via `.\deploy.ps1` as before; set `source: service` per card |
| Smoke test | `scripts/e2e_local.py` |

## 1. Configure and start the service

1. Pick the sources. `discovery.config.json` is the *operator's* registry: it lists every site the server may read and where each one has events (`coverage`). An area only uses the sources whose coverage overlaps it, so a place nothing covers reports a clear error. The shipped file covers San Diego (four sources) and the UT Austin campus (Localist). Add sources by editing this file (see README, "Source registry"); API clients cannot add or change sources, URLs or adapters.
2. Optional limits: add or edit the `api` block (defaults shown):
   ```json
   "api": { "max_areas": 8, "max_radius_miles": 100, "creations_per_hour": 10, "geocodes_per_hour": 20,
            "refresh_interval_seconds": 3600, "manual_refresh_min_seconds": 900,
            "pause_after_days": 7, "delete_after_days": 30 }
   ```
3. Create the API key and put it in the service's environment (Docker: `.env` next to `compose.yaml`; the file is not committed):
   ```
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   AREA_EVENTS_API_KEY=<that value>
   ```
   Alternatively `AREA_EVENTS_API_KEY_FILE=/path/to/secret` (a mounted secret file). A second slot (`AREA_EVENTS_API_KEY_2` / `_FILE`) lets you rotate: set the new key as the second, update Home Assistant, then move it to the first. Keys shorter than 32 characters are refused and leave the API off. With no key the API does not exist (every `/api/v1` path answers 404).
4. Start or rebuild the service (see [docker-deployment.md](docker-deployment.md)); the log says `management API enabled`.

The default area (today's `/events.json`, hourly refresh, and the 30-minute Home Assistant copy) is untouched and keeps working while multi-area mode is on.

## 2. Connect Home Assistant

1. `secrets.yaml`: `area_events_authorization: "Bearer <the same key>"`.
2. Add the `rest_command:` block and the scripts from `homeassistant/area_events.example.yaml` (change the address to your server). Check the configuration, then reload Rest commands and Scripts.
3. Test from Developer tools > Actions: run `script.area_events_list`; the response should show `status: 200` and the default area.

## 3. Use it in a card

```yaml
type: custom:area-events-card
source: service
title: Events near me
country: US            # optional: used for ZIP codes and to bias city names (defaults to Home Assistant's country)
radius_miles: 25       # optional default radius
# area: { name: Office, latitude: 40.7, longitude: -74.0 }   # optional explicit starting area
# id: left             # give identical cards on one dashboard different ids so they keep separate choices
```

With no saved choice a card starts at its `area`, else at the Home Assistant home location, else asks you to choose. Open **Filters > Area** to type a city, ZIP code or `latitude, longitude`, set a radius and an optional name, use the Home Assistant home location, use your browser location (only when you click; coordinates are rounded to about 1 km before they leave the browser), pick a saved area, or refresh now. The choice is saved for that card in your Home Assistant user data. Two cards can show two areas at once; changing one never changes the other.

The first discovery for a new area takes from about a minute (a campus calendar) to several minutes (many sites); the card shows "Discovering" and then the events. Areas nobody has viewed for 7 days stop refreshing and are removed after 30.

## Security model

- **Authentication:** every management route needs `Authorization: Bearer <key>`, compared in constant time; failures answer a uniform 401, and five failures from one address per minute block that address for the rest of the minute (429). Without a configured key the API is off.
- **What a client can ask for:** only a place (name, ZIP + country, or coordinates), a radius, an optional display name and timezone. Unknown fields, URLs, adapter names, provider names and file paths are rejected; area ids are server-computed and validated before they touch the filesystem. Request bodies are limited to 4 KB and must carry a length.
- **Cost limits:** at most 8 areas; 10 new areas per hour; 20 uncached geocoding lookups per hour (cached lookups are free); radius at most 100 miles; one discovery job at a time across all areas (the default area's job shares the same slot); manual refresh at most once per 15 minutes per area; automatic refresh hourly, only for areas that were viewed in the last 7 days.
- **No credentials in the browser:** the key lives in `secrets.yaml` and the server's environment. Card code only calls `script.area_events_*` through Home Assistant's authenticated connection.
- **No CORS on `/api/v1`:** browsers are not meant to call it. The public read-only `/events.json` and `/health` keep their previous behavior.
- **Network exposure:** keep the published port bound to the host's LAN address (`AREA_EVENTS_BIND`) and, if the host has a firewall, allow only the Home Assistant machine to reach it. The key crosses the LAN in cleartext; terminate TLS in front of the service if that matters to you.
- **Who can use it:** any signed-in Home Assistant user who can run scripts can choose areas (within the limits above). Restricting that to administrators needs a custom integration (design: step 2 in the architecture document).
- **Privacy:** areas contain coordinates and names and are returned only to authenticated callers.

## Troubleshooting

| Symptom | Check |
|---|---|
| Card says "Home Assistant could not run script.area_events_..." | The scripts are missing or not reloaded; run them from Developer tools. |
| "The Home Assistant script returned no response" | The rest_command or script returned nothing; check the Home Assistant log, the server address in the rest_command, and that the key matches. |
| Server answers 401 | Wrong or missing key in `secrets.yaml`; 429 `too_many_failures` means five bad attempts, wait a minute. |
| 404 on every `/api/v1` path | The service has no API key (or it is shorter than 32 characters; see its log). |
| "Waiting for the server to start discovery..." for long | Other areas are running (one job at a time) or the server is busy; `GET /api/v1/health` shows the running area. |
| "no enabled source applies to ..." | No source in `discovery.config.json` covers that place; add sources for it. |
| "could not resolve location" | The place name was not found; use a more specific name, a ZIP code with a country, or coordinates. |
| 429 `area_limit`, `creation_rate`, `refresh_rate` | A limit above was reached; the `Retry-After` header and the message say when. |
| An area's events look old | The area's status (Filters > Area) shows the last success and any error; its previous feed is kept while discovery fails. |
| Browser location refused | Browsers only offer location on https pages or localhost; use the Home Assistant home location or type the place. |

Logs: `docker compose logs discovery` and `/data/refresh.log`. Per-area status: `GET /api/v1/areas`.

## Local end-to-end check

`python scripts/e2e_local.py` starts a throwaway service, plays Home Assistant's part (authenticated API calls), creates an Austin area and a San Diego area against the real sites, waits for both feeds and verifies refusals, isolation and the default feed. It does not use Home Assistant itself.

## Rollback

- Turn multi-area mode off: remove `AREA_EVENTS_API_KEY` (and the second slot) and restart the container. The API disappears; the default area, `/events.json` and the 30-minute copy continue unchanged. Area data stays in the volume under `areas/`, `areas.json` and `geocode-cache.json` and is ignored.
- Remove the data: delete `/data/areas`, `/data/areas.json` and `/data/geocode-cache.json` (inside the container volume).
- Cards: remove `source: service` (use `feed:` as before); saved area choices stay in preferences and are ignored.
- Home Assistant: delete the scripts and rest_commands and the `area_events_authorization` secret.
- Software: redeploy the previous image tag as described in [docker-deployment.md](docker-deployment.md) (`area-events:previous`), and the previous card build with `.\deploy.ps1` from the earlier commit.

## What still needs a real Home Assistant to verify

The server, API, security controls, scheduler, isolation and the card's logic are tested locally (the card against a faithful stand-in for the scripts). These points depend on the installed Home Assistant version and could not be tested here:

1. That a script's `stop` + `response_variable` is returned to the WebSocket `call_service` call with `return_response` in the shape the card expects (the card accepts the response directly or wrapped), and how large a feed (about 0.5 MB) that path carries.
2. That `rest_command` returns the status and body for non-2xx answers (the card shows server errors such as an invalid place or a quota) instead of failing the script.
3. That a non-administrator can run the scripts.
4. Browser geolocation on your dashboard URL (https or localhost only).
