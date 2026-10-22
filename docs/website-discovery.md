# Website discovery and detail pages (first version)

How Area Events finds events on ordinary websites, built on the existing adapters (nothing was replaced). It applies to user-added websites (`sources`) and to the automatic sources that read a listing and then each event's own page.

## What already existed (reused)

- `SafeFetcher` (public addresses only, pinned connections, validated redirects, robots.txt, per-host spacing, size and time limits, binary mode) for every user-entered address.
- The generic website adapter (`sources/web.py`): schema.org JSON-LD and microdata, same-site ICS and RSS/Atom feeds, repeated HTML listing blocks, bounded pagination and detail pages, a request and time budget per website, a 15-minute shared crawl cache, `probe` for the card's warnings, skipping of past, undated, online and cancelled events, duplicates by title and start.
- Per-adapter detail fetching for the San Diego Reader, DoSD and City of San Diego, with events cached by the engine and handed back as `previous`.

## What was added

1. **`sources/detailwork.py`: one resumable detail queue** used by the Reader, DoSD and City adapters (their own detail parsers are unchanged). State lives in the cached events (`extraction.detail_page`, `detail_v`, `detail_failures`, `detail_retry_after`), so nothing new is stored. A completed page is reused; a failed page waits 15 minutes, doubling to at most 12 hours, while the budget goes to the other events (a broken page used to be asked again first on every run and held up everything behind it); a page whose parsing finds nothing is treated as a failure; adapters raise `detail_v` when they learn to read more, and older reads are redone. The per-run budgets (Reader 30, DoSD 30, City 20) and the 2 s spacing are unchanged, so the work always progresses and the request rate does not.
2. **City of San Diego "Get Directions" address.** Every event page carries it; it is now used when it is a house number and street (not the coordinates some pages show), when the page's own location text does not name only other streets, and when it does not head three or more unrelated events (an office or footer address). Otherwise it is dropped, and the evidence line and `address` field say where it came from.
3. **`sources/webdiscover.py`: where a site keeps its events.** When the page given has no events: public Google Calendar embeds (an iframe, a Duda calendar widget) are turned into their published feeds; up to four same-site links that look like a calendar, upcoming events, a bulletin or an event are read one hop deep (navigation, accounts, giving, shops, archives, feeds and other sites never score); linked PDFs and flyer images are noted. JSON-wrapped pages (Duda sites answer with `{"content": "<html>..."}`) are unwrapped.
4. **`sources/webtext.py`: events written in text, and PDF text.** Only when nothing structured was found. A line becomes an event only with a calendar date, a start time and a title of its own; posting dates, deadlines, history, navigation and footers are not events; a venue is only an explicit "Location:" label next to the event and an address only one written in that same text. Text events never duplicate or replace a structured one (same day and hour, similar title). Text-based PDFs are read with the standard library (Flate streams, ToUnicode tables for two-byte fonts); scanned, image-only or unreadable PDFs and flyer images are reported in the notes with their links.

Bounds per website: the existing 30 requests and 100 seconds, at most 4 hub pages, 2 PDFs and 2 embedded calendars; all fetches go through the same safe fetcher and the same limits.

## Measured (2026-10-22, real sites)

**San Diego sources, four consecutive refresh passes from the saved first-run cache (the same budgets as before):**

| | saved first run | after 4 passes |
|---|---|---|
| events with an address | 73 of 240 (30%) | 212 of 332 (64%) |
| City of San Diego events with an address | 0 of 38 | 20 of 40 (the rest have no usable directions address: coordinates, street blocks, organizers) |
| Reader pages completed | 23 of 137 | 119 of 228 (109 still waiting; 30 per pass) |
| DoSD pages completed | 29 of 57 | 60 of 60 |
| City pages completed | 20 of 38 | 40 of 40 |

The event counts differ because the listings cover the next few days from the day they are read (the second run is later); they are not extra events found by the new code. No page failed in these passes, so the new backoff was exercised by tests only. Map coverage after the passes was **not measured**: 166 new San Diego place candidates (mostly the new addresses) have not been through the geocoder, which runs at the provider's 4 lookups a minute (about 40 minutes); the earlier baseline stays 241 of 557 (43.3%).

**Ordinary websites (twelve tried, old versus new code; five did not resolve or refused access):** Little Italy's site went from 0 to 1 genuine upcoming event (Trick-or-Treat on India Street, 25 October, found by following its events link and reading the text; the title was taken from the line after the date, "Family-Fun Halloween", which is the event's subtitle, so the title is not the best one); North Park Main Street and Balboa Park were already read by their feeds and are unchanged; the others had no events in readable form (the Rancho Bernardo chamber redirects to the San Diego chamber's calendar, whose dated events found had already passed; San Diego Air and Space's calendar pages hold no text dates). No false events were produced on the sites tried. **St. Michael's Poway** (the difficult site): the old code called it JavaScript-only; the new code reads the JSON-wrapped page, finds the embedded Google Calendar (stmikespoway@gmail.com) and its feed is refused by Google's robots.txt, so the calendar cannot be read; the site's pages hold only the Fall Festival (9 and 10 October, already past) and sponsorship PDFs. It still yields no events, and now says why.

## Not done, and why

- **Unchanged-page detection** (ETag, Last-Modified, content hashes kept between runs): needs persistent per-URL state outside the cached events. Today a page is read again each run, except completed event pages, which are reused.
- **Raising the per-run detail budgets** to clear the Reader's backlog faster (about four more passes at 30): kept as is to preserve request rates.
- **Layout-aware text titles** (title above or below its date line) and recurring events written in text ("Every Saturday").
- **Image flyers and scanned PDFs** are kept as links only.
- **Church bulletins as PDFs** work only when text-based; fonts without ToUnicode tables are reported as unreadable.

## Next step for image flyers

Queue each flyer image or scanned PDF (cap per run, cached by content hash) and read it with Tesseract in the app container (about 30 MB plus English language data, no model download, a second or two a page on a Raspberry Pi 4), then pass the text through `webtext.events_in` and the location verifier unchanged. Keep the result marked "image text" with lower trust, never merge it over a structured event, and show the flyer link as provenance. A local language model is not needed for dates, times and addresses.
