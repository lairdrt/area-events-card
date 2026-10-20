"""City of San Diego events listing adapter (https://www.sandiego.gov/events).

The listing is server-rendered with a title, a written date/time line and a
description per event; the event page adds a location. There is no
machine-readable data and no event imagery, so images are left empty. Date
lines that are not a single day with a start time (multi-day runs, "all day")
are skipped rather than guessed.
"""
import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin

from ..core import FetchError, clock_range, finish_event, html_to_text, localize_pacific, make_event, now_pacific

KEY = "sandiego_gov"
NAME = "City of San Diego"
LISTING_URL = "https://www.sandiego.gov/events"
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_LINE = re.compile(rf"^\w+, ({MONTHS}) (\d{{1,2}}), (\d{{4}}),\s*(.+)$")


def _clean(value):
    return re.sub(r"\s+", " ", unescape(value)).strip() if value else None


def _parse_listing(page, listing_url=LISTING_URL):
    for block in page.split('class="grid-x grid-margin-x event-listing-large"')[1:]:
        title = _clean(re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S) and re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S).group(1))
        date_text = _clean(re.search(r'class="post__date[^>]*>\s*<strong>(.*?)</strong>', block, re.S) and
                           re.search(r'class="post__date[^>]*>\s*<strong>(.*?)</strong>', block, re.S).group(1))
        link = re.search(r'<a href="(/event/[^"]+)"', block)
        info = block.split('</p>', 1)[1] if '</p>' in block else ""
        description = html_to_text(info.split('<p><a href="/event/')[0].replace("<br>", "\n")) if info else None
        if title and date_text and link:
            yield {"title": title, "date_text": date_text, "url": urljoin(listing_url, link.group(1)), "description": description or None}


def _parse_start_end(date_text):
    match = DATE_LINE.match(date_text)
    if not match:
        return None, None
    start_hm, end_hm = clock_range(match.group(4))
    if not start_hm:
        return None, None
    day = datetime.strptime(f"{match.group(1)} {match.group(2)} {match.group(3)}", "%B %d %Y")
    start = localize_pacific(day.replace(hour=start_hm[0], minute=start_hm[1]))
    end = localize_pacific(day.replace(hour=end_hm[0], minute=end_hm[1])) if end_hm else None
    return start, end


def _parse_detail(page, title, start):
    text = unescape(re.sub(r"<[^>]+>", " ", page.split("<main", 1)[-1]))
    match = re.search(r"Location:\s*(.+?)(?:\s{2,}|-->|Get Directions)", text, re.S)
    day = f"{start:%B} {start.day}, {start.year}"
    return {"venue": _clean(match.group(1)) if match else None,
            "date_on_page": _clean(text) is not None and day in _clean(text) and title in _clean(text)}


def discover(fetcher, previous, details=True, limit=None, pages=4, max_details_per_run=20, source_id=KEY, source_name=NAME, url=LISTING_URL, **_):
    retrieved_at = now_pacific().isoformat(timespec="seconds")
    now = now_pacific()
    cached = {e["id"]: e for e in previous if e.get("extraction", {}).get("detail_page")}
    notes = {"pages": pages, "listing_items": 0, "unparsed_date_skipped": 0, "past_skipped": 0,
             "detail_fetched": 0, "detail_reused": 0, "detail_failed": 0, "detail_deferred": 0}
    events, seen, pages_ok, budget = [], set(), 0, max_details_per_run

    for number in range(pages):
        page_url = url if number == 0 else f"{url}?page={number}"
        try:
            page = fetcher.get(page_url)
        except Exception as error:
            notes.setdefault("listing_errors", []).append(str(error))
            continue
        pages_ok += 1
        for item in _parse_listing(page, url):
            notes["listing_items"] += 1
            start, end = _parse_start_end(item["date_text"])
            if not start:
                notes["unparsed_date_skipped"] += 1
                continue
            if (end or start) < now:
                notes["past_skipped"] += 1
                continue
            slug = item["url"].rstrip("/").rsplit("/", 1)[-1]
            local_id = f"{slug}@{start.date().isoformat()}"
            if local_id in seen or (limit is not None and len(events) >= limit):
                continue
            seen.add(local_id)

            detail, old = None, cached.get(f"{source_id}:{local_id}")
            if old:
                detail = {"venue": old["venue"], "date_on_page": old["verification"]["status"] == "corroborated"}
                notes["detail_reused"] += 1
            elif details and budget > 0:
                budget -= 1
                try:
                    detail = _parse_detail(fetcher.get(item["url"]), item["title"], start)
                    notes["detail_fetched"] += 1
                except Exception as error:
                    notes["detail_failed"] += 1
                    notes.setdefault("detail_errors", []).append(f"{item['url']}: {error}")
            elif details:
                notes["detail_deferred"] += 1

            event = make_event(source_id, source_name, url, item["url"], item["title"], start, local_id=local_id,
                               end=end.isoformat() if end else None, venue=(detail or {}).get("venue"),
                               description=item["description"])
            event["extraction"]["detail_page"] = detail is not None
            evidence = [f"listing date line '{item['date_text']}' parsed as a single-day Pacific time start"]
            date_on_page = bool(detail and detail.get("date_on_page"))
            if date_on_page:
                evidence.append("event page repeats the title and the event date")
            finish_event(event, retrieved_at, evidence, corroborated=date_on_page)
            events.append(event)
    if not pages_ok:
        raise FetchError("; ".join(notes.get("listing_errors", ["no listing page readable"])))
    return events, notes
