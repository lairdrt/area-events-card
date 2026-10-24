# Event processing, deduplication and persistence (refactor step 4)

```
INDEPENDENT SOURCE PRODUCERS -> STANDARD RECORDS -> EVENT PROCESSING (extract / normalize / validate) -> DEDUPLICATION <-> PERSISTENCE <-> ONE SQLITE DATABASE -> PRESENTATION
```

Three capabilities, three modules, each replaceable without touching the others.

## Event processing (`service/processing.py`, `service/extraction.py`)
Owns extraction, normalization and validation. A worker extracts an event from a stored candidate; `ProcessingService.validate` turns its result into a `CleanEvent` (title, start, end, place text, derived location status, missing facts, completeness). It holds no SQL and imports no storage: it asks the `ProcessingStorage` port to lease work and to accept a validated result.

## Deduplication (`service/deduplication/`, interface `discovery.dedup.v1`)
Owns every matching rule and nothing else. `Deduplicator.decide(Incoming, EventLookup) -> Decision`:

| | |
|---|---|
| `Incoming` | the candidate key, the `CleanEvent` it produced, and its `Association` (the event it already describes and whether it is primary or secondary), or none |
| `EventLookup` | read-only, valid for one call: keyset-paged `starting_between(earliest, latest, after, limit)`, answering with plain `ExistingEvent` facts and its stable `event_key` |
| `Decision` | `create` (new event), `update` (the primary description revises its event), `link` (a secondary description of an existing event), `keep` (already secondary: nothing changes) |

Strategies: `exact` (same normalized title and start instant, plus matching stated address or location text) and `tolerant` (same title, start within a window, with matching address, venue or location evidence and no contradictory stated place facts). Exact matching requires comparable address/location context; tolerant matching can use the same stated venue when addresses are missing. When both records give an end time, exact requires equality and tolerant rejects differences larger than its time window. Missing or conflicting place facts do not trigger a merge. Both strategies page through the full indexed time window; there is no result cap. They import neither SQLite nor Persistence nor any producer. Choosing one is the composition root's argument: `build(..., deduplication="exact" | "tolerant" | <any Deduplicator>)`, or `--deduplication` on the command line. Matching reads event facts directly; `event_key` remains stable and strategy changes do not require rebuilding identities.

## Persistence (`service/persistence/`)
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

No connection, cursor or transaction crosses a port, and no port takes one. Only `service/app.py` builds Persistence (`persistence.build.open_persistence`) and hands it the deduplication strategy.

### How atomic operations are guaranteed
`ProcessingStorage.submit_result` is ONE write transaction on the single write connection (`BEGIN IMMEDIATE`, serialized across threads by a lock and across processes by SQLite): it checks the lease and that the candidate still has the leased payload hash (an older revision's result is refused and releases the work), stores the extraction, builds a **lookup bound to that same transaction**, calls the strategy, **validates the decision** against the candidate's state (`create` and `link` need a candidate with no event; `update` only for the primary of the named event; `keep` only for its secondary; `link` needs an existing event; anything else is `DecisionRejected`), applies exactly that decision, marks the work done and appends the change record. The strategy gets the lookup and the incoming facts; it never sees the transaction. A second submission cannot start its lookup until the first has committed, so two twins can never both be new. Any failure (strategy error, rejected decision, storage error) rolls everything back, the work stays leased and can be retried; an identical retried result is acknowledged again (`replayed`) without a second link.

## What a new strategy, producer or storage costs
- A new matching algorithm: one class implementing `Deduplicator` and its registration in `app.DEDUPLICATORS`. No change to Persistence, processing, producers or presentation.
- A new producer: its package and its registration (see producer-contract.md).
- A different Persistence for processing: implement `ProcessingStorage` (`tests/inmemory.py` does, over dictionaries, and runs the whole extraction and deduplication path without importing a database).

## Enforced by `tests/test_architecture.py`
sqlite3, SQL text and database calls only inside Persistence; no module outside Persistence imports a concrete storage class or anything of Persistence but its ports (only the root builds it); Persistence imports the deduplication interface, never a strategy, and contains no matching logic; deduplication imports no database, no Persistence and no producer; ports take no transaction; plus the earlier rules (producer isolation, no legacy engine, reviewed layering, cycles, source names, producer state). Each rule is exercised by deliberately injecting a violation into a copy of the tree.

## Remaining violations
1. The earlier engine and its tests remain in the repository (not part of the new application).
2. Producers and known sources are wired by hand in `app.py` / `cli.py`.
3. Persistence imports `service/events.py` for the event record types (records only); they belong in `contracts.py`.
4. Deduplication is invoked once per submission; there is no batch interface.
5. Some tests reach into private attributes (`_store`, `_store_plan`, `_candidates`) to inject storage faults, and inspect the database file directly with sqlite3; production code never does.
6. The Home Assistant feed adapter still has a per-source display-name table and drops categories; the card classifies by keywords.
