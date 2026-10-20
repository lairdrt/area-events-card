"""San Diego Reader event adapter.

Day listings (/events/YYYY/mon/DD/, allowed by robots.txt; the query-string
search pages are disallowed and never requested) give title, time, venue,
image and categories. Each event page carries a schema.org Event JSON-LD with
an exact start time, address, image and description; those pages are fetched
at most `max_details_per_run` times per refresh and cached afterwards.
"""
import json
import re
from datetime import datetime, timedelta
from html import unescape
from urllib.parse import urljoin

from ..core import clock_range, finish_event, html_to_text, localize_pacific, make_event, now_pacific

KEY = "sandiegoreader"
NAME = "San Diego Reader"
PUBLISHER = "sandiegoreader.com"
BASE = "https://www.sandiegoreader.com"
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _day_url(day):
    return f"{BASE}/events/{day.year}/{MONTHS[day.month - 1]}/{day.day:02d}/"


def _first(pattern, text):
    match = re.search(pattern, text, re.S)
    return match.group(1) if match else None


def _clean(value):
    return re.sub(r"\s+", " ", unescape(value)).strip() if value else None


def _parse_items(page, day):
    for block in page.split('class="event-item')[1:]:
        href = _first(r'href="([^"]+)" class="event-title"', block)
        title = _clean(_first(r'class="event-title">(.*?)</a>', block))
        if not href or not title or "/events/ongoing/" in href:
            continue
        time_text = _clean(_first(r'event-time[^>]*>\s*<div>(.*?)</div>', block))
        image = _first(r"background-image: url\('([^']+)'\)", block)
        if image and "events_default_icon" in image:
            image = None  # the site's generic placeholder, not event imagery
        yield {
            "url": urljoin(BASE, unescape(href)),
            "title": title,
            "time_text": time_text,
            "location": _clean(_first(r'class="event-location">(.*?)</div>', block)),
            "image": unescape(image) if image else None,
            "categories": [_clean(c) for c in re.findall(r'class="place-type event-type">(.*?)</a>', block)],
            "day": day,
        }


def _parse_detail(page):
    """The schema.org Event JSON-LD of an event page, flattened."""
    for raw in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Event":
            place = data.get("location") or {}
            address = place.get("address") or {}
            street = ", ".join(p for p in (address.get("streetAddress"), address.get("addressLocality")) if p)
            start = data.get("startDate")
            image = data.get("image")
            return {
                "start": datetime.strptime(start, "%Y-%m-%dT%H:%M%z") if start else None,
                "venue": place.get("name"),
                "address": street or None,
                "description": html_to_text(unescape(data["description"])) if data.get("description") else None,
                "image": image[0] if isinstance(image, list) and image else image,
                "url": data.get("url"),
            }
    return None


def _build(item, detail, retrieved_at, listing_url):
    start_hm, end_hm = clock_range(item["time_text"] or "")
    start = localize_pacific(datetime.combine(item["day"], datetime.min.time()).replace(hour=start_hm[0], minute=start_hm[1]))
    end = localize_pacific(datetime.combine(item["day"], datetime.min.time()).replace(hour=end_hm[0], minute=end_hm[1])) if end_hm else None
    detail = detail or {}
    event_id = _first(r"[?&]et=(\d+)", item["url"]) or item["url"].rstrip("/").rsplit("/", 1)[-1]
    event = make_event(
        KEY, NAME, listing_url, item["url"], item["title"], start,
        local_id=event_id, end=end.isoformat() if end else None,
        venue=detail.get("venue") or item["location"], address=detail.get("address"),
        description=detail.get("description"), categories=item["categories"],
        image_url=detail.get("image") or item["image"],
        image_source="event page JSON-LD" if detail.get("image") else ("listing" if item["image"] else None),
    )
    event["extraction"]["detail_page"] = bool(detail)
    evidence = []
    if f"/events/{item['day'].year}/{MONTHS[item['day'].month - 1]}/{item['day'].day:02d}/" in item["url"]:
        evidence.append("event URL date matches the day listing it appeared on")
    if detail.get("start"):
        same = detail["start"] == start
        evidence.append("event page JSON-LD startDate matches listing time" if same
                        else "CONFLICT: event page JSON-LD startDate differs from listing time")
    if detail.get("url") and detail["url"].split("?")[0] == item["url"].split("?")[0]:
        evidence.append("event page JSON-LD url equals listing URL")
    corroborated = bool(detail.get("start")) and len(evidence) >= 3 and not any(e.startswith("CONFLICT") for e in evidence)
    return finish_event(event, retrieved_at, evidence, corroborated)


def discover(fetcher, previous, details=True, limit=None, days=3, max_details_per_run=30, **_):
    """Return (events, notes). Raises FetchError if no day listing could be read."""
    retrieved_at = now_pacific().isoformat(timespec="seconds")
    today = now_pacific().date()
    cached = {e["url"]: e for e in previous if e.get("extraction", {}).get("detail_page")}
    notes = {"days": days, "listing_items": 0, "no_time_skipped": 0, "past_skipped": 0,
             "detail_fetched": 0, "detail_reused": 0, "detail_failed": 0, "detail_deferred": 0}
    events, seen, listings_ok = [], set(), 0
    budget = max_details_per_run
    now = now_pacific()

    for offset in range(days):
        day = today + timedelta(days=offset)
        listing_url = _day_url(day)
        try:
            page = fetcher.get(listing_url)
        except Exception as error:
            notes.setdefault("listing_errors", []).append(str(error))
            continue
        listings_ok += 1
        for item in _parse_items(page, day):
            notes["listing_items"] += 1
            start_hm, _ = clock_range(item["time_text"] or "")
            if not start_hm:
                notes["no_time_skipped"] += 1
                continue
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            if limit is not None and len(events) >= limit:
                continue
            detail = None
            old = cached.get(item["url"])
            if old and old["start"][:10] == item["day"].isoformat():
                detail = {"start": datetime.fromisoformat(old["start"]), "venue": old["venue"], "address": old["address"],
                          "description": old["description"], "url": item["url"],
                          "image": old["image_url"] if old.get("image_source") == "event page JSON-LD" else None}
                notes["detail_reused"] += 1
            elif details and budget > 0:
                budget -= 1
                try:
                    detail = _parse_detail(fetcher.get(item["url"]))
                    notes["detail_fetched"] += 1
                except Exception as error:
                    notes["detail_failed"] += 1
                    notes.setdefault("detail_errors", []).append(f"{item['url']}: {error}")
            elif details:
                notes["detail_deferred"] += 1  # picked up by a later refresh
            event = _build(item, detail, retrieved_at, listing_url)
            if datetime.fromisoformat(event["end"] or event["start"]) < now:
                notes["past_skipped"] += 1
                continue
            events.append(event)
    if not listings_ok:
        from ..core import FetchError
        raise FetchError("; ".join(notes.get("listing_errors", ["no day listing readable"])))
    return events, notes
