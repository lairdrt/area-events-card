# Reviewed reconciliation and the controlled release

## The operation

`discovery duplicates reconcile --db DB --manifest FILE [--apply --backup-dir DIR]` reconciles **only the event pairs a reviewed manifest names**. It is not the automatic duplicate cleanup (`duplicates clean`, unchanged and still matcher-driven) and not a force-merge: it never searches for pairs, never consults the matcher, and refuses the whole manifest unless **every** pair is proven from the stored records.

Manifest (`discovery.reconciliation.v1`): `source_key`, optional `expect_events_after`, and `pairs` of full event keys `{label, survivor, remove}`. An event key may appear once. The operator chooses the survivor; the tool checks it.

Proof required per pair (preview prints it):

- both events exist and every candidate of both belongs to the manifest's source, all found on one listing page (`provenance.listing_url`);
- each event has exactly one primary candidate and a complete revision history;
- every title of the removed event is a title of the survivor followed only by date/time label words (`Date`, `Date & Time`, `Date: Sat.`, `Date: Thurs., Sept. 17 to Sat.`);
- the primary candidates state the same calendar day;
- a different start or end on the removed event is accepted only if the **survivor's own revision history records the correction from that value** (the corrected Valley Center Dec 12 time);
- the survivor loses no fact: an end, place, address or description only the removed event states refuses the pair, as does a conflicting place.

Applying (one transaction, one verified backup first, rolled back whole on any mismatch): the survivor keeps its event key and every fact; the removed event's candidates become `duplicate` associations of the survivor (the survivor's primary stays primary); its extractions and revisions move to the survivor (appended after the survivor's own, which are untouched); one final revision records `reconciled_from`; the removed event is deleted; change records (`event.reconciled` upsert for the survivor, delete for the removed key) reach the API and card. A second run reports `already reconciled` and writes nothing. `expect_events_after` refuses a manifest that would not leave exactly that many events.

The Bates manifest is `docs/maintenance/bates-2026-10-reconciliation.json` (sha256 `695ce9d8…ca477`): 13 pairs, 490 → 477 events on the production data of 2026-10-28.

## Release sequence

Run from the repository root on `main` after review. Production is untouched until step 3.

1. **Confirm**: `git status` clean; on the reviewed commit; `git log -1`.
2. **Test and preflight**: full suite (`python -m unittest discover -s tests`), then `scripts/deploy-area-events.ps1 -DryRun` (stops before any write).
3. **Backup before any write**: the deployment takes its own SQLite API backup and code archive (`/home/robohobo/backups/area-events-deploy/<timestamp>`). Additionally take a manual one to a directory outside the application tree: `python -m area_events.cli sources --db data/discovery/discovery.db reconcile-names --apply --backup-dir /home/robohobo/backups/area-events-maintenance` also produces one (step 5).
4. **Merge and deploy**: merge the reviewed branch to `main`, then `scripts/deploy-area-events.ps1`. Verify the service is active and `/v1` answers.
5. **Stale display names** (on Cubi, in `/home/robohobo/area-events-discovery`): `.venv/bin/python -m area_events.cli sources --db data/discovery/discovery.db reconcile-names` (dry run; expect only `mission_trails`: 2 candidates, 2 image credits, 4 bookkeeping rows, old name `Mission Trails Marketplace`), then the same with `--apply --backup-dir /home/robohobo/backups/area-events-maintenance`; a rerun reports nothing stale.
6. **Raise the page budget**: `PATCH /v1/sources/mission_trails` (control token) with `expected_revision` and the full options `{"urls": ["https://mtrp.org/marketplace/", "https://batesnutfarm.biz/pages/events"], "max_depth": 2, "max_events": 75, "max_pages": 20, "timezone": "America/Los_Angeles"}`. Rehearsal: one scan reads 16 pages (Bates 13, mtrp.org 3) and the budget is fairly shared; 20 leaves headroom for four more event pages. The hard limit is 24. Do not run repeated scans against batesnutfarm.biz (it rate-limits): let the schedule run one scan.
7. **Verify discovery**: after one scan, the notes show `event_pages_followed`, `by_website` for both sites, no `page_budget_exhausted`; candidates unchanged in number (492), no new events; the two expected changes only: East County Cruisers gains Bates Nut Farm and its address; Christmas in Valley Center Dec 12 moves to 9:30 AM–2:30 PM (same event key, revision 2); a second scan changes nothing; event photographs still resolve.
8. **Revalidate** the manifest against current production: `discovery duplicates reconcile --db … --manifest docs/maintenance/bates-2026-10-reconciliation.json` (dry run). Every pair must say `ready` with its evidence, and `events N -> N-13`. Anything else: stop, do not apply.
9. **Apply**: the same command with `--apply --backup-dir /home/robohobo/backups/area-events-maintenance` (it proves every pair, then takes the verified backup, then writes). Rerun it: all `already reconciled`.
10. **Verify**: API event count 477; the 13 removed keys return 404 and the survivors exist with their facts (Dec 12 9:30–14:30; Cruisers venue and address); `duplicates scan` finds no Bates group; the card shows one entry per occurrence; the service is active; media files of all current events exist.

Recovery: nothing here deletes a backup. Code: the deployer's archive. Database: the pre-step backups above (the deployer never restores over live data; restore is a deliberate operator action with the service stopped).
