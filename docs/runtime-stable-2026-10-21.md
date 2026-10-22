# Runtime-stable baseline (2026-10-21)

The Area Events card, integration and app as accepted after repeated runtime testing in a real Home Assistant. Return to this point with the tag `area-events-runtime-stable-2026-10-21`. It supersedes [ha-native-runtime-baseline.md](ha-native-runtime-baseline.md) as the current accepted state.

## What this baseline is

| | |
|---|---|
| Branch | `ha-native-refactor` |
| Accepted source | commit `a5254cc` ("Define the card after `<home-assistant>`, in the registry the frontend's scoped-registry polyfill installs"). The tag points at the commit that adds this document on top of the branch head; the card, integration and app code are byte-identical to `a5254cc`. The only later changes are in `scripts/deploy-ha.ps1` and `scripts/test-deploy-ha.ps1` (restart handling), which are not part of the runtime. |
| Card build | `AREA-EVENTS 0.2.0-b50af579` (integration and app version 0.2.0), as built into `custom_components/area_events/frontend/area-events-card.js` at `a5254cc` |
| Environment | Home Assistant Core 2026.10.2 (read from the API during the deployment work), on a Raspberry Pi 4 running Home Assistant OS; Google Chrome as the browser. The Home Assistant OS version and the Chrome version were not recorded in this baseline and are not stated here. |

## Runtime verified (observed by the project owner in the real system)

- The Area Events card loads reliably across repeated normal and hard browser refreshes.
- Switching Home Assistant apps and minimizing and restoring Chrome did not produce configuration errors.
- The registration fix works at runtime: the card is defined only after Home Assistant has initialized (`<home-assistant>` is defined), in the registry the frontend's scoped-registry polyfill installs. Background: the failure was the card being defined in the browser's original registry before the polyfill replaced it, so Lovelace did not see it.
- Automatic geographic sources stay active when additional websites are configured.
- An unsupported additional website (for example one that needs JavaScript) produces a compact, nonfatal warning and does not remove events.
- The graphite-and-orange discovery and loading interface is visually approved.

## Tested in code

- Card registration, including a simulated registry swap and the already-started case (`tests/card.test.mjs`).
- Additive sources and error-state regressions: automatic sources always run, added websites supplement them, superseded errors are cleared when a run starts, unreadable websites are warnings (`tests/test_additive_sources.py`, `tests/test_web.py`, `tests/test_engine.py`, card tests).
- The relevant Python (323 tests), frontend (66 tests) and lint (ruff) checks pass at `a5254cc`. Unit tests do not prove cold-browser reliability; the runtime observations above do.

## Known limitations and open items

- Long-term registration reliability remains subject to continued runtime observation. The card records `window.__AREA_EVENTS_CARD_LOAD__` to help diagnose any recurrence.
- The Home Assistant deployment script (`scripts/deploy-ha.ps1`) reported failure when the API connection closed during a successful Core restart. The branch head after `a5254cc` changes the script to treat that as indeterminate and to confirm the restart by watching Core go down and recover; that change has offline tests only and has not been exercised against a live restart.
- PDF and flyer event discovery is not implemented.
- Curated category photographs are not integrated; redistribution licensing is unresolved (see [category-photography.md](category-photography.md)).
- Event de-duplication needs improvement.
