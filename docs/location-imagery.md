# Location imagery: venue photographs and maps

For an event without a photograph of its own, the card can show a **verified photograph of the venue** or a **map of where it is**, drawn from open data. This replaces nothing: events and their own photographs are unchanged, and everything here is an optional enrichment that arrives later, in the background.

## What the card shows

Image priority (each step hands over to the next when an image is missing or fails to load):

1. the event's own photograph, then verified alternatives from other matched records (no badge);
2. a verified photograph of the actual venue, badge **VENUE**, with its credit and licence;
3. a map of the event's location, badge **MAP**, with a pin, the venue name and a small credit button;
4. the category illustration, labelled "Illustration".

Generic category photographs are **not** part of the selection (the curated set described in [category-photography.md](category-photography.md) is kept on disk but unused).

`show_images: false` shows none of 2 and 3. The pin, badges, venue name and credit button are elements of the card, never part of an image. Alt text says what each image is ("Map of the event location: Viejas Arena (approximate position)", "Photograph of the venue, Viejas Arena, not of the event").

## Maps

*Why not USGS.* The USGS National Map was measured first ([location-imagery-evaluation.md](location-imagery-evaluation.md)). Its Topo service is a raster cache whose finest level (zoom 16, about 2.4 m per pixel) is the limit: tiles beyond it do not exist (HTTP 404), and exports at 1280 x 720 and 1920 x 1080 show the same stair-stepped building outlines and the same soft labels as 640 x 360, because they are upscaled. Its dynamic services have no building footprints. So it cannot be sharp on a high-density display and was not used.

*What is used.* [OpenFreeMap](https://openfreemap.org) serves OpenStreetMap-based **vector** tiles with no key, no account and no cost. One tile at zoom 14 covers about 2.4 km, so a city's venues share a handful of tiles. The engine (standard library only: a small vector-tile reader and an SVG writer in `area_events/discovery/maps.py`) draws the 480 m by 270 m around a position as an **SVG** in two colour schemes with identical geometry: light, and graphite with orange main roads. SVG is sharp at any size and pixel density, is 12 to 50 KB, has no script, no text except street names, and no external reference. Nothing is rendered by a heavy framework and nothing is added to the app's dependencies.

*Terms (from the operator's pages).* OpenFreeMap: free, no limit on requests, no API key; automated downloads are allowed "as long as each download is at most 3,000 tiles or 50 MB", with at most 4 parallel requests and a unique User-Agent (this uses one tile per area, one request at a time, and caches them); required credit "OpenFreeMap © OpenMapTiles Data from OpenStreetMap". The data is OpenStreetMap's (ODbL; a drawn map is a "produced work" that needs the credit, which the card shows as a link to openstreetmap.org/copyright). The terms do not address caching explicitly; the explicit allowance for downloads is what this relies on.

## Credits

A tile carries no attribution text. A small round information button (bottom left, 22 px with a 44 px touch target, a real `<button>` with an accessible name, `aria-expanded` and `aria-controls`) opens the full credit as a panel over the image; the button, Escape or a change of image closes it, and it works the same by mouse, keyboard and touch. For a map the panel reads "Map: OpenFreeMap © OpenMapTiles Data from OpenStreetMap" (OpenFreeMap's required text), each part linking to openfreemap.org, openmaptiles.org and openstreetmap.org/copyright, and says when the pin is approximate; for a venue photograph it gives the creator, licence and the Commons file page. The event's own photographs have no button.

## Trusting a position

A pin is drawn only when the position is trustworthy; the reason and evidence are stored with it (`location.confidence`, `precision`, `provenance`) and refusals are cached with their reason.

- **source**: coordinates the event's own source supplied for the venue (the engine classified them *confirmed*). Not approximate, though a building coordinate is not an entrance.
- **address**: a street address whose **house number and street name** both match the geocoder's result (street-type and direction words are normalised), in the right locality, inside the search area. Marked approximate: it says "somewhere on this property".
- **venue**: a named place the geocoder returned with **exactly** the venue's name, a place-like type (not a city, road or district), inside the search area, and no second candidate of that name elsewhere. Marked approximate.
- **intersection**: a location given only as the corner of two streets and no house number ("A and B", "A & B", "A at B", "Corner of A and B", also in the address field; "Eighth" and "8th" are the same). Both sides must look like streets (they need a street-type word, so "Arts and Crafts" is not one). The first street is geocoded within the listing's locality (or the search area) and must come back as a road of that name; then OpenStreetMap's road network is asked (Overpass, one query per corner, at most one every 10 s, 60 s timeout, a failure is retried later and never cached as a refusal) for ways named like either street, and the pin is placed on the **node the two streets actually share**. Streets that merely pass near each other, two junctions of the same pair (ambiguous), a junction outside the search area, or a street that is absent or in another locality are refused with the reason. A different street type ("Drive" in the listing, "Avenue" on the map) is accepted only when the name and everything else agrees, and the difference is recorded in the provenance. Marked approximate; no photograph is sought; coordinates are never estimated from the text or from the city centre. Source coordinates, when the source gives trustworthy ones, still take priority. A one-word street without a type ("Broadway") is not recognised as a street, so "5th Avenue and Broadway" is not read as a corner.

Never used: city-centre estimates, partial addresses, a corner whose streets do not meet in the map data, near-miss names ("Texas Union" never becomes "Greater Texas Federal Credit Union"), ambiguous names, results outside the area. Geocoding is OpenStreetMap Nominatim, limited to the area's country when known; its policy allows a long-running program 4 lookups a minute and requires caching, so lookups are 15 s apart and cached for 60 days (refusals 14 days).

## Venue photographs

A photograph is accepted only through an identity chain, all of which is recorded:

1. the geocoded place carries an OpenStreetMap `wikidata` link, **or** exactly one Wikipedia article whose title equals the venue's name stands within 250 m of the verified position;
2. the Wikidata item's label or an alias equals the venue's name;
3. its coordinates agree with the verified position within 250 m;
4. it names an image (`P18`) that is a JPEG photograph on Wikimedia Commons (not a logo, map or plan by file name), at least 800 px wide;
5. the licence is CC0, public domain, CC BY or CC BY-SA with no usage restriction; non-commercial, no-derivatives, GFDL and unknown licences are refused.

The card shows "Photo: *creator* · *licence* · Wikimedia Commons" with links to the licence and the file page (when a file names no creator, the credit is the link to its Commons page, which is how those licences allow attribution). The thumbnail (about 640 to 960 px) is stored by the app and served from the integration; it is not re-encoded, and the licence, creator, file page, Wikidata item and evidence are kept in the cache record. Verification is strict, so coverage is low (see below); a venue that merely has a similarly named article gets a map instead.

## Caching, load and storage

- Work happens in a background thread of the app, **only while no profile is being discovered**, a few places per cycle (at most 8 new places, then 20 s to 10 min of rest). Events are published as before and never wait for imagery; if Wikimedia, OpenFreeMap or Nominatim are unreachable the events are unaffected and the lookup is retried in a later cycle.
- Results are cached on disk by a stable key (place) and a versioned file name (content hash plus the renderer version): about 40 KB of maps (two variants) and 60 to 240 KB for a photograph per place, tiles cached 14 days (at most 120 files, about 25 MB at most), records bounded to 2,000, unreferenced files removed. The same venue is looked up once however many events use it.
- Files reach the dashboard through the integration (`/area_events/media/<name>`): names must match a strict pattern, SVGs are served with a policy forbidding scripts and any resource load, responses are cacheable for a year (the names change when content does). The app's own API needs the token; the integration forwards with it.
- A request that is a pure `since` poll still learns about new imagery: the app reports a `revision` (0 while there is none), part of the token the card sends.
- Outbound requests go through the safe fetcher (public addresses only, pinned connections, size and time limits) to fixed hosts: nominatim.openstreetmap.org, overpass-api.de (intersections only), tiles.openfreemap.org, en.wikipedia.org, www.wikidata.org, commons.wikimedia.org, upload.wikimedia.org and thumb.wikimedia.org. robots.txt is not consulted for these documented APIs. Everything fetched is treated as untrusted data (validated, bounded, text escaped); images are never executed.
- **Off switch:** the app option *Venue photographs and maps* (`location_imagery`, default on) stops all lookups and attaches nothing.

## Light and dark

The card reads the effective Home Assistant theme (`hass.themes.darkMode`) on every update, falling back to the browser's colour scheme only when Home Assistant gives none. A dark theme requests only the graphite map, a light theme only the light one, and a change of theme swaps the maps on screen at once with no refresh.

## Limits

- **Measured coverage** (real San Diego and Austin feeds, 557 events, 240 distinct places, 2026-10-21): **235 events (42.2%) got a map** (106 places resolved: 38 from source coordinates, 38 from an exact house-number match, 30 from an exact venue-name match); 134 places were refused (102 not found by the geocoder, 28 with no sufficiently close match, 4 ambiguous). **26 events (4.7%, 9 venues) got a verified venue photograph** (Viejas Arena, Fleet Science Center, Petco Park, Balboa Theatre, San Diego Museum of Art, La Valencia Hotel, Cactus Cafe, LBJ School of Public Affairs; licences CC BY 2.0 to 4.0 and CC BY-SA 2.0 and 3.0). Stored data for that run: about 15 MB (tiles 7 MB, maps and photographs 7.5 MB; an average map is 26 KB).
- A multi-part venue ("LBJ School of Public Affairs, Bass Lecture Hall") is matched on its first part; the photograph then shows that building, which the alt text names as the venue.
- An address or venue pin is approximate within the property; a building coordinate is not an entrance.
- Street names appear where they fit; business names are not drawn on the map (the card shows the venue name).
- The first fill of a large area takes hours because of the geocoder's rate; it never affects event discovery.
- **The Haunted Trail of Balboa Park** (venue text "On the corner of Balboa Drive and Eighth Drive."): the text is read as a corner, but OpenStreetMap has no "Eighth Drive" (nor a "8th Drive"), its only "Balboa Drive" is a short stub, and the nearest "8th Avenue" does not connect to it, so the check fails and the event keeps its illustration instead of a guessed pin. The city's event page embeds a map query with an invalid latitude, so its coordinates are not used either. If OpenStreetMap is corrected, the corner is found at the next cycle after the 14-day refusal expires.
- Overpass is a shared public service that is sometimes slow or unavailable; those lookups simply wait for a later cycle.

## Event text

Sources sometimes leave HTML entities in what they publish ("Porto Vista Hotel &amp; Suites"). The engine decodes them once, where events enter (and for events cached before the fix), in the title, venue, address, description, organizer, price, neighbourhood, category tags and source label. Only complete entities are decoded (named, decimal and hexadecimal; unknown names and invalid numbers are left as written; "AT&T" and "Q&A" are untouched), a second pass happens only for a double-encoded entity ("&amp;amp;"), and web addresses and identifiers are never changed. Markup is not interpreted: the card shows every field as text.

## Staying put

A map that has loaded stays across polls, filters and background updates (a tile whose event is unchanged keeps its image element). It goes only when the app stops listing it, which happens when the event's location evidence changes. If the other theme's variant fails to load after a map has loaded, the card keeps the variant that loaded and tries again at the next theme change. A tile whose images all failed (a busy or restarting app) tries again on later polls, at least a minute apart and three times at most. The app lists a map or photograph only while its files are stored, and draws a place again if its files have gone.

## Places named in the event's own text

Besides source coordinates, the address field and a venue name, a place is now also read from text the event already carries (no extra web requests: `discovery/textloc.py` only reads text, and every result is still verified by the geocoder before a pin is drawn). In order of preference:

1. a full street address in the venue text (and, when the venue is empty or only a sentence, in the description): house number, street, optional unit, a city only when a state code follows it or it is the search city. "At 4325 Wightman" (no street type) counts only after the word "at". More than one different address in the text means none is used;
2. a corner in the address field, then in the venue field (a house number first means it is an address, and each side may have at most five words, so a sentence containing "and" is not a corner);
3. the venue name, with a room, suite or floor suffix removed as an additional query ("SEAY Library - Room 4.244"); a venue field that is a sentence is never searched as a name, but a park or centre named at its start ("De Anza Cove Park is a scenic…") is;
4. an address in the description when the venue is a name (tried only if the name is refused), then a "corner of A and B" / "intersection of A and B" phrase.

At most three candidates are chained; the next is tried only after the one before is refused, and the record keeps every reason. An address taken from text must match on house number and street name, in the search area's city when the text names none, and the pin says "the address was taken from the event's description / venue text". A result whose name differs from the venue's only by the search city ("Hard Rock Hotel San Diego" for "Hard Rock Hotel") is accepted; any other different name is not. Refusals recorded under the earlier rules are retried once, only for places the new rules read differently.

### Measured on the saved San Diego and Austin feeds (557 events, 543 not virtual/online)

| | before | after |
|---|---|---|
| events with a map | 231 (41.5%) | 241 (43.3%; 44.4% of the 543 eligible) |

Gained: 4 by a street address (structured address fields that earlier rules had not looked up), 3 by an address read from the text (Hillcrest Farmers Market and two others), 2 by a venue name (one only because the city suffix is set aside), 1 by a corner. No event lost its map. Remaining 316 events: 160 geocoder found nothing (venue not in OpenStreetMap under that name), 91 no location text, 34 no close enough match, 14 virtual/online, 11 ambiguous names, 6 corners that do not meet in the map data. 0 pending, 2 temporary Overpass failures (retried).

The named examples: Hillcrest Farmers Market is placed (1717 University Ave. in San Diego). South Clairemont Winter Carnival: "3605 Clairemont Drive" exists at three points in OpenStreetMap about 600 m apart, so it is refused as ambiguous. ASCENDKemet: OpenStreetMap has no house number 4325 on Wightman Street. VisionWalk: OpenStreetMap has "De Anza Cove" only as a natural bay, not a park. Haunted Trail of Balboa Park and North Park Farmers Market: the streets do not meet in OpenStreetMap. None of these is given a guessed pin.
