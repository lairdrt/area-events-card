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
from .profiles import ProfileError, location_spec, match_registered, normalize_profile, profile_id, public_host
from .publish import FeedError, validate_feed, write_atomic
from .refresh import SOURCES_FILE, load_config, refresh

log = logging.getLogger("area-events")
log.addHandler(logging.NullHandler())

MIN_INTERVAL = 900
RETRY_SECONDS = 600            # after a failed run
ACTIVE_SECONDS = 7 * 86400     # profiles nobody asked for within this long stop refreshing ...
DROP_SECONDS = 14 * 86400      # ... and are deleted after this long
IDLE_EVICT_SECONDS = 600       # a profile asked for within this long is never evicted to make room
MAX_PROFILES = 12


class EngineError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


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


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def probe_localist(fetcher, url):
    """Is `url` a Localist calendar? Returns True/False; raises FetchError when it cannot be reached."""
    origin = "/".join(url.split("/", 3)[:3])
    payload = json.loads(fetcher.get(f"{origin}/api/2/events?pp=1"))
    return isinstance(payload, dict) and isinstance(payload.get("events"), list)


class Engine:
    def __init__(self, data_dir, sources_path=SOURCES_FILE, interval=3600, clock=time.time, refresh_fn=refresh,
                 fetcher_factory=None, host_check=public_host, probe=probe_localist, max_profiles=MAX_PROFILES):
        self.data_dir = os.path.join(data_dir, "profiles")
        self.sources_path = sources_path
        self.interval = max(MIN_INTERVAL, interval)
        self.clock, self.refresh_fn, self.host_check, self.probe, self.max_profiles = clock, refresh_fn, host_check, probe, max_profiles
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
        keys = ("id", "profile", "requested_at", "state", "error", "attempt_at", "success_at", "report", "outcome")
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
            if self._feed(pid) is None or self._due(self._profiles[pid]):
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
                self._save(record)
                self._enqueue(pid)
                return pid
            record["requested_at"] = now
            retry = record["state"] == "error" and now - (record.get("attempt_at") or 0) >= RETRY_SECONDS
            if self._feed(pid) is None and (retry or record["state"] == "pending"):
                self._enqueue(pid)
            elif self._due(record):
                self._enqueue(pid)
            return pid

    def _make_room(self):
        if len(self._profiles) < self.max_profiles:
            return
        now = self.clock()
        idle = [r for r in self._profiles.values() if r["id"] != self._running and now - r["requested_at"] > IDLE_EVICT_SECONDS]
        if not idle:
            raise EngineError(429, f"too many different areas are in use (at most {self.max_profiles}); remove a card or wait a few minutes")
        self._drop(min(idle, key=lambda r: r["requested_at"])["id"])

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
                if since and since == feed["generated_at"]:
                    result["unchanged"] = True
                elif with_events:
                    result["events"] = feed["events"]
            return result

    def status(self):
        with self._lock:
            return {"profiles": len(self._profiles), "queued": len(self._queue), "running": self._running,
                    "interval_seconds": self.interval, "uptime_seconds": int(self.clock() - self.started)}

    # --- scheduling -----------------------------------------------------------------------------

    def _due(self, record):
        now = self.clock()
        if record["queued"] or self._running == record["id"] or now - record["requested_at"] > ACTIVE_SECONDS:
            return False
        wait = RETRY_SECONDS if record["state"] == "error" else self.interval
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
                if now - record["requested_at"] > DROP_SECONDS and not record["queued"] and self._running != pid:
                    log.info("dropping unused profile %s", pid)
                    self._drop(pid)
                elif self._due(record):
                    self._enqueue(pid)

    def run_next(self):
        """Run the next queued profile to completion. Returns its id, or None if nothing is queued."""
        with self._lock:
            if not self._queue:
                return None
            pid = self._queue.popleft()
            record = self._profiles.get(pid)
            if record is None:
                return pid
            record["queued"] = False
            self._running = pid
            record["progress"] = record.get("progress") or self._new_progress("queued", record)
            record["progress"].update(phase="searching", started_at=_iso(self.clock()), finished_at=None)
        try:
            self._run(record)
        finally:
            with self._lock:
                self._running = None
        return pid

    def run_forever(self, stop):
        while not stop.is_set():
            self.tick()
            if self.run_next() is None:
                self._wake.wait(30)
                self._wake.clear()

    def wake(self):
        self._wake.set()

    # --- one run ------------------------------------------------------------------------------------

    def _sources_for(self, profile, config, fetcher):
        """(raw registry entries, explicit?, report) for the profile's requested websites."""
        urls = profile.get("sources") or []
        if not urls:
            return config["sources"], False, []
        registered = registry.load_sources(config)
        raw_by_id = {entry["id"]: entry for entry in config["sources"]}
        matched, rest = match_registered(urls, registered)
        entries, report = [], []
        for url, source in matched:
            entries.append(raw_by_id[source.id])
            report.append({"url": url, "status": "supported", "adapter": source.adapter, "name": source.name})
        for url in rest:
            host = url.split("/")[2]
            try:
                if not self.host_check(host):
                    raise FetchError("the address is not a public website")
                is_localist = self.probe(fetcher, url)
            except (FetchError, ValueError) as error:
                report.append({"url": url, "status": "unsupported", "reason": f"could not be read: {error}"[:200]})
                continue
            if not is_localist:
                report.append({"url": url, "status": "unsupported", "reason": "no adapter reads this website (only the registered sources and Localist calendars are supported)"})
                continue
            digest = hashlib.sha1(host.encode()).hexdigest()[:8]
            entries.append({"id": f"localist_{digest}", "name": host, "adapter": "localist", "url": "/".join(url.split("/", 3)[:3]),
                            "coverage": {"type": "global"}, "options": {"days": 14}})
            report.append({"url": url, "status": "supported", "adapter": "localist", "name": host})
        return entries, True, report

    def _run(self, record):
        pid, profile = record["id"], record["profile"]
        started = self.clock()
        record["attempt_at"] = started
        error, report, outcome = None, [], "failed"
        folder = self._dir(pid)
        stderr = _LogWriter(logging.WARNING)
        try:
            config = load_config(self.sources_path)
            fetcher = self.fetcher_factory()
            entries, explicit, report = self._sources_for(profile, config, fetcher)
            if explicit and not entries:
                raise EngineError(422, "none of the requested websites can be read by an available adapter")
            run_config = {"retention_days": config.get("retention_days", 3), "verification": config.get("verification", {}),
                          "location": location_spec(profile), "sources": entries}
            log.info("discovery starting for profile %s", pid)
            with self._lock:
                record["state"] = "discovering"
            with contextlib.redirect_stdout(_LogWriter(logging.INFO)), contextlib.redirect_stderr(stderr):
                code = self.refresh_fn(run_config, {"interval": 2.0, "explicit_sources": explicit, "progress": self._progress_callback(record)}, fetcher=fetcher,
                                       cache_path=os.path.join(folder, "source-cache.json"), output=os.path.join(folder, "events.json"),
                                       lock_path=os.path.join(folder, ".refresh.lock"),
                                       location_cache_path=os.path.join(folder, "location-cache.json"))
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
        except Exception as caught:  # a crashing adapter or bad configuration must not stop the worker
            log.exception("discovery for profile %s crashed", pid)
            error = f"discovery failed unexpectedly: {caught}"[:400]
        with self._lock:
            record["report"] = report
            record["error"] = error
            record["outcome"] = outcome
            record["progress"] = {**(record.get("progress") or self._new_progress("complete", record)), "phase": "complete", "current": None,
                                  "finished_at": _iso(self.clock())}
            feed = self._feed(pid)
            if feed:
                record["progress"]["events"] = len(feed["events"])
            record["state"] = "error" if error else "ready"
            if not error:
                record["success_at"] = self.clock()
            self._save(record)
        log.info("discovery for profile %s finished in %.0fs: %s", pid, self.clock() - started, error or "ok")
