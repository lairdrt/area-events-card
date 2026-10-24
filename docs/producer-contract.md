# Standard producer contract (refactor step 1)

`Source -> Adapter -> Standard Records`. Every producer answers `crawl(fetcher, previous, options)` with one immutable, versioned envelope, `producer.ProducerResult` (`discovery.producer.v1`), holding:

- **`EventCandidate`**: `local_id`, `title`, `starts_at` (ISO 8601 with UTC offset) are required; `ends_at`, `timezone`, `venue`, `address`, `description`, `url`, `image_url`, `image_source`, `categories`, `organizer`, `price`, `geo` are optional and stay empty when the source did not say them; `provenance` (source name, listing URL, retrieved-at, every listing that carried it); `verification` (status, evidence lines, missing fields, note, the producer's own `verified` claim); `private` (the producer's working state, opaque to everything downstream). A candidate is a source's claim, not a validated event.
- **`SourceLead`**: `url`, `kind`, `decision` (accepted/rejected/pending), `reasons`, `score`, `found_on`, `link_text`, `events`, `fetched`. It has no event fields and is rejected if sent as a candidate.
- `notes`: scalar facts about the run.

`producer.accept()` is the one boundary the Discovery Service applies: it checks the envelope and every record, sets invalid ones aside with a reason (`records_rejected` in the run notes) and passes on only valid, de-duplicated ones. `EventCandidate.to_payload()` is the one compatibility boundary to the stored candidate: byte-for-byte the document stored before (stored data, payload hashes and extraction results are unchanged). The earlier `AdapterResult` (event dictionaries) is still accepted, converted by the same function.

The City of San Diego, Localist and craft-fair adapters conform; `ProducerResult.from_events` is the only place their reading code's dictionaries become records. `tests/test_producer_contract.py` proves it, and shows an independent producer written only against the contract substituting for a real one with no downstream change.

## Violations that remain (for later steps)
1. Extraction and media enrichment still read the stored legacy dictionary (`payload`), not records; enrichment rebuilds a "legacy event" from it.
2. The reading code (`sources/*.py`, `core.make_event`) still builds source-shaped event dictionaries; producers get their `previous` state back as stored dictionaries.
3. `AdapterResult` is still accepted by the boundary (for producers and tests not yet moved).
4. Adapters and known sources are wired by hand in `service/app.py`; `page_role` is per adapter; the service's evidence search knows `found_on` and `listing_url`.
5. The Home Assistant feed adapter keeps a per-source display-name table and drops categories; the card classifies by keywords.
6. Deduplication is exact (title, start, street address) and runs inside event persistence; leads exist only for the acquisition producer.
7. The earlier engine (`registry`, `workbench`, `web.crawl`) is a separate pipeline with its own formats.
