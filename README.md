# Area Events

Local events for a Home Assistant dashboard: a Lovelace card, a Home Assistant integration that serves it, and an app that discovers the events. It runs on Home Assistant OS (a Raspberry Pi 4 is enough) and needs no other machine.

```
Area Events card  ──WebSocket──►  Area Events integration  ──HTTP + token──►  Area Events app
(dashboard, YAML +                 (custom_components/area_events:            (area_events/: discovery
 visual editor)                     config entry, profile hub, coordinator)    engine, adapters, cache)
```

- The **card** owns everything you configure: area, radius, source websites, categories, interests, excluded topics, date range, maximum, sorting and layout. It talks only to Home Assistant, over your signed-in session. It holds no secret, contacts no other server and needs no HA scripts or files.
- The **integration** validates each card's request, groups identical requests into one *profile*, caches the results and keeps them fresh with a single coordinator. Different card configurations never overwrite one another.
- The **app** is the one owner of discovery. It runs profiles one after another, keeps a persistent cache per profile and never replaces good events with a failed run.

Details and the API contracts: [docs/architecture.md](docs/architecture.md).

## Install

Requirements: Home Assistant OS or Supervised (for the app), Home Assistant 2025.1 or newer.

1. **App.** Settings > Apps > App store > ⋮ > Repositories, add `https://github.com/lairdrt/area-events-card`, install **Area Events**, set a **Token** (any random secret of 32+ characters) in its Configuration tab and start it.
2. **Integration.** Copy the `custom_components/area_events` folder into your Home Assistant `config/custom_components/` and restart Home Assistant (or install the repository with HACS as a custom integration repository). Then Settings > Devices & services > Add integration > **Area Events**. Enter `http://` + the app's **Hostname** (Info tab) + `:8765`, and the same token.
3. **Card.** Edit a dashboard > Add card > **Area Events Card**. The integration loads the card itself: no dashboard resource and no file copying.

There is no step that needs the machine you develop on, and nothing needs the Cubi that earlier versions used.

## Card configuration

Use the visual editor or YAML; every option is optional.

```yaml
type: custom:area-events-card
title: Austin Events
area: Austin, TX          # city, ZIP code or "latitude, longitude"; omit for the Home Assistant home location
radius_miles: 25
sources:                  # optional website addresses (see "Source websites")
  - https://calendar.utexas.edu
categories: [music, food-drink]       # limit to these categories; omit for all
interests:                            # extra topics, shown as their own filters
  - "Board games: board game, tabletop"
exclude: [politics]                   # hide events that mention these
days: 14                              # only events starting within 14 days
max_events: 30
sort: date                            # date | title | verified
columns: 3                            # at most this many columns
show_images: true
show_filters: true
```

| Option | Meaning |
|---|---|
| `title` | Header text. |
| `id` | Optional short name (letters, digits, `-`, `_`) that keeps saved filters separate for identical cards on one dashboard. |
| `area`, `radius_miles` | What to search (radius 1 to 250, default 25). Without `area`, the Home Assistant home location is used; if Home Assistant has none, the card says so. |
| `sources` | Up to 10 website addresses. Only websites with an adapter work; the rest are listed in the card as unsupported. |
| `categories` | Built-in category ids or names to keep: music, festivals, food-drink, arts, sports, science, technology, family, community, outdoors, education, history, theater, comedy, markets, other. |
| `interests` | Extra topics as `Name: keyword, keyword` (or `{name, keywords}`), up to 20. |
| `exclude` | Topics to hide, matched as whole words in title, description and categories. |
| `days`, `max_events`, `sort` | Date range, event limit and ordering. |
| `columns`, `show_images`, `show_filters` | Layout. |
| `events`, `demo` | A fixed list of events for demonstrations ([examples/demo-card.yaml](examples/demo-card.yaml)); the card then asks no one. |

The filter chips (Show all, categories, your own categories) are the viewer's choices, saved per card instance in the viewer's Home Assistant user data and browser. `feed:` and `entity:` from earlier versions are no longer supported and produce a clear error. Saved filters were keyed by those options, so set an `id` if you want to start fresh with a stable key.

Event fields the card shows: `title`, `start`, `end`, `location`/`venue`, `categories`, `image`, `description`, `url`, `verified` and the verification evidence.

## Source websites

Accepting an address in `sources` does **not** mean any website can be read. The app reads:

- the registered sources in [area_events/discovery/sources.json](area_events/discovery/sources.json): KPBS, San Diego Reader, DoSD and City of San Diego (San Diego, CA) and UT Austin Events (Localist);
- any public calendar that runs on **Localist** (the app probes the address and says whether it is one).

Anything else is reported in the card as *Source not supported* with the reason, and is never fetched. Addresses that resolve to private or local network addresses are refused. Without `sources`, the app uses the registered sources whose declared coverage overlaps the area; for an area with no such source the card says that no source applies. Adding a source for another place means adding an adapter module or a Localist entry, see [docs/architecture.md](docs/architecture.md).

## Development

```
npm test                                   # card tests (no dependencies)
python -m unittest discover -s tests -t .  # engine and integration logic (standard library only)
npm run build                              # stamps the card into custom_components/area_events/frontend/
```

`npm run build` writes the card the integration serves; commit the result. The Home Assistant glue (`config_flow.py`, `coordinator.py`, `websocket_api.py`, `__init__.py`) and the app image cannot run without Home Assistant and its Supervisor, so the tests only check them statically. Those, the ha-form editor and the app on real hardware need a runtime check; see [docs/architecture.md](docs/architecture.md#limitations-and-runtime-checks).

Previous single-location design and its rollback references: tag `area-events-pre-ha-refactor-2026-10-20` and [docs/ha-native-refactor-baseline.md](docs/ha-native-refactor-baseline.md).
