"""Conservative matching: the same start instant, a stated place that agrees, and a title that is (nearly) the same; or the same start instant at the same specific
venue and address under related titles, whatever the pages (`venue_time_identity`). Every weight, tolerance and threshold is the `MatchProfile`'s; pass `profile=` or keyword overrides
(`ExactMatch(threshold=0.95)`) to tune it."""
from . import Decision, Incoming, anchors, decide_by_confidence, same_event_facts
from .scoring import MatchProfile

PROFILE = MatchProfile(name="exact", no_place_title_min=0.97)


class ExactMatch:
    name, version = "exact", 6

    def __init__(self, profile=None, **overrides):
        self.profile = (profile or PROFILE).with_(**overrides)

    def matches(self, left, right):
        return same_event_facts(left, right, self.profile)

    def anchors(self, event):
        return anchors(event)

    def decide(self, incoming: Incoming, lookup) -> Decision:
        return decide_by_confidence(incoming, lookup, self.profile)
