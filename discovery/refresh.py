"""One refresh run: fetch enabled sources, keep a per-source cache, merge, publish."""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from . import registry
from .place import CityResolver, locate_events
from .core import FetchError, Fetcher, load_events, save_events
from .location import LocationError, PROVIDERS, resolve_location
from .merge import merge_events
from .publish import FeedError, publish_feed, validate_feed

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA_DIR = os.path.join(ROOT, "data")
CONFIG_FILE = os.path.join(ROOT, "discovery.config.json")
LOCK_STALE_SECONDS = 30 * 60


class RefreshLocked(Exception):
    pass


class Lock:
    """Exclusive lock file so overlapping scheduled runs cannot collide."""

    def __init__(self, path):
        self.path = path

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for _ in range(2):
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(handle, f"{os.getpid()} {time.time()}".encode())
                os.close(handle)
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.path)
                except OSError:
                    continue
                if age < LOCK_STALE_SECONDS:
                    raise RefreshLocked(f"another refresh is running ({self.path})")
                os.remove(self.path)  # stale lock from a crashed run
        raise RefreshLocked(f"could not acquire {self.path}")

    def release(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


def load_config(path=CONFIG_FILE):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _load_cache(path, legacy_events_path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        pass
    # First run: seed from the previous single-source feed so cached details survive.
    cache = {}
    for event in (load_events(legacy_events_path) or {}).get("events", []):
        if "sources" not in event and event.get("source", {}).get("name") == "KPBS":
            entry = cache.setdefault("kpbs", {"events": [], "ok": True, "last_success_at": event.get("retrieved_at")})
            entry["events"].append(event)
    return cache


def _unexpired(events, now):
    return [e for e in events if datetime.fromisoformat(e.get("end") or e["start"]) >= now]


def build_document(config, cache, now, selected, location=None):
    """Merge the cached events of the `selected` registry sources into the feed document."""
    retention = timedelta(days=config.get("retention_days", 3))
    publishers = {source.id: source.publisher for source in selected}
    records, status = {}, {}
    for source in selected:
        entry = cache.get(source.id, {})
        events = _unexpired(entry.get("events", []), now)
        last_ok = entry.get("last_success_at")
        stale = bool(last_ok) and now - datetime.fromisoformat(last_ok) > retention
        if stale:
            events = []  # failing for longer than retention: too old to trust
        records[source.id] = events
        status[source.id] = {"ok": entry.get("ok", False), "stale": stale, "last_success_at": last_ok,
                             "last_attempt_at": entry.get("last_attempt_at"), "error": entry.get("error"),
                             "events": len(events), "notes": entry.get("notes", {})}
    minimum = config.get("verification", {}).get("min_independent_publishers", 2)
    merged = merge_events(records, publishers, minimum)
    document = {"generated_at": now.isoformat(timespec="seconds"), "sources": status, "events": merged}
    if location:
        document["location"] = location
    return document


def refresh(config, options, cache_path=None, output=None, publish_to=None, lock_path=None, fetcher=None,
            location_cache_path=None, location_providers=None):
    """Return an exit code: 0 all ok, 2 partial, 1 nothing refreshed / error."""
    cache_path = cache_path or os.path.join(DATA_DIR, "source-cache.json")
    output = output or os.path.join(DATA_DIR, "events.json")
    lock = Lock(lock_path or os.path.join(DATA_DIR, ".refresh.lock"))
    fetcher = fetcher or Fetcher(options.get("interval", 2.0))
    only = set(options.get("only") or [])

    try:
        lock.acquire()
    except RefreshLocked as error:
        print(error, file=sys.stderr)
        return 1
    try:
        location_cache_path = location_cache_path or os.path.join(os.path.dirname(os.path.abspath(cache_path)), "location-cache.json")
        try:
            sources = registry.load_sources(config)
        except registry.RegistryError as error:
            print(f"Configuration error: {error}. Feed left unchanged.", file=sys.stderr)
            return 1
        try:
            location = resolve_location(config.get("location"), fetcher, location_cache_path, location_providers)
        except LocationError as error:
            print(f"Location error: {error}. Feed left unchanged.", file=sys.stderr)
            return 1
        selected, skipped = registry.select_sources(sources, location)
        for source, reason in skipped:
            print(f"[{source.name}] skipped: {reason}")
        if not selected:
            print(f"Coverage error: no enabled source applies to {location['display_name']} "
                  f"({location['radius_miles']:.0f} mi). Feed left unchanged; it is not replaced by results for another area.", file=sys.stderr)
            return 1
        chosen = [source for source, _ in selected]

        cache = _load_cache(cache_path, output)
        place_key = f"{location['latitude']:.4f},{location['longitude']:.4f},{location['radius_miles']:.0f}"
        if cache.get("_location_key", place_key) != place_key:
            cache = {}  # cached events belong to another area: never carry them into this one
        cache["_location_key"] = place_key
        now = datetime.now(timezone.utc)
        stamp = now.isoformat(timespec="seconds")
        resolver = CityResolver(fetcher, location_cache_path, location_providers or PROVIDERS)
        succeeded = failed = 0
        if options.get("offline"):  # rebuild the feed from cached source data only
            succeeded = 1
        for source in chosen:
            if options.get("offline") or (only and source.id not in only):
                continue
            entry = cache.setdefault(source.id, {"events": []})
            entry["last_attempt_at"] = stamp
            try:
                found, notes = source.discover(fetcher, entry.get("events", []), details=not options.get("no_details"),
                                               limit=options.get("limit"), location=location)
                found, summary = locate_events(found, location, resolver)
                notes["places"] = summary
                notes["outside_area"] = summary["outside_area"]
                if not found:
                    raise FetchError(f"all {summary['outside_area']} events are outside the {location['radius_miles']:.0f} mi search area of {location['display_name']}"
                                     if summary["outside_area"] else "extraction produced no events")
            except Exception as error:  # a failing source must never lose its previous data
                entry.update(ok=False, error=str(error))
                failed += 1
                print(f"[{source.name}] FAILED, keeping {len(entry.get('events', []))} cached events: {error}", file=sys.stderr)
                continue
            entry.update(ok=True, error=None, events=found, notes=notes, last_success_at=stamp)
            succeeded += 1
            print(f"[{source.name}] {len(found)} events; {notes}")

        if not succeeded:
            print("No source refreshed; feed and cache left untouched.", file=sys.stderr)
            return 1

        document = build_document(config, cache, now, chosen, location)
        try:
            validate_feed(document)
        except FeedError as error:
            print(f"Refusing to write feed: {error}", file=sys.stderr)
            return 1
        if options.get("dry_run"):
            print(f"Dry run: {len(document['events'])} merged events, nothing written")
            return 2 if failed else 0

        save_events(cache, cache_path)
        save_events(document, output)
        print(f"Wrote {len(document['events'])} merged events to {output}")
        if publish_to:
            try:
                count = publish_feed(document, publish_to)
            except (FeedError, OSError) as error:
                print(f"Publish to {publish_to} FAILED (previous feed left in place): {error}", file=sys.stderr)
                return 1
            print(f"Published {count} events to {publish_to}")
        return 2 if failed else 0
    finally:
        lock.release()
