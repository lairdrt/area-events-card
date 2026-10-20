# Changing the discovery location from Home Assistant

The service discovers events for **one** place: the `location` in `discovery.config.json` (San Diego until someone changes it). This optional feature lets the Area Events Card ask the service to switch that one place, for example from San Diego to Austin and back. Nothing here is needed to keep today's setup running: without an API key the service behaves exactly as before.

```
card (browser)  --HA session-->  Home Assistant scripts  --Bearer key, LAN-->  discovery service
                                  (key in secrets.yaml)                         PUT/GET /api/v1/location
```

The browser never contacts the service and holds no credential. The scripts run with the signed-in user's own Home Assistant session and add the key from `secrets.yaml`.

## How a change works

1. The request is validated: only a place (name, ZIP + country, or coordinates), a radius up to 100 miles and an optional display name are accepted. URLs, adapter names, provider names and file paths are rejected.
2. Discovery runs for the new place **in a separate staging directory with its own cache**, holding the same single job slot as the hourly refresh. The published feed, the active location and the cache stay exactly as they were meanwhile; the card keeps showing the old events.
3. Only if that run produced a valid, non-empty feed labeled with the requested place is it published: the new place is saved (`active-location.json`, so it survives restarts), then the cache and the feed are replaced atomically.
4. If anything fails (invalid place, no source covering it, sources down, a crash, a restart, a failed write), nothing is published, the previous place is restored, and the failure and its reason are reported by `GET /api/v1/location` and shown in the card.

After a successful change the service's feed is for the new place immediately; the copy Home Assistant serves at `/local/area-events-card/events.json` follows at the next 30-minute sync, or at once with the card's "Fetch the new results now" button (it runs your existing `shell_command.sync_area_events`).

Changes are limited to one at a time, one accepted change per 5 minutes and 20 uncached geocoding lookups per hour. Sources always come from the operator's registry in `discovery.config.json`; a place no source covers fails with a clear message. The shipped registry covers San Diego (four sources) and the UT Austin campus.

## Setup

1. Create a key and give it to the service. For Docker, put it in `.env` next to `compose.yaml` (not committed) and recreate the container:
   ```
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   AREA_EVENTS_API_KEY=<that value>
   ```
   Or use `AREA_EVENTS_API_KEY_FILE=/path/to/secret`. A second slot (`AREA_EVENTS_API_KEY_2` / `_FILE`) allows rotation: add the new key as the second, update Home Assistant, then swap. Keys shorter than 32 characters are refused and leave the API off. With no key every `/api/v1` path answers 404. The log says `location control enabled`.
2. In Home Assistant add `area_events_authorization: "Bearer <key>"` to `secrets.yaml`, then the `rest_command` and `script` blocks from [homeassistant/location_control.example.yaml](../homeassistant/location_control.example.yaml); reload Rest commands and Scripts; run `script.area_events_location` from Developer tools to check it returns status 200.
3. In the card (which keeps its `feed:` option as before):
   ```yaml
   type: custom:area-events-card
   feed: /local/area-events-card/events.json
   location_control: true
   country: US           # optional: used for ZIP codes and to bias city names (defaults to Home Assistant's country)
   radius_miles: 25      # optional default radius for the Home Assistant home / browser location
   ```
   Open **Filters > Area**: choose the Home Assistant home location or your browser location (only on click; coordinates are rounded to about 1 km) and press **Use this area for discovery**, or type a city, ZIP code or `latitude, longitude`, a radius and a name and press **Switch discovery to this place**. The panel shows what the server is discovering, the progress of a running change, and any failure.

Run `script.area_events_location` in Developer tools at any time to see the same state. To return to the configured place, delete `active-location.json` from the data volume (or change to the configured place again).

## Security

- Authentication is a Bearer key compared in constant time; failures answer a uniform 401, and five failures from one address in a minute block it for the rest of the minute (429). Unauthenticated requests change nothing.
- The key lives in the service's environment/secret file and in `secrets.yaml` only. Card code only runs `script.area_events_*`.
- `/api/v1/location` sends no CORS headers (browsers are not meant to call it). The public read-only `/events.json` and `/health` are unchanged and remain unauthenticated.
- Keep the published port bound to the host's LAN address and, if the host has a firewall, allow only the Home Assistant machine to reach it. The key crosses the LAN in cleartext; terminate TLS in front of the service if that matters to you.
- Any signed-in Home Assistant user who can run scripts can request a change (within the limits above). Restricting it to administrators needs a custom integration, which is not built.

## Troubleshooting

| Symptom | Check |
|---|---|
| "Home Assistant could not run script.area_events_..." | The scripts are missing or not reloaded; run them from Developer tools. |
| "The Home Assistant script returned no response" | Check the Home Assistant log, the address in the rest_command and that the key matches. |
| Server answers 401 | Wrong key in `secrets.yaml`; 429 `too_many_failures` means five bad attempts, wait a minute. |
| 404 on `/api/v1/location` | The service has no valid API key (see its log). |
| "no enabled source applies to ..." | No source in `discovery.config.json` covers that place; add sources for it. |
| "could not resolve location" | Use a more specific name, a ZIP code with a country, or coordinates. |
| 409 / 429 | A change is already running, or the last accepted change was under 5 minutes ago; the message says when to retry. |
| Change succeeded but the card still shows old events | The Home Assistant copy follows at the next 30-minute sync; press "Fetch the new results now". |
| Browser location refused | Browsers only offer location on https pages or localhost; use the Home Assistant home location or type the place. |

Logs: `docker compose logs discovery` and `/data/refresh.log`.

## Rollback

- Turn it off: remove `AREA_EVENTS_API_KEY` (and the second slot) and recreate the container. The API disappears; discovery, `/events.json` and the 30-minute copy continue. If a change had been made, the service keeps using that place until `active-location.json` is deleted.
- Home Assistant: delete the scripts, the rest_commands and the `area_events_authorization` secret. Cards: remove `location_control`.
- Software: redeploy the previous image tag as described in [docker-deployment.md](docker-deployment.md) (`area-events:previous`); it ignores the extra files in the volume.

## What still needs a real Home Assistant to verify

The service, API, security controls, switching and failure handling are tested locally, and the card against a faithful stand-in for the scripts. These depend on the installed Home Assistant and could not be tested here: that a script's `stop` + `response_variable` reaches the card through WebSocket `call_service` with `return_response` in the shape the card expects (it accepts the response directly or wrapped); that `rest_command` returns the status and body for non-2xx answers (so server messages reach the card); that a non-administrator can run the scripts; and browser geolocation on your dashboard URL.
