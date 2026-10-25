# Area Events

Local events for a Home Assistant dashboard: a Lovelace card, a Home Assistant integration that serves it, and a Discovery Service (on its own host) that finds, extracts and stores the events in one SQLite database.

```
card (San Diego) ─┐                                                        ┌─ Discovery  (finds + stores candidates)
card (Austin)    ─┼─ WebSocket ─ Area Events integration ─ HTTPS + token ──┤  Extraction (candidates -> events)
card ...         ─┘   (custom_components/area_events)   (read / control)   └─ discovery.db  ← the ONE authoritative database
```

- The **card** shows one discovery *area* (`area: San Diego`) and owns everything about how it is shown: categories, interests, exclusions, date range, sorting and layout. It talks only to Home Assistant, over your signed-in session, and holds no secret.
- The **integration** keeps one shared view of the Discovery Service for all cards: one check of the service's change feed per interval, one read per followed area, and a message to only those cards whose area really changed. Tokens stay in Home Assistant's backend. Administrators can change shared discovery settings through WebSocket commands.
- The **Discovery Service** (`src/area_events`, installed from the repository with `python -m pip install .`, then run with `area-events serve`) owns the database, discovers independently of Home Assistant and exposes an authenticated API. Cards never cause a crawl.

Details: [docs/ha-integration-2d.md](docs/ha-integration-2d.md) (this integration), [docs/presentation-architecture.md](docs/presentation-architecture.md) (replaceable event renderers), [docs/producer-contract.md](docs/producer-contract.md) (discovery input), and [docs/deduplication-and-persistence.md](docs/deduplication-and-persistence.md) (event storage).

## Install

Run the Discovery Service on its own host and put an HTTPS reverse proxy in front of it; copy the integration to Home Assistant, add the integration (service address, read token, optional control token) and add the card. There is no dashboard resource to register. Step by step, including the secure transport and a verification checklist: [docs/ha-integration-2d.md](docs/ha-integration-2d.md).

## Card configuration

Use the visual editor or YAML. `area` names a discovery area configured in the Discovery Service; everything else is optional and private to the card.

```yaml
type: custom:area-events-card
title: Austin Events
area: Austin                          # the name (or key) of a discovery area; required
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
| `area` | The discovery area this card follows. Events are matched to an area by the sources it uses, **not** by a verified distance; events whose place is only a description are marked unverified. `radius_miles` and `sources` from earlier versions are accepted and ignored: areas and their sources belong to the Discovery Service. |
| `categories` | Built-in category ids or names to keep: music, festivals, food-drink, arts, sports, science, technology, family, community, outdoors, education, theater, comedy, markets, other. |
| `interests` | Extra topics as `Name: keyword, keyword` (or `{name, keywords}`), up to 20. |
| `exclude` | Topics to hide, matched as whole words in title, description and categories. |
| `days`, `max_events`, `sort` | Date range, event limit and ordering. |
| `columns`, `show_images`, `show_filters` | Layout. |
| `events`, `demo` | A fixed list of events for demonstrations ([examples/demo-card.yaml](examples/demo-card.yaml)); the card then asks no one. |

The filter chips (Show all, categories, your own categories) are the viewer's choices, saved per card instance in the viewer's Home Assistant user data and browser. Changing one card never changes another card or the shared Discovery area.

Every tile has a picture: the event's own photo, else a labelled illustration for its category that ships inside the card ([docs/images.md](docs/images.md)). Event fields the card shows: `title`, `start`, `end`, `venue`, `address`, `description`, `url` and the location status.

## Development

```
npm test                                   # card tests (no dependencies)
python -m unittest discover -s tests -t .  # engine and integration logic (standard library only)
npm run build                              # stamps the card into custom_components/area_events/frontend/
```

`npm run build` reads the card and registered presentation modules from `src/frontend` and writes generated assets into `custom_components/area_events/frontend`; commit the result. Python code is a standard `src`-layout package in `src/area_events`, installable with `python -m pip install .`. The Home Assistant glue (`config_flow.py`, `coordinator.py`, `websocket_api.py`, `__init__.py`) is tested against stand-ins shaped after the Home Assistant API; the integration's logic is tested against the real Discovery Service. Neither proves it runs inside a real Home Assistant: use the checklist in [docs/ha-integration-2d.md](docs/ha-integration-2d.md#verification-on-the-device).

For the supported one-command deployment and recovery process, see [docs/automated-deployment.md](docs/automated-deployment.md).
