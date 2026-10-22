# Imagery runtime-accepted baseline (2026-10-22)

The Area Events card, integration and app with the location-imagery work, as accepted by the project owner after running it in a real Home Assistant. Return to this point with the tag `area-events-imagery-runtime-accepted-2026-10-22`. It builds on [runtime-stable-2026-10-21.md](runtime-stable-2026-10-21.md) (tag `area-events-runtime-stable-2026-10-21`, which is unchanged) and describes the current accepted state of the imagery.

## What this baseline is

| | |
|---|---|
| Branch | `ha-native-refactor` |
| Accepted source | commit `e13455e` ("Six new illustrations: farmers market, carnival, car show, Halloween, Christmas, street festival"). The tag points at the commit that adds this document on top of it; no application code differs. |
| Card build | `AREA-EVENTS 0.2.0-8db615c7` (integration and app version 0.2.0), served by the integration after the deployment |
| Environment | Home Assistant Core 2026.10.2 (read from the API during the deployment), the Area Events app `local_area_events` started, the integration loaded; deployed with `.\scripts\deploy-ha.ps1 -AppsShare X:\` (without `-Force`). The operating system and browser versions were not read for this document. |
| Tests at the accepted commit | Python 470, card 91, ruff clean (`--select F,E9`), build succeeded |

## What it contains

- **Six new illustrations** (original in-card SVG, theme variables only): `farmers_market`, `carnival`, `car_show`, `halloween`, `christmas`, `street_festival`, 17 illustrations in all. The classifier (`discovery/classify.py`) chooses them from clear subjects in the title or description; the terms they own were taken out of festival and food, and a specific subject in the title takes precedence over festival, food, family, community and outdoors. The earlier eleven categories are unchanged.
- **Image hierarchy:** the event's own photograph, then a verified venue photograph, then a map of the location, then the matching illustration, which is always the last fallback. Generic category photographs are not used. `show_images: false` shows none of the first three.
- **Theme-aware vector maps:** OpenFreeMap vector tiles drawn by the app as SVG in a light and a graphite scheme; the card follows the Home Assistant theme (`hass.themes.darkMode`) and swaps the variant live. A pin, a MAP badge and a venue chip are card elements, not part of the image.
- **Venue-photo verification:** a photograph is shown only through an exact identity chain (OpenStreetMap or Wikipedia link, Wikidata label, coordinates within 250 m, a Commons JPEG of at least 800 px, a permissive licence). See [location-imagery.md](location-imagery.md).
- **Map lifecycle fixes:** a loaded map is kept when the other theme's variant fails (the loaded one stays and the swap is retried); a tile whose images all failed is retried on later polls (60 s apart, three times); the app lists a map or photograph only while its files are stored and draws the place again when they are missing; a map the app stops listing is removed, never kept.
- **HTML entities and attribution:** entities left in listings ("Hotel &amp; Suites") are decoded once, strictly, where events enter the engine (web addresses and identifiers untouched, markup shown as text). The map and photo credit is a small information button that opens the full credit and its required links (OpenFreeMap, OpenMapTiles, OpenStreetMap; photo creator, licence, Commons) instead of a strip on the tile.
- **Icon alignment:** date, location and verification rows share one layout, an icon box one text line tall centred on the first line, so wrapped text keeps the icon aligned.
- **Location recovery:** places are also read from the event's own text (a full address in venue text or the description, a park or centre named at the start of a venue sentence, a corner of two streets, room-free venue names), each only as a candidate that the geocoder must verify; street corners are verified against shared OpenStreetMap road nodes (Overpass) and marked approximate. Source coordinates, then structured addresses, remain first.

## Current location coverage

Measured on the saved San Diego and Austin feeds (557 events, 543 not virtual or online): **241 of 557 events (43.3%) have a map** (44.4% of the 543 eligible), up from 231 (41.5%) before the text-recovery work, with none lost. The 67% target was not reached without relaxing verification, which was not done. Remaining: 160 events whose venue the geocoder does not know by name, 91 with no location text, 34 with no close enough match, 14 virtual or online, 11 ambiguous names, 6 street corners that do not meet in the map data. Details and the named examples are in [location-imagery.md](location-imagery.md).

## Known limitations and pending work

- Coverage is bounded by OpenStreetMap: venues absent from it, house numbers it lacks, duplicated addresses (for example "3605 Clairemont Drive" at three points) and parks mapped only as a bay or area are refused rather than guessed. Examples refused on purpose: South Clairemont Winter Carnival, ASCENDKemet Farmers Market, San Diego VisionWalk, The Haunted Trail of Balboa Park (its listed corner, Balboa Drive and Eighth Drive, does not exist in OpenStreetMap), North Park Farmers Market.
- Venue photographs are few (about 5% of events): the identity chain is strict.
- Overpass, the street-corner service, is shared and sometimes unavailable; those lookups wait for a later cycle.
- Classification limits: events whose title does not name the subject keep their old category; a title naming both Halloween and Christmas goes to Halloween; "Christmas Concert" gets the Christmas illustration; there is no illustration for other winter holidays; a bare "Santa" is not a Christmas signal.
- Pending, not started: a way to use trusted location evidence from another record of the same event, building-level abbreviation tables for campus venues (UT Austin rooms), and street-segment locations such as "400 and 500 blocks of 5th Avenue".
- Earlier baselines and their tags are unchanged: `area-events-runtime-stable-2026-10-21`, `area-events-ha-native-runtime-2026-10-21`, `area-events-cubi-runtime-accepted-2026-10-20`, `area-events-pre-ha-refactor-2026-10-20`.
