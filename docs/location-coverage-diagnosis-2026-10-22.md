# Why events lack a verified map: diagnosis (2026-10-22)

A diagnostic over the saved San Diego and Austin feeds (557 events, the same set and denominator as the baseline in [imagery-runtime-accepted-2026-10-22.md](imagery-runtime-accepted-2026-10-22.md)): 241 events have a verified map, 316 do not. Nothing in the application was changed; event-level reports are kept outside Git (`review/`). The few live lookups made (the sources' detail pages, a bounded name search and 11 street addresses on Nominatim) went through the existing rate limits.

## The 316 events without a map, one primary cause each

| Primary cause | Events | % of 557 | % of the 316 |
|---|---|---|---|
| Unfinished processing: the source's detail page (with the venue's address) was not fetched yet | 105 | 18.9% | 33.2% |
| Extracted, but the geocoder found nothing (Austin campus rooms and buildings; 3 San Diego venues) | 97 | 17.4% | 30.7% |
| Location genuinely absent from the record (Austin: no venue, address or coordinates) | 65 | 11.7% | 20.6% |
| Extracted, but verification rejected it (near-miss names or numbers 14, ambiguous results 9, a corner not in the road network 1) | 24 | 4.3% | 7.6% |
| Online or virtual | 14 | 2.5% | 4.4% |
| Ambiguous or conflicting source information (Haunted Trail, one car show) | 6 | 1.1% | 1.9% |
| Location present on the page but our extraction did not capture it | 5 | 0.9% | 1.6% |

There is no temporary failure left over: 0 places were pending in the media service and 2 transient Overpass errors were retried. The "unfinished" row is different: the San Diego Reader, DoSD and City of San Diego adapters fetch only a limited number of detail pages per run (111, 27 and 18 events were deferred in this first-run snapshot), and the address is on the detail page. Those events are still waiting, not refused.

## Rates

- **Extraction:** a location candidate was produced for 462 of 557 events (82.9%). Of the 95 without one, 65 have no location at all, 20 are waiting for their detail page, 5 had a location the extractor missed, 4 are virtual, 1 is conflicting. Where the record holds location evidence, extraction succeeded for about 98.7% (462 of roughly 468); text extraction misses are 5 to 15 events.
- **Geographic resolution:** 241 of 462 candidates resolved (52.2%); excluding events waiting for detail pages (85) and virtual ones (10), 241 of 367 (65.7%).
- **Detail pages decide the San Diego numbers:** where the detail page was fetched, 78% of Reader events (18 of 23) and 82% of DoSD events (14 of 17) have a map; where it is still deferred, 33% (37 of 112) and 36% (8 of 22). City of San Diego pages carry the address on a "Get Directions" line the adapter does not read (1 of 20 mapped).

## Recurring mechanisms

1. **Address not yet fetched or not captured at the source** (about 110 events: the 105 waiting plus the 5 whose City page line was not captured): the page lists the venue's full address, for example Porto Vista Hotel and SideYard Coffee on the Reader, Whistle Stop Bar and NOVA SD on DoSD, and every City of San Diego page.
2. **Campus rooms and buildings that OpenStreetMap does not name that way** (about 94 Austin events): "SEAY Library - Room 4.244", UT building codes ("SRH 3.122"), "Gregory Gym Lounge". A bounded name-only search found nothing for 90 of 93 distinct unresolved venue names, so the gap is in the map data (or needs a building-code table), not in how we ask.
3. **No location in the record** (65 Austin events): application windows, committee meetings, programs.
4. **Street-closure and block events** whose text gives blocks, not a point (Gaslamp Artisan Market, Mercato).
5. **Duplicated or missing OpenStreetMap data** that verification correctly refuses (ambiguous "3605 Clairemont Drive", no house number 4325 on Wightman Street, De Anza Cove mapped only as a bay).
6. **A name carrying its locality** ("Austin, Texas, Moody Center"): the real name is the third part (10 events); a name search finds Moody Center and Gregory Gymnasium.

## The five named examples

| Event | What the record and page hold | Candidate and result |
|---|---|---|
| South Clairemont Winter Carnival | description: South Clairemont Recreation Center, 3605 Clairemont Drive; page directions line: the same | address candidate; OpenStreetMap has that number at three points about 600 m apart, so refused as ambiguous |
| ASCENDKemet Farmers Market | text: "between 43rd and Fairmount at 4325 Wightman"; directions line repeats it | address candidate; OpenStreetMap has no house number 4325 there, so refused |
| San Diego VisionWalk | venue sentence names De Anza Cove Park; directions line: 3000 N Mission Bay Dr | park candidate; OpenStreetMap has only "De Anza Cove" as a bay, not a park; the directions address is a near-miss, so refused |
| Haunted Trail of Balboa Park | corner Balboa Drive and Eighth Drive; page coordinates invalid (324.06); other listings of the event say Juniper Road | corner candidate; the streets do not meet in OpenStreetMap; conflicting source information |
| Hillcrest Farmers Market | address in the venue text (1717 University Ave.); the directions line says 1601 University Ave | address candidate, verified, mapped |

## Which bottleneck matters most

Not text extraction (A): well under 3% of the misses. The opportunity is in **getting the source's own address** (the adapters' detail pages: about 110 events, 35% of the misses) and, second, in **geographic resolution of Austin campus venues** (about 94 events, 30%), where the venue is named but not findable by name. Truly missing information (C) is about a fifth of the misses and mostly Austin administrative events.

An optimistic projection, assuming the deferred events resemble the fetched ones, puts San Diego at roughly 55% overall after the detail pages are fetched and the City address line is read (inference, not measured); it does not reach 67% without a campus-building table. Of 11 distinct City "Get Directions" addresses, 7 verify on Nominatim; the directions line can name an organizer's address rather than the event (the car show's), so such addresses still need the existing verification.

## Would spaCy help?

Plausibly not for the failures observed. Candidates were produced for almost every event that had location evidence, and the refusals come from the geocoder and map data, not from missing or mis-split place names. The text-level misses (a locality-prefixed name, a directions line) are fixed by rules. Named-entity recognition might find an occasional place in free descriptions, but those places would still need to be found and verified in OpenStreetMap.

## Recommended next experiment

Let the three San Diego adapters finish their detail pages (a few ordinary refresh cycles with the existing caps, or one run with a higher cap) and read the City of San Diego "Get Directions" address, then re-measure on the same 557 events with the unchanged verifier. This changes no thresholds, tests the largest bucket, and shows how much of the 33% "unfinished" group becomes maps. A second, separate experiment: a small UT Austin building-code and name table (from the university's own building list) tried against the 94 campus events.
