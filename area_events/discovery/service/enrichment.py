"""MediaEnrichmentService: an independent worker that finds and caches imagery for stored events. It never runs inside discovery, extraction or a query.

    work       LocalMediaWork    pending candidates in, finished media out (no SQL here)
    resolver   MediaService      the accepted imagery code, unchanged: verified places, OpenFreeMap maps, Wikimedia Commons venue photographs
    photos     SourcePhotos      the photograph a source supplied for an event, fetched once within bounds and cached
    classify   classifiers.annotate   which category illustration an event gets when it has no photograph

Image priority for every event, in the order the card tries them: (1) the event's own photograph, (2) a verified photograph of the venue,
(3) a map of a verified location, (4) the category illustration (the card draws it; this service only decides the category). Nothing here
verifies a venue or a location by itself: the accepted rules in `discovery/media.py` do, or they refuse, and a refusal is cached.

Lifecycle and politeness: one thread, `run_once` / `start` / `stop` / `status`, capped error backoff. Lookups to public services are spaced by the
resolver (Nominatim: 15 s) and by the shared fetcher's host policy, a few places per pass, so a large calendar is enriched over time and the
events are usable from the first moment (their illustration is decided at once).
"""
import hashlib
import logging
import threading
from dataclasses import dataclass

from .. import classifiers, images
from ..core import FetchError
from ..safe import BlockedError
from .media_store import MAX_FILE_BYTES, inside_area

log = logging.getLogger("area-events.enrichment")
PHOTO_TYPES = ("image/jpeg", "image/png", "image/webp")
MEDIA_URL = "/area_events/media"
PHOTO_TTL = 60 * 86400
REFUSAL_TTL = 14 * 86400


@dataclass(frozen=True)
class EnrichmentConfig:
    batch: int = 12                  # candidates looked at per pass
    places_per_pass: int = 8         # new places resolved per pass (each may need a polite pause: Nominatim allows 4 a minute)
    idle_wait_s: float = 30.0
    busy_wait_s: float = 1.0
    error_wait_s: float = 5.0
    error_wait_cap_s: float = 300.0
    shared_image_titles: int = 3     # an image carried by this many different titles is a default picture, not any event's photograph
    max_photo_bytes: int = MAX_FILE_BYTES


def _sniff(body):
    """The image type from the bytes themselves (never from the server's word): extension or None."""
    if body[:3] == b"\xff\xd8\xff":
        return "jpg"
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "webp"
    return None


class SourcePhotos:
    """The photograph a source supplied for one event: judged by its address, fetched once through the safe fetcher, checked by content, kept by content hash.
    Where the picture host's robots.txt forbids fetching it, nothing is copied: the entry then carries the source's own https address for the user's browser
    to show (as the source's calendar API intends), with the same credit, and the refusal is remembered."""

    def __init__(self, fetcher, store, work, clock, config=EnrichmentConfig()):
        self._fetcher, self._store, self._work, self._clock, self._config = fetcher, store, work, clock, config

    def cache(self, event, source_name):
        """(media entry or None, finished). `finished` is False after a temporary failure, so the lookup is repeated later."""
        url = event.get("image_url")
        usable, reason = images.judge(url)
        if not usable:
            return None, True
        key = "e:" + hashlib.sha1(url.encode()).hexdigest()[:20]
        records, now = self._store.index["records"], self._clock()
        record = records.get(key)
        if record and now - record.get("at", 0) < (PHOTO_TTL if record.get("ok") else REFUSAL_TTL) and (not record.get("ok") or self._store.intact(record)):
            return (self._entry(record) if record.get("ok") else self._hotlink(event, source_name, url) if record.get("blocked") else None), True
        if self._work.titles_sharing_image(url) >= self._config.shared_image_titles:
            records[key] = {"at": now, "ok": False, "reason": "the same picture is attached to several unrelated events: a default image, not this event's photograph"}
            return None, True
        try:
            response = self._fetcher.fetch(url, binary=PHOTO_TYPES)
            body = response.body
        except BlockedError as error:   # robots.txt of the picture host: we do not copy it; the browser may show the source's own address
            records[key] = {"at": now, "ok": False, "blocked": True, "reason": str(error)[:200]}
            return self._hotlink(event, source_name, url), True
        except FetchError as error:
            if "HTTP 4" in str(error):   # gone or forbidden: remember it
                records[key] = {"at": now, "ok": False, "reason": str(error)[:200]}
                return None, True
            log.warning("event photograph not fetched (will retry): %s", error)
            return None, False
        extension = _sniff(body or b"")
        if extension is None or not 0 < len(body) <= self._config.max_photo_bytes:
            records[key] = {"at": now, "ok": False, "reason": "not a JPEG, PNG or WebP picture within the size limit"}
            return None, True
        name = f"p{hashlib.sha1(body).hexdigest()[:24]}.{extension}"
        self._store.put_file(name, body)
        record = {"at": now, "ok": True, "kind": "event_photo", "photo": {"file": name, "bytes": len(body)}, "source_url": url,
                  "page_url": event.get("url"), "credit": source_name, "license": "as published by the source"}
        records[key] = record
        return self._entry(record), True

    @staticmethod
    def _hotlink(event, source_name, url):
        if not url.lower().startswith("https://"):
            return None   # a plain-http picture would be blocked as mixed content and is not worth the risk
        return {"url": url, "credit": source_name, "license": "as published by the source", "source_url": event.get("url") or url, "original_url": url, "cached": False}

    @staticmethod
    def _entry(record):
        if not record.get("ok"):
            return None
        return {"url": f"{MEDIA_URL}/{record['photo']['file']}", "credit": record["credit"], "license": record["license"], "source_url": record["page_url"] or record["source_url"],
                "original_url": record["source_url"], "cached": True}


class MediaEnrichmentService:
    def __init__(self, work, resolver, photos, clock, config=EnrichmentConfig(), classify=classifiers.annotate):
        self._work, self._resolver, self._photos, self._clock, self.config, self._classify = work, resolver, photos, clock, config, classify
        self._stop, self._thread, self._lock = threading.Event(), None, threading.Lock()
        resolver.sleep = self._stop.wait   # a stop never waits out a polite pause between lookups
        self._state = {"state": "stopped", "passes": 0, "enriched": 0, "errors": 0, "last_error": None, "last_pass_at": None}
        self._error_wait = config.error_wait_s

    # --- one pass ----------------------------------------------------------------------------------------------------

    def run_once(self):
        """Enrich up to one batch of candidates; returns how many were handled. A failure of one candidate's lookups leaves the others alone."""
        items = self._work.pending(self.config.batch)
        groups = {}
        for item in items:
            groups.setdefault((item["area"]["latitude"], item["area"]["longitude"]), []).append(item)
        results = []
        for group in groups.values():
            area = group[0]["area"]
            events = [self._legacy_event(item) for item in group]
            self._classify(events)
            self._resolver.cycle([(area, events)], limit=self.config.places_per_pass)
            annotated = self._resolver.annotate(events, area)
            for item, event, shown in zip(group, events, annotated):
                media = dict(shown.get("media") or {})
                image, finished = self._photos.cache(event, item["source_name"])
                if image:
                    media["image"] = image
                places_done = self._resolver.pending([(area, [event])]) == 0
                results.append({"candidate_id": item["candidate_id"], "payload_hash": item["payload_hash"], "media": media, "illustration": event.get("illustration"),
                                "complete": finished and places_done})
        if results:
            self._work.finish(results)
        self._note(passes=1, enriched=len(results), last_pass_at=int(self._clock()))
        return len(results)

    @staticmethod
    def _legacy_event(item):
        """The event in the shape the accepted imagery code reads. Coordinates the source supplied count as confirmed only inside the area."""
        event = dict(item["payload"])
        if event.get("geo") and inside_area(event["geo"], item["area"]):
            event["place"] = {"classification": "confirmed"}
        return event

    # --- lifecycle ---------------------------------------------------------------------------------------------------

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._state["state"] = "running"
            self._thread = threading.Thread(target=self._loop, name="media-enrichment", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout=10):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def status(self):
        with self._lock:
            return {**self._state, "resolver": dict(self._resolver.stats)}

    def _note(self, **values):
        with self._lock:
            for key, value in values.items():
                if key in ("passes", "enriched", "errors"):
                    self._state[key] += value
                else:
                    self._state[key] = value

    def _loop(self):
        while not self._stop.is_set():
            try:
                handled = self.run_once()
                self._error_wait = self.config.error_wait_s
                wait = self.config.busy_wait_s if handled else self.config.idle_wait_s
            except Exception as error:  # a broken pass never ends the worker: count it, say why, wait longer
                log.exception("media enrichment pass failed")
                self._note(errors=1, last_error=f"{type(error).__name__}: {str(error)[:200]}")
                wait = self._error_wait
                self._error_wait = min(self._error_wait * 2, self.config.error_wait_cap_s)
            self._stop.wait(wait)
        self._note(state="stopped")

