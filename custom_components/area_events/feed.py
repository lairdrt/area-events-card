"""From the Event API's records to what the card shows. No Home Assistant imports.

A small, versioned adapter: the service's `discovery.events.v1` event becomes the event dictionary the card already understands
(`start`/`end` as ISO times, `venue`, `address`, `url`, `verification`). Nothing is added that the service did not say: there are no
coordinates, no confidence and no category; the location status is passed through so the card can say when a place is unverified.
"""
import types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FEED_SCHEMA = "area-events.feed.v1"
SOURCE_NAMES = types.MappingProxyType({"sandiego_gov": "City of San Diego"})
UNVERIFIED_LOCATIONS = frozenset(("description_only", "unknown"))


def _iso(epoch, zone_name):
    if epoch is None:
        return None
    try:
        zone = ZoneInfo(zone_name) if zone_name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc
    return datetime.fromtimestamp(epoch, tz=zone).isoformat(timespec="seconds")


def source_name(source_key):
    """A readable source name; data from a fixture source says so, so it is never mistaken for a live discovery."""
    name = SOURCE_NAMES.get(source_key) or str(source_key).replace("_", " ").title()
    return f"{name} (fixture data, not live)" if str(source_key).endswith("_fixture") else name


def to_feed_event(event):
    """One `discovery.events.v1` event record -> the card's event dictionary."""
    zone = event.get("timezone")
    sources = event.get("sources") or []
    status = event.get("location_status") or "unknown"
    media = event.get("media") or {}
    image = media.get("image")
    result = {
        "id": event["event_key"],
        "title": event["title"],
        "start": _iso(event["starts_at"], zone),
        "end": _iso(event.get("ends_at"), zone),
        "timezone": zone,
        "venue": event.get("venue_name") or event.get("location_text"),
        "address": event.get("address"),
        "description": event.get("description"),
        "url": event.get("source_url"),
        "categories": [],
        "source": {"name": source_name(sources[0]) if sources else None, "key": sources[0] if sources else None},
        "verified": False,
        "verification": {"status": "unverified", "evidence": [], "missing": list(event.get("missing") or []),
                         "note": "Read from the source's own listing; not independently confirmed."},
        "images": [image["url"]] if image else [],
        "image_credit": {"text": image.get("credit"), "license": image.get("license"), "source_url": image.get("source_url")} if image else None,
        "media": {key: media[key] for key in ("photo", "map") if media.get(key)} or None,
        "illustration": event.get("illustration"),
        "location_status": status,
        "location_verified": status == "address",
        "revision": event.get("revision"),
        "updated_at": event.get("updated_at"),
    }
    # Optional additive typed details are passed through when a future read contract supplies them.
    # The current discovery.events.v1 record has neither field; the adapter does not infer a type from its source.
    event_type = event.get("event_type", event.get("type"))
    if isinstance(event_type, str) and event_type.strip():
        result["event_type"] = event_type
    details = event.get("details")
    if isinstance(details, dict):
        result["details"] = details
    return result


def to_feed_events(page):
    return [to_feed_event(e) for e in page.get("events", ())]
