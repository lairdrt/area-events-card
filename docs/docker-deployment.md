# Area Events discovery service (Docker)

The discovery engine packaged as a standalone Compose project, `area-events`, for a small always-on Linux host (the MSI Cubi, `robohobo@192.168.1.88`, Debian 13, Docker 29). It refreshes events on startup and about hourly, and serves the newest valid feed read-only over HTTP. It does not touch any other Compose project on the host.

## Design

| | |
|---|---|
| Image | `python:3.13-slim` + the `discovery/` package and `discovery.config.json`; standard library only, no `pip install` |
| Process | one Python process: a scheduler thread (refresh at start, then 1 hour after each refresh finishes, so runs never overlap) and an HTTP server |
| User | `events` (uid 10001), read-only root filesystem, all capabilities dropped, `no-new-privileges` |
| Limits | 0.5 CPU, 256 MB, 128 processes; logs rotate at 3 x 10 MB |
| Project / names | Compose project `area-events`, container `area-events`, volume `area-events_data`, default network `area-events_default` |
| Port | container 8080, published as `${AREA_EVENTS_BIND:-127.0.0.1}:${AREA_EVENTS_PORT:-8765}` |
| Persistence | named volume `area-events_data` mounted at `/data` |

Persistent files in `/data`: `events.json` (the feed), `source-cache.json` (per-source cache and cached event details), `status.json` (last run result, survives restarts), `refresh.log` (rotating, 5 x 1 MB).

### Endpoints (read-only)

- `GET /events.json` returns the newest valid feed. If no valid feed exists yet it returns `503` with an error body, never an empty feed. A corrupt or empty file on disk is ignored and the last valid copy keeps being served.
- `GET /health` returns JSON with: `service` (running, uptime), `discovery` (state, last attempt, last success, last exit code, duration, next run), `feed` (present, events, generated_at, age, stale), `sources` (per source `ok`, `error`, `last_success_at`, `stale`) and `source_errors`. `status` is `starting` (no feed yet, first 20 minutes), `ok`, `degraded` (a source or the last run failed, but the feed is fresh) or `unhealthy` (no feed after startup, or feed older than 3 refresh intervals). Only `unhealthy` returns HTTP 503.
- Everything else is `404`; `POST/PUT/DELETE/PATCH` are `405`. Responses are `Cache-Control: no-store` and `Access-Control-Allow-Origin: *` (the data is public and read-only; this lets the HA page fetch it).

### Security assumptions

- Plain HTTP, no authentication: it serves only public event data and accepts no input. Keep it on the LAN.
- The host port defaults to `127.0.0.1`. Set `AREA_EVENTS_BIND` to the host's LAN address (`192.168.1.88`) to serve the LAN. Do not bind `0.0.0.0` unless you intend to.
- The host firewall is not changed by this project. If one is active, allow the Home Assistant machine to reach the port.
- Home Assistant over `https://` cannot fetch an `http://` feed (mixed content); use HA over `http://` on the LAN or put a TLS proxy in front.
- Outbound: the container fetches the public source websites (one request per 2 seconds per site, honoring robots.txt).

## Local checks (development machine)

```
python -m unittest discover -s tests -t .
docker compose config        # validate the Compose file
docker build -t area-events:local .
```

## Deploy over SSH (first time)

Nothing here modifies the existing Frigate project. Work only in `~/area-events`.

1. **Look first (read-only).**
   ```
   ssh robohobo@192.168.1.88
   docker compose ls                      # Frigate's project should be listed; do not touch it
   docker ps --format 'table {{.Names}}\t{{.Ports}}'
   ss -ltn | grep ':8765' || echo "8765 is free"
   ip -4 addr show | grep inet            # confirm 192.168.1.88
   df -h /var/lib/docker
   ```
   If 8765 is taken, pick another port for `AREA_EVENTS_PORT` in step 3.
2. **Copy the code** (from the development machine, in the repo folder):
   ```
   git archive --format=tar.gz -o area-events.tgz HEAD
   scp area-events.tgz robohobo@192.168.1.88:/tmp/
   ssh robohobo@192.168.1.88 "mkdir -p ~/area-events && tar xzf /tmp/area-events.tgz -C ~/area-events"
   ```
3. **Configure.** On the host:
   ```
   cd ~/area-events
   cp .env.example .env        # review: AREA_EVENTS_BIND=192.168.1.88, AREA_EVENTS_PORT=8765
   docker compose config       # validate; shows the resolved ports and names
   ```
4. **Build and start** (always from `~/area-events`):
   ```
   docker compose build
   docker compose up -d
   docker compose ps
   docker compose logs -f --tail 50     # Ctrl+C to stop following
   ```
5. **Verify.** The first refresh takes about 4 minutes; `/health` shows `starting` until the feed exists.
   ```
   curl -s http://192.168.1.88:8765/health
   curl -s http://192.168.1.88:8765/events.json | head -c 400
   docker inspect --format '{{.State.Health.Status}}' area-events
   ```
   Also confirm Frigate is unaffected: `docker ps` still shows its containers up and `docker compose ls` still lists its project.
6. **Point the card at it** (later, when ready), using the card's existing `feed` option:
   ```yaml
   type: custom:area-events-card
   title: San Diego Events
   feed: http://192.168.1.88:8765/events.json
   ```

## Update

```
cd ~/area-events
docker tag area-events:local area-events:previous      # keep the running image for rollback
# copy the new code over ~/area-events as in step 2, then:
docker compose build && docker compose up -d
```

Data in the volume is kept across updates and restarts.

## Rollback

- **Previous image:** `cd ~/area-events && AREA_EVENTS_TAG=previous docker compose up -d --no-build`
- **Stop the service, keep data:** `docker compose down` (the `area-events_data` volume and the image remain)
- **Remove everything this project created:**
  ```
  cd ~/area-events
  docker compose down -v --rmi all       # also deletes the data volume and the image
  cd ~ && rm -rf ~/area-events
  ```
  The Frigate project is never involved. Do not run `docker system prune` or `docker compose down` from Frigate's directory.
- **Back up the data volume (optional):**
  ```
  docker run --rm -v area-events_data:/d -v "$PWD":/b python:3.13-slim tar czf /b/area-events-data.tgz -C /d .
  ```

## Troubleshooting

- `docker compose logs discovery` and `/data/refresh.log` (`docker compose exec discovery tail -50 /data/refresh.log`) show each refresh with per-source counts and errors.
- `/health` `source_errors` lists failing sources; their previous events are kept for 3 days (`retention_days`).
- `unhealthy` with a feed older than 3 hours means refreshes are failing: check outbound DNS/HTTPS from the host.
- Settings: `AREA_EVENTS_INTERVAL_SECONDS` (minimum 300). To change sources, edit `discovery.config.json`, rebuild, and `up -d`.

## Runtime verification record

**Accepted baseline: commit `44d6b65`**, deployed on 2026-10-20 and tagged `area-events-cubi-runtime-accepted-2026-10-20`.

| | |
|---|---|
| Host | MSI Cubi, Debian 13 |
| Compose project / container | `area-events` / `area-events` |
| Deployed commit | `44d6b65` |
| `/health` status | `ok` |
| Unique events in the feed | 227 |
| Discovery sources | all four succeeded (KPBS, San Diego Reader, DoSD, City of San Diego) |
| Refresh duration (first run) | 295.6 s |
| Scheduling | hourly refresh configured |
| Other services | Frigate (separate Compose project) remained healthy |

These figures were reported from the live deployment. Later versions should be compared against this record, and `git checkout area-events-cubi-runtime-accepted-2026-10-20` returns to the accepted code.

### Deployment record: evidence-based deduplication (commit `7aacd4f`), 2026-10-20

| | |
|---|---|
| Deployed commit | `7aacd4f` (replaces `44d6b65`) |
| Method | `git archive` + scp, `docker compose build && docker compose up -d` in `~/area-events` (only the `area-events` container was recreated) |
| `/health` | `ok`; refresh exit code 0 in 125 s (cached details reused) |
| Sources | KPBS 29, San Diego Reader 106, DoSD 57, City of San Diego 40, none failing |
| Feed | 207 merged events (matches the 207-event offline baseline), 24 Verified, 24 cross-source groups (1 with three sources), 162 with images |
| Dedup check | Ocean Beach Restaurant Walk is one event with KPBS, Reader and DoSD URLs, retrieval times and `match_reasons`; every event keeps per-source URL and retrieval time |
| Frigate | `frigate` stayed `Up 5 weeks (healthy)`; its project was not touched |
| Rollback | image `area-events:previous` (the `44d6b65` build) and a data backup at `~/area-events-backups/area-events-data-20261020-121000.tgz` on the Cubi; `AREA_EVENTS_TAG=previous docker compose up -d --no-build` restores the old image, and the `area-events_data` volume was never removed |

## Location control (optional)

Setting `AREA_EVENTS_API_KEY` in `.env` turns on the authenticated `GET`/`PUT /api/v1/location` API that lets Home Assistant switch the one active location; see [location-control.md](location-control.md). The volume gains `active-location.json`, `location-change.json` and (while a change runs) `staging/`; the previous image (`area-events:previous`) ignores them. Removing the key turns the API off; delete `active-location.json` to return to the configured location.

### Deployment record: single-area location control (commit `6fdc21b`), 2026-10-20

| | |
|---|---|
| Deployed commit | `6fdc21b` on the Cubi (replaces `7aacd4f`); card build `AREA-EVENTS 6fdc21b-ae4c3d` via `.\deploy.ps1` |
| Method | `git archive` + scp, then `docker compose build && docker compose up -d` in `~/area-events`; only the `area-events` container was recreated; the API key was added to the protected `.env` (mode 600) |
| Cubi | container healthy; `/events.json` and `/health` unchanged and public; four San Diego sources `ok` (KPBS 29, San Diego Reader 102, DoSD 55, City of San Diego 40); 201 events, 24 Verified; next hourly refresh scheduled (`interval_seconds` 3600) |
| API | no key / wrong key / unauthenticated `PUT` all 401 and changed nothing; authorized `GET /api/v1/location` 200; unknown field 422; invalid place 422 |
| Switching (same URL and Authorization value the Home Assistant `rest_command` uses, read from HA's `secrets.yaml`) | San Diego 201 events, label San Diego, CA -> **Austin** (35 s): 336 events, UT Austin Events only, label Austin, TX, old feed served while it ran -> **Sydney** (no covering source): change `failed` ("no enabled source applies"), Austin feed byte-identical -> **San Diego** (312 s cold): 201 events from the four San Diego sources, none of the Austin events |
| HA copy | after each step the feed was copied to `Z:\www\area-events-card\events.json` with `sync_area_events.py` (the script the HA automation runs); the copy carried the same label and event count as the Cubi |
| Restart | `docker compose restart discovery`: container healthy again, saved place (`active-location.json`, San Diego) and feed intact, startup refresh ok, `/api/v1/location` `source: changed`, change `succeeded` |
| Frigate | `frigate` stayed `Up 5 weeks (healthy)`; its project was not touched |
| Rollback | image `area-events:rollback-7aacd4f` (the `7aacd4f` build) and `area-events:previous`; data backup `~/area-events-backups/area-events-data-pre-6fdc21b-20261020-160839.tgz` and `env-pre-6fdc21b` on the Cubi; HA files backed up in `area-events-card-ha-backups` next to the repository |
| Home Assistant | `configuration.yaml` (`rest_command`), `scripts.yaml` (3 scripts) and `secrets.yaml` (`area_events_authorization`) were installed append-only through `Z:\` and validated as YAML; **not yet loaded by Home Assistant** (a reload is needed), so the card -> script -> server path and the 30-minute sync automation are **unverified** |

`deploy.ps1` copies `data\events.json` to Home Assistant if that file exists locally. Home Assistant's feed comes from the Cubi, so keep no local `data\events.json` when deploying the card.

To finish activation, reload the YAML in Home Assistant (Developer tools > YAML > check configuration, then reload all YAML configuration), run `script.area_events_location` from Developer tools > Actions (expect status 200), and add `location_control: true` to the card ([examples/location-control-card.yaml](../examples/location-control-card.yaml)).
