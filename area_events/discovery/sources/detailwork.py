"""Shared, resumable detail-page work for adapters that read a listing and then each event's own page.

An adapter reads a listing (cheap, complete) and completes each event from its page (one request each, so limited per run). This keeps
the bookkeeping that every such adapter needs in one place, and keeps it in the events themselves, which the engine already caches
and hands back as `previous`: nothing new is stored.

* completed pages are reused, never fetched again (an event whose page has been read carries `extraction.detail_page`);
* a page that failed is not retried at once: its event records the number of failures and `detail_retry_after` (15 minutes, doubling,
  at most 12 hours), and meanwhile the budget goes to other events, so one broken page cannot hold up the rest;
* events are taken in listing order, which is date order, so the nearest events are completed first;
* a run never fetches more than `budget` pages; the rest stay as they are (their listing data is published) and are picked up by
  later runs, so the work always progresses.
"""
from datetime import datetime, timedelta, timezone

RETRY_BASE = 15 * 60
RETRY_MAX = 12 * 3600


def _now():
    return datetime.now(timezone.utc)


class DetailWork:
    def __init__(self, previous, key, budget, enabled=True, version=1, now=None):
        """`previous`: the events cached by the last run; `key(event)`: the identity an adapter uses for an event; `version`: the
        extraction version this adapter writes (an older completed page is read again when the adapter learns something new)."""
        self.old = {}
        for event in previous or []:
            try:
                self.old[key(event)] = event
            except (KeyError, TypeError):
                continue
        self.budget, self.enabled, self.version = budget, enabled, version
        self.now = now or _now()
        self.counts = {"detail_fetched": 0, "detail_reused": 0, "detail_failed": 0, "detail_deferred": 0, "detail_backoff": 0}
        self.errors = []

    def reusable(self, ident):
        """The cached event whose page was already read (at this extraction version), else None."""
        old = self.old.get(ident)
        extraction = (old or {}).get("extraction") or {}
        if old and extraction.get("detail_page") and extraction.get("detail_v", 1) >= self.version:
            return old
        return None

    def reused(self):
        self.counts["detail_reused"] += 1

    def _waiting(self, ident):
        extraction = ((self.old.get(ident) or {}).get("extraction")) or {}
        after = extraction.get("detail_retry_after")
        if not after:
            return False
        try:
            return datetime.fromisoformat(after) > self.now
        except ValueError:
            return False

    def should_fetch(self, ident):
        """May this run fetch the event's page? Counts the reason when it may not."""
        if not self.enabled:
            return False
        if self._waiting(ident):
            self.counts["detail_backoff"] += 1
            return False
        if self.budget <= 0:
            self.counts["detail_deferred"] += 1
            return False
        self.budget -= 1
        return True

    def done(self):
        self.counts["detail_fetched"] += 1

    def failed(self, ident, url, error):
        self.counts["detail_failed"] += 1
        self.errors.append(f"{url}: {error}"[:300])

    def stamp(self, event, ident, completed, failed=False):
        """Record the page state on the event: completed (with the extraction version), or the failure and when to try again."""
        extraction = event.setdefault("extraction", {})
        extraction["detail_page"] = bool(completed)
        if completed:
            extraction["detail_v"] = self.version
            return event
        previous = ((self.old.get(ident) or {}).get("extraction")) or {}
        failures = int(previous.get("detail_failures") or 0)
        if failed:
            failures += 1
            wait = min(RETRY_BASE * 2 ** (failures - 1), RETRY_MAX)
            extraction["detail_retry_after"] = (self.now + timedelta(seconds=wait)).isoformat(timespec="seconds")
        elif previous.get("detail_retry_after"):
            extraction["detail_retry_after"] = previous["detail_retry_after"]  # still backing off: carry it forward
        if failures:
            extraction["detail_failures"] = failures
        return event

    def notes(self, notes):
        notes.update(self.counts)
        if self.errors:
            notes["detail_errors"] = self.errors[:5]
        return notes
