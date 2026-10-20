"""KPBS (San Diego public media) event adapter.

KPBS exposes no JSON-LD, feed or documented API for events, so this reads the
server-rendered markup. The listing page carries one machine-readable time per
event (the "add to calendar" links hold exact UTC start/end); each event page
adds venue, address, description and a large image.

Adapter contract: NAME, and discover(fetcher, previous, details, limit) ->
(events, notes). Events are plain dicts as built by _normalize().
"""
import re
from datetime import datetime, timezone
from html import unescape

from ..core import TIMEZONE_NAME, html_to_text, parse_compact_utc, to_pacific

KEY = "kpbs"
NAME = "KPBS"
PUBLISHER = "kpbs.org"
LISTING_URL = "https://www.kpbs.org/events/all"
PROMO_SPLIT = '<ps-promo class="EventPromoC"'
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
IMAGE_SOURCE_DETAIL = "event page og:image"


def _first(pattern, text, flags=re.S):
    match = re.search(pattern, text, flags)
    return match.group(1) if match else None


def _clean(value):
    return re.sub(r"\s+", " ", unescape(value)).strip() if value else None


def _parse_clock(text):
    """'5 PM' / '4:30 PM' -> (hour, minute) in 24h."""
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))? ([AP])M", text.strip())
    hour = int(match.group(1)) % 12 + (12 if match.group(3) == "P" else 0)
    return hour, int(match.group(2) or 0)


def _listing_text_start(text):
    """Naive local start parsed from 'Tuesday, October 20, 2026 at 4:30 PM' (or 'from 5 PM to 9 PM')."""
    match = re.search(rf"({MONTHS}) (\d{{1,2}}), (\d{{4}}) (?:at|from) (\d{{1,2}}(?::\d{{2}})? [AP]M)", text or "")
    if not match:
        return None
    hour, minute = _parse_clock(match.group(4))
    day = datetime.strptime(f"{match.group(1)} {match.group(2)} {match.group(3)}", "%B %d %Y")
    return day.replace(hour=hour, minute=minute)


def _parse_promo(block):
    url = _first(r'EventPromoC-title"\s*>\s*<a href="([^"]+)"', block)
    if not url:
        return None
    image = None
    img = _first(r"(<img[^>]*>)", block)
    if img:
        srcset = _first(r'srcset="([^"]+)"', img)
        image = _first(r'\ssrc="([^"]+)"', img)
        if srcset:  # "<url> 2x": the larger rendition
            image = srcset.rsplit(" ", 1)[0]
    # Exact UTC start/end live in the "add to Google Calendar" link.
    times = re.search(r"dates=(\d{8}T\d{6}Z)/(\d{8}T\d{6}Z)", block)
    return {
        "url": url,
        "title": _clean(_first(r'EventPromoC-title"\s*>\s*<a[^>]*>(.*?)</a>', block)),
        "time_text": _clean(_first(r'EventPromoC-time-text">(.*?)</span>', block)),
        "start_utc": parse_compact_utc(times.group(1)) if times else None,
        "end_utc": parse_compact_utc(times.group(2)) if times else None,
        "venue": _clean(_first(r'EventPromoC-venue[^>]*>\s*<svg.*?</svg>(.*?)</div>', block)),
        "price": _clean(_first(r'EventPromoC-price[^>]*>\s*<svg.*?</svg>(.*?)</div>', block)),
        "categories": [_clean(c) for c in re.findall(r'EventPromoC-categories-item"><a[^>]*>(.*?)</a>', block)],
        "image": unescape(image) if image else None,
    }


def _parse_detail(page):
    """Venue, address, description and image from an individual event page."""
    description = _first(r'<div class="EventPageSmall-description">(.*?)</div>\s*<div class="EventPageSmall-actions"', page)
    start = _first(r'data-platform="ics-file"[^>]*data-start-time="(\d{8}T\d{6}Z)"', page)
    image = _first(r'<meta property="og:image:url" content="([^"]+)"', page)
    return {
        "canonical": _first(r'<meta property="og:url" content="([^"]+)"', page),
        "image": unescape(image) if image else None,
        "venue": _clean(_first(r'EventPageSmall-information-venueName">\s*<svg.*?</svg>(.*?)</div>', page)),
        "street": _clean(_first(r'PlaceInformation-address-streetAddress">(.*?)</div>', page)),
        "city": _clean(_first(r'PlaceInformation-address-city">(.*?)</span>', page)),
        "description": html_to_text(description) if description else None,
        "start_utc": parse_compact_utc(start) if start else None,
    }


def _normalize(promo, detail, retrieved_at):
    start_utc, end_utc = promo["start_utc"], promo["end_utc"]
    start = to_pacific(start_utc)
    end = to_pacific(end_utc) if end_utc else None
    detail = detail or {}

    address = ", ".join(p for p in (detail.get("street"), detail.get("city")) if p) or None
    venue = detail.get("venue") or promo["venue"]
    image = detail.get("image") or promo["image"]
    evidence, missing = [], []

    # Evidence: independent signals tying this event page to this date.
    slug_date = re.search(r"/events/(\d{4})/(\d{2})/(\d{2})/", promo["url"])
    if slug_date and tuple(map(int, slug_date.groups())) == (start.year, start.month, start.day):
        evidence.append("event URL date matches calendar-link start date")
    listed = _listing_text_start(promo["time_text"])
    if listed and listed == start.replace(tzinfo=None):
        evidence.append(f"listing text '{promo['time_text']}' matches calendar-link UTC start in {TIMEZONE_NAME}")
    elif listed:
        evidence.append(f"CONFLICT: listing text '{promo['time_text']}' differs from calendar-link start {start.isoformat()}")
    if detail.get("start_utc"):
        same = detail["start_utc"] == start_utc
        evidence.append("event page ICS start matches listing" if same else "CONFLICT: event page ICS start differs from listing")
    if detail.get("canonical") == promo["url"]:
        evidence.append("event page og:url equals listing URL")

    for field, value in (("end", end), ("venue", venue), ("address", address),
                         ("description", detail.get("description")), ("image", image)):
        if not value:
            missing.append(field)
    if not detail:
        missing.append("event page not fetched")

    corroborated = (len(evidence) >= 3 and not any(e.startswith("CONFLICT") for e in evidence)
                    and bool(detail.get("start_utc")))
    return {
        "id": "kpbs:" + promo["url"].split("/events/", 1)[1],
        "title": promo["title"],
        "start": start.isoformat(),
        "end": end.isoformat() if end else None,
        "timezone": TIMEZONE_NAME,
        "venue": venue,
        "address": address,
        "description": detail.get("description"),
        "image_url": image,
        "image_source": (IMAGE_SOURCE_DETAIL if detail.get("image") else "listing") if image else None,
        "url": promo["url"],
        "categories": promo["categories"],
        "price": promo["price"],
        "source": {"name": NAME, "listing_url": LISTING_URL},
        "retrieved_at": retrieved_at,
        # Verification policy: extraction from one site never sets verified=True.
        # "corroborated" only means this site's own signals agree with each other.
        "verified": False,
        "verification": {
            "status": "corroborated" if corroborated else "unverified",
            "evidence": evidence,
            "missing": missing,
            "note": "Single-source; verified stays false until independently confirmed.",
        },
    }


def _detail_from_cache(old, start_utc):
    """Reuse previously fetched event-page data so refreshes stay light on the site."""
    street, _, city = (old.get("address") or "").partition(", ")
    return {
        "canonical": old["url"],
        "image": old.get("image_url") if old.get("image_source") == IMAGE_SOURCE_DETAIL else None,
        "venue": old.get("venue"),
        "street": street or None,
        "city": city or None,
        "description": old.get("description"),
        "start_utc": start_utc,
    }


def discover(fetcher, previous, details=True, limit=None, **_):
    """Return (events, notes). Raises FetchError if the listing cannot be read."""
    now = datetime.now(tz=timezone.utc)
    retrieved_at = to_pacific(now).isoformat(timespec="seconds")
    page = fetcher.get(LISTING_URL)
    notes = {"listing_promos": 0, "ongoing_skipped": 0, "past_skipped": 0, "no_time_skipped": 0,
             "detail_fetched": 0, "detail_reused": 0, "detail_failed": 0}

    cached = {e["url"]: e for e in previous if e.get("source", {}).get("name") == NAME}
    events = []
    for block in page.split(PROMO_SPLIT)[1:]:
        notes["listing_promos"] += 1
        promo = _parse_promo(block)
        if not promo or not promo["title"]:
            continue
        if "/events/ongoing/" in promo["url"] or (promo["time_text"] or "").startswith("Ongoing"):
            notes["ongoing_skipped"] += 1  # exhibits / recurring runs, not discrete events
            continue
        if not promo["start_utc"]:
            notes["no_time_skipped"] += 1
            continue
        if (promo["end_utc"] or promo["start_utc"]) < now:
            notes["past_skipped"] += 1
            continue
        if limit is not None and len(events) >= limit:
            break

        detail = None
        old = cached.get(promo["url"])
        if old and old.get("start") == to_pacific(promo["start_utc"]).isoformat() and old.get("description"):
            detail = _detail_from_cache(old, promo["start_utc"])
            notes["detail_reused"] += 1
        elif details:
            try:
                detail = _parse_detail(fetcher.get(promo["url"]))
                notes["detail_fetched"] += 1
            except Exception as error:  # one bad event page must not sink the run
                notes["detail_failed"] += 1
                notes.setdefault("detail_errors", []).append(f"{promo['url']}: {error}")
        events.append(_normalize(promo, detail, retrieved_at))
    return events, notes
