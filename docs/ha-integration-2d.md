# Home Assistant integration

How the Area Events card, Home Assistant and the Discovery Service fit together, how to set it up, and how to verify it on the device.

The integration is tested against the Discovery Service and Home Assistant API stand-ins. The deployment workflow also checks the configured Home Assistant instance, integration load state, WebSocket event and subscription commands, and manifest-required frontend assets. A signed-in browser check is still needed to confirm the dashboard's visual layout.

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

`area:` is the name (or key) of a discovery area, shown in `area_events/areas`. `radius_miles` is not used for event matching. See the README for the filters the card keeps for itself. Two cards:

```yaml
type: custom:area-events-card
title: San Diego
area: San Diego
days: 14
---
type: custom:area-events-card
title: Austin
area: Austin
days: 30
```

## Tokens and transport

`area-events serve` creates three tokens, each in a private file next to the database: `api-token` (control), `api-read-token` (read) and `api-process-token` (extraction workers only). A read token cannot change anything; a process token can only use the processing routes; neither of the others can submit extraction results. Enter the read token (and, only if administrators should change settings from Home Assistant, the control token) in the integration's setup form. They are stored in the config entry on the Home Assistant backend and are never sent to a dashboard. Install the package first with `python -m pip install .`.

Tokens are sent in every request, so they must never cross a network in plain text. The service therefore **refuses to bind beyond loopback without TLS** (`--allow-plain-remote` is the explicit, informed exception). The simplest secure setup needs no extra software: the service serves https itself with a certificate you make once on the Cubi, and Home Assistant trusts that one certificate file.

On the Cubi, once (use the Cubi's real address in `subjectAltName`):

```
mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes -keyout certs/key.pem -out certs/cert.pem -days 825     -subj "/CN=area-events-discovery" -addext "subjectAltName=IP:192.168.1.88"
chmod 600 certs/key.pem
area-events serve --bind 192.168.1.88 --allow-remote --tls-cert certs/cert.pem --tls-key certs/key.pem
```

Copy only the public `cert.pem` to Home Assistant (for example `/config/area_events_cert.pem`) and enter that path in the integration's **Certificate file** field with the address `https://192.168.1.88:8780`. The private key never leaves the Cubi. (A reverse proxy with a certificate Home Assistant already trusts, such as Caddy with a real domain, works too: leave the certificate field empty.) Never share `discovery.db` over SMB or NFS: Home Assistant reaches it only through the API.

## Deployment and verification

Use the supported one-command deployment described in [automated-deployment.md](automated-deployment.md). It installs the matching integration and frontend, restarts Home Assistant when required, and checks service health, event API responses, WebSocket delivery, and the assets registered in the presentation manifest. After it completes, open the signed-in dashboard and confirm that the configured cards render.

## Area selection

Events belong to an area through the sources configured for that area; the Discovery Service owns source selection. `radius_miles` is not applied to event matching. Events whose place is only a description are marked unverified in the card.
