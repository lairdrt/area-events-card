# Home Assistant integration (Milestone 2D)

How the Area Events card, Home Assistant and the Discovery Service fit together, how to set it up, and how to verify it on the device.

## Status of the evidence

- **Verified here** (automated tests, Windows laptop): the integration's client, hub and feed adapter against the **real** Discovery Service (HTTP API, SQLite, discovery, extraction, change feed); the card against a Home Assistant stand-in; the Home Assistant glue (config flow, setup/unload, coordinator, WebSocket commands) against minimal stand-ins shaped after the Home Assistant API.
- **Not verified here**: any of it inside a real Home Assistant 2026.10.2. No Home Assistant was available, and none was deployed or restarted. Use the checklist at the end.
- **Austin** has only ever been exercised through a fixture of the City adapter (demonstration data). There is no live Austin source. The card says so; it does not say nothing happens in Austin.

## Shape

```
card "San Diego" ─┐                                           GET /v1/discovery/status, /v1/changes,
card "Austin"    ─┼─ HA WebSocket ─ integration (one hub) ─── /v1/events, /v1/processing/status   (read token)
card ...         ─┘  area_events/subscribe                    PATCH/POST/PUT /v1/areas, /v1/discovery/*  (control token, admins only)
                                                                         │
                                                      Discovery host: discovery.db is the only database
```

One database (`discovery.db`) holds areas, sources, candidates, events, extraction history, job state and the change feed. Home Assistant holds no event store: the hub keeps an in-memory cache per followed area, which can be rebuilt from the service at any time.

## Components (custom_components/area_events)

| Module | Role |
|---|---|
| `discovery_client.py` | Async client for the service API over Home Assistant's shared aiohttp session; typed errors; 20 s timeouts. No Home Assistant imports. |
| `feed.py` | Small versioned adapter from `discovery.events.v1` events to the card's event dictionary. Adds nothing the service did not say (no coordinates, no confidence, no categories); fixture sources are labelled. |
| `hub.py` | The shared view: area list, one snapshot per followed area, card subscriptions, the refresh that reads the change feed, and the control commands. Instance-owned state only. |
| `coordinator.py` | One `DataUpdateCoordinator` per config entry that calls `hub.refresh()` every `scan_interval` seconds (default 10, 5 to 300). Not one per card. |
| `websocket_api.py` | The WebSocket commands. Tokens never appear in any message. |
| `config_flow.py` | Address, read token, optional control token; reauth; options (interval). |
| `__init__.py` | Config entry setup/unload (`entry.runtime_data`), serves the card script. |

## WebSocket commands

| Command | Who | What |
|---|---|---|
| `area_events/areas` | any signed-in user | Discovery areas (name, enabled, intensity, revision, sources) and the service state. |
| `area_events/status` | any signed-in user | Availability, paused/mode/intensity, settings revision, extraction backlog. |
| `area_events/subscribe` `{area}` | any signed-in user | **The card's live view of one area**: confirmed first, then the full current view now and again whenever it changes. Ends with the card's connection. |
| `area_events/events` `{area, from?, to?, after?, limit?}` | any signed-in user | One page straight from the store (cursor pagination, 200 max) for tools and previews. |
| `area_events/event` `{event_key}` | any signed-in user | One event with revision history and the evidence behind each field. |
| `area_events/area_create`, `area_update`, `settings`, `pause`, `resume` | **administrators** | Shared Discovery settings. Updates and settings require `expected_revision`; a stale one fails with `revision_conflict` and the current revision. |

Errors have actionable codes: `not_configured`, `unknown_area` (lists the areas that exist), `control_not_configured`, `forbidden` (the service refuses that token), `unauthorized` (token rejected: reauthentication is started), `revision_conflict`, `not_found`, `rejected`, `unavailable`.

A successful settings command means the **service saved it** (and appended a durable change record). It does not mean any crawl has finished. If the service cannot be reached the command fails; nothing is reported as saved.

Mutations need two things: Home Assistant's administrator check on the signed-in user, and a control token configured on the entry. A read-only entry (no control token) still serves every card.

## Shared update mechanism

- One coordinated refresh per interval for the whole integration: one status call and, when cards exist, one extraction-status call and one `GET /v1/changes` call, however many cards there are.
- A followed area is read again only when the change feed shows an `event.created`, an `event.updated`/`removed` for an event that area shows, or an area configuration change after that snapshot's position. Each snapshot records the change-feed position it was read at.
- Only cards whose view really changed are sent a message (a digest of events and their revisions, the area, the notice and freshness).
- Cards of the same area share one snapshot; cards of different areas never see each other's data. A card's display filters (categories, interests, exclusions, date range, sorting, layout) stay in the card.
- Nothing a card does starts a crawl. Discovery follows the Discovery Service's own schedule and the shared settings.

### Outages and restarts

- Service unreachable: every affected card is sent one `unavailable` message (the last known events are kept, not blanked). The check is repeated each interval; the cards are not told again until something changes.
- Recovery: the hub rereads every followed area, so a revision committed during the outage is not missed. A newly created hub (after a Home Assistant restart) starts from the authoritative store.
- The Discovery host keeps running when Home Assistant stops, restarts or reloads the entry; the host restarting only interrupts reads until it is back.
- The card re-subscribes by itself when its browser reconnects (Home Assistant's `subscribeMessage`), which delivers the current view again.
- Persistent consumer checkpoints belong to the service (`/v1/changes` and its consumer acknowledgements). The hub keeps none: it does not need to, because a snapshot is always rebuilt from the store.

## Area matching is not a radius

An event belongs to a discovery area because **a source that area uses** produced it. Events carry no verified coordinates, so the area's radius is not applied and the card never shows "within N miles". Each event states how its location is known: `address`, `venue`, `description_only` (the source's own description of a place) or `unknown`; the card marks the last two as unverified. Precise radius filtering needs a geolocation component that does not exist yet.

## Card configuration

`area:` is the name (or key) of a discovery area, shown in `area_events/areas`. `radius_miles` and `sources` are no longer card settings and are ignored. See the README for the filters the card keeps for itself. Two cards:

```yaml
type: custom:area-events-card
title: San Diego
area: San Diego
days: 14
---
type: custom:area-events-card
title: Austin (demonstration data)
area: Austin
days: 30
```

## Tokens and transport

`python scripts/discovery.py serve` creates three tokens, each in a private file next to the database: `api-token` (control), `api-read-token` (read) and `api-process-token` (extraction workers only). A read token cannot change anything; a process token can only use the processing routes; neither of the others can submit extraction results. Enter the read token (and, only if administrators should change settings from Home Assistant, the control token) in the integration's setup form. They are stored in the config entry on the Home Assistant backend and are never sent to a dashboard.

Tokens are sent in every request, so they must never cross a network in plain text. The service therefore **refuses to bind beyond loopback without TLS** (`--allow-plain-remote` is the explicit, informed exception). The simplest secure setup needs no extra software: the service serves https itself with a certificate you make once on the Cubi, and Home Assistant trusts that one certificate file.

On the Cubi, once (use the Cubi's real address in `subjectAltName`):

```
mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes -keyout certs/key.pem -out certs/cert.pem -days 825     -subj "/CN=area-events-discovery" -addext "subjectAltName=IP:192.168.1.88"
chmod 600 certs/key.pem
python3 scripts/discovery.py serve --bind 192.168.1.88 --allow-remote --tls-cert certs/cert.pem --tls-key certs/key.pem
```

Copy only the public `cert.pem` to Home Assistant (for example `/config/area_events_cert.pem`) and enter that path in the integration's **Certificate file** field with the address `https://192.168.1.88:8780`. The private key never leaves the Cubi. (A reverse proxy with a certificate Home Assistant already trusts, such as Caddy with a real domain, works too: leave the certificate field empty.) Never share `discovery.db` over SMB or NFS: Home Assistant reaches it only through the API.

## Verification on the device

Runtime evidence so far (Milestone 2D acceptance, 2026-10-22): the Discovery Service ran on the real Cubi (Python 3.13, own directory, loopback and then TLS on the LAN) and was exercised from the laptop with the integration's real client and hub; Home Assistant itself was **not** changed or restarted. What remains is Home Assistant's side, in order:

1. **Discovery host (Cubi)**, from `~/area-events-discovery` (already staged; start it as above). `python3 scripts/discovery.py status` shows areas, jobs and `events N normalized`. To keep it running across reboots, run it under a systemd user unit (`loginctl enable-linger`) or your usual supervisor; this was deliberately not installed.
2. **Areas.** San Diego (source `sandiego_gov`) and Austin (no source) exist in the staged database. Austin has no live source; its card says so.
3. **Remove the old Area Events entry** (Settings, Devices & services): it belongs to the earlier app (`source: hassio`) and the new integration refuses it with an instruction in the log.
4. **Copy the integration** (`scripts\deploy-ha.ps1 -AppsShare X:\`, never `-Force`; it also rebuilds the now-unneeded legacy app) and **restart Home Assistant yourself**. Check the log for `area_events` errors.
5. **Add the integration**: `https://192.168.1.88:8780`, the read token, optionally the control token, and the certificate path. A wrong token says "rejected"; an untrusted certificate says "could not be reached".
6. **Add two cards** (San Diego, Austin). Expected: San Diego lists real City events; Austin shows "No event sources are attached"; neither lists the other's events; the browser console shows one `area_events/subscribe` per card and no repeating requests.
7. **Update propagation.** `python3 scripts/discovery.py run --now` on the Cubi (or wait for the hourly crawl), then one interval: a changed event reaches only its area's card; the Discovery log shows no crawl caused by a card.
8. **Disconnect.** Stop the service: cards show "Events unavailable" with earlier events kept. Start it: they recover within one interval. Restart Home Assistant or reload the entry with Discovery running: cards reconnect.
9. **Shared settings.** As an administrator, `hass.callWS({type: "area_events/pause"})` from the browser console; check `status` on the Cubi; as a non-administrator the call must fail with `unauthorized`. Resume afterwards.
10. **Security.** Search the Home Assistant log and a card's WebSocket traffic for the tokens: they must not appear.

## Known limits

- No Home Assistant runtime acceptance yet (above).
- Area matching is by source; there is no radius filtering. Events whose place is only a description are marked unverified.
- One live source (City of San Diego). Austin is demonstration data only.
- The card editor does not list areas yet (type the name); a settings panel was deliberately not built: use the WebSocket commands or `scripts/discovery.py`.
- The hub holds at most 1000 events per followed area (reported as truncated).
- An event whose start moves into a followed area's window from outside it, or a brand-new event, makes every followed area reread once (the change record does not name an area).
- Legacy profile-driven crawling, the Supervisor app discovery and the imagery proxy were removed from the integration; the earlier runtime remains available from its Git tags.
