"""Coordinates every card's request against the one Area Events app. No Home Assistant imports.

Cards describe profiles (see profile.py). The hub keeps one record per distinct profile: identical requests from any
number of cards, dashboards or editor previews share that record and its cached results, and different profiles
never touch each other's. Registration with the app happens once per profile (and is repeated now and then), not
once per request. Refreshing visits the active records one after another; there is no task per card.
"""
import asyncio
import json
import time

from .client import AreaEventsAuthError, AreaEventsConnectionError, AreaEventsRequestError
from .const import ACTIVE_SECONDS, REGISTER_TTL_SECONDS, UNPROVEN_SECONDS

BUSY_STATES = ("pending", "discovering")


class Record:
    def __init__(self, key, profile):
        self.key, self.profile = key, profile
        self.id = None
        self.snapshot = None          # the app's last answer, events included
        self.error = None             # why the app could not be reached or refused, if so
        self.registered_at = 0.0
        self.last_requested = 0.0
        self.lock = asyncio.Lock()


class Hub:
    def __init__(self, client, clock=time.monotonic):
        self.client, self.clock = client, clock
        self._records = {}

    @property
    def busy(self):
        """True while any active profile is still being discovered, so polling should be quick."""
        return any(self._busy(r) for r in self._records.values())

    @staticmethod
    def _busy(record):
        if record.error and record.snapshot is None:
            return True  # keep trying to reach the app
        snapshot = record.snapshot or {}
        return snapshot.get("state") in BUSY_STATES or bool(snapshot.get("refreshing"))

    @property
    def profiles(self):
        return len(self._records)

    # --- cards ----------------------------------------------------------------------------

    async def request(self, profile, since=None):
        """The current answer for a profile. Never waits for discovery. Raises AreaEventsAuthError / AreaEventsRequestError."""
        # "San Diego, CA" and "san diego, ca" are the same place: one record, one registration
        key = json.dumps({**profile, "area": profile["area"].lower()} if "area" in profile else profile, sort_keys=True)
        record = self._records.get(key)
        created = record is None
        if created:
            record = self._records[key] = Record(key, profile)
        record.last_requested = self.clock()
        if created or record.snapshot is None:
            try:
                async with record.lock:
                    await self._sync(record)
            except AreaEventsRequestError:
                self._records.pop(key, None)  # the app refused this profile: do not keep retrying it
                raise
        return self._present(record, since)

    def _present(self, record, since):
        snapshot = record.snapshot or {}
        result = {
            "state": snapshot.get("state") or "unavailable",
            "message": record.error or snapshot.get("error"),
            "refreshing": bool(snapshot.get("refreshing")),
            "initial": bool(snapshot.get("initial", True)),
            "outcome": snapshot.get("outcome"),
            "progress": snapshot.get("progress"),
            "connected": record.error is None,
            "generated_at": snapshot.get("generated_at"),
            "location": snapshot.get("location"),
            "sources": snapshot.get("sources") or {},
            "report": snapshot.get("report") or [],
        }
        if "events" in snapshot:
            if since and since == snapshot.get("generated_at"):
                result["unchanged"] = True
            else:
                result["events"] = snapshot["events"]
        return result

    # --- upkeep -----------------------------------------------------------------------------

    async def refresh(self):
        """Check the app is reachable, then bring each active profile up to date, one after another."""
        await self.client.status()
        now = self.clock()
        for key, record in list(self._records.items()):
            idle = now - record.last_requested
            proven = bool(record.snapshot) and "events" in record.snapshot
            if idle > ACTIVE_SECONDS or (not proven and idle > UNPROVEN_SECONDS):
                del self._records[key]  # no card is asking for it any more; the app expires it by itself
        for record in list(self._records.values()):
            async with record.lock:
                await self._sync(record)

    async def _sync(self, record, retry=True):
        """Register if needed, then fetch the profile's answer. Connection problems are recorded, not raised."""
        try:
            if record.id is None or self.clock() - record.registered_at > REGISTER_TTL_SECONDS:
                record.id = (await self.client.register(record.profile))["id"]
                record.registered_at = self.clock()
            known = (record.snapshot or {}).get("generated_at")
            answer = await self.client.get(record.id, since=known)
            if answer.get("unchanged") and record.snapshot:
                answer = {**answer, "events": record.snapshot.get("events", [])}
                answer.pop("unchanged")
            record.snapshot, record.error = answer, None
        except AreaEventsConnectionError as error:
            record.error = str(error)
        except AreaEventsRequestError as error:
            if error.status == 404 and retry:
                record.id = None  # the app forgot the profile (restart, clean-up): register it again
                await self._sync(record, retry=False)
            elif record.snapshot is None:
                raise  # refused outright (invalid profile, too many areas): the card is told
            else:
                record.error = error.message
        except AreaEventsAuthError:
            record.error = "the Area Events app rejected the token"
            raise
