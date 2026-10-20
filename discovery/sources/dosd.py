"""DoSD (dosd.com) event adapter.

Day listings (/events/YYYY/MM/DD) give title, time, venue name, categories and
an image per event. Each event page embeds schema.org JSON-LD (an Event and
its venue Place with a street address); those pages are fetched at most
`max_details_per_run` times per refresh and cached. The page description is
generic ticketing boilerplate, so no description is taken from DoSD.
"""
import json
import re
from datetime import datetime, timedelta
from html import unescape
from urllib.parse import urljoin

from ..core import FetchError, clock_range, finish_event, localize_pacific, make_event, now_pacific

KEY = "dosd"
NAME = "DoSD"
PUBLISHER = "dosd.com"
BASE = "https://dosd.com"


def _clean(value):
    return re.sub(r"\s+", " ", unescape(value)).strip() if value else None


def _parse_cards(page):
    feed = page.split('class="feed', 1)[-1]
    for block in feed.split('data-card-kind="event"')[1:]:
        href = re.search(r'class="card-link[^"]*" href="([^"]+)"', block)
        title = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
        paragraphs = [_clean(p) for p in re.findall(r"<p class=\"svelte-[a-z0-9]+(?: date| enrichment)?\">(.*?)</p>", block, re.S)]
        date_line = re.search(r'class="svelte-[a-z0-9]+ date">(.*?)</p>', block, re.S)
        image = re.search(r'<img src="([^"]+)"', block)
        if not (href and title and date_line):
            continue
        venue = next((p for p in paragraphs if p and p != _clean(date_line.group(1)) and "·" not in p and "�" not in p and " · " not in p), None)
        enrichment = re.search(r'class="svelte-[a-z0-9]+ enrichment">(.*?)</p>', block, re.S)
        yield {
            "url": urljoin(BASE + "/events/", unescape(href.group(1))),
            "title": _clean(title.group(1)),
            "time_text": (_clean(date_line.group(1)).split("|", 1) + [""])[1].strip(),
            "venue": venue,
            "categories": [c.strip() for c in re.split(r"\s+[·•·]\s+|\s+�\s+", _clean(enrichment.group(1)))][:1] if enrichment else [],
            "image": unescape(image.group(1)) if image else None,
        }


def _parse_detail(page):
    """Event and venue Place from the JSON-LD @graph."""
    for raw in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
        try:
            graph = json.loads(raw).get("@graph", [])
        except (ValueError, AttributeError):
            continue
        event = next((n for n in graph if n.get("@type") == "Event"), None)
        if not event:
            continue
        place_ref = (event.get("location") or {}).get("@id")
        place = next((n for n in graph if n.get("@id") == place_ref), {})
        address = place.get("address") or {}
        street = ", ".join(p for p in (address.get("streetAddress"), address.get("addressLocality")) if p)
        image = event.get("image")
        return {
            "start": datetime.fromisoformat(event["startDate"]) if event.get("startDate") else None,
            "end": datetime.fromisoformat(event["endDate"]) if event.get("endDate") else None,
            "venue": place.get("name"),
            "address": street or None,
            "image": image[0] if isinstance(image, list) and image else image,
            "url": event.get("url"),
        }
    return None


def discover(fetcher, previous, details=True, limit=None, days=3, max_details_per_run=30, **_):
    retrieved_at = now_pacific().isoformat(timespec="seconds")
    today, now = now_pacific().date(), now_pacific()
    cached = {e["url"]: e for e in previous if e.get("extraction", {}).get("detail_page")}
    notes = {"days": days, "listing_items": 0, "no_time_skipped": 0, "past_skipped": 0,
             "detail_fetched": 0, "detail_reused": 0, "detail_failed": 0, "detail_deferred": 0}
    events, seen, listings_ok, budget = [], set(), 0, max_details_per_run

    for offset in range(days):
        day = today + timedelta(days=offset)
        listing_url = f"{BASE}/events/{day.year}/{day.month:02d}/{day.day:02d}"
        try:
            page = fetcher.get(listing_url)
        except Exception as error:
            notes.setdefault("listing_errors", []).append(str(error))
            continue
        listings_ok += 1
        for card in _parse_cards(page):
            notes["listing_items"] += 1
            start_hm, end_hm = clock_range(card["time_text"])
            if not start_hm:
                notes["no_time_skipped"] += 1
                continue
            if card["url"] in seen or (limit is not None and len(events) >= limit):
                continue
            seen.add(card["url"])
            midnight = datetime.combine(day, datetime.min.time())
            start = localize_pacific(midnight.replace(hour=start_hm[0], minute=start_hm[1]))
            end = localize_pacific(midnight.replace(hour=end_hm[0], minute=end_hm[1])) if end_hm else None
            if (end or start) < now:
                notes["past_skipped"] += 1
                continue

            detail, old = None, cached.get(card["url"])
            if old and old["start"][:10] == day.isoformat():
                detail = {"start": datetime.fromisoformat(old["start"]), "venue": old["venue"], "address": old["address"],
                          "image": old["image_url"] if old.get("image_source") == "event page JSON-LD" else None,
                          "url": card["url"]}
                notes["detail_reused"] += 1
            elif details and budget > 0:
                budget -= 1
                try:
                    detail = _parse_detail(fetcher.get(card["url"]))
                    notes["detail_fetched"] += 1
                except Exception as error:
                    notes["detail_failed"] += 1
                    notes.setdefault("detail_errors", []).append(f"{card['url']}: {error}")
            elif details:
                notes["detail_deferred"] += 1

            detail = detail or {}
            event = make_event(
                KEY, NAME, listing_url, card["url"], card["title"], start,
                local_id=card["url"].split("/events/", 1)[-1], end=end.isoformat() if end else None,
                venue=detail.get("venue") or card["venue"], address=detail.get("address"),
                categories=card["categories"], image_url=detail.get("image") or card["image"],
                image_source="event page JSON-LD" if detail.get("image") else ("listing" if card["image"] else None),
            )
            event["extraction"]["detail_page"] = bool(detail)
            evidence = []
            if f"/events/{day.year}/{day.month:02d}/{day.day:02d}/" in card["url"]:
                evidence.append("event URL date matches the day listing it appeared on")
            if detail.get("start"):
                evidence.append("event page JSON-LD startDate matches listing time" if detail["start"] == start
                                else "CONFLICT: event page JSON-LD startDate differs from listing time")
            if detail.get("url") == card["url"]:
                evidence.append("event page JSON-LD url equals listing URL")
            corroborated = bool(detail.get("start")) and len(evidence) >= 3 and not any(e.startswith("CONFLICT") for e in evidence)
            events.append(finish_event(event, retrieved_at, evidence, corroborated))
    if not listings_ok:
        raise FetchError("; ".join(notes.get("listing_errors", ["no day listing readable"])))
    return events, notes
