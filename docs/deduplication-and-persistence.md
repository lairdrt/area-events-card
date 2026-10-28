# Event processing, deduplication and persistence (refactor step 4)

```
INDEPENDENT SOURCE PRODUCERS -> STANDARD RECORDS -> EVENT PROCESSING (extract / normalize / validate) -> DEDUPLICATION <-> PERSISTENCE <-> ONE SQLITE DATABASE -> PRESENTATION
```

Three capabilities, three modules, each replaceable without touching the others.

## Event processing (`src/area_events/validation/processing.py`, `src/area_events/extraction/`)
Owns extraction, normalization and validation. A worker extracts an event from a stored candidate; `ProcessingService.validate` turns its result into a `CleanEvent` (title, start, end, place text, derived location status, missing facts, completeness). It holds no SQL and imports no storage: it asks the `ProcessingStorage` port to lease work and to accept a validated result.

## Deduplication (`src/area_events/deduplication/`, interface `discovery.dedup.v1`)
Owns every matching rule and nothing else. `Deduplicator.decide(Incoming, EventLookup) -> Decision`:

| | |
|---|---|
| `Incoming` | the candidate key, the `CleanEvent` it produced, and its `Association` (the event it already describes and whether it is primary or secondary), or none |
| `EventLookup` | read-only, valid for one call: keyset-paged `starting_between(earliest, latest, after, limit)`, answering with plain `ExistingEvent` facts and its stable `event_key` |
| `Decision` | `create` (new event), `update` (the primary description revises its event), `link` (a secondary description of an existing event), `keep` (already secondary: nothing changes) |

Strategies (version 5): `exact` and `tolerant` are two `MatchProfile`s of one weighted, confidence-based comparison (`deduplication/scoring.py`, RapidFuzz `==3.14.6`, a pinned core dependency installed by the deployment). Each field both events state is scored 0..1; the confidence is the weighted mean of the compared fields, so a missing field neither helps nor hurts. Weights favour start time (0.30), calendar date (0.10) and place (address 0.15, venue 0.10, location text 0.05), then title (0.20), description (0.05) and end time (0.05). Hard vetoes come first and are never outvoted: a start or end beyond its tolerance, conflicting source pages (exact), a title/address/venue below its floor, differing numbers, numerals or single letters ("Night 1"/"Night 2", "Concert"/"Concert II", "Hall A"/"Hall B"), no comparable place fact that agrees, or too little comparable evidence. All of this is configurable per profile: weights, `start_tolerance_s`, `end_tolerance_s`, `time_decay`, `min_title`, `min_title_same_page`, `min_address`, `min_venue`, `url_conflict_vetoes`, `threshold`, `min_coverage`, `location_agreement`, `venue_alone_agrees`, `ambiguity_margin` and `max_compared` (`ExactMatch(threshold=0.95)`). `exact`: same start instant, address or location text must agree (a venue alone is not enough), threshold 0.92. `tolerant`: start within 10 minutes and ends within 15, lower floors, threshold 0.85, venue alone may be place evidence, pages need not agree. A decision scores every known event inside the start tolerance (bounded keyset pages, at most `max_compared` events), links the single best match, and CREATES instead when two matches are about equally good (ambiguous) or the window is too crowded to certify one: never merging is the safe error. `ExistingEvent` also exposes `description` and `timezone` (the date is judged in the event's own zone). Events that state NO place on either side match only on an exceptionally strong identity (`no_place_title_min` 0.97, default on in both profiles, `no_place_description_min` 0.80): identical start (and end), near-identical titles, agreeing descriptions when both exist, and every veto still applies; an event with a place never matches one without. A trailing field label that a listing scraped into a title ("... Date", "... Date: Sat.", "... Date & Time") is ignored when titles are compared, and a maintenance scan buckets place-less events by start and title.

### Optional duplicate maintenance extension

The original `discovery.dedup.v1` contract **still requires only** `name`, `version`, and `decide(Incoming, EventLookup)`. A third-party strategy implementing only those members remains fully valid for normal event processing.

Stored-event scanning is a separate optional `discovery.dedup.maintenance.v1` capability (`MaintenanceMatcher`, extending `Deduplicator`): `matches(ExistingEvent, ExistingEvent) -> bool` and `anchors(ExistingEvent) -> tuple`. The built-in exact/tolerant strategies provide both interfaces. The maintenance entry point fails with a clear message if the selected strategy lacks the extension; it does not change normal processing behavior. No event/schema migrations are required.

## Persistence (`src/area_events/persistence/`)
Exclusively owns the database: connections, transactions, SQL, schema, event/candidate links, revision history, evidence and work records. The rest of the service sees only `persistence/ports.py`:

| port | used by | what one call does |
|---|---|---|
| `CrawlStorage` | `DiscoveryService` | claim a job; read a source's previous records; **record a whole run** (lease check, pages, contents, candidates, evidence, leads, changes, source and job state) in one transaction; release a failed job; retention |
| `SchedulingStorage` | `DiscoveryScheduler` | synchronize jobs with the configuration under a pure policy; paused?; next due |
| `ControlStorage` | `LocalDiscoveryControl` | areas and settings: reads and revision-checked commands, each with its change record |
| `ProcessingStorage` | `ProcessingService` | lease work; **accept a validated result**; fail work; status |
| `EventQueries` | `EventQueryService` | read normalized events |
| `DiscoveryReader` (contracts) | API, CLI | changes, snapshot, candidates, pages, status |
| `MediaWork` | enrichment | pending candidates in, finished media out |
| `MediaCache` | imagery resolver and source-photo enrichment | copy-in/copy-out record operations, cached files, pruning and persistence; no mutable cache index escapes |

No connection, cursor or transaction crosses a port, and no port takes one. Only `src/area_events/app.py` builds Persistence (`persistence.build.open_persistence`) and hands it the deduplication strategy.

### How atomic operations are guaranteed
`ProcessingStorage.submit_result` is ONE write transaction on the single write connection (`BEGIN IMMEDIATE`, serialized across threads by a lock and across processes by SQLite): it checks the lease and that the candidate still has the leased payload hash (an older revision's result is refused and releases the work), stores the extraction, builds a **lookup bound to that same transaction**, calls the strategy, **validates the decision** against the candidate's state (`create` and `link` need a candidate with no event; `update` only for the primary of the named event; `keep` only for its secondary; `link` needs an existing event; anything else is `DecisionRejected`), applies exactly that decision, marks the work done and appends the change record. The strategy gets the lookup and the incoming facts; it never sees the transaction. A second submission cannot start its lookup until the first has committed, so two twins can never both be new. Any failure (strategy error, rejected decision, storage error) rolls everything back, the work stays leased and can be retried; an identical retried result is acknowledged again (`replayed`) without a second link.

## What a new strategy, producer or storage costs
- A new matching algorithm: one class implementing `Deduplicator` and its registration in `app.DEDUPLICATORS`. No change to Persistence, processing, producers or presentation.
- A new producer: its package and its registration (see producer-contract.md).
- A different Persistence for processing: implement `ProcessingStorage` (`tests/inmemory.py` does, over dictionaries, and runs the whole extraction and deduplication path without importing a database).

## Duplicate maintenance

`area-events duplicates scan` opens the existing database with SQLite URI `mode=ro`, does not initialize the service or run migrations, and reports likely future-event groups. `area-events duplicates clean` previews the scan. Applying requires an external verified backup, an explicit maximum group limit, and `--group EVENT_KEY,EVENT_KEY` selections. Persistence asks the injected deduplication strategy to match selected pairs while holding one `BEGIN IMMEDIATE` transaction, preserves candidate links, reassigns extraction and revision history to a deterministic surviving event, adds a reconciliation revision, publishes event upsert/delete changes, and checks foreign keys before commit. Larger ambiguous match components are reported but cannot be applied automatically.

## Enforced by `tests/test_architecture.py`
sqlite3, SQL text and database calls only inside Persistence; no module outside Persistence imports a concrete storage class or anything of Persistence but its ports (only the root builds it); Persistence imports the deduplication interface, never a strategy, and contains no matching logic; deduplication imports no database, no Persistence and no producer; ports take no transaction; plus producer isolation, reviewed layering, cycles, source names and producer-state boundaries. Each rule is exercised by deliberately injecting a violation into a copy of the tree.

## Remaining design notes
1. Producers and known sources are wired by hand in `app.py`.
2. Persistence imports `api/events.py` for the event query record type (records only); shared records belong in `contracts/models.py`.
3. Deduplication is invoked once per submission; there is no batch interface.
4. Some tests reach into private attributes (`_store`, `_store_plan`, `_candidates`) to inject storage faults, and inspect the database file directly with sqlite3; production code never does.
5. The Home Assistant feed adapter still has a per-source display-name table and drops categories; the card classifies by keywords.
