# Event imagery in the Discovery architecture (Milestone 2F)

The accepted imagery system (`discovery/media.py`, `images.py`, `classifiers.py`, `maps.py`, and the card's artwork) is back. Nothing was rewritten: the accepted code now runs inside an independent **MediaEnrichmentService** on the Cubi, on top of the one discovery database, and its results travel to the card with the events.

## Why the card showed only blue calendars

The new pipeline never ran the classifier, so no event carried an `illustration` decision and the card fell back to its "general" calendar for every tile. It also never ran the media service, so no event had a photograph or map.

## Priority (what the card tries, in order, for every event)

1. **The event's own photograph** (the source's `photo_url`), judged by `images.judge` (no placeholders, logos, pixels). An image attached to three or more unrelated event titles is a default picture, not a photograph of any of them, and is not used. If the picture host's `robots.txt` allows it, the service copies it into the bounded cache and the card shows the cached copy; if the host forbids fetching (as the Localist image CDN does), nothing is copied and the card is given the source's own https address to show directly, with the same credit. Never a plain-http address.
2. **A verified photograph of the venue** (Wikimedia Commons, through the accepted identity chain: OpenStreetMap/Wikidata link, exact label, coordinates within 250 m, permissive licence). Licence, creator and file page are stored and shown.
3. **A map of a verified location** (OpenFreeMap tiles drawn as a light and a dark SVG). Verified means the accepted rules: coordinates the source gave for the venue and that lie inside the area, or a street address whose number and street the geocoder matched in the right locality, or an exactly named place. Address pins are labelled approximate. Nothing weaker is mapped; a refusal is cached with its reason.
4. **The category illustration** the accepted keyword classifier chose (stored with its reasons), drawn by the card from its own artwork. The general calendar is used only when nothing clearer is found.

No stock photograph is ever substituted for an event.

## Components

| Piece | Where | Role |
|---|---|---|
| `MediaEnrichmentService` | `service/enrichment.py` | One thread (`run_once`/`start`/`stop`/`status`), capped error backoff, polite pacing (Nominatim 15 s apart, shared host policy). Constructor-injected `work`, `resolver`, `photos`, `clock`, `classify`. Holds no connection; started by `serve` (`--no-media` to disable). |
| `MediaService` (accepted code) | `discovery/media.py` | Unchanged except that a store can be injected. |
| `SqliteMediaStore`, `MediaRepository`, `LocalMediaWork`, `LocalMediaFiles` | `service/media_store.py` | The accepted store's interface over the database (`media_records`, `media_files`, `candidate_media`, migration 4) plus the bounded files folder `<db dir>/media/files`. |
| `GET /v1/media/{name}` | `service/api.py` | A cached picture by name, read scope, strict name pattern, immutable caching, correct type, no path ever from a request. |
| `AreaEventsMediaView` | `custom_components/area_events/media_view.py` | `/area_events/media/{name}` for the browser: relays the picture from the service with the integration's read token. |
| `feed.py` | integration | Adds `images`, `image_credit`, `media` (venue photo, map), `illustration` to the card's event. |
| card | `src/area-events-card.js` | Accepts the integration's cached photographs and shows an unobtrusive credit button for an event photograph (source, licence, link to the event page). |

Metadata and references live in the existing SQLite database; image bytes live in a managed folder bounded by a byte budget (250 MB), record counts and expiry (verified places 60 days, refusals 14 days, source photographs 60 days); files no record uses are removed.

## Delivery and security

The browser never sees the service address or a token. It asks Home Assistant for `/area_events/media/<name>`; the integration asks the Cubi with its read token and relays the bytes with the content type taken from the name. The names are content hashes (photographs) or hashes of the map position, so they cannot be enumerated; only public pictures are cached; the view answers only names of the exact pattern and has no file path input. This view is public (`requires_auth = False`) because an `<img>` request cannot authenticate; that is the same arrangement the accepted legacy integration used. Pictures are immutable by name, so a card refresh downloads nothing new. Card refreshes and views never trigger image discovery: only the enrichment worker looks things up.

## Verified (tests, and real stored events)

Tests (`tests/test_media_enrichment.py`, `tests/test_ha_integration.py`, `tests/test_grid_layout.py`, card tests): the own-photo path with credit, maps only from in-area source coordinates, a verified Commons photograph with licence, refused and shared images, temporary versus permanent failures, a host that forbids fetching, repeat runs without new requests, a changed event enriched again, worker restart and recovery, a stalled lookup not slowing discovery or queries, the cache budget, the API's authentication and path safety under 20 concurrent clients, the integration's relay, and the grid layout in a real browser for 1, 2, 3, 4, 5 and 20 events at five widths.

On a copy of the Cubi database with the real network (not production): Austin events got source photographs (shown from the source's own https address, credited), maps for venues with source coordinates (venue-precise) and for matched addresses (approximate), and category decisions; San Diego events got maps for matched addresses and category decisions. Default pictures shared by unrelated events were recognised and left out. No verified venue photograph appeared in that sample: the Wikimedia identity chain is strict.

## Not yet in this milestone

- Venue photographs depend on the accepted strict chain; most ordinary venues will not have one.
- Enrichment is slow by design for geocoded addresses (Nominatim allows about four lookups a minute), so a large calendar is mapped over hours; events show their illustration until then.
- San Diego City events carry no photographs from the source.
- The integration labels the Austin source "Ut Austin".

## Deploying (together with the grid fix)

1. Cubi (new code, new tables on start, then restart; the database is migrated in place, back it up first):
   copy `area_events/` to `~/area-events-discovery/`, then `systemctl --user restart area-events-discovery`.
2. Home Assistant (integration, media view and card): `.\scripts\deploy-ha.ps1 -SkipApp` (restarts Core).
