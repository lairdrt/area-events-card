# HA-native runtime baseline (2026-10-21)

The Home Assistant-native Area Events system, as accepted after running on real hardware. Return to this point with the tag `area-events-ha-native-runtime-2026-10-21`.

## What this baseline is

| | |
|---|---|
| Source | branch `ha-native-refactor`, code commit `08ca29a` (the tag points to the commit that adds this document on top of it; no source changed) |
| Card build | `AREA-EVENTS 0.2.0-3e2843d1` (integration and app version 0.2.0) |
| Environment | Home Assistant OS on a Raspberry Pi 4; HA Core 2026.9.1 and HA OS 18.2 (last confirmed); configuration share `Z:`, local apps share `X:`; test dashboard `test_2` |
| Components | card + visual editor (`src/`, served from `custom_components/area_events/frontend/`), integration (`custom_components/area_events/`), app (`area_events/`) |
| Before it | tag `area-events-pre-ha-refactor-2026-10-20` (Cubi, shell/REST bridge); see [ha-native-refactor-baseline.md](ha-native-refactor-baseline.md) |

## Confirmed at runtime on Home Assistant

- The Area Events app is installed as a local app and runs on HA OS on the Raspberry Pi 4.
- Supervisor discovery works and the integration was set up from the discovered entry, with no token typed or copied.
- The card shows real events for San Diego and for Austin.
- The visual editor opens, saves and works.
- Switching the card between San Diego and Austin works in both directions.
- Cached results load immediately.
- Desktop and mobile layouts are responsive.
- The legacy dashboard resource (`/local/area-events-card/loader.js`) was removed; the integration loads the card by itself.
- Server side, checked from outside the browser: the integration loads on every Home Assistant start, the HA index page imports `/area_events/area-events-card.js`, that URL returns the deployed file, and the deployed integration files match the repository.

## Verified by automated tests only, or only in part

Unit and stand-in tests pass (204 Python, 49 card); a local run of the engine against the real sources also exercised discovery. These were not separately observed in Home Assistant:

- **Progressive discovery and intermediate updates.** Events published as each source finishes, progress reporting, tile reconciliation without flashing. A real San Diego run through the engine API grew from 27 to 174 to 216 events as sources finished; the card's progress panel and in-place updates are tested against stand-ins.
- **Profile cleanup and expiration.** Eviction of profiles without events, expiry of abandoned profiles, no scheduling of unresolvable places, reuse of equivalent places.
- **Failure and recovery handling.** Failed refreshes keeping earlier events, permanent versus retried errors, capacity refusals, an interrupted write leaving the previous feed whole.

The integration glue (config flow, coordinator, WebSocket command) is also covered only against stand-ins, apart from what the runtime results above demonstrate.

## Known limitations

- **Missing event images.** Many events have no image; the card shows its placeholder.
- **Duplicate events across sources.** De-duplication is evidence-based and conservative, so some events appear more than once.
- **Limited discovery coverage.** Registered sources cover San Diego, CA and the University of Texas at Austin; other areas report that no source applies. Named websites work only if an adapter exists (registered sources and Localist calendars).
- **Slow initial San Diego discovery.** The first search takes minutes because of polite request spacing and per-event detail requests; later loads use the cache.
- **Intermittent frontend loading, previously observed.** The card once failed with "Custom element doesn't exist: area-events-card" (and, earlier, the visual editor reported as unsupported while an old pre-integration card resource was still registered). A reload fixed it. The exact cause is **unproven**: the server side was healthy throughout (integration loaded, module served, file identical), so it was on the browser side, for example a page loaded while Home Assistant was starting. If it recurs, the console banner and the Network request for `/area_events/area-events-card.js` show which case it is. Since the card now records its own load, run `window.__AREA_EVENTS_CARD_LOAD__` in the browser console on a failing page: `undefined` means the module was never evaluated (check the Network tab for the script request: status, size, timing, and the console for "Failed to fetch dynamically imported module"); `{defined: false}` means evaluation stopped before the card was defined (the console shows the exception); `{defined: true}` means the card was defined and the failure was only the 2-second wait Home Assistant gives a custom element before showing its error (the card replaces the error when it is defined). The integration registers the script the way Home Assistant documents (`async_register_static_paths` then `add_extra_js_url`, with a `frontend` dependency); Home Assistant loads such scripts with a dynamic `import()` that has no retry or error reporting, so a failed or slow request is invisible unless the Network tab is open.
- Leftovers outside the repository: the old `www/area-events-card` folder on the configuration share and the old Area Events scripts and sync automation, if still present, are unused by this system and can be removed.
