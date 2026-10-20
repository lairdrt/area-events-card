"""One refresh run: fetch enabled sources, keep a per-source cache, merge, publish."""
import json
import os
import sys
import time
from datetime import datetime, timedelta

from . import sources
from .core import FetchError, Fetcher, load_events, now_pacific, save_events
from .location import LocationError, resolve_location, select_sources
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


def build_document(config, cache, now, keys=None, location=None):
    keys = keys if keys is not None else [s["key"] for s in config["sources"] if s.get("enabled", True)]
    publishers = {k: sources.load(k).PUBLISHER for k in keys}
    retention = timedelta(days=config.get("retention_days", 3))
    records, status = {}, {}
    for key in keys:
        entry = cache.get(key, {})
        events = _unexpired(entry.get("events", []), now)
        last_ok = entry.get("last_success_at")
        stale = bool(last_ok) and now - datetime.fromisoformat(last_ok) > retention
        if stale:
            events = []  # failing for longer than retention: too old to trust
        records[key] = events
        status[key] = {"ok": entry.get("ok", False), "stale": stale, "last_success_at": last_ok,
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
            location = resolve_location(config.get("location"), fetcher, location_cache_path, location_providers)
        except LocationError as error:
            print(f"Location error: {error}. Feed left unchanged.", file=sys.stderr)
            return 1
        selected, skipped = select_sources(config["sources"], location, sources.load)
        for spec, reason in skipped:
            print(f"[{spec['key']}] skipped: {reason}")
        if not selected:
            print(f"Coverage error: no enabled source covers {location['display_name']} "
                  f"({location['radius_miles']:.0f} mi). Feed left unchanged; it is not replaced by results for another area.", file=sys.stderr)
            return 1
        selected_keys = {spec["key"] for spec, _ in selected}

        cache = _load_cache(cache_path, output)
        place_key = f"{location['latitude']:.4f},{location['longitude']:.4f},{location['radius_miles']:.0f}"
        if cache.get("_location_key", place_key) != place_key:
            cache = {}  # cached events belong to another area: never carry them into this one
        cache["_location_key"] = place_key
        now = now_pacific()
        stamp = now.isoformat(timespec="seconds")
        succeeded = failed = 0
        if options.get("offline"):  # rebuild the feed from cached source data only
            succeeded = 1
        for spec in config["sources"]:
            key = spec["key"]
            if options.get("offline") or key not in selected_keys or (only and key not in only):
                continue
            adapter = sources.load(key)
            entry = cache.setdefault(key, {"events": []})
            entry["last_attempt_at"] = stamp
            kwargs = {k: v for k, v in spec.items() if k not in ("key", "enabled", "coverage")}
            if (getattr(adapter, "COVERAGE", None) or spec.get("coverage") or {}).get("kind") == "searchable":
                kwargs["location"] = location
            try:
                found, notes = adapter.discover(fetcher, entry.get("events", []), details=not options.get("no_details"),
                                                limit=options.get("limit"), **kwargs)
                if not found:
                    raise FetchError("extraction produced no events")
            except Exception as error:  # a failing source must never lose its previous data
                entry.update(ok=False, error=str(error))
                failed += 1
                print(f"[{adapter.NAME}] FAILED, keeping {len(entry.get('events', []))} cached events: {error}", file=sys.stderr)
                continue
            entry.update(ok=True, error=None, events=found, notes=notes, last_success_at=stamp)
            succeeded += 1
            print(f"[{adapter.NAME}] {len(found)} events; {notes}")

        if not succeeded:
            print("No source refreshed; feed and cache left untouched.", file=sys.stderr)
            return 1

        document = build_document(config, cache, now, keys=[k for k in (s['key'] for s in config['sources']) if k in selected_keys], location=location)
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
