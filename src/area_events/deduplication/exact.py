"""Conservative exact matching by normalized title, start instant and stated place facts."""
from . import Decision, Incoming, LOOKUP_PAGE_SIZE, anchors, retain, same_event_facts


class ExactMatch:
    name, version = "exact", 3

    def matches(self, left, right):
        return same_event_facts(left, right)

    def anchors(self, event):
        return anchors(event)

    def decide(self, incoming: Incoming, lookup):
        event = incoming.event
        known = retain(incoming)
        if known is not None:
            return known
        after = None
        while True:
            page = lookup.starting_between(event.starts_at, event.starts_at, after, LOOKUP_PAGE_SIZE)
            for other in page:
                if self.matches(event, other):
                    return Decision("link", other.event_key, "matching title, start and compatible stated facts")
            if len(page) < LOOKUP_PAGE_SIZE:
                break
            last = page[-1]
            after = (last.starts_at, last.event_key)
        return Decision("create", reason="no known event states the same title, start and place")
