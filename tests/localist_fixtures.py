"""Small, re-dated Localist API responses used by offline producer tests."""
import json
import os
from datetime import date, datetime, timedelta

from area_events.app import KNOWN_SOURCES
from area_events.discovery.safe import FetchError, Response

SITE = "https://calendar.utexas.edu"
API = f"{SITE}/api/2/events?days=14&pp=100&page="
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SOURCE = KNOWN_SOURCES["ut-austin"]


def sample_pages(days_ahead=2):
    pages = []
    for number in (1, 2):
        with open(os.path.join(DATA, f"localist_ut_austin_page{number}.json"), encoding="utf-8") as handle:
            pages.append(json.load(handle))
    first = min(date.fromisoformat(wrapper["event"]["first_date"]) for page in pages for wrapper in page["events"])
    shift = timedelta(days=(date.today() + timedelta(days=days_ahead) - first).days)
    for page in pages:
        for wrapper in page["events"]:
            event = wrapper["event"]
            for field in ("first_date", "last_date"):
                if event.get(field):
                    event[field] = (date.fromisoformat(event[field]) + shift).isoformat()
            for item in event["event_instances"]:
                instance = item["event_instance"]
                for field in ("start", "end"):
                    if instance.get(field):
                        instance[field] = (datetime.fromisoformat(instance[field]) + shift).isoformat()
    return pages


def options():
    return {**dict(SOURCE.options), "source_key": SOURCE.source_key, "name": SOURCE.name, "url": SOURCE.base_url}


class Recorder:
    """The producer's text-fetch interface backed by local fixture pages."""

    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def get(self, url, check_robots=True):
        self.urls.append(url)
        entry = self.pages.get(url)
        if entry is None:
            raise FetchError(f"{url}: HTTP 404")
        if isinstance(entry, Exception):
            raise entry
        return entry if isinstance(entry, str) else json.dumps(entry)


class JsonClient:
    """SafeFetcher-shaped fake for Localist JSON and City HTML pages."""

    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def fetch(self, url, check_robots=True, binary=None, timeout=None):
        self.urls.append(url)
        entry = self.pages.get(url)
        if entry is None:
            raise FetchError(f"{url}: HTTP 404")
        if isinstance(entry, Exception):
            raise entry
        kind = "application/json; charset=utf-8" if "/api/" in url else "text/html; charset=utf-8"
        return Response(url, 200, kind, entry if isinstance(entry, str) else json.dumps(entry))
