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

## Checks
`tests/test_architecture.py` reads the real import graph: only the composition root imports a producer; producers import only the contract, the fetch client and URL identity; nothing downstream imports source reading code or the legacy event builders; the contract is a leaf; no cycles; no `AdapterResult`; no reader of producer state outside the repository; no module outside the producers and the composition root names a source. `tests/test_producer_contract.py` shows an independent producer (complete, partial, invalid, duplicate, lead, working state, failure) replacing a real one with no change downstream.

## Remaining violations (later steps)
1. The accepted imagery library (`media.py`, `place.py`, `images.py`, `classify.py`) takes its own dictionary interface. `enrichment._imagery_input` builds it from the standard record's fields in one place; it is an input for a neighbour not yet moved to records, not a stored form.
2. The earlier engine (`engine`, `registry`, `server`, `workbench`, `sources/*.discover`, `core.make_event`) is still a separate pipeline of event dictionaries. The service producers use only the sources' page-reading functions, including some private ones (`sandiego_gov._parse_listing`, `localist._geo`), which couples them to modules the legacy engine also owns.
3. Producers and known sources are wired by hand in `app.py` (and `cli.py`).
4. Deduplication is exact (title, start, street address) and lives in event persistence (`EventRepository.apply`), not in a module of its own.
5. The Home Assistant feed adapter has a per-source display-name table and drops categories; the card classifies by keywords.
6. The candidate read API (`CandidateRecord.payload`) now serves the standard document under the unchanged `discovery.v1` label.
