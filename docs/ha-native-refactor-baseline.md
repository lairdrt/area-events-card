# Baseline before the Home Assistant native-integration refactor (2026-10-20)

Work on the refactor happens on the `ha-native-refactor` branch. This records what to return to.

## Source versus deployment

| | Version | Notes |
|---|---|---|
| Source (`main`) | `f7f86b4`, tag `area-events-pre-ha-refactor-2026-10-20` | Includes YAML-driven location (`area`, `radius_miles`), the card's duplicate-request handling and the idempotent location API. |
| Deployed on the Cubi | `6fdc21b` (image `area-events:local`, built 2026-10-20 16:08, container healthy) | Does **not** yet include the backend change in `f7f86b4` (identical `PUT /api/v1/location` requests joining a running change). |
| Home Assistant card | Deployed from `main` with `.\deploy.ps1`; independent of the Cubi image. | |

The earlier tag `area-events-cubi-runtime-accepted-2026-10-20` is unchanged.

## Rollback assets (checked 2026-10-20, read-only)

On the Cubi (`robohobo@192.168.1.88`, `~/area-events`):

- Images: `area-events:local` (running), `area-events:rollback-7aacd4f` (the `7aacd4f` build), `area-events:previous`.
- Data backups in `~/area-events-backups/`: `area-events-data-20261020-121000.tgz`, `area-events-data-pre-6fdc21b-20261020-160839.tgz`, and `env-pre-6fdc21b` (protected, mode 600; never copy or print it).
- Roll back an image: `cd ~/area-events && AREA_EVENTS_TAG=rollback-7aacd4f docker compose up -d --no-build`. The `area-events_data` volume is kept. See [docker-deployment.md](docker-deployment.md).

Home Assistant YAML backups are in `area-events-card-ha-backups` next to the repository (timestamped `20261020-114212` and `20261020-160838`): `configuration.yaml`, `automations.yaml`, `scripts.yaml` and `secrets.yaml`. The secrets backup contains the API key, so it stays outside Git.

To return to this baseline in source: `git checkout area-events-pre-ha-refactor-2026-10-20`.
