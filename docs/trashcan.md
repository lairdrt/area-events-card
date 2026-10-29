# Trashcan (v1)

A Home Assistant administrator can delete an unwanted event from the card for good. The Discovery Service keeps a record of it, and rediscovery cannot bring it back.

## Using it

- On the card, an administrator sees a trashcan at the bottom-right of each tile when hovering that corner (on a touch screen it is always faintly shown; it is a real button, so Tab reaches it). Other users never see it; nor do demonstration (inline) events, nor an integration without a control token.
- The dialog asks **Delete this event?** with one required reason: Duplicate event, Not relevant to my area, Incorrect or misleading information, Expired or canceled event, Advertisement / not a real event, Other / don't want to see it. **Cancel** and **X** (and Escape) write nothing. **Delete** waits for the service: the tile goes only once the event is deleted; a failure is shown in the dialog and the tile stays.
- The management page has a **Trashcan** section: every deleted event (title, date, source, reason, when it was deleted, how often rediscovery was suppressed), the total, counts by reason and suppressed rediscoveries. V1 has no restore and no bulk delete.

## The path of a deletion

card -> `area_events/event_delete` (WebSocket; Home Assistant administrator only) -> hub -> `POST /v1/events/{key}/trash` (control token only; `discovery.trashcan.v1`) -> `persistence/trashcan.py`.

One write transaction (`SqliteTrashcan.trash`):

1. the trashcan record is saved: the event's own columns, its candidates' standard records and its revision history (no picture bytes, no media decisions), with the normalized facts rediscovery is compared with;
2. every candidate of the event is removed with what only it owns (evidence, extractions, processing work, media decision, association), then the event and its revisions;
3. shared rows stay: pages, page contents, the photograph cache, sources and every other event;
4. the usual change records follow (`candidate` deletes, `event.removed`), so the API, the change feed and every card agree.

An event whose candidates' extractions are part of another event's history is refused (409 `shared_history`) and nothing is written. Deleting an event already in the trashcan answers `already_deleted` and changes nothing. The foreign-key check runs inside the transaction.

`GET /v1/trashcan` (read token) returns the records and totals.

## Rediscovery

The shared crawl storage (`SqliteCrawlStorage.record_run`, which every producer's results pass through) asks the trashcan about each candidate it has not stored before, BEFORE storing it. A match is counted on the trashcan record (`suppressed_count`) and in the run report (`candidates_suppressed`), and nothing of the candidate is stored, so no event, extraction, photograph or processing work can follow. No producer has a rule of its own.

The rule (`area_events/trashcan`, pure and deterministic, like deduplication):

- the candidate comes from a source that supplied the deleted event (the immutable source key);
- the source's own identity for it (`candidate_key`) within a day of a deleted occurrence is that occurrence (a corrected time keeps its key); otherwise
- it starts at exactly a deleted occurrence's start, and its title is one of the deleted titles once case, punctuation, accents, `&`/`and` and spacing are set aside; a title one letter away (titles of eight characters or more) also needs the deleted event page or the deleted venue or address.

A shared date, time, venue or listing page alone never matches, and another year's occurrence never starts at the same instant.

## Database

Migration 10 adds `trashcan` (one row per deleted event key: title, start, end, reason, record, match facts, deletion time, suppressed count) and `trashcan_occurrences` (source key and start of each deleted occurrence, the lookup index).
