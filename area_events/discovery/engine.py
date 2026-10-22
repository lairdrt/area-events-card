"""The discovery engine: one owner for every discovery run.

Cards (through the Home Assistant integration) register *profiles* (see profiles.py). The engine keeps one folder
per profile under <data>/profiles/<id>/ holding its feed, per-source cache and geocoding cache, so different
profiles can never overwrite one another and identical requests share one set of results.

One worker thread runs the profiles one after another: new and stale profiles are queued, never run in parallel,
and there is no per-card or per-profile thread. A failed run leaves the previous feed and cache untouched. A
profile nobody has asked for in a while is dropped together with its folder.
"""
import collections
import contextlib
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone

from . import registry
from .core import FetchError, Fetcher
from .location import LocationError, resolve_location
from .profiles import ProfileError, location_spec, match_registered, normalize_profile, profile_id, public_host
from .publish import FeedError, validate_feed, write_atomic
from .refresh import SOURCES_FILE, load_config, refresh
from .media import MediaService
from .safe import SafeFetcher
from .sources import web

log = logging.getLogger("area-events")
log.addHandler(logging.NullHandler())

MIN_INTERVAL = 900
RETRY_SECONDS = 600            # after a failed run
PERMANENT_RETRY_SECONDS = 6 * 3600  # after a failure that repeating cannot fix (place not found, no source covers it)
ACTIVE_SECONDS = 7 * 86400     # profiles nobody asked for within this long stop refreshing ...
DROP_SECONDS = 14 * 86400      # ... and are deleted after this long
IDLE_EVICT_SECONDS = 600       # a profile with events asked for within this long is not evicted to make room
ABANDON_SECONDS = 3600         # a profile that never produced events and nobody has asked for in this long is deleted
MAX_PROFILES = 12


class EngineError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


class PermanentError(EngineError):
    """A failure that repeating the search will not fix, so it is not retried on a timer."""


class _LogWriter:
    """File-like object that logs complete lines and remembers the last few."""

    def __init__(self, level, keep=None):
        self.level, self.buffer, self.lines = level, "", keep if keep is not None else collections.deque(maxlen=8)

    def write(self, text):
        self.buffer += text
        *lines, self.buffer = self.buffer.split("\n")
        for line in lines:
            if line.strip():
                self.lines.append(line.strip())
                log.log(self.level, line.rstrip())
        return len(text)

    def flush(self):
        pass


def snapshot_key(generated_at, revision):
    """What a card sends as `since`: the feed's time, plus the media revision once there is any (so new imagery reaches the card)."""
    return f"{generated_at}#{revision}" if revision else generated_at


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def probe_web(fetcher, url):
    """What can the generic website adapter read at `url`? See sources/web.py: a dict with status and reason."""
    return web.probe(fetcher, url)


def probe_localist(fetcher, url):
    """Is `url` a Localist calendar? Returns True/False; raises FetchError when it cannot be reached."""
    origin = "/".join(url.split("/", 3)[:3])
    client = fetcher if hasattr(fetcher, "fetch") else SafeFetcher(getattr(fetcher, "interval", 2.0))  # a typed-in address: never the local network
    payload = json.loads(client.get(f"{origin}/api/2/events?pp=1"))
    return isinstance(payload, dict) and isinstance(payload.get("events"), list)


class Engine:
    def __init__(self, data_dir, sources_path=SOURCES_FILE, interval=3600, clock=time.time, refresh_fn=refresh,
                 fetcher_factory=None, host_check=public_host, probe=probe_localist, max_profiles=MAX_PROFILES, geocode=None, web_probe=probe_web, media=None, media_enabled=True):
        self.data_dir = os.path.join(data_dir, "profiles")
        self.geocode = geocode or resolve_location
        self._geocode_cache = os.path.join(data_dir, "geocode-cache.json")  # shared: a place is looked up once, not once per profile
        self.sources_path = sources_path
        self.interval = max(MIN_INTERVAL, interval)
        self.clock, self.refresh_fn, self.host_check, self.probe, self.max_profiles = clock, refresh_fn, host_check, probe, max_profiles
        self.web_probe = web_probe
        # Location imagery (venue photographs and maps) is found in the background, after and apart from discovery; it never gates events.
        self.media = media if media is not None else MediaService(data_dir, SafeFetcher(1.1), clock=clock)
        self.media_enabled = media_enabled  # off: nothing is looked up, drawn or attached (the app option location_imagery)
        self.fetcher_factory = fetcher_factory or (lambda: Fetcher(2.0))
        self.started = clock()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._queue = collections.deque()
        self._profiles = {}
        self._running = None
        self._feeds = {}          # id -> (mtime_ns, document)
        os.makedirs(self.data_dir, exist_ok=True)
        self._load()

    # --- persistence ---------------------------------------------------------------------

    def _dir(self, pid):
        return os.path.join(self.data_dir, pid)

    def _save(self, record):
        keys = ("id", "profile", "requested_at", "state", "error", "attempt_at", "success_at", "report", "outcome", "permanent", "place_key")
        try:
            write_atomic(os.path.join(self._dir(record["id"]), "profile.json"), json.dumps({k: record.get(k) for k in keys}, indent=2) + "\n")
        except OSError as error:
            log.error("could not save profile %s: %s", record["id"], error)

    def _load(self):
        for pid in sorted(os.listdir(self.data_dir)):
            try:
                with open(os.path.join(self._dir(pid), "profile.json"), encoding="utf-8") as handle:
                    saved = json.load(handle)
                profile = normalize_profile(saved["profile"])
                if profile_id(profile) != pid:
                    raise ValueError("folder does not match its profile")
            except (OSError, ValueError, KeyError, ProfileError) as error:
                log.warning("ignoring profile folder %s: %s", pid, error)
                continue
            saved["profile"] = profile
            if saved.get("state") in ("pending", "discovering"):
                saved["state"] = "pending"  # a restart abandoned the run
            self._profiles[pid] = {"queued": False, "report": [], "progress": None, **saved}
            record = self._profiles[pid]
            if self._feed(pid) is None:
                # A profile that never produced events is only worth retrying if someone asked for it lately; the rest
                # (typed-over editor previews, removed cards) wait until a card asks again.
                if self.clock() - record["requested_at"] <= IDLE_EVICT_SECONDS:
                    self._enqueue(pid)
            elif self._due(record):
                self._enqueue(pid)

    # --- feeds -----------------------------------------------------------------------------

    def _feed(self, pid):
        """The newest valid feed of a profile, or None. An invalid file never replaces a good copy."""
        path = os.path.join(self._dir(pid), "events.json")
        try:
            info = os.stat(path)
            mtime = (info.st_mtime_ns, info.st_size)  # two quick publishes can share a timestamp, but not also a size
        except OSError:
            return self._feeds.get(pid, (None, None))[1]
        cached = self._feeds.get(pid)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
            validate_feed(document)
        except (OSError, ValueError, FeedError) as error:
            log.error("feed of profile %s is not valid (%s); serving the last valid copy", pid, error)
            return cached[1] if cached else None
        self._feeds[pid] = (mtime, document)
        return document

    # --- requests ----------------------------------------------------------------------------

    def register(self, raw):
        """Idempotently register a profile. Returns its id; raises ProfileError or EngineError."""
        profile = normalize_profile(raw)
        pid = profile_id(profile)
        with self._lock:
            now = self.clock()
            record = self._profiles.get(pid)
            if record is None:
                self._make_room()
                os.makedirs(self._dir(pid), exist_ok=True)
                record = self._profiles[pid] = {"id": pid, "profile": profile, "state": "pending", "error": None, "queued": False,
                                                "attempt_at": None, "success_at": None, "report": [], "progress": None, "outcome": None}
                record["requested_at"] = now
                sibling = self._sibling(profile, pid)
                if sibling and self._reuse(sibling, self._dir(pid)):
                    # Only the added websites differ: keep showing the events already found while the difference is worked out.
                    wanted = set(profile.get("sources") or [])
                    record["report"] = [dict(item) for item in sibling.get("report", []) if item.get("url") in wanted and item.get("status") == "supported"]
                    record["seeded"] = True
                    record["success_at"] = sibling["success_at"]  # it already has events to show, so it is not an "initial" search
                self._save(record)
                self._enqueue(pid)
                log.info("new profile %s: %s (%d in use)", pid, profile.get("area") or profile.get("name") or "home coordinates", len(self._profiles))
                return pid
            record["requested_at"] = now
            wait = PERMANENT_RETRY_SECONDS if record.get("permanent") else RETRY_SECONDS
            retry = record["state"] == "error" and now - (record.get("attempt_at") or 0) >= wait
            if self._feed(pid) is None and (retry or record["state"] == "pending"):
                self._enqueue(pid)
            elif self._due(record):
                self._enqueue(pid)
            return pid

    def _make_room(self):
        """Free a slot for a new profile, or refuse. The one running is never touched; profiles that never produced
        events (a card's area typed letter by letter in the editor, a removed card) go first, then idle ones with events."""
        if len(self._profiles) < self.max_profiles:
            return
        now = self.clock()
        others = [r for r in self._profiles.values() if r["id"] != self._running]
        unproven = [r for r in others if r.get("success_at") is None and self._feed(r["id"]) is None]
        idle = [r for r in others if now - r["requested_at"] > IDLE_EVICT_SECONDS]
        victims = unproven or idle
        if not victims:
            raise EngineError(429, f"too many different areas are in use (at most {self.max_profiles}); remove a card or wait a few minutes")
        victim = min(victims, key=lambda r: r["requested_at"])
        log.info("evicting profile %s (%s) to make room", victim["id"], victim["profile"].get("area") or "coordinates")
        self._drop(victim["id"])

    def _drop(self, pid):
        self._profiles.pop(pid, None)
        with contextlib.suppress(ValueError):
            self._queue.remove(pid)
        self._feeds.pop(pid, None)
        folder = self._dir(pid)
        if os.path.dirname(os.path.abspath(folder)) == os.path.abspath(self.data_dir):
            shutil.rmtree(folder, ignore_errors=True)

    def snapshot(self, pid, since=None, with_events=True):
        """What a card needs: status, location, per-source results and (unless unchanged since `since`) the events."""
        with self._lock:
            record = self._profiles.get(pid)
            if record is None:
                raise EngineError(404, "unknown profile")
            feed = self._feed(pid)
            running = self._running == pid
            if feed:
                state = "ready"
            elif running:
                state = "discovering"
            else:
                state = "error" if record["state"] == "error" else "pending"
            result = {"id": pid, "state": state, "refreshing": bool(feed) and (running or record["queued"]),
                      "initial": record.get("success_at") is None, "outcome": record.get("outcome"),
                      "progress": dict(record["progress"]) if record.get("progress") else None,
                      "error": record.get("error"), "requested_at": _iso(record["requested_at"]),
                      "attempt_at": _iso(record["attempt_at"]) if record.get("attempt_at") else None,
                      "success_at": _iso(record["success_at"]) if record.get("success_at") else None,
                      "report": record.get("report", []), "generated_at": None, "location": None, "sources": {}}
            if feed:
                result.update(generated_at=feed["generated_at"], location=feed.get("location"), sources=feed.get("sources", {}))
                revision = self.media.revision if self.media_enabled else 0
                result["revision"] = revision
                if since and since == snapshot_key(feed["generated_at"], revision):
                    result["unchanged"] = True
                elif with_events:
                    result["events"] = self._with_media(feed)
            return result

    def media_file(self, name):
        """(bytes, content type) of a stored map or venue photograph, or None. The name is validated by the store."""
        data = self.media.store.read_file(name)
        if data is None:
            return None
        return data, "image/svg+xml" if name.endswith(".svg") else "image/jpeg"

    def _with_media(self, feed):
        """The feed's events with verified venue photos and maps attached; a failure here must never cost the events."""
        if not self.media_enabled:
            return feed["events"]
        try:
            return self.media.annotate(feed["events"], feed.get("location"))
        except Exception:
            log.exception("could not attach location imagery")
            return feed["events"]

    def status(self):
        with self._lock:
            return {"profiles": len(self._profiles), "queued": len(self._queue), "running": self._running,
                    "interval_seconds": self.interval, "uptime_seconds": int(self.clock() - self.started)}

    # --- scheduling -----------------------------------------------------------------------------

    def _due(self, record):
        now = self.clock()
        if record["queued"] or self._running == record["id"] or now - record["requested_at"] > ACTIVE_SECONDS:
            return False
        if self._feed(record["id"]) is None:
            return False  # a profile with no events is only searched when a card asks for it, never on a timer
        wait = PERMANENT_RETRY_SECONDS if record.get("permanent") else RETRY_SECONDS if record["state"] == "error" else self.interval
        return now - (record.get("attempt_at") or 0) >= wait

    def _enqueue(self, pid):
        record = self._profiles[pid]
        if not record["queued"] and self._running != pid:
            record["queued"] = True
            record["progress"] = self._new_progress("queued", record)
            self._queue.append(pid)
            self._wake.set()

    def _new_progress(self, phase, record):
        feed = self._feed(record["id"])
        return {"phase": phase, "current": None, "completed": 0, "total": None, "failed": 0, "empty": 0, "sources": [],
                "events": len(feed["events"]) if feed else 0, "started_at": None, "finished_at": None}

    def _progress_callback(self, record):
        """Receives refresh()'s progress notifications and keeps the profile's progress current."""
        def notify(kind, **info):
            with self._lock:
                progress = record["progress"]
                if kind == "start":
                    progress.update(phase="searching", total=info["total"], sources=info["sources"], completed=0, failed=0, empty=0, current=None)
                elif kind == "source_start":
                    progress.update(phase="searching", current=info["name"])
                elif kind == "source_processing":
                    progress.update(phase="processing", current=info["name"])
                elif kind == "source_done":
                    progress["completed"] += 1
                    progress["failed"] += 0 if info["ok"] else 1
                    progress["empty"] += 1 if info["empty"] else 0
                    progress["phase"] = "searching"
                elif kind == "published":
                    progress["events"] = info["events"]
        return notify

    def tick(self):
        """Queue profiles that are due and drop those nobody uses."""
        with self._lock:
            now = self.clock()
            for pid, record in list(self._profiles.items()):
                unused = now - record["requested_at"]
                abandoned = unused > ABANDON_SECONDS and self._feed(pid) is None
                if (unused > DROP_SECONDS or abandoned) and self._running != pid:
                    log.info("dropping unused profile %s (%s)", pid, record["profile"].get("area") or "coordinates")
                    self._drop(pid)
                elif self._due(record):
                    self._enqueue(pid)

    def run_next(self):
        """Run the next queued profile to completion. Returns its id, or None if nothing is queued."""
        with self._lock:
            if not self._queue:
                return None
            # Whoever asked most recently is the one still waiting; older first-time requests that nobody repeated are
            # left for later and skipped once abandoned.
            pid = max(self._queue, key=lambda queued: self._profiles[queued]["requested_at"] if queued in self._profiles else 0)
            self._queue.remove(pid)
            record = self._profiles.get(pid)
            if record is None:
                return pid
            record["queued"] = False
            if self._feed(pid) is None and self.clock() - record["requested_at"] > IDLE_EVICT_SECONDS:
                return pid  # abandoned before it ran: not worth a search; a card that still wants it asks again
            self._running = pid
            record["progress"] = record.get("progress") or self._new_progress("queued", record)
            record["progress"].update(phase="searching", started_at=_iso(self.clock()), finished_at=None)
        try:
            self._run(record)
        finally:
            with self._lock:
                self._running = None
        return pid

    def _media_feeds(self):
        with self._lock:
            return [(feed.get("location"), feed["events"]) for feed in (self._feed(pid) for pid in list(self._profiles)) if feed]

    def _media_loop(self, stop):
        """Slowly resolve the places in the feeds into maps and venue photographs, only while discovery is idle."""
        stop.wait(30)
        while not stop.is_set():
            busy = self._running is not None or self._queue
            pending = 0
            if not busy:
                try:
                    feeds = self._media_feeds()
                    self.media.cycle(feeds)
                    pending = self.media.pending(feeds)
                except Exception:
                    log.exception("location imagery cycle failed")
            stop.wait(15 if busy else 20 if pending else 600)

    def run_forever(self, stop):
        if self.media_enabled:
            threading.Thread(target=self._media_loop, args=(stop,), name="media", daemon=True).start()
        while not stop.is_set():
            self.tick()
            if self.run_next() is None:
                self._wake.wait(30)
                self._wake.clear()

    def wake(self):
        self._wake.set()

    # --- one run ------------------------------------------------------------------------------------

    def _automatic(self, profile, config):
        """(entries, report, other urls): every registry source (coverage decides which apply to the area), with the registered
        sources a card names forced on, and the typed-in addresses that no registered source reads."""
        urls = profile.get("sources") or []
        entries = list(config["sources"])
        if not urls:
            return entries, [], []
        matched, rest = match_registered(urls, registry.load_sources(config))
        report = []
        for url, source in matched:
            position = next(i for i, entry in enumerate(entries) if entry["id"] == source.id)
            entries[position] = dict(entries[position], coverage={"type": "global"})  # named by the card: used wherever the card is
            report.append({"url": url, "status": "supported", "adapter": source.adapter, "name": source.name, "id": source.id})
        return entries, report, rest

    @staticmethod
    def _added_entry(adapter, url, name, ident):
        """The registry entry for a website that was added to the card (read through the safe fetcher; applies wherever the card is)."""
        if adapter == "localist":
            return {"id": ident, "name": name, "adapter": "localist", "url": "/".join(url.split("/", 3)[:3]), "coverage": {"type": "global"}, "options": {"days": 14, "safe": True}}
        return {"id": ident, "name": name, "adapter": "web", "url": url, "coverage": {"type": "global"}, "options": {"days": 60, "safe": True}}

    def _probe_added(self, fetcher, urls):
        """(entries, report) for typed-in addresses no registered source reads: Localist calendars, then the generic website reader.
        An address that cannot be read is reported with its reason and contributes nothing; it never affects the other sources."""
        entries, report = [], []
        for url in urls:
            host = url.split("/")[2]
            try:
                if not self.host_check(host):
                    raise FetchError("the address is not a public website")
                is_localist = self._is_localist(fetcher, url)
            except FetchError as error:
                report.append({"url": url, "status": "blocked", "reason": f"{error}"[:200]})
                continue
            if is_localist:
                ident, name = f"localist_{hashlib.sha1(host.encode()).hexdigest()[:8]}", host
                entries.append(self._added_entry("localist", url, name, ident))
                report.append({"url": url, "status": "supported", "adapter": "localist", "name": name, "id": ident})
                continue
            found = self.web_probe(fetcher, url)
            if found["status"] != "supported":
                report.append({"url": url, "status": found["status"], "reason": found.get("reason", "")})
                continue
            ident, name = f"web_{hashlib.sha1(url.encode()).hexdigest()[:8]}", (found.get("name") or host)[:60]
            entries.append(self._added_entry("web", url, name, ident))
            report.append({"url": url, "status": "supported", "adapter": "web", "name": name, "id": ident, "format": found.get("format"), "found": found.get("events")})
        return entries, report

    def _is_localist(self, fetcher, url):
        """Is `url` a Localist calendar? A site that cannot be read as one simply is not (the generic reader tries next)."""
        try:
            return bool(self.probe(fetcher, url))
        except (FetchError, ValueError):
            return False

    @staticmethod
    def _base(profile):
        """A profile without its added websites: the same card edited only in its Source websites shares this."""
        return {key: value for key, value in profile.items() if key != "sources"}

    def _sibling(self, profile, exclude):
        """The most recently refreshed other profile for the same place and settings (differing only in added websites) that has a feed."""
        base = self._base(profile)
        siblings = [r for r in self._profiles.values() if r["id"] != exclude and self._base(r["profile"]) == base and r.get("success_at") and self._feed(r["id"])]
        return max(siblings, key=lambda r: r["success_at"], default=None)

    @staticmethod
    def _cached_ok(folder):
        """Ids of the sources whose results in this profile's cache came from a successful read."""
        try:
            with open(os.path.join(folder, "source-cache.json"), encoding="utf-8") as handle:
                cache = json.load(handle)
        except (OSError, ValueError):
            return set()
        return {key for key, entry in cache.items() if isinstance(entry, dict) and entry.get("ok") and entry.get("events")}

    def _reuse(self, twin, folder):
        """Copy a twin's feed (and source cache) into `folder`; False, copying nothing, if that is not possible."""
        source = self._dir(twin["id"])
        try:
            feed_copy = os.path.join(folder, "events.json")
            shutil.copyfile(os.path.join(source, "events.json"), feed_copy)
            if os.path.exists(os.path.join(source, "source-cache.json")):
                shutil.copyfile(os.path.join(source, "source-cache.json"), os.path.join(folder, "source-cache.json"))
            return True
        except OSError as error:
            log.warning("could not reuse the results of %s: %s", twin["id"], error)
            return False

    def _equivalent(self, record):
        """Another profile with results for exactly the same resolved place, radius and sources, if any."""
        for other in self._profiles.values():
            if other is not record and not other.get("seeded") and other.get("place_key") == record.get("place_key") and other.get("success_at") and self._feed(other["id"]):
                return other
        return None

    def _run(self, record):
        pid, profile = record["id"], record["profile"]
        started = self.clock()
        record["attempt_at"] = started
        with self._lock:
            # A new run supersedes the outcome of the last one: a failure recorded earlier (even by an older version of the app,
            # persisted in profile.json) must not be reported while this run is already publishing events. The run sets both again.
            record["error"], record["outcome"] = None, None
        error, report, outcome, permanent, reused = None, [], "failed", False, None
        folder = self._dir(pid)
        stderr = _LogWriter(logging.WARNING)
        try:
            config = load_config(self.sources_path)
            fetcher = self.fetcher_factory()
            auto, report, others = self._automatic(profile, config)
            # Websites that were read successfully before are part of the regular run (so their events never drop out of the feed
            # while others are being worked out); only addresses not yet known to work are probed, after the automatic sources.
            carried = [item for item in record.get("report", []) if item.get("status") == "supported" and item.get("adapter") in ("web", "localist")
                       and item.get("id") and item.get("url") in others]
            known = [self._added_entry(item["adapter"], item["url"], item.get("name") or item["url"], item["id"]) for item in carried]
            report += [dict(item) for item in carried]
            pending = [url for url in others if url not in {item["url"] for item in carried}]
            base_entries = auto + known
            # The place is resolved here, once, by the shared geocoder; the search below only ever sees coordinates.
            try:
                place = self.geocode(location_spec(profile), fetcher, self._geocode_cache)
            except LocationError as caught:
                raise PermanentError(422, str(caught)) from caught
            location = {"latitude": place["latitude"], "longitude": place["longitude"], "radius_miles": profile["radius_miles"], "name": place["display_name"]}
            location.update({key: place[key] for key in ("country", "region", "timezone") if place.get(key)})
            run_config = {"retention_days": config.get("retention_days", 3), "verification": config.get("verification", {}),
                          "location": location, "sources": base_entries}
            with self._lock:
                record["place_key"] = [round(place["latitude"], 3), round(place["longitude"], 3), profile["radius_miles"], sorted(profile.get("sources") or [])]
                twin = self._equivalent(record)
            def run_refresh(config_to_run, **extra):
                with self._lock:
                    record["state"] = "discovering"
                with contextlib.redirect_stdout(_LogWriter(logging.INFO)), contextlib.redirect_stderr(stderr):
                    return self.refresh_fn(config_to_run, {"interval": 2.0, "progress": self._progress_callback(record), **extra}, fetcher=fetcher,
                                           cache_path=os.path.join(folder, "source-cache.json"), output=os.path.join(folder, "events.json"),
                                           lock_path=os.path.join(folder, ".refresh.lock"),
                                           location_cache_path=os.path.join(folder, "location-cache.json"))

            codes = []
            if twin and self._feed(pid) is None and self._reuse(twin, folder):
                # A different spelling of a place already searched with the same radius and sources: reuse those results.
                log.info("profile %s reuses the results of %s (same place, radius and sources)", pid, twin["id"])
                reused = twin["success_at"]
                report, pending = [dict(item) for item in twin.get("report", [])] or report, []
                with self._lock:
                    record["progress"] = {**self._new_progress("complete", record), "phase": "complete", "finished_at": _iso(self.clock())}
                codes.append(0)
            elif record.get("seeded") and self._feed(pid):
                # The feed and source results of a sibling profile are already here: rebuild the feed for this exact set of
                # websites from them (nothing is fetched), so an edit does not repeat the automatic discovery.
                log.info("profile %s starts from its sibling's results; only new websites are read", pid)
                codes.append(run_refresh(run_config, offline=True))
            else:
                log.info("discovery starting for profile %s", pid)
                codes.append(run_refresh(run_config))
            record["seeded"] = False
            if pending:
                added, found = self._probe_added(fetcher, pending)
                report += found
                with self._lock:
                    record["report"] = report
                fresh = [entry for entry in added if entry["id"] not in self._cached_ok(folder)]
                if fresh:
                    codes.append(run_refresh(dict(run_config, sources=base_entries + added), only=[entry["id"] for entry in fresh]))
                elif added:
                    codes.append(run_refresh(dict(run_config, sources=base_entries + added), offline=True))
            code = 0 if all(c == 0 for c in codes) else 2 if any(c in (0, 2) for c in codes) else 1
            progress = record["progress"]
            if code not in (0, 2):
                reasons = [line for line in stderr.lines if not line.startswith("No source refreshed")] or list(stderr.lines)
                error = "; ".join(reasons[-3:])[:400] or "discovery produced no events"
                if progress.get("empty") and progress["failed"] == progress["empty"]:
                    outcome = "no_results"  # every source answered, none had events here: not a failure of any source
                    error = f"No upcoming events were found by the {progress['completed']} source{'s' if progress['completed'] != 1 else ''} searched."
                else:
                    outcome = "failed"
            else:
                outcome = "partial" if code == 2 else "complete"
        except EngineError as caught:
            error = caught.message
            permanent = isinstance(caught, PermanentError)
        except Exception as caught:  # a crashing adapter or bad configuration must not stop the worker
            log.exception("discovery for profile %s crashed", pid)
            error = f"discovery failed unexpectedly: {caught}"[:400]
        with self._lock:
            record["report"] = report
            record["error"] = error
            record["outcome"] = outcome
            record["permanent"] = permanent or (not profile.get("sources") and any(line.startswith("Coverage error") for line in stderr.lines))
            record["progress"] = {**(record.get("progress") or self._new_progress("complete", record)), "phase": "complete", "current": None,
                                  "finished_at": _iso(self.clock())}
            feed = self._feed(pid)
            if feed:
                record["progress"]["events"] = len(feed["events"])
            record["state"] = "error" if error else "ready"
            if not error:
                record["success_at"] = reused or self.clock()
            self._save(record)
        log.info("discovery for profile %s finished in %.0fs: %s", pid, self.clock() - started, error or "ok")
