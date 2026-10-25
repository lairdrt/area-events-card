"""The integration's one shared view of the Discovery Service. No Home Assistant imports.

One hub per config entry. It owns three small things, all in memory and none authoritative: the list of discovery areas, one cached
event snapshot per area that some card follows, and the list of card subscriptions. The database stays with the Discovery host.

    card A (San Diego) ─┐                          ┌─ read  client → GET /v1/events, /v1/changes, /v1/discovery/status
    card B (Austin)    ─┼─ hub ─ one refresh loop ─┤
    card C (San Diego) ─┘   (the coordinator)      └─ control client → area / settings commands (administrators only)

Updates. The coordinator calls `refresh()` on a bounded interval. A refresh makes one status call and one change-feed call however
many cards exist; only when the feed reports an event or area change after a snapshot's position does it read that area again, once,
for every card that follows it, and only cards whose view really changed are sent a message. Each snapshot remembers the change-feed
position it was read at, so nothing that happens while the service is unreachable or Home Assistant restarts is missed: the next
successful refresh reads the changes after that position (or, after an outage, rereads the followed areas). A card's own display
filters never reach the hub, so one card cannot change what another sees, and asking for events never starts a crawl.
"""
import hashlib
import itertools
import json
import logging
import time
from datetime import datetime, timezone

from .discovery_client import DiscoveryAuthError, DiscoveryConnectionError
from .feed import UNVERIFIED_LOCATIONS, source_name, to_feed_events

_LOGGER = logging.getLogger(__name__)
CHANGE_PAGE = 200
MAX_CHANGE_PAGES = 5
EVENT_PAGE = 200
MAX_EVENT_PAGES = 5            # a followed area holds at most this many events (1000); more is reported as truncated
LOOKBACK_SECONDS = 6 * 3600    # events that started a few hours ago may still be going on; the card hides the ones that ended
RELEVANT_KINDS = ("area.configuration_changed",)


class UnknownArea(LookupError):
    def __init__(self, reference, names):
        super().__init__(f"no discovery area '{reference}'" + (f"; available: {', '.join(names)}" if names else "; none are configured"))
        self.reference, self.names = reference, names


class ControlNotConfigured(Exception):
    """No control token was entered, so shared settings cannot be changed from Home Assistant."""


class _Feed:
    def __init__(self, area_key):
        self.area_key = area_key
        self.events, self.latest_seq, self.loaded_at, self.truncated = [], 0, 0.0, False

    @property
    def keys(self):
        return {e["id"] for e in self.events}


class _Subscription:
    def __init__(self, area_key, send):
        self.area_key, self.send, self.digest = area_key, send, None


def _norm(text):
    return " ".join(str(text or "").lower().split())


class DiscoveryHub:
    def __init__(self, read, control=None, clock=time.time):
        self._read, self._control, self._clock = read, control, clock
        self._areas, self._status, self._processing = None, None, None
        self._areas_stale = False   # the cached list is out of date but still the last known one (the cards keep being served from it)
        self._feeds, self._subs, self._ids = {}, {}, itertools.count(1)
        self.connected, self.last_error, self._closed = None, None, False
        self._sources = {}

    @property
    def can_control(self):
        return self._control is not None

    @property
    def followed(self):
        """(areas with a cached snapshot, card subscriptions): what one refresh serves."""
        return len(self._feeds), len(self._subs)

    # --- areas and status ---------------------------------------------------------------------------------------------

    async def areas(self, reload=False):
        if self._areas is None or reload or self._areas_stale:
            self._areas = list(await self._read.areas())
            self._areas_stale = False
        return [dict(a) for a in self._areas]

    async def resolve(self, reference):
        """The discovery area a card named: its key or its name, ignoring case and extra spaces."""
        for attempt in (0, 1):
            wanted = _norm(reference)
            for area in await self.areas(reload=bool(attempt)):
                if reference == area["area_key"] or _norm(area["name"]) == wanted:
                    return area
        raise UnknownArea(reference, sorted(a["name"] for a in self._areas or ()))

    async def status(self):
        """Availability, shared settings and freshness, read from the service (one call)."""
        status = await self._read.status()
        self._remember_status(status)
        return self._service_view()

    def _remember_status(self, status):
        self.connected, self.last_error = True, None
        self._status = status
        self._sources = {s["source_key"]: s for s in status.get("sources", ())}

    def _service_view(self):
        status = self._status or {}
        settings = status.get("settings") or {}
        runtime = status.get("runtime") or {}
        return {"connected": bool(self.connected), "message": self.last_error, "paused": bool(settings.get("paused")), "mode": settings.get("mode"),
                "intensity": settings.get("intensity"), "settings_revision": settings.get("revision"), "areas": status.get("areas"),
                "areas_enabled": status.get("areas_enabled"), "engine": runtime.get("state"), "can_control": self.can_control,
                "extraction_pending": (self._processing or {}).get("pending")}

    # --- cards ---------------------------------------------------------------------------------------------------------

    async def page(self, reference, starts_from=None, starts_to=None, after=None, limit=EVENT_PAGE):
        """One page straight from the authoritative store (nothing cached): events in feed form, the next cursor and the feed position."""
        area = await self.resolve(reference)
        page = await self._read.events(area=area["area_key"], starts_from=starts_from, starts_to=starts_to, after=after, limit=limit)
        return {"events": to_feed_events(page), "next_cursor": page.get("next_cursor"), "latest_seq": page.get("latest_seq"), "area": self._area_view(area)}

    async def media(self, name):
        """A cached picture (bytes, content type), straight from the service. Pictures are immutable by name, so the browser caches them; the hub keeps nothing."""
        return await self._read.media(name)

    async def event(self, event_key):
        detail = await self._read.event(event_key)
        return {"event": to_feed_events({"events": [detail["event"]]})[0], "history": detail.get("history", []), "evidence": detail.get("evidence", {}),
                "candidates": detail.get("candidate_links", [])}

    async def snapshot(self, reference):
        area = await self.resolve(reference)
        feed = self._feeds.get(area["area_key"]) or await self._load(area)
        return self._present(area, feed)

    async def subscribe(self, reference, send):
        """Follow an area for one card. `send(message)` receives the full current view now and again whenever it changes.
        Returns the function that ends the subscription."""
        if self._closed:
            raise DiscoveryConnectionError("the Area Events integration is shutting down")
        area = await self.resolve(reference)
        if self._status is None:   # so the first view already carries the service's state
            self._remember_status(await self._read.status())
            self._processing = await self._read.processing()
        feed = self._feeds.get(area["area_key"]) or await self._load(area)
        key = next(self._ids)
        sub = self._subs[key] = _Subscription(area["area_key"], send)
        self._push(sub, area, feed)

        def unsubscribe():
            self._subs.pop(key, None)
            if not any(s.area_key == area["area_key"] for s in self._subs.values()):
                self._feeds.pop(area["area_key"], None)   # nobody follows it any more: nothing to keep fresh

        return unsubscribe

    # --- the shared refresh --------------------------------------------------------------------------------------------

    async def refresh(self):
        """One coordinated update for every card. Raises DiscoveryConnectionError / DiscoveryAuthError after telling the cards."""
        was_down = self.connected is False
        try:
            self._remember_status(await self._read.status())
            if self._feeds or self._subs:
                self._processing = await self._read.processing()
                dirty = await self._changed_feeds(was_down)
            else:
                dirty = set()
        except DiscoveryConnectionError as error:
            self._set_availability(False, str(error))
            raise
        except DiscoveryAuthError as error:
            self._set_availability(False, str(error))
            raise
        self.connected, self.last_error = True, None
        try:
            if dirty == "all" or self._areas_stale or any(key not in {a["area_key"] for a in self._areas or ()} for key in dirty):
                await self.areas(reload=True)
            by_key = {a["area_key"]: a for a in self._areas or ()}
            for key in (list(self._feeds) if dirty == "all" else sorted(dirty)):
                if key in by_key and key in self._feeds:
                    await self._load(by_key[key])
        except (DiscoveryConnectionError, DiscoveryAuthError) as error:
            self._set_availability(False, str(error))
            raise
        self._push_all()

    async def _changed_feeds(self, outage):
        """The followed areas whose events or definition changed since each snapshot was read (or "all" when that cannot be told)."""
        if not self._feeds:
            return set()
        after, dirty = min(f.latest_seq for f in self._feeds.values()), set()
        for _ in range(MAX_CHANGE_PAGES):
            batch = await self._read.changes(after, CHANGE_PAGE)
            if batch.get("resync_required"):
                return "all"
            for change in batch.get("changes", ()):
                kind, seq = change["kind"], change["seq"]
                newer = {k: f for k, f in self._feeds.items() if f.latest_seq < seq}
                if kind in RELEVANT_KINDS:
                    self._areas_stale = True
                    dirty |= set(newer)
                elif kind == "event.created":
                    dirty |= set(newer)      # a new event may belong to any followed area; which one is not in the record
                elif kind in ("event.updated", "event.removed"):
                    known = any(change["entity_key"] in f.keys for f in self._feeds.values())
                    # an event no followed area shows may have moved into one (a new start time): read them all; otherwise only its own
                    dirty |= {k for k, f in newer.items() if change["entity_key"] in f.keys} if known else set(newer)
            if len(batch.get("changes", ())) < CHANGE_PAGE:
                return "all" if outage else dirty
            after = batch["next_seq"]
        return "all"

    async def _load(self, area):
        feed = _Feed(area["area_key"])
        cursor, floor = None, int(self._clock()) - LOOKBACK_SECONDS
        for page_number in range(MAX_EVENT_PAGES):
            page = await self._read.events(area=area["area_key"], starts_from=floor, after=cursor, limit=EVENT_PAGE)
            if page_number == 0:
                feed.latest_seq = int(page.get("latest_seq") or 0)   # the position of the first read: later changes are seen again
            feed.events.extend(to_feed_events(page))
            cursor = page.get("next_cursor")
            if not cursor:
                break
        else:
            feed.truncated = True
        feed.loaded_at = self._clock()
        self._feeds[area["area_key"]] = feed
        return feed

    # --- messages ----------------------------------------------------------------------------------------------------

    def _area_view(self, area):
        return {"area_key": area["area_key"], "name": area["name"], "enabled": area["enabled"], "intensity": area["intensity"], "revision": area["revision"],
                "sources": list(area.get("sources", ())), "radius": area["radius"], "unit": area["unit"]}

    def _present(self, area, feed):
        """The card's view of one area: the existing reply shape, plus the area and service blocks."""
        sources = {}
        for key in area.get("sources", ()):
            known = self._sources.get(key) or {}
            count = sum(1 for e in feed.events if key in (e.get("source_keys") or [(e.get("source") or {}).get("key")]))
            sources[key] = {"name": source_name(key), "ok": not known.get("last_error"), "events": count, "last_success_at": known.get("last_success_at"),
                            "error": known.get("last_error")}
        service = self._service_view()
        message = None if self.connected else (self.last_error or "the Discovery Service is not reachable")
        return {
            "state": "ready" if self.connected else "unavailable",
            "message": message,
            "refreshing": False, "initial": False, "outcome": "ok" if feed.events else "no_results",
            "connected": bool(self.connected),
            "generated_at": self._freshness(sources),
            "revision": feed.latest_seq,
            "location": {"display_name": area["name"], "coverage": "sources"},
            "sources": sources, "report": [],
            "events": feed.events, "truncated": feed.truncated,
            "area": self._area_view(area), "service": service,
            "notice": self._notice(area, feed, service),
        }

    @staticmethod
    def _freshness(sources):
        """When the area's sources were last read successfully (the newest of them), or None if none has been: this is what "Updated" means."""
        latest = max((s["last_success_at"] for s in sources.values() if s.get("last_success_at")), default=None)
        return datetime.fromtimestamp(latest, tz=timezone.utc).isoformat(timespec="seconds") if latest else None

    def _notice(self, area, feed, service):
        if not area.get("sources"):
            return f"No event sources are attached to {area['name']}, so nothing is being discovered for it."
        notes = []
        if not area["enabled"]:
            notes.append(f"Discovery for {area['name']} is switched off.")
        if service["paused"]:
            notes.append("Discovery is paused; showing what was found earlier.")
        if any((e.get("source") or {}).get("key", "").endswith("_fixture") for e in feed.events):
            notes.append("Some events here are demonstration data, not live discoveries.")
        if not feed.events:
            if not any((self._sources.get(k) or {}).get("last_success_at") for k in area["sources"]):
                notes.append(f"The sources attached to {area['name']} have not completed a read yet.")
            else:
                notes.append(f"No events have been discovered for {area['name']} from its attached sources. That does not mean nothing is happening there.")
        elif any(e.get("location_status") in UNVERIFIED_LOCATIONS for e in feed.events):
            notes.append("Events are matched to this area by their source, not by a verified distance; some locations are unverified.")
        return " ".join(notes) or None

    def _digest(self, message):
        stable = {k: v for k, v in message.items() if k not in ("generated_at", "revision")}
        stable["service"] = {k: v for k, v in message["service"].items() if k != "extraction_pending"}   # operational detail, not worth a push
        stable["events"] = [(e["id"], e.get("revision")) for e in message["events"]]
        return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()

    def _push(self, sub, area, feed, force=False):
        message = self._present(area, feed)
        digest = self._digest(message)
        if digest == sub.digest and not force:
            return
        sub.digest = digest
        try:
            sub.send(message)
        except Exception:  # a broken connection never stops the others
            _LOGGER.debug("dropping a card subscription that could not be written to", exc_info=True)
            for key, value in list(self._subs.items()):
                if value is sub:
                    self._subs.pop(key, None)

    def _push_all(self):
        by_key = {a["area_key"]: a for a in self._areas or ()}
        for sub in list(self._subs.values()):
            area, feed = by_key.get(sub.area_key), self._feeds.get(sub.area_key)
            if area is not None and feed is not None:
                self._push(sub, area, feed)

    def _set_availability(self, connected, message):
        changed = self.connected != connected
        self.connected, self.last_error = connected, message
        if changed:
            self._push_all()

    async def aclose(self):
        """The integration is unloading: tell every card, then forget them."""
        self._closed = True
        self.connected, self.last_error = False, "the Area Events integration was unloaded"
        self._push_all()
        self._subs.clear()
        self._feeds.clear()

    # --- shared settings (administrators; the websocket layer checks Home Assistant's authorization first) --------------------

    def _controller(self):
        if self._control is None:
            raise ControlNotConfigured("no control token is configured for this integration")
        return self._control

    async def _changed(self, call):
        """Run a control command. Success means the service saved it; the next refresh brings the cards up to date."""
        result = await call
        self._areas_stale = True
        return result

    async def create_area(self, **fields):
        return await self._changed(self._controller().create_area(**fields))

    async def update_area(self, area_key, expected_revision, **changes):
        return await self._changed(self._controller().update_area(area_key, expected_revision, **changes))

    async def set_settings(self, expected_revision, mode=None, intensity=None):
        return await self._changed(self._controller().set_settings(expected_revision, mode=mode, intensity=intensity))

    async def pause(self):
        return await self._changed(self._controller().pause())

    async def resume(self):
        return await self._changed(self._controller().resume())
