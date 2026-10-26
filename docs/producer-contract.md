# The standard producer-to-processing interface

```
SOURCE PRODUCERS -> STANDARD RECORDS -> PROCESSING / VALIDATION -> DEDUPLICATION <-> PERSISTENCE (one database) -> PRESENTATION
```

A producer (`src/area_events/discovery/producers/*`, wired in `src/area_events/app.py`) answers `crawl(fetcher, previous, options)` with one immutable, versioned `producer.ProducerResult` (`discovery.producer.v1`) built directly from:

- **`EventCandidate`** (a source's claim, never an event): required `local_id`, `title`, `starts_at` (ISO 8601 with a UTC offset); optional `ends_at`, `timezone`, `venue`, `address`, `description`, `url`, `image_url`, `image_source`, `categories`, `organizer`, `price`, `geo`; `provenance` (source name, listing URL, retrieved-at, every listing that carried it); `verification` (status, evidence lines, missing facts, note, the producer's own `verified` claim); `state`.
- **`SourceLead`** (a page considered while looking for events, with where it was found and why it was accepted or rejected): no event fields; the boundary rejects one sent as an event.
- The records are closed: no extras, no place for source-specific fields. `state` is the producer's own working state (for example which detail pages it has read). It is stored apart from the record (`candidates.state_json`), is not part of the record's identity (`candidate_digest`), is handed back to the same producer as `previous`, and no other module reads it (checked by `tests/test_architecture.py`).

`producer.accept()` is the one validation boundary: only a `ProducerResult` crosses it (anything else, including dictionaries, is a producer defect and fails that job); invalid records are set aside with a reason (`records_rejected` in the run notes) and valid, de-duplicated ones pass. The service stores each accepted candidate as `producer.document()` (a closed, versioned JSON document) with a digest of its comparable facts. Candidates are evidence and checkpoints; only processing (extraction and validation) turns one into an event, so an unvalidated candidate is never canonical.

Processing reads the stored standard document (`starts_at`, `ends_at`, ...). There is no reader for the earlier stored form, no `AdapterResult`, and no conversion between records and event dictionaries. Migration 7 converted existing stored candidates once (a data migration with its own frozen field mapping), carrying completed extraction and media work over to the new identity; it ran on copies of the real database without loss.

## Current producer modules
The service is a standard `src`-layout Python package. Its three registered adapters live in `src/area_events/discovery/producers/`: `CityOfSanDiegoProducer` reads City listings; `LocalistProducer` reads paged Localist JSON APIs; `SourcesProducer` reads configured lists of public websites, bounded relevant links, HTML JSON-LD Events and iCalendar (`.ics`) feeds. All implement the same producer contract and are composed solely in `src/area_events/app.py`. SourcesProducer does not assume an event from generic publication dates or undated prose. Unsupported/unstructured sites yield inspection leads and diagnostics, not fabricated events. No direct persistence, timers or HA behavior is contained in any producer.

- Each producer owns its source: `producers/city_of_san_diego.py` (listing and event-page reading, the "Get Directions" address, and its resumable detail-page queue) or `producers/localist.py` (the Localist API interpretation).
- Shared utilities include `safe` (fetch policy, public-address checks, robots.txt, spacing, limits, and `FetchError`), `timeutil` (Pacific time and clock text), `textutil` (HTML to text and entity decoding), `textloc` (address and place text), `evidence` (locating a quote in a page), and the imagery libraries used by enrichment (`media`, `maps`, `images`, `classify`, `classifiers`).

## Configured producer instances

The implementation type is a registered producer adapter; each row in SQLite `sources` is one configured instance with a stable `source_key`. Multiple rows may use one adapter. The authenticated Control API lists instances at `GET /v1/sources`, creates them with `POST /v1/sources`, updates them with revision-checked `PATCH /v1/sources/{id}`, and enables or disables them through revision-checked `POST /v1/sources/{id}/enable|disable`. Reads accept the read token; mutations require the control token. Configuration is validated in Control using the producer option rules supplied by the composition root. The URL is accepted as `options.url` and stored in the existing `base_url` column; `interval_minutes` is stored as `interval_s`.

The registered City and Localist instances remain the built-ins (`sandiego_gov` and `ut_austin`) and cannot be replaced through the configured-instance API. Startup registration is insert-only, so a service restart does not overwrite persisted settings. Configured instance revisions and source change records are written in the same SQLite transaction; the existing scheduler observes them and updates durable jobs without changing candidate keys or deleting candidate data.

## Checks
`tests/test_architecture.py` evaluates `violations(root)` on the real import graph (AST): neutral purity, producers importing only their allowed contracts and utilities, producer independence, persistence boundaries, composition-root registration, reviewed service layering, cycles, dynamic imports, source-specific logic outside producers, and reads of a producer's working state. A second group copies the tree, injects each kind of violation, and requires the rule to fire. `tests/test_producer_contract.py` runs both registered producers and an independent substitute through the same service, extraction, API and feed.

## Remaining dependencies and violations (later steps)
1. Producers and known sources are wired in `app.py`.
2. (Resolved in step 4: deduplication is its own module behind an interface, and Persistence owns all database mechanics; see deduplication-and-persistence.md.)
3. The Home Assistant feed adapter has a per-source display-name table and drops categories; the card classifies by keywords.
4. The candidate read API (`CandidateRecord.payload`) serves the standard document under the unchanged `discovery.v1` label.
