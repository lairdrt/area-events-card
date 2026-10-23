# Austin source (Milestone 2E)

**Source:** the University of Texas at Austin public events calendar, https://calendar.utexas.edu, read through its public, key-less Localist JSON API (`/api/2/events?days=14&pp=100&page=N`, hourly, up to 5 pages).

**Why this one:** it is an official, structured, public calendar with real upcoming events (about 340 in two weeks), exact start and end times with the UTC offset, venue names, addresses for about 40% of them and venue coordinates for many. The existing `sources/localist.py` adapter already reads it, so the work was one small wrapper (`LocalistAdapter` in `service/adapters.py`) and one registered source (`ut-austin` in `KNOWN_SOURCES`). Reads go through SafeFetcher (robots.txt, per-host spacing) like every other source. Times use the calendar's own offsets; `America/Chicago` is only recorded as the calendar's zone.

**What it is not:** Austin's city, community or commercial listings. It is a university calendar, so most events are campus events open to the public or to students. The Austin card therefore says "matched by source", not "everything in Austin", and no distance is verified.

**Added to the service, not specific to Austin:** JSON API responses are now searched for evidence quotes (string values of the stored response), so Localist events carry quoted titles and fields like HTML-sourced ones do.

**Wiring:** the source is registered by `register_known_sources()` (the service does this at start) and attached to the existing Austin area through the control API (`update_area`, revision-checked). Nothing else in the pipeline changed.

**Known quality limits:**
- About 9% of the entries (32 of 341 at the first read) have a start of exactly 00:00 local time: the calendar gives that for some date-only items (offers, deadlines) without flagging them all-day. They are kept as given.
- About 18% of the events have no location at all and 7% only a description; a third carry a street address. These are marked unverified or unknown on the card.
- Event pages, categories and images from the source are not used; the card shows its category illustrations.
- The integration labels the source "Ut Austin" (a title-cased key); a name entry for `ut_austin` in `custom_components/area_events/feed.py` would fix it at the next integration deployment.
- The area view carries every upcoming event of the area (hundreds), with descriptions, in each push to the card.
