"""Tolerant field matching: normalized title, nearby starts and non-contradictory stated place evidence."""
import re

from . import Decision, Incoming, LOOKUP_PAGE_SIZE, retain


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


class TolerantMatch:
    name, version = "tolerant", 2

    def __init__(self, window_s=900):
        self.window_s = int(window_s)

    def decide(self, incoming: Incoming, lookup):
        event = incoming.event
        known = retain(incoming)
        if known is not None:
            return known
        title = _norm(event.title)
        earliest, latest = event.starts_at - self.window_s, event.starts_at + self.window_s
        after = None
        while True:
            page = lookup.starting_between(earliest, latest, after, LOOKUP_PAGE_SIZE)
            for other in page:
                if _norm(other.title) == title and self._same_place(event, other):
                    return Decision("link", other.event_key, "the same title, a start within the window, and matching place evidence")
            if len(page) < LOOKUP_PAGE_SIZE:
                break
            last = page[-1]
            after = (last.starts_at, last.event_key)
        return Decision("create", reason="no known event states the same title near this start in a comparable place")

    @staticmethod
    def _same_place(event, other):
        evidence_agrees = False
        for left, right in ((event.address, other.address), (event.venue_name, other.venue_name),
                            (event.location_text, other.location_text)):
            left, right = _norm(left), _norm(right)
            if left and right:
                if left != right:
                    return False
                evidence_agrees = True
        return evidence_agrees
