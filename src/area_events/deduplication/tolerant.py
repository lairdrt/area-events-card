"""Tolerant field matching: similar titles, exact starts and compatible stated place evidence."""
from . import Decision, Incoming, LOOKUP_PAGE_SIZE, anchors, retain, same_event_facts


class TolerantMatch:
    name, version = "tolerant", 3

    def __init__(self, window_s=900):
        self.window_s = int(window_s)

    def matches(self, left, right):
        return same_event_facts(left, right, 0, allow_venue_only=True, end_window_s=self.window_s)

    def anchors(self, event):
        return anchors(event)

    def decide(self, incoming: Incoming, lookup):
        event = incoming.event
        known = retain(incoming)
        if known is not None:
            return known
        earliest = latest = event.starts_at
        after = None
        while True:
            page = lookup.starting_between(earliest, latest, after, LOOKUP_PAGE_SIZE)
            for other in page:
                if self.matches(event, other):
                    return Decision("link", other.event_key, "matching title, exact start, and compatible place evidence")
            if len(page) < LOOKUP_PAGE_SIZE:
                break
            last = page[-1]
            after = (last.starts_at, last.event_key)
        return Decision("create", reason="no known event states a matching title, exact start and comparable place")
