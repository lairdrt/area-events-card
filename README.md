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

Home Assistant OS or Supervised, Home Assistant 2025.1 or newer. In short: copy the integration, install and start the app, accept the discovered integration, add the card. There is no token to copy and no dashboard resource to register. Step by step, with the PowerShell helper that copies the integration to the Samba share: [docs/installation.md](docs/installation.md).

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
| `sources` | Up to 10 website addresses: an events page, a calendar (ICS) feed or an RSS/Atom feed. Addresses the app cannot read are listed in the card with the reason. |
| `categories` | Built-in category ids or names to keep: music, festivals, food-drink, arts, sports, science, technology, family, community, outdoors, education, history, theater, comedy, markets, other. |
| `interests` | Extra topics as `Name: keyword, keyword` (or `{name, keywords}`), up to 20. |
| `exclude` | Topics to hide, matched as whole words in title, description and categories. |
| `days`, `max_events`, `sort` | Date range, event limit and ordering. |
| `columns`, `show_images`, `show_filters` | Layout. |
| `events`, `demo` | A fixed list of events for demonstrations ([examples/demo-card.yaml](examples/demo-card.yaml)); the card then asks no one. |

The filter chips (Show all, categories, your own categories) are the viewer's choices, saved per card instance in the viewer's Home Assistant user data and browser. `feed:` and `entity:` from earlier versions are no longer supported and produce a clear error. Saved filters were keyed by those options, so set an `id` if you want to start fresh with a stable key.

Every tile has a picture: the event's own photo, else a matched source's photo, else a labelled illustration for its category that ships inside the card ([docs/images.md](docs/images.md)).

Event fields the card shows: `title`, `start`, `end`, `location`/`venue`, `categories`, `image`, `description`, `url`, `verified` and the verification evidence.

## Source websites

Type the address of an events page, an ICS calendar or an RSS/Atom feed into **Source websites** in the card editor and save; the app discovers the events itself, with no per-site code. In order of preference it reads:

1. **Schema.org events** in the page (JSON-LD, including `@graph`, lists and nested events, and microdata);
2. **calendar feeds** the page links to on the same site: iCalendar (ICS, including simple recurrences) and RSS/Atom feeds that state event dates;
3. only when neither exists, **listing blocks** that hold a `<time datetime>` and a titled link;
4. a few same-site **event pages**, to fill in a venue or picture a listing lacks, or to follow links a list gave without dates.

The address may also be a feed itself, or a single event page. Nothing is invented: an event with no start time (a bare date is not one), no determinable time zone, an online-only venue, or a past end is skipped. Venue, address, image, organizer and price appear only when the page states them. Events then go through the same location filter, de-duplication, classification and image handling as every other source.

The card shows what happened to each address: *events found*, *no events found*, *unsupported format* (for example a page that builds its events with JavaScript, which the app never runs), *blocked* (robots.txt, HTTP 401/403/429, or a non-public address) or *unavailable*. Not every website can be read: pages without structured data or feeds, sites that require JavaScript, logins or CAPTCHAs, and sites whose robots.txt forbids the app are reported, not worked around. The registered sources in [area_events/discovery/sources.json](area_events/discovery/sources.json) (KPBS, San Diego Reader, DoSD, City of San Diego, UT Austin) and any public **Localist** calendar keep their dedicated readers, which take precedence for those addresses.

**Safety.** Typed-in addresses are untrusted: http/https on ports 80 and 443 only; the host is resolved once, every address must be public (loopback, private, link-local, metadata and carrier-grade addresses are refused) and the connection is made to that exact address, so DNS rebinding cannot swap it; each redirect (at most four) is re-checked; robots.txt and Crawl-delay are honoured; requests to a host are spaced at least two seconds apart; responses are limited to 1.5 MB (compressed bodies are inflated within the same limit) and a site gets at most 30 requests (12 event pages, 3 list pages, 3 feeds) and about 100 seconds. The result of a crawl is cached for 15 minutes so profiles that share a website do not fetch it twice. No JavaScript is executed and no browser is involved. Without `sources`, the app uses the registered sources whose declared coverage overlaps the area; for an area with no such source the card says that no source applies. Details are in [docs/architecture.md](docs/architecture.md).

## Development

```
npm test                                   # card tests (no dependencies)
python -m unittest discover -s tests -t .  # engine and integration logic (standard library only)
npm run build                              # stamps the card into custom_components/area_events/frontend/
```

`npm run build` writes the card the integration serves; commit the result. The Home Assistant glue (`config_flow.py`, `coordinator.py`, `websocket_api.py`, `__init__.py`) and the app image cannot run without Home Assistant and its Supervisor, so the tests only check them statically. Those, the ha-form editor and the app on real hardware need a runtime check; see [docs/architecture.md](docs/architecture.md#limitations-and-runtime-checks).

Previous single-location design and its rollback references: tag `area-events-pre-ha-refactor-2026-10-20` and [docs/ha-native-refactor-baseline.md](docs/ha-native-refactor-baseline.md).
