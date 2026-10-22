"""One refresh run: fetch enabled sources, keep a per-source cache, merge, publish."""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from . import classifiers, images, photo_subject, registry
from .place import CityResolver, locate_events
from .core import FetchError, Fetcher, normalize_event_text, save_events
from .location import LocationError, PROVIDERS, resolve_location
from .merge import merge_events
from .publish import FeedError, validate_feed

SOURCES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources.json")
LOCK_STALE_SECONDS = 30 * 60


class RefreshLocked(Exception):
    pass


class EmptyResult(FetchError):
    """A source was read successfully but has no events for this area (not the same as a source being down)."""


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


def load_config(path=SOURCES_FILE):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _load_cache(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _unexpired(events, now):
    return [e for e in events if datetime.fromisoformat(e.get("end") or e["start"]) >= now]


def build_document(config, cache, now, selected, location=None):
    """Merge the cached events of the `selected` registry sources into the feed document."""
    retention = timedelta(days=config.get("retention_days", 3))
    publishers = {source.id: source.publisher for source in selected}
    records, status = {}, {}
    for source in selected:
        entry = cache.get(source.id, {})
        events = [normalize_event_text(e) for e in _unexpired(entry.get("events", []), now)]  # also events cached before this cleanup existed
        last_ok = entry.get("last_success_at")
        stale = bool(last_ok) and now - datetime.fromisoformat(last_ok) > retention
        if stale:
            events = []  # failing for longer than retention: too old to trust
        records[source.id] = events
        status[source.id] = {"ok": entry.get("ok") if entry else None, "pending": not entry, "stale": stale, "last_success_at": last_ok,
                             "last_attempt_at": entry.get("last_attempt_at"), "error": entry.get("error"),
                             "events": len(events), "notes": entry.get("notes", {})}
    minimum = config.get("verification", {}).get("min_independent_publishers", 2)
    merged = merge_events(records, publishers, minimum)
    images.drop_overused(merged)
    classifiers.annotate(merged)  # which illustration an event gets when it has no photo; never touches the photo choice
    photo_subject.annotate(merged)  # for food events: farmers market, food truck or restaurant, for the card's category photographs
    document = {"generated_at": now.isoformat(timespec="seconds"), "sources": status, "events": merged}
    if location:
        document["location"] = location
    return document


def refresh(config, options, cache_path, output, lock_path, fetcher=None, location_cache_path=None, location_providers=None):
    """Discover events for config["location"] into `output`. Returns an exit code: 0 all ok, 2 partial, 1 nothing refreshed / error.

    The feed is republished (atomically, after the cache) each time a source finishes, so usable events appear
    without waiting for the slowest source, and the merge/verification is redone with everything known so far.
    `options["progress"]`, if given, is called as progress(kind, **info) with kind one of
    start (total, sources), source_start (name, index), source_processing (name), source_done (name, ok, empty,
    events, error), published (events) so a caller can show real activity.
    """
    lock = Lock(lock_path)
    notify = options.get("progress") or (lambda kind, **info: None)
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
        if options.get("explicit_sources"):  # the caller named these sources: coverage does not filter them
            selected = [(source, "requested") for source in sources if source.enabled]
            skipped = []
        else:
            selected, skipped = registry.select_sources(sources, location)
        for source, reason in skipped:
            print(f"[{source.name}] skipped: {reason}")
        if not selected:
            print(f"Coverage error: no enabled source applies to {location['display_name']} "
                  f"({location['radius_miles']:.0f} mi). Feed left unchanged; it is not replaced by results for another area.", file=sys.stderr)
            return 1
        chosen = [source for source, _ in selected]

        cache = _load_cache(cache_path)
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
        active = [source for source in chosen if not (options.get("offline") or (only and source.id not in only))]
        notify("start", total=len(active), sources=[source.name for source in active])

        def publish(final=False):
            """Write the cache, then the feed, from everything known so far. Skips (returns None) while there is nothing usable."""
            document = build_document(config, cache, datetime.now(timezone.utc), chosen, location)
            try:
                validate_feed(document)
            except FeedError as error:
                if final:
                    print(f"Refusing to write feed: {error}", file=sys.stderr)
                return None
            if options.get("dry_run"):
                return document
            save_events(cache, cache_path)
            save_events(document, output)
            notify("published", events=len(document["events"]))
            return document

        for index, source in enumerate(active):
            notify("source_start", name=source.name, index=index)
            entry = cache.setdefault(source.id, {"events": []})
            entry["last_attempt_at"] = stamp
            try:
                found, notes = source.discover(fetcher, entry.get("events", []), details=not options.get("no_details"),
                                               limit=options.get("limit"), location=location)
                notify("source_processing", name=source.name)
                found = [normalize_event_text(event) for event in found]  # entities left in listings are decoded once, here, before anything reads them
                found, summary = locate_events(found, location, resolver)
                notes["places"] = summary
                notes["outside_area"] = summary["outside_area"]
                if not found:
                    raise EmptyResult(f"all {summary['outside_area']} events are outside the {location['radius_miles']:.0f} mi search area of {location['display_name']}"
                                      if summary["outside_area"] else "extraction produced no events")
            except Exception as error:  # a failing source must never lose its previous data
                entry.update(ok=False, error=str(error))
                failed += 1
                print(f"[{source.name}] FAILED, keeping {len(entry.get('events', []))} cached events: {error}", file=sys.stderr)
                notify("source_done", name=source.name, ok=False, empty=isinstance(error, EmptyResult), events=0, error=str(error))
                continue
            entry.update(ok=True, error=None, events=found, notes=notes, last_success_at=stamp)
            succeeded += 1
            print(f"[{source.name}] {len(found)} events; {notes}")
            if not options.get("dry_run"):
                publish()
            notify("source_done", name=source.name, ok=True, empty=False, events=len(found), error=None)

        if not succeeded:
            print("No source refreshed; feed and cache left untouched.", file=sys.stderr)
            return 1

        document = publish(final=True)
        if document is None:
            return 1
        if options.get("dry_run"):
            print(f"Dry run: {len(document['events'])} merged events, nothing written")
        else:
            print(f"Wrote {len(document['events'])} merged events to {output}")
        return 2 if failed else 0
    finally:
        lock.release()
