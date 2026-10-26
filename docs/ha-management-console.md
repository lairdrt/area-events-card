# Area Events Home Assistant management console — first slice

This console is **part of the existing Area Events Home Assistant integration**. It is a native administrator-only custom HA panel at `/area-events-manage`, with an entry in HA's sidebar. The standard integration Configure form remains for Cubi connection credentials and HA refresh interval.

## Current functions

- **Overview** — reads Discovery Service health and configuration state.
- **Sources** — lists configured producer instances and operational status; creates source instances (initially disabled), edits their names, intervals and producer-specific configuration, and enables/disables them after confirmation.
- **Areas** — creates areas, edits area geography and enabled state, and assigns multiple producer instances. Updates carry the current area revision.
- Producer form fields are supplied by the installed Cubi adapters as a *versioned* metadata contract, not hardcoded in HA.
- Retired source adapters appear as retired. They can be disabled without reviving the removed adapter; enabling or editing them is blocked. Existing events are retained.
- **No source deletion** in this first slice: removal semantics require a deliberate control/persistence contract and tests. The UI does not perform SQL operations.

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
