# Reviewed reconciliation and the controlled release

## The operation

`discovery duplicates reconcile --db DB --manifest FILE [--apply --backup-dir DIR]` reconciles **only the event pairs a reviewed manifest names**. It is not the automatic duplicate cleanup (`duplicates clean`, unchanged and still matcher-driven) and not a force-merge: it never searches for pairs, never consults the matcher, and refuses the whole manifest unless **every** pair is proven from the stored records.

Manifest (`discovery.reconciliation.v1`): `source_key`, optional `expect_events_after`, and `pairs` of full event keys `{label, survivor, remove}`. An event key may appear once. The operator chooses the survivor; the tool checks it.

Proof required per pair (preview prints it):

- both events exist and every candidate of both belongs to the manifest's source, all found on one listing page (`provenance.listing_url`);
- each event has exactly one primary candidate and a complete revision history;
- the original listing evidence: the heading the producer recorded for the removed event's primary candidate on the shared listing page is the survivor's recorded heading there followed only by date/time label words (`Date`, `Date & Time`, `Date: Sat.`, `Date: Thurs., Sept. 17 to Sat.`); a primary with no recorded heading refuses the pair;
- every title of the removed event likewise extends a verified survivor title: its current title, its candidates' titles and listing headings, or an earlier title its own revision history records (an event page may have renamed it, e.g. `&` to `and`, or added quotation marks). Titles compare by words: case, punctuation and quotation marks are ignored and `&` reads as `and`; there is no fuzzy matching;
- the primary candidates state the same calendar day;
- a different start or end on the removed event is accepted only if the **survivor's own revision history records the correction from that value** (the corrected Valley Center Dec 12 time);
- the survivor loses no fact: an end, place, address or description only the removed event states refuses the pair, as does a conflicting place.

Applying (one transaction, one verified backup first, rolled back whole on any mismatch): the survivor keeps its event key and every fact; the removed event's candidates become `duplicate` associations of the survivor (the survivor's primary stays primary); its extractions and revisions move to the survivor (appended after the survivor's own, which are untouched); one final revision records `reconciled_from`; the removed event is deleted; change records (`event.reconciled` upsert for the survivor, delete for the removed key) reach the API and card. A second run reports `already reconciled` and writes nothing. `expect_events_after` refuses a manifest that would not leave exactly that many events.

The Bates manifest is `docs/maintenance/bates-2026-10-reconciliation.json` (sha256 `785531b3…6adfe`): 13 pairs. `expect_events_after` is 481: production had 494 events on 2026-10-29 after the depth-2 scans added four (see below), so 494 → 481. (Applied 2026-10-29; other producers have added events since, so this manifest's absolute count no longer previews. Its pairs report `already reconciled` without it.)

### Second proof: one occurrence on two listing pages (`"proof": "shared_event_page"`)

A pair that names this proof is the same occurrence listed on two of the source's listing pages, both cards linking to one event page (the depth-2 scans read the Bates home page as well as its events page). The listing-page and label-word steps above are replaced by all of:

- every candidate of the removed event, and the survivor's primary, names the same event page (payload `url`), lists it in `found_on`, and records `Facts read from the event page <url>`; the event page is no listing page;
- the original listing evidence: the removed event's primary also recorded a heading **on the survivor's listing page**, and it equals the survivor's heading there word for word;
- every title and every recorded heading of the removed event equals a verified survivor title word for word (no added words, no fuzzy match);
- start and end are identical, and no other event of the source names that event page with that start (otherwise the pair is ambiguous and refused).

The other requirements (ownership, one primary each, complete histories, same day, no fact loss) and the apply behaviour are unchanged. A title that differs in wording (Alias: "Alias | Live at Bates on Halloween" vs "Alias | Live Music") or a card that recorded no event page is refused by both proofs.

`docs/maintenance/bates-2026-10-shared-event-page.json` (sha256 `44534f09…58fb3`): two pairs, Halloween Finale and The 2026 Pumpkin Patch (Oct 31). It has no `expect_events_after`: the other producers change the total every hour. The gate is the preview: exactly these two pairs `ready`, and `events N -> N-2`. Release, once authorized: preview on production; apply with `--apply --backup-dir /home/robohobo/backups/area-events-maintenance`; rerun (both `already reconciled`); verify the two keys return 404, the survivors keep their facts, every candidate and association remains, and the card shows one entry for each.

## Release sequence

Run from the repository root on `main` after review. Production is untouched until step 3.

1. **Confirm**: `git status` clean; on the reviewed commit; `git log -1`.
2. **Test and preflight**: full suite (`python -m unittest discover -s tests`), then `scripts/deploy-area-events.ps1 -DryRun` (stops before any write).
3. **Backup before any write**: the deployment takes its own SQLite API backup and code archive (`/home/robohobo/backups/area-events-deploy/<timestamp>`). Additionally take a manual one to a directory outside the application tree: `python -m area_events.cli sources --db data/discovery/discovery.db reconcile-names --apply --backup-dir /home/robohobo/backups/area-events-maintenance` also produces one (step 5).
4. **Merge and deploy**: merge the reviewed branch to `main`, then `scripts/deploy-area-events.ps1`. Verify the service is active and `/v1` answers.
5. **Stale display names** (on Cubi, in `/home/robohobo/area-events-discovery`): `.venv/bin/python -m area_events.cli sources --db data/discovery/discovery.db reconcile-names` (dry run; expect only `mission_trails`: 2 candidates, 2 image credits, 4 bookkeeping rows, old name `Mission Trails Marketplace`), then the same with `--apply --backup-dir /home/robohobo/backups/area-events-maintenance`; a rerun reports nothing stale.
6. **Raise the page budget**: `PATCH /v1/sources/mission_trails` (control token) with `expected_revision` and the full options `{"urls": ["https://mtrp.org/marketplace/", "https://batesnutfarm.biz/pages/events"], "max_depth": 1, "max_events": 75, "max_pages": 20, "timezone": "America/Los_Angeles"}`. `max_depth` must be 1: listing -> event page, and no further. At depth 2 (production, 2026-10-29) the crawl went on from the event pages to the Bates home page, a second listing of the same events, and four extra candidates and events appeared under listing_url `https://batesnutfarm.biz/`. Production runs depth 1 since source revision 8; a depth-1 scan reads Bates' listing, its event pages and the event-word pages the listing itself links to, never the home page. The hard limit is 24. Do not run repeated scans against batesnutfarm.biz (it rate-limits): let the schedule run one scan.
7. **Verify discovery**: after one scan, the notes show `event_pages_followed`, `by_website` for both sites, no `page_budget_exhausted`; candidates unchanged in number (492), no new events; the two expected changes only: East County Cruisers gains Bates Nut Farm and its address; Christmas in Valley Center Dec 12 moves to 9:30 AM–2:30 PM (same event key, revision 2); a second scan changes nothing; event photographs still resolve.
8. **Revalidate** the manifest against current production: `discovery duplicates reconcile --db … --manifest docs/maintenance/bates-2026-10-reconciliation.json` (dry run). Every pair must say `ready` with its evidence, and `events N -> N-13`. Anything else: stop, do not apply.
9. **Apply**: the same command with `--apply --backup-dir /home/robohobo/backups/area-events-maintenance` (it proves every pair, then takes the verified backup, then writes). Rerun it: all `already reconciled`.
10. **Verify**: API event count 477; the 13 removed keys return 404 and the survivors exist with their facts (Dec 12 9:30–14:30; Cruisers venue and address); `duplicates scan` finds no Bates group; the card shows one entry per occurrence; the service is active; media files of all current events exist.

Recovery: nothing here deletes a backup. Code: the deployer's archive. Database: the pre-step backups above (the deployer never restores over live data; restore is a deliberate operator action with the service stopped).
