"""An alternative strategy behind the same interface: tolerant matching. Descriptions are one event when they state the same title (as lower-case alphanumerics) and
starts within `window_s` seconds of each other, and their places do not contradict each other: two street addresses must be the same, and when an address is missing on either
side the venue names must be the same non-empty name. A description whose place says nothing at all is not matched (nothing is invented to make it comparable)."""
import re

from . import Decision, Incoming, retain


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


class TolerantMatch:
    name, version = "tolerant", 1

    def __init__(self, window_s=900):
        self.window_s = int(window_s)

    def identity(self, event):
        return f"tolerant1:{_norm(event.title)}|{event.starts_at}"

    def decide(self, incoming: Incoming, lookup):
        event = incoming.event
        known = retain(incoming)
        if known is not None:
            return Decision(known.action, known.event_key, self.identity(event) if known.action == "update" else None, known.reason)
        title = _norm(event.title)
        for other in lookup.starting_between(event.starts_at - self.window_s, event.starts_at + self.window_s):
            if _norm(other.title) == title and self._same_place(event, other):
                return Decision("link", other.event_key, None, "the same title, a start within the window, and a place that does not contradict")
        return Decision("create", None, self.identity(event), "no known event states the same title near this start in the same place")

    @staticmethod
    def _same_place(event, other):
        if event.address and other.address:
            return _norm(event.address) == _norm(other.address)
        venue, theirs = _norm(event.venue_name), _norm(other.venue_name)
        return bool(venue) and venue == theirs
