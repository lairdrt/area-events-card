# Source acquisition pilot: San Diego craft fairs (Milestone 2G)

Proof that Area Events can find event sources it was not told about and turn them into events through the normal pipeline.

**What it is.** One new source, `sd_craft_fairs` (adapter `craft_fairs`), registered like the City and Austin sources. Its options name three **seed pages**: two public vendor listings of the craft fairs the vendor sells at
(sweetandquirky.com, my2dragonflies.com) and one organizer page (Holy Family parish). Everything else is found by following links.

**How a run works** (`service/producers/craft_fairs.py`, reading rules in `service/producers/craft_fairs/`; no browser, no JavaScript, depth one, at most 10 followed pages, all through the shared SafeFetcher: public addresses, robots.txt, per-host spacing):

1. Each seed is read. A listing page yields its dated cards (title, date, clock time, the place the card writes, the card's links).
2. A card becomes a candidate only if it names a **craft fair** (craft fair/show/sale, handmade or artisan fair or marketplace, makers market; a farmers or flea market, a job or science fair, a street festival is not) and a **San Diego County place** (a county place name or a 919xx-921xx ZIP; a card under a San Diego heading that names a place is accepted; an address in another state is not).
3. The links of the accepted cards are classified: social networks (need a login), shorteners, shops, payment and map links, documents and the listing's own site are **rejected with the reason**; the rest are fetched.
4. Each fetched page is assessed (names a craft fair? San Diego County? a dated event of its own?) and stored as a **lead** in `source_leads` (migration 6): URL, host, kind, accepted/rejected, reasons, score, the page it was found on, the link text, event count, first/last seen. The pages themselves are stored like every other page, attributed to the source.
5. Listings and organizer pages that describe one event (same day and street address, or the same distinctive title words) are merged into **one candidate**. The organizer's own page wins where it states a date, time, place or title; each difference is reported (`extraction.conflicts`, the evidence list and the run notes). Evidence lists every listing and page the event was read from; extraction quotes them.
6. The candidate goes through the existing pipeline: stored candidate, ExtractionService, EventQuery, the HA feed adapter. Category "Craft Fair" is set as the candidate's source category; the card's own keywords classify the same events by title and description (the feed carries no categories).

**Not done / limits**
- Depth one only; the vendor listing must use the card markup these two sites use (a list item or article with a heading, a date line, a clock time and an address); another layout needs its own reader behind the same interface.
- An organizer page without a date (a school's "see the flyer" page) is evidence for the event a listing dates, not an event.
- Facebook event pages are never read. A listing's own photo is not used; only the organizer's event page image (Open Graph or schema.org) is.
- A seed that is itself an event page and names no place takes the operator's declared region (`seed_region`); its location stays unknown unless the page gives an address.
- The vendors' lists were verified on 2026-10-24; they change, and every run reads them again.

**Deploying it** needs the new code on the Cubi (migration 6 runs at start), the source registered (the service registers `KNOWN_SOURCES` at start) and attached to the San Diego area with the control API (`update_area`, revision-checked), and the integration's source-name entry deployed to Home Assistant.
