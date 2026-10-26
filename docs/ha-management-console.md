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
