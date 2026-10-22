# Location-based fallback imagery: evaluation (2026-10-21)

Investigation and one prototype only. Nothing in the card, engine, photo pipeline or classifier was changed, and no map or venue image is committed. The scripts are in [experiments/location-imagery/](../experiments/location-imagery/); their data and the prototype images are in the git-ignored `review/location-imagery/`.

**Proposed fallback order** (evaluated here): event photo, then a verified venue photo, then a map with a pin, then the category photograph, then the SVG illustration.

## 1. Location coverage and precision

Method: the real registered sources were run through the real engine for San Diego, CA and Austin, TX (25 miles, 2026-10-21): 240 + 317 = **557 events**. Counts are from each event's `venue`, `address`, `geo` and the engine's `place` classification. "Full street address" means a house number and street name; "identifiable venue" means a short name that is not a URL, an address, "online" or a paragraph.

| | San Diego (240) | Austin (317) | Both (557) |
|---|---|---|---|
| Coordinates supplied by the source | 0 (0%) | 123 (38.8%) | **123 (22.1%)** |
| Full street address | 66 (27.5%) | 113 (35.6%) | 179 (32.1%) |
| Identifiable venue name | 209 (87.1%) | 240 (75.7%) | 449 (80.6%) |
| Partial address or city only | 73 (30.4%) | 11 (3.5%) | 84 (15.1%) |
| Vague, online or paragraph-length text | 11 (4.6%) | 18 (5.7%) | 29 (5.2%) |
| No location at all | 18 (7.5%) | 58 (18.3%) | 76 (13.6%) |

Exclusive tiers (each event counted once, best evidence first): **A** source coordinates 123 (22.1%); **B** street address only 66 (11.8%); **C** venue name only 260 (46.7%); **D** city-only 1; **E** missing or ambiguous 107 (19.2%).

Source detail: only the UT Austin (Localist) source supplies coordinates (123 of 317 events); no San Diego source does. Engine `place` classes: San Diego 73 estimated (city centre, never fit for a pin) and 167 unknown; Austin 123 confirmed and 194 unknown.

Real examples:
- **A**, usable pin: "New Roots: Cultivating Black Mental Wellness", GWB 2.116, 210 W 24th St, Austin (30.2878, -97.7399, from the source).
- **B**: The Adicts at Music Box SD, 1337 India Street, San Diego (address only).
- **C**: "Wine Lips" at Soda Bar; "Murda Beatz" at Spin Nightclub (a name, nothing else).
- **D/E**: "Virtual Talk - Understanding Mannerism" (venue "Online Internet Events"); "The Haunted Trail of Balboa Park" (no venue); Ocean Beach Farmers' Market (venue is a paragraph about the street closure).

**Geocoded coordinates are not automatically precise.** I geocoded a random sample of 60 unique venue/address pairs (30 per city) with Nominatim (1 request per second, descriptive User-Agent):

- Street-address queries: 21; geocoded 19; the house number matched exactly in **17 (89%)**.
- Wrong or doubtful results even then: "SDSU: Main Stage Theatre, 5500 Campanile Drive" geocoded to **Student Health Services** at that number (a plausible but wrong building); "Clark-Caven Courts, 301 Jester Circle" came back as a bare address.
- Name-only queries: 39; geocoded 17 (44%); the venue's name genuinely appears in the result for 15 (38%), and one of those is a false match ("Texas Union" resolved to *Greater Texas Federal Credit Union*). "Austin, TX" and "SSB" resolved to generic or different places.

So a pin can honestly be placed for: source coordinates (22.1% of events), street addresses with an exact house-number match (about 11.8% × 0.89, so roughly 10%, at building or block accuracy, to be labelled approximate), and name-only venues only where the full venue name is found in the result (roughly a third of that 46.7%, with a residual false-match rate that a strict name check must drive down). A realistic upper bound is **about half of events**, and a conservative one **about a third** (source coordinates plus exact street matches).

## 2. Venue photographs

- **Photos from the event's own source:** already used by the engine (`images.py`), with provenance. Nothing more to gain; this is tier 1.
- **Official venue websites** (an `og:image`): copyright rests with the venue, there is no licence to copy or display it, and scraping it is exactly what to avoid. Not recommended.
- **Google Places photos:** needs a key and billing, and its terms restrict storing the images. Rejected.
- **Wikipedia/Wikimedia Commons:** the only open source with licence metadata. In the 60-venue sample, 8 venues had a Wikipedia article within 120 m whose title resembled the venue, **7 with a lead photograph** (about 12%): Viejas Arena, Carlsbad City Library, Escondido Public Library, Fleet Science Center, Frontwave Arena, Clock Knot and others. Licences found: CC BY-SA 3.0 (2) and 4.0 (5). **Identity is the risk:** a loose name match paired "LandShark Bar & Grill" with the unrelated *Tivoli Bar and Grill* 97 m away. Only an exact match (coordinates and full venue name or a Wikidata link) is acceptable, which cuts that 12% to roughly 10%.
- **Rights:** CC BY-SA needs visible attribution (author, licence, link) and share-alike for adaptations. That is workable for an image the app downloads and caches locally (it is not redistributed in the repository), but the card tile would need credit text.
- **Cost and complexity:** two extra lookups per venue (geosearch, then file metadata), cached forever per venue; small. **Coverage is landmarks only** (libraries, arenas, museums, campuses), not bars, clubs or rooms.

Verdict: feasible but low coverage (about 10% of events) with a real identity-mismatch risk. Worth doing later as a strict, optional tier; not the first step.

## 3. Static map options

| | Google Static Maps | OSM public tile servers (stitched) | Hosted OSM-based (e.g. Geoapify) | USGS The National Map (US only) |
|---|---|---|---|---|
| Key and billing | Key plus enabled billing on every project | No key; a proper User-Agent and Referer are required | Free key | **None** |
| Free allowance | 10,000 Static Maps events per month, then $2 per 1,000 (verified on Google's pricing page) | No numeric limit stated; "heavy or inappropriate use" is blocked | 3,000 credits a day; a static map costs 1 + tiles/4 + icons credits, so about 3 to 5 each, roughly 600 to 1,000 maps a day | No limit stated on the pages I could read |
| Storing the images | Google's terms are restrictive about storing and re-serving Maps content; I could not retrieve the exact text (the terms pages returned truncated) and I rely on your note and on general knowledge. Treat as prohibited | **Bulk and pre-emptive tile downloading and offline use are prohibited**; a map for 250 events fetched in advance is that. Tiles may be cached only as ordinary viewing needs (Cache-Control, at least 7 days) | The terms page I read says nothing about storing; **unverified**, ask Geoapify | USGS-produced data are public domain (the copyright page says so); **service-specific usage terms and rate limits not found**: unverified |
| Attribution | Required (Google logos and notices) | "© OpenStreetMap contributors", visible | Geoapify credit **and** OpenStreetMap credit, mandatory on the free plan | Credit "U.S. Geological Survey"; the service's copyright text lists USGS, Census, USFS, Natural Earth, State Dept, NOAA |
| Quality at card size | Best | Good | Good | Good for streets and buildings (topographic style with contours); labels shrink at tile size |
| Fit for an open-source HA project | Poor: every user needs a Google Cloud billing account | Poor for static or bulk use (policy) | Medium: every user needs a key; terms unverified | **Good** for US users; **no coverage outside the US** |
| Pi 4 impact | One request per map | Many tile requests plus image stitching (Pillow is not in the app) | One request per map | One request per map, no rendering |

MapTiler, Stadia and Mapbox: my searches returned nothing usable, so they are **not evaluated**. A self-hosted renderer from OSM extracts has clear rights (ODbL) but needs a rendering stack (Mapnik or a vector renderer plus data) that does not fit the stdlib-only Alpine app on a Pi 4, so I did not pursue it. Interactive maps (Leaflet inside the browser, as Home Assistant's own map card does) follow the tile policy for normal viewing but are heavy for 250 tiles and are not static imagery.

**OSM data being open does not mean `tile.openstreetmap.org` may be used to generate maps in advance.** The policy text I fetched prohibits bulk downloading and offline use and says nothing that allows static-map generation.

**Best fit found: the USGS National Map export service.** A single HTTPS request returns a finished map image for any bounding box (`.../USGSTopo/MapServer/export?bbox=...&size=640,360&format=jpg`): no key, no billing, no tile stitching, no image library on the Pi. Measured: 640x360 JPEG about 36 KB (PNG about 98 KB), 0.3 to 0.5 s per request. The data are US public domain, which suits an open-source project. Limits: **United States only**; I could not find the service's own terms of use or rate limits (the policy pages I could reach did not cover web services), so politeness and a short contact with USGS are advisable before shipping.

## 4. Prototype

Two real events, rendered with the USGS export plus a pin and label drawn locally. Images: `review/location-imagery/maps/viejas-sheet.jpg` (San Diego) and `ut-gwb-sheet.jpg` (Austin), each showing 450 m, 900 m and 1,800 m across, at full size and scaled to card width. They are not committed.

- **Viejas Arena** (5500 Canyon Crest Drive; geocoded POI whose name matches the venue; also has a CC BY-SA Wikipedia photo) and the **Austin GWB building** (coordinates supplied by the source).
- **Readability:** the 450 m view reads well at full size, and street names survive at tile width only partly; 900 m is the better default for context but its labels become small at tile width. The pin must be large; a pin drawn by the card (an SVG overlay) stays sharp at any size. A "Map: event location" label and the USGS credit are visible, so it cannot be mistaken for a photograph.
- **Size:** about 10 to 40 KB as WebP, 24 to 36 KB as JPEG straight from the service.

## 5. Recommended image hierarchy

1. The event's own photograph, then verified alternatives from other matched records (unchanged).
2. A strictly verified venue photograph (later, optional, Wikimedia only, with credit).
3. A map with a pin **only when the position is trustworthy**: source coordinates, or an address whose house number matched exactly (labelled approximate), or a POI whose full name matched. City centres are never used.
4. The category photograph (private preview) or illustration as today.
5. The SVG illustration.

A map always carries a "Map" label, a pin, alt text "Map of the event location: <venue>" and the data credit.

## 6. Implementation recommendation (for approval)

Smallest dependable path:

1. **Engine:** add a small, optional location step for US events: take source coordinates when present; otherwise geocode the street address through the existing shared geocoder, with its result type and exact house-number match recorded, and keep the result in the existing geocode cache. Record `location_quality` (`source`, `address`, `poi`, `none`); never reuse the city-centre estimates.
2. **Maps:** one USGS export request per unique rounded position (about 4 decimal places), spaced out, stored as a JPEG in the app's data folder, with the credit in a field. For about 250 events that is roughly 200 unique maps, about 7 MB and roughly 4 minutes the first time, then only new venues. No Pillow and no new dependency.
3. **Serving:** the integration serves the cached maps beside the photographs (a content-addressed file name, cacheable), and the card draws the pin as an SVG overlay with the label and credit.
4. **Option that avoids all storage:** the card loads the USGS image directly in the browser for the few visible tiles. It is simpler (no engine work, no cache) but sends viewers' IP addresses to USGS and depends on a service whose terms I could not read. I would not choose it as the default.
5. **Outside the US,** or when location quality is `none`, nothing changes: the category photograph or illustration shows.

Open items before building: confirm the USGS service terms (a short question to USGS), decide whether approximate address-level pins are acceptable (I recommend yes, labelled), and decide whether to pursue Wikimedia venue photographs at all given about 10% coverage.

## 7. Limits of this investigation

- Coverage numbers are from one run per city on one day, with regex-based classification of free-text venues; they are indicative, not exact.
- The precision sample is 60 venues; percentages carry wide margins.
- I did not verify Google's storage terms from the source, nor Geoapify's storage terms, nor any USGS service terms; MapTiler, Stadia and Mapbox were not evaluated.
- No Raspberry Pi measurement was made; the load figures are from the request sizes and times seen on this PC.
