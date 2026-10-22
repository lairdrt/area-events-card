# Discovery architecture: source files to review

Read [discovery-service-architecture.md](discovery-service-architecture.md) first. These are the files a reviewer should inspect before the Discovery Service is implemented. Line counts at commit `881ad8c`. Every file listed is included in the review package (`review/architecture/discovery-review.zip`) at its repository-relative path.

## Essential (11)

| Path | Responsibility | Lines | Why it matters |
|---|---|---|---|
| `area_events/discovery/engine.py` | profile lifecycle, the single discovery worker, scheduling (`tick`, `run_next`, `_run`), imagery loop start, API snapshots | 608 | where discovery is driven by card profiles today, where runs, persistence (JSON per profile) and the imagery loop meet; the main coupling to remove |
| `area_events/discovery/refresh.py` | one refresh run: fetch sources, normalize, locate, merge, classify, publish | 217 | discovery and processing in one synchronous call; `build_document` is what processing will reuse |
| `area_events/discovery/registry.py` | source registry, coverage, adapter selection | 146 | the seed of the `sources` table and the source identity rules |
| `area_events/discovery/safe.py` | SafeFetcher: public addresses only, robots.txt, per-host spacing, limits; module-global `_STATE` | 259 | the fetch safety that must be kept; the global host state that must become an injected `HostPolicy` |
| `area_events/discovery/sources/web.py` | generic website crawl and candidate → event conversion; module-global crawl cache | 526 | the page traversal to be turned into jobs; candidate format; budgets; a module-level cache to remove |
| `area_events/discovery/sources/detailwork.py` | resumable detail-page queue (reuse, backoff, versions) stored in events | 103 | the retry/backoff model that maps onto the `jobs` table |
| `area_events/discovery/sources/sandiego_gov.py` | City of San Diego adapter (listing + detail + address rule) | 169 | a representative registry adapter: what a `crawl_source` job must wrap |
| `area_events/discovery/server.py` | the app's HTTP API, token, settings, process start | 233 | the composition point today (`main`), the API the integration relies on, the token used for a remote Discovery API |
| `area_events/discovery/classifiers.py` | replaceable classifier contract; module-global `_current` | 102 | the contract pattern to follow, and a global to replace by injection |
| `area_events/discovery/core.py` | `Fetcher`, `make_event`/`finish_event`, text normalization | 264 | the event record schema and the registry fetcher |
| `custom_components/area_events/hub.py` | integration: one record per card profile, polling the app | 138 | how cards reach events today; what must stay byte-compatible |

## Supporting (only where needed)

| Path | Responsibility | Lines | Why it matters |
|---|---|---|---|
| `area_events/discovery/media.py` | geographic resolution, imagery, `MediaStore`, `MediaService` worker | 910 | an existing independent worker with its own store and rate limits; processing-side model |
| `area_events/discovery/profiles.py` | profile normalization and ids | 162 | profile → interest region mapping |
| `area_events/discovery/evidence.py` | evidence reference structure | 52 | the `evidence` table's shape |
| `area_events/discovery/workbench.py` | diagnostic runner over production calls, `RecordingClient` | 540 | the recording-fetcher pattern for `crawl_source` jobs; future discovery inspector |
| `area_events/discovery/sources.json` | registry configuration | 89 | the sources to seed |
| `custom_components/area_events/websocket_api.py` | card WebSocket command | 59 | the card request shape (`area`, `radius_miles`, `sources`, `since`) |
| `custom_components/area_events/client.py` | integration HTTP client to the app | 94 | transport, token and error mapping that a remote reader can mirror |
| `area_events/config.yaml`, `area_events/Dockerfile` | app options, base image (Python 3.14, Alpine) | 27, 12 | where new options go; standard library only (`sqlite3` must be verified in the container) |
| `docs/architecture-modules.md` | milestone 1 module map | 185 | the module vocabulary this design extends |
| `tests/test_modules.py` | classifier contract and interchange tests | 142 | the contract-test pattern for new interfaces |
| `tests/test_site_discovery.py` | detail queue, discovery limits and SSRF-safe link tests | 447 | the behaviour the job system must preserve |

Excluded on purpose: credentials and tokens (none are in these files), Home Assistant configuration, any database, downloaded media, caches, `review/` data, the card bundle and unrelated modules.
