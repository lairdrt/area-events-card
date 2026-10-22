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

Tokens are sent in every request, so do not send them across a network in plain text. The service binds to loopback by default. For the Cubi-to-Pi link, terminate TLS in front of it with an authenticated reverse proxy on the Cubi, for example Caddy:

```
events.example.lan {
    tls internal                      # or a certificate your Home Assistant trusts
    reverse_proxy 127.0.0.1:8780
}
```

and enter `https://events.example.lan` as the service address. (This milestone documents the configuration only; no proxy was installed or deployed.) The config flow warns when the address is plain http beyond this computer. Never share `discovery.db` over SMB or NFS: Home Assistant reaches it only through the API.

## Verification on the device

Nothing here was run on a real Home Assistant. In order:

1. **Discovery host (Cubi).** `python scripts/discovery.py serve --bind 127.0.0.1 --port 8780` behind the TLS proxy (or `--bind` a trusted address with `--allow-remote` and accept plain http on a trusted link). `python scripts/discovery.py status` shows areas, jobs and `events N normalized`.
2. **Areas.** `python scripts/discovery.py areas list` shows San Diego with source `sandiego_gov`. For Austin, create the area only if you accept that it has no live source (`areas create Austin 30.2672 -97.7431 35`); an area without sources shows a clear notice.
3. **Copy the integration** (`scripts\deploy-ha.ps1 -AppsShare X:\` as before; never `-Force`) and **restart Home Assistant yourself**. Check the log for `area_events` errors.
4. **Add the integration** (Settings, Devices & services, Add, Area Events): address, read token, and optionally the control token. Expected: it is created; a wrong token says "rejected"; a wrong address says "could not be reached".
5. **Add two cards** (San Diego, Austin). Expected: San Diego lists real City events; Austin shows its notice (no live source, or demonstration data); neither lists the other's events; the browser console shows one `area_events/subscribe` per card and no repeating requests.
6. **Update propagation.** Change something the service re-reads (or `python scripts/discovery.py run --now`), wait for extraction and one interval: the San Diego card updates without a reload; the Austin card does not change; the Discovery log shows no crawl caused by the card.
7. **Disconnect.** Stop the Discovery service: cards show "Events unavailable"/"Update failed" with earlier events kept. Start it: the cards recover within one interval. Restart Home Assistant (or reload the integration) with Discovery running: cards reconnect and show the current events.
8. **Shared settings.** As an administrator, call `area_events/settings` / `area_events/pause` from Developer Tools (WebSocket test via the browser console `hass.callWS`), check `python scripts/discovery.py status`; as a non-administrator the call must fail with `unauthorized`.
9. **Security.** Search the Home Assistant log and a card's WebSocket traffic for the tokens: they must not appear.

## Known limits

- No Home Assistant runtime acceptance yet (above).
- Area matching is by source; there is no radius filtering. Events whose place is only a description are marked unverified.
- One live source (City of San Diego). Austin is demonstration data only.
- The card editor does not list areas yet (type the name); a settings panel was deliberately not built: use the WebSocket commands or `scripts/discovery.py`.
- The hub holds at most 1000 events per followed area (reported as truncated).
- An event whose start moves into a followed area's window from outside it, or a brand-new event, makes every followed area reread once (the change record does not name an area).
- Legacy profile-driven crawling, the Supervisor app discovery and the imagery proxy were removed from the integration; the earlier runtime remains available from its Git tags.
