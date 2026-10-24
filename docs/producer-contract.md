# The standard producer-to-processing interface

```
SOURCE PRODUCERS -> STANDARD RECORDS -> PROCESSING / VALIDATION -> DEDUPLICATION <-> PERSISTENCE (one database) -> PRESENTATION
```

A producer (`service/producers/*`, wired in `service/app.py`) answers `crawl(fetcher, previous, options)` with one immutable, versioned `producer.ProducerResult` (`discovery.producer.v1`) built directly from:

- **`EventCandidate`** (a source's claim, never an event): required `local_id`, `title`, `starts_at` (ISO 8601 with a UTC offset); optional `ends_at`, `timezone`, `venue`, `address`, `description`, `url`, `image_url`, `image_source`, `categories`, `organizer`, `price`, `geo`; `provenance` (source name, listing URL, retrieved-at, every listing that carried it); `verification` (status, evidence lines, missing facts, note, the producer's own `verified` claim); `state`.
- **`SourceLead`** (a page considered while looking for events, with where it was found and why it was accepted or rejected): no event fields; the boundary rejects one sent as an event.
- The records are closed: no extras, no place for source-specific fields. `state` is the producer's own working state (for example which detail pages it has read). It is stored apart from the record (`candidates.state_json`), is not part of the record's identity (`candidate_digest`), is handed back to the same producer as `previous`, and no other module reads it (checked by `tests/test_architecture.py`).

`producer.accept()` is the one validation boundary: only a `ProducerResult` crosses it (anything else, including dictionaries, is a producer defect and fails that job); invalid records are set aside with a reason (`records_rejected` in the run notes) and valid, de-duplicated ones pass. The service stores each accepted candidate as `producer.document()` (a closed, versioned JSON document) with a digest of its comparable facts. Candidates are evidence and checkpoints; only processing (extraction and validation) turns one into an event, so an unvalidated candidate is never canonical.

Processing reads the stored standard document (`starts_at`, `ends_at`, ...). There is no reader for the earlier stored form, no `AdapterResult`, and no conversion between records and event dictionaries. Migration 7 converted existing stored candidates once (a data migration with its own frozen field mapping), carrying completed extraction and media work over to the new identity; it ran on copies of the real database without loss.

## Independence from the earlier engine (step 3)
The new application stands on its own. `service/` and the neutral modules it uses never import, directly or through any chain of imports, the earlier engine (`core`, `engine`, `registry`, `server`, `workbench`, `supervisor`, `profiles`, `refresh`, `publish`, `merge`, `location`) or its source readers (`sources/`). The dependency runs the other way where it exists: the earlier engine's City and Localist adapters import the producers' page-reading functions, so each source's reading logic has one owner, its producer.

- Each producer owns its source: `producers/city_of_san_diego.py` (listing and event-page reading, the "Get Directions" address, the resumable detail-page queue), `producers/localist.py` (the Localist API interpretation), `producers/craft_fairs/` (the bounded acquisition, card and organizer-page reading, and its own small page reader).
- Neutral utilities, kept because they are generic and belong to no engine or source: `safe` (the fetch policy: public addresses, robots.txt, spacing, limits; also `FetchError`), `timeutil` (Pacific time without tzdata, clock-time text), `textutil` (HTML to text, entity decoding), `textloc` (address and place text), `evidence` (locating a quote in a page), and the imagery libraries used by enrichment (`media`, `maps`, `images`, `classify`, `classifiers`). `core` is now only the earlier engine's plumbing; it re-exports the utilities it used to hold.

## Checks
`tests/test_architecture.py` evaluates `violations(root)` on the real import graph (AST; comments and names prove nothing): new-to-legacy and transitive legacy dependencies, neutral purity, producers importing anything but the contract, the fetch client, URL identity and neutral utilities, producers depending on each other or reaching persistence (imports, `sqlite3`, database calls), only the composition root naming a producer, a reviewed layering of the service modules, cycles, imports by name at run time, legacy event builders, any source named in processing, deduplication, persistence or neutral code, and reads of a producer's working state. A second group copies the tree, injects each kind of violation, and requires the rule to fire. `tests/test_producer_contract.py` runs the real output of every producer and an independent one through the same service, extraction, API and feed.

## Remaining dependencies and violations (later steps)
1. The imagery libraries (`media`, `maps`, `images`, `classify`, `classifiers`) are shared with the earlier engine and take their own dictionary input; `enrichment._imagery_input` builds it from the standard record's fields in one place.
2. The earlier engine and its tests are still in the repository. It imports the producers' reading functions for City and Localist, keeps its own `DetailWork` and event dictionaries (`core.make_event`), and is not deployed with the service. Retiring it is a separate decision.
3. Producers and known sources are wired by hand in `app.py` (and `cli.py`).
4. (Resolved in step 4: deduplication is its own module behind an interface, and Persistence owns all database mechanics; see deduplication-and-persistence.md.)
5. The Home Assistant feed adapter has a per-source display-name table and drops categories; the card classifies by keywords.
6. The candidate read API (`CandidateRecord.payload`) serves the standard document under the unchanged `discovery.v1` label.
