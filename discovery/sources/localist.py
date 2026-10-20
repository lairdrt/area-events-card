"""Localist calendar adapter (public, key-less JSON API).

Localist is a hosted event-calendar platform used by universities, cities and
libraries. Every Localist site exposes the same read-only API
(`<site>/api/2/events`) with exact start/end times (with UTC offset), venue,
address, image and, usually, the venue's latitude/longitude. One adapter
therefore serves any number of sites; each is configured as its own source:

    {"key": "ut_austin", "adapter": "localist", "site": "https://calendar.utexas.edu",
     "name": "UT Austin Events", "days": 14}

Coordinates are taken only from the API's `geo` block, never guessed; events
without them are kept and classified `unknown` by the shared location filter.
All-day entries (exhibits, deadlines) have no start time and are skipped.
"""
import json
import re
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlparse

from ..core import FetchError, finish_event, make_event, now_pacific

ACCEPTS_LOCATION = True
PAGE_SIZE = 100


def _host(site):
    return urlparse(site).hostname or site


def _clean(value):
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) and value.strip() else None


def _geo(raw):
    try:
        latitude, longitude = float(raw["latitude"]), float(raw["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    return {"latitude": latitude, "longitude": longitude, "source": "Localist API geo"}


def _event_from(event, instance, key, name, site, listing_url, retrieved_at, timezone_name):
    start = datetime.fromisoformat(instance["start"])
    end = datetime.fromisoformat(instance["end"]) if instance.get("end") else None
    filters = event.get("filters") or {}
    categories = [c["name"] for c in filters.get("event_types") or [] if c.get("name")]
    address = _clean(event.get("address"))
    geo = _geo(event.get("geo"))
    record = make_event(
        key, name, listing_url, event.get("localist_url") or event.get("url"), _clean(event.get("title")), start,
        local_id=f"{event['id']}:{instance['id']}", end=end.isoformat() if end else None,
        venue=_clean(event.get("location_name") or event.get("location")), address=address,
        description=_clean(event.get("description_text")), categories=categories,
        image_url=event.get("photo_url") or None, image_source="Localist API photo_url" if event.get("photo_url") else None,
        price="Free" if event.get("free") else _clean(event.get("ticket_cost")),
    )
    if timezone_name:
        record["timezone"] = timezone_name
    if geo:
        record["geo"] = geo
    evidence = [f"start time from the {_host(site)} Localist API event instance ({instance['start']})"]
    page_host = urlparse(record["url"] or "").hostname
    if page_host and (page_host == _host(site)):
        evidence.append("event page is on the same Localist site")
    if geo:
        evidence.append(f"venue coordinates from the API ({geo['latitude']}, {geo['longitude']})")
    in_range = event.get("first_date") and event.get("last_date") and event["first_date"] <= start.date().isoformat() <= event["last_date"]
    return finish_event(record, retrieved_at, evidence, corroborated=bool(in_range and page_host == _host(site)))


def discover(fetcher, previous, details=True, limit=None, days=14, max_pages=5, site=None, key="localist", name=None,
             location=None, **_):
    """Return (events, notes). Raises FetchError if the API cannot be read."""
    site = site.rstrip("/")
    name = name or _host(site)
    retrieved_at = now_pacific().isoformat(timespec="seconds")
    now = datetime.now().astimezone()
    timezone_name = (location or {}).get("timezone")
    notes = {"site": site, "api_events": 0, "instances": 0, "all_day_skipped": 0, "not_live_skipped": 0, "ended_skipped": 0, "pages": 0}
    events, seen = [], set()

    for page in range(1, max_pages + 1):
        url = f"{site}/api/2/events?days={int(days)}&pp={PAGE_SIZE}&page={page}"
        try:
            payload = json.loads(fetcher.get(url))
        except ValueError as error:
            raise FetchError(f"{url}: not JSON ({error})") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise FetchError(f"{url}: unexpected response (no events list)")
        notes["pages"] += 1
        for wrapper in payload["events"]:
            event = wrapper.get("event") if isinstance(wrapper, dict) else None
            if not event or not event.get("id"):
                continue
            notes["api_events"] += 1
            if event.get("status") != "live":
                notes["not_live_skipped"] += 1
                continue
            for item in event.get("event_instances") or []:
                instance = (item or {}).get("event_instance") or {}
                if not instance.get("start") or not instance.get("id"):
                    continue
                notes["instances"] += 1
                if instance.get("all_day"):
                    notes["all_day_skipped"] += 1
                    continue
                start = datetime.fromisoformat(instance["start"])
                if datetime.fromisoformat(instance["end"] if instance.get("end") else instance["start"]) < now:
                    notes["ended_skipped"] += 1
                    continue
                identity = (event["id"], instance["id"])
                if identity in seen or (limit is not None and len(events) >= limit):
                    continue
                seen.add(identity)
                events.append(_event_from(event, instance, key, name, site, f"{site}/", retrieved_at, timezone_name))
        total_pages = (payload.get("page") or {}).get("total", page)
        if page >= total_pages:
            break
    return events, notes


def make_source(spec):
    """Bind the shared adapter to one configured Localist site."""
    site = spec.get("site")
    if not isinstance(site, str) or not site.startswith(("http://", "https://")):
        raise ValueError(f"source '{spec.get('key')}' needs a 'site' URL")
    host = _host(site)
    labels = host.split(".")
    key, name = spec["key"], spec.get("name") or host

    def bound(fetcher, previous, **options):
        options = {k: v for k, v in options.items() if k not in ("adapter", "site", "name", "key")}
        return discover(fetcher, previous, site=site, key=key, name=name, **options)

    return SimpleNamespace(
        KEY=key, NAME=name, PUBLISHER=".".join(labels[-2:]) if len(labels) > 2 else host,
        COVERAGE=spec.get("coverage") or {"kind": "configured"}, ACCEPTS_LOCATION=True, discover=bound,
    )
