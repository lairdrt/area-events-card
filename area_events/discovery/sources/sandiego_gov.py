"""City of San Diego events listing adapter (https://www.sandiego.gov/events).

The listing is server-rendered with a title, a written date/time line and a
description per event; the event page adds a location. There is no
machine-readable data and no event imagery, so images are left empty. Date
lines that are not a single day with a start time (multi-day runs, "all day")
are skipped rather than guessed.
"""
from datetime import datetime  # noqa: F401  (kept for callers of the earlier module)

from ..core import FetchError, finish_event, make_event, now_pacific
# The page reading belongs to the Discovery Service's producer for this source; the earlier engine's adapter below only assembles its own event dictionaries from it.
from ..service.producers.city_of_san_diego import (DETAIL_VERSION, LISTING_URL, NAME, REPEATED_ADDRESS, directions_address,  # noqa: F401
                                                   parse_detail as _parse_detail, parse_listing as _parse_listing, parse_start_end as _parse_start_end)
from .detailwork import DetailWork

KEY = "sandiego_gov"


def discover(fetcher, previous, details=True, limit=None, pages=4, max_details_per_run=20, source_id=KEY, source_name=NAME, url=LISTING_URL, **_):
    retrieved_at = now_pacific().isoformat(timespec="seconds")
    now = now_pacific()
    work = DetailWork(previous, lambda e: e["id"], max_details_per_run, enabled=details, version=DETAIL_VERSION)
    notes = {"pages": pages, "listing_items": 0, "unparsed_date_skipped": 0, "past_skipped": 0}
    events, seen, pages_ok = [], set(), 0

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

            ident, failed = f"{source_id}:{local_id}", False
            detail, old = None, work.reusable(ident)
            if old:
                detail = {"venue": old["venue"], "address": old.get("address"), "date_on_page": old["verification"]["status"] == "corroborated"}
                work.reused()
            elif work.should_fetch(ident):  # what is not fetched now is picked up by a later refresh
                try:
                    detail = _parse_detail(fetcher.get(item["url"]), item["title"], start)
                    work.done()
                except Exception as error:
                    work.failed(ident, item["url"], error)
                    failed = True

            event = make_event(source_id, source_name, url, item["url"], item["title"], start, local_id=local_id,
                               end=end.isoformat() if end else None, venue=(detail or {}).get("venue"),
                               address=(detail or {}).get("address"), description=item["description"])
            work.stamp(event, ident, detail is not None, failed)
            evidence = [f"listing date line '{item['date_text']}' parsed as a single-day Pacific time start"]
            date_on_page = bool(detail and detail.get("date_on_page"))
            if date_on_page:
                evidence.append("event page repeats the title and the event date")
            if event.get("address"):
                evidence.append("address from the event page's Get Directions line (it names a street that the page's location text also names)")
            finish_event(event, retrieved_at, evidence, corroborated=date_on_page)
            events.append(event)
    if not pages_ok:
        raise FetchError("; ".join(notes.get("listing_errors", ["no listing page readable"])))
    titles = {}
    for event in events:
        if event.get("address"):
            titles.setdefault(event["address"].lower(), set()).add(event["title"])
    general = {address for address, names in titles.items() if len(names) >= REPEATED_ADDRESS}
    for event in events:
        if event.get("address") and event["address"].lower() in general:
            event["address"] = None  # the same address on several unrelated events is not a venue
            event["verification"]["missing"] = [f for f in ("end", "venue", "address", "description", "image_url") if not event.get(f)]
            event["verification"]["evidence"] = [e for e in event["verification"]["evidence"] if "Get Directions" not in e]
            notes["general_address_dropped"] = notes.get("general_address_dropped", 0) + 1
    return events, work.notes(notes)
