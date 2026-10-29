# Area Events Home Assistant management console — first slice

This console is **part of the existing Area Events Home Assistant integration**. It is a native administrator-only custom HA panel at `/area-events-manage`, with an entry in HA's sidebar. The standard integration Configure form remains for Cubi connection credentials and HA refresh interval.

## Current functions

- **Overview** — reads Discovery Service health and configuration state.
- **Sources** — lists configured producer instances and operational status; creates source instances (initially disabled), edits their names, intervals and producer-specific configuration, and enables/disables them after confirmation.
- **Areas** — creates areas, edits area geography and enabled state, and assigns multiple producer instances. Updates carry the current area revision.
- Producer form fields are supplied by the installed Cubi adapters as a *versioned* metadata contract, not hardcoded in HA.
- Retired source adapters appear as retired. They can be disabled without reviving the removed adapter; enabling or editing them is blocked. Existing events are retained.
- The UI does not perform SQL operations or delete sources. Operators can use the Discovery Service's `area-events sources cleanup SOURCE_KEY` command for an audited, transactional removal; it previews by default and `--apply` requires a verified SQLite backup outside the application tree plus explicit `--expect-*` identity, state, ownership, job, and source-history guards. The deletion transaction rechecks those expectations and independently refuses enabled sources, source-owned candidates or events, area assignments, leased jobs, and processing work.

## Generic website SourcesProducer (AE-10B4)

The **Website event sources (generic)** producer appears in **Sources → Add producer**.
It is an ordinary configured instance of the replaceable adapter in
`src/area_events/discovery/producers/sources_producer.py`:

- Enter 1–12 public HTTP(S) **website URLs, one per line**.
- Configure the scan interval, maximum pages (1–24), link depth (0–2), maximum events (1–150), and **IANA timezone** (e.g., `America/Los_Angeles`) for websites displaying local clock times without offsets.
- Newly created producers are disabled until deliberately enabled and assigned to an area.
- The existing scheduler handles repeat discovery; the shared SafeFetcher applies URL validation, robots.txt, private-network blocking, request budgets and host pacing.
- **The same SourcesProducer module** reads structured **JSON-LD Event**, **iCalendar (.ics)**, and unstructured HTML text. It finds dated events across headings, event detail pages, lists, and cards; links are followed through the bounded same-host crawl. It never delegates text extraction to an extra producer.
- Unstructured text events require a visible event name, explicit calendar date, actual event clock time and configured site timezone; missing evidence leads to **no candidate**. That avoids mistaking operating hours, news publication dates, vendor names and schedules without dates for individual events. Configured timezone supplies the local offset rather than inventing one.
- **Two-stage reading.** A listing card supplies the occurrences and, when it links to a page, that event page's address. The producer follows those links (same website only, within the page budget) and reads the labelled facts there (Date, Time, Location, Address, Description, the page image). The event page's facts win; whatever it omits keeps the listing's value. There is ONE candidate per occurrence: it keeps the listing candidate's identity (`local_id`), and its `url` becomes the event page. An event page that cannot be fetched leaves the listing facts as they were.
- **Occurrences and identity.** Every occurrence takes the time stated with ITS date ("Dec 11 4-8 pm" and "Dec 12 9:30 am-2:30 pm" are two different times). An event page speaks only for the listing occurrences whose cards linked to it: a related event on the same page is ignored, a page that states no date still supplies its labelled place and description, a page that words the title differently ("Alias | Live Music on Halloween" for the card "Alias | Live at Bates on Halloween") is that card's event only when it holds exactly one event, at exactly the time of the one linking card at that time (an end time that differs refuses it), and the two titles share a word that is not generic; the listing's title is then kept, and every disagreement between listing and page is kept in the evidence. When a stated time is corrected, the occurrence keeps the stored candidate's id if exactly one earlier record of the same listing, title and day exists (the notes count `identities_kept_after_correction` and `identity_corrections_ambiguous`); an event page that cannot be read in a scan leaves what an earlier scan learned from it in place. Occurrences that ONLY an event page lists are reported as `event_page_only_occurrences`: their id will differ if the listing later adds them.
- **Limits.** `max_events` caps the candidates; every starting URL is still read, and event pages of known candidates are still read (and enrich them) after the cap, so the notes report `candidate_limit_reached` and `events_not_added`. The notes distinguish a spent page budget (`page_budget_exhausted`, per-site `pages_unvisited`), a failed page, links beyond the per-site cap (`links_over_cap`) and pages skipped because of the candidate limit. A multi-week date range with seasonal hours (a pumpkin patch "Sept 17 to Oct 31") is not expanded into daily occurrences.
- **Page budget.** `max_pages` is the budget for the whole scan: starting URLs AND every followed page, all websites together. `max_depth` 0 reads only the starting URLs; 1 also reads the event pages they link to. When the budget runs out the result's notes say so (`page_budget_exhausted`, `pages_unvisited`), and a budget with no room beyond the starting URLs while `max_depth` > 0 is reported as `configuration_warning`.
- **Fair crawling.** Every starting URL is read first. The remaining budget is then shared by the websites in turn, one page each, and a website offers its event-detail links before its generic discovery links, so one site with many links cannot starve another. The scan notes report, per website (`by_website`, a JSON text), the pages attempted, fetched, failed and skipped and the event pages followed and failed.
- The venue is only ever what a page states; it is never taken from the producer's display name (which is attribution only).
- **Renaming a producer** changes its display name only: its `source_key` is its identity. The stored copies of the name (candidate provenance, image credits) are reconciled in the same transaction; no candidate or event is created or reprocessed.
- **Stored display-name copies of a producer renamed earlier** are brought up to date with `discovery sources --db DB reconcile-names [SOURCE_KEY]` (a dry run; `--apply --backup-dir DIR` takes a verified backup first and runs one transaction; running it again changes nothing).
- Every event uses the standard candidate contract; leads, fetched pages, candidate records and event results use the same shared persistence and processing interfaces as other producers.

The generic producer **does use conservative text heuristics**, but is not a general-purpose AI browser or a guarantee for every arbitrary website layout. JavaScript-only, authenticated, CAPTCHA-restricted, and robot-blocked pages may remain unreadable. Structured and ordinary text paths share the identical candidate output and downstream processing.
No new API, database, timer or per-source downstream path is introduced.

**Offline acceptance fixture:** `tests/test_sources_producer.py` uses the currently published
`https://mtrp.org/marketplace/` event details. With the producer timezone set to
`America/Los_Angeles`, it must emit exactly **two 2026 Holiday Marketplace occurrences**
(November 14 and 15, 9:00 a.m.–3:00 p.m., Mission Trails Regional Park Visitor Center),
not vendors, parking instructions or Visitor Center hours. Live site access is tested
separately (with `AREA_EVENTS_LIVE_MTRP=1 python -m unittest tests.test_sources_producer.GenericProducer.test_mtrp_marketplace_live_public_page`) because CI should not depend on public-site availability. The live smoke test is explicit opt-in and honors the shared SafeFetcher network/robots policy.

## Architecture and security

```text
HA management panel (presentation only, admin sidebar)
         |
HA admin-only, validated WebSocket commands
         |
DiscoveryHub + DiscoveryControlClient (tokens remain server-side)
         |
Cubi versioned HTTPS API -> Control Service -> ControlStorage port
         |                                  |
         +--- read contract             Persistence -> ONE SQLite database
                         Scheduler -> independent producer instances -> standard
                         candidates -> extraction/normalization/validation ->
                         separate deduplication <-> persistence
```

No direct browser-to-Cubi request, HA-to-SQLite request, source-specific processing bypass, additional database, or new service is introduced. The original event cards and processing pipeline remain unchanged.

## Supported management APIs

- HA WebSocket `area_events/management`: admin-only read snapshot with
  `area_events.management.v1` schema and `producer_types.schema = area_events.producer_forms.v1`.
- HA WebSocket `area_events/source_command`: admin-only `create`, `update`, `toggle`; writes use expected source revisions where applicable.
- Existing HA WebSocket `area_events/area_create`, `area_events/area_update` are reused.
- Cubi GET `/v1/producer-types`: producer-defined form fields.
- Existing Cubi source and area endpoints: create/edit and state transitions.
- Cubi `ControlStorage.set_source_enabled` is a state-only operation, allowing *disable* of a retired adapter while rejecting unsafe enable. Other producer edits continue to use validation.

## Acceptance test (on a test installation, after CI and review)

1. Confirm integration and existing event cards work as before.
2. Open **Area Events** in the HA sidebar; confirm sources and areas load, and actual status appears.
3. Create a disabled Localist source using a valid public calendar URL. Check it appears in the list and remains disabled.
4. Edit that source's name and refresh interval, then enable and disable it. Verify state survives a refresh.
5. Create an area, assign and unassign the source, and verify that other area associations are unchanged.
6. Disable any retired source via the explicit confirmation. Do not try to re-enable it until its adapter is installed.
7. Verify non-admin HA users cannot open the panel or call management WebSockets, and that no Cubi credential appears in the browser.
8. Verify that stale-revision writes show a conflict instead of silently overwriting configuration.

The panel has not been exercised in an actual HA runtime yet; mocked HA tests and CI provide pre-installation evidence only.
