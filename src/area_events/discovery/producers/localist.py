"""Producer for any Localist calendar (public, key-less JSON API, paged). One producer serves every Localist site; each is its own source, with the site address as its base
URL and its `days`, `max_pages` and `timezone` as options. The timezone is the calendar's own: the API gives each time with its UTC offset, and the zone name is only recorded,
never used to guess a time. Coordinates are only the API's own `geo` block."""
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse, urlsplit

from ..safe import FetchError
from ..producer import EventCandidate, ProducerResult, Provenance, stated

PAGE_SIZE = 100
_TIMEZONE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._+-]*(?:/[A-Za-z0-9._+-]+)+$")


def host_of(site):
    return urlparse(site).hostname or site


def clean(value):
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) and value.strip() else None


def geo_of(raw):
    """The venue coordinates in the API's own `geo` block, else None: coordinates are never guessed."""
    try:
        latitude, longitude = float(raw["latitude"]), float(raw["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    return {"latitude": latitude, "longitude": longitude, "source": "Localist API geo"}


class LocalistProducer:
    adapter = "localist"
    extractor = "producers/localist@v1"
    method = "adapter"

    @staticmethod
    def validate_options(options):
        """Validate a Localist instance's site and producer-specific options."""
        if any(not isinstance(key, str) for key in options):
            raise ValueError("option names must be text")
        unknown = sorted(set(options) - {"url", "days", "max_pages", "timezone"})
        if unknown:
            raise ValueError(f"unsupported localist options: {', '.join(unknown)[:100]}")
        url = options.get("url")
        try:
            parts = urlsplit(url) if isinstance(url, str) else None
            host = parts.hostname if parts else None
            port = parts.port if parts else None
        except ValueError:
            parts, host, port = None, None, None
        if not parts or parts.scheme not in ("http", "https") or not host or parts.username is not None or parts.password is not None or parts.query or parts.fragment or (port is not None and not 1 <= port <= 65535):
            raise ValueError("options.url must be an http(s) site address without credentials, query or fragment")
        clean_options = {}
        for key, value, low, high in (("days", options.get("days", 14), 1, 365),
                                      ("max_pages", options.get("max_pages", 5), 1, 100)):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{key} must be an integer between {low} and {high}")
            if key in options:
                clean_options[key] = value
        if "timezone" in options:
            value = options["timezone"]
            if not isinstance(value, str) or not 1 <= len(value) <= 64 or not _TIMEZONE_NAME.fullmatch(value):
                raise ValueError("timezone must be an IANA time-zone name")
            clean_options["timezone"] = value
        return url.rstrip("/"), clean_options

    def crawl(self, fetcher, previous, options):
        site, days, max_pages = options["url"].rstrip("/"), int(options.get("days", 14)), int(options.get("max_pages", 5))
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        now = datetime.now().astimezone()
        notes = {"site": site, "api_events": 0, "instances": 0, "all_day_skipped": 0, "not_live_skipped": 0, "ended_skipped": 0, "pages": 0}
        candidates, seen = [], set()
        for page in range(1, max_pages + 1):
            url = f"{site}/api/2/events?days={days}&pp={PAGE_SIZE}&page={page}"
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
                    if datetime.fromisoformat(instance["end"] if instance.get("end") else instance["start"]) < now:
                        notes["ended_skipped"] += 1
                        continue
                    identity = (event["id"], instance["id"])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    candidates.append(self._candidate(event, instance, options, site, retrieved_at))
            if page >= (payload.get("page") or {}).get("total", page):
                break
        return ProducerResult(tuple(candidates), (), notes)

    @staticmethod
    def _candidate(event, instance, options, site, retrieved_at):
        start = datetime.fromisoformat(instance["start"])
        end = datetime.fromisoformat(instance["end"]) if instance.get("end") else None
        categories = tuple(c["name"] for c in (event.get("filters") or {}).get("event_types") or [] if c.get("name"))
        geo = geo_of(event.get("geo"))
        url = event.get("localist_url") or event.get("url")
        venue = clean(event.get("location_name") or event.get("location"))
        address, description = clean(event.get("address")), clean(event.get("description_text"))
        host = host_of(site)
        evidence = [f"start time from the {host} Localist API event instance ({instance['start']})"]
        page_host = urlparse(url or "").hostname
        if page_host and page_host == host:
            evidence.append("event page is on the same Localist site")
        if geo:
            evidence.append(f"venue coordinates from the API ({geo['latitude']}, {geo['longitude']})")
        in_range = event.get("first_date") and event.get("last_date") and event["first_date"] <= start.date().isoformat() <= event["last_date"]
        image = event.get("photo_url") or None
        return EventCandidate(local_id=f"{event['id']}:{instance['id']}", title=clean(event.get("title")), starts_at=start.isoformat(),
                              ends_at=end.isoformat() if end else None, timezone=options.get("timezone"), venue=venue, address=address, description=description, url=url,
                              image_url=image, image_source="Localist API photo_url" if image else None, categories=categories,
                              price="Free" if event.get("free") else clean(event.get("ticket_cost")), geo=geo,
                              provenance=Provenance(options.get("name", "Localist"), f"{site}/", retrieved_at),
                              verification=stated(evidence, bool(in_range and page_host == host), end.isoformat() if end else None, venue, address, description, image))

    @staticmethod
    def page_role(url, base_url):
        return "feed"
