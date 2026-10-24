"""Conservative exact matching by normalized title, start instant and stated place facts."""
import re

from . import Decision, Incoming, LOOKUP_PAGE_SIZE, retain


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _same_place(event, other):
    address, other_address = _norm(event.address), _norm(other.address)
    if address or other_address:
        return bool(address) and address == other_address
    location, other_location = _norm(event.location_text), _norm(other.location_text)
    return bool(location) and location == other_location


class ExactMatch:
    name, version = "exact", 2

    def decide(self, incoming: Incoming, lookup):
        event = incoming.event
        known = retain(incoming)
        if known is not None:
            return known
        title = _norm(event.title)
        after = None
        while True:
            page = lookup.starting_between(event.starts_at, event.starts_at, after, LOOKUP_PAGE_SIZE)
            for other in page:
                end_times_agree = event.ends_at is None or other.ends_at is None or event.ends_at == other.ends_at
                if _norm(other.title) == title and end_times_agree and _same_place(event, other):
                    return Decision("link", other.event_key, "the same title, start and stated place")
            if len(page) < LOOKUP_PAGE_SIZE:
                break
            last = page[-1]
            after = (last.starts_at, last.event_key)
        return Decision("create", reason="no known event states the same title, start and place")
