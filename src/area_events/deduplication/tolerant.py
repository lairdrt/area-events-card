"""Permissive matching: titles and place names that are merely similar, starts that differ by a few minutes, and pages that need not agree, still weighed by confidence.
More permissive means lower floors and thresholds, never fewer vetoes: differing numbers, distant starts or ends and conflicting places still keep events apart."""
from . import Decision, Incoming, anchors, decide_by_confidence, same_event_facts
from .scoring import MatchProfile

PROFILE = MatchProfile(name="tolerant", start_tolerance_s=600, end_tolerance_s=900, min_title=0.85, min_title_same_page=0.78, min_address=0.85, min_venue=0.75,
                       url_conflict_vetoes=False, threshold=0.85, min_coverage=0.50, location_agreement=0.80, venue_alone_agrees=True, ambiguity_margin=0.005, no_place_title_min=0.97)


class TolerantMatch:
    name, version = "tolerant", 5

    def __init__(self, window_s=None, profile=None, **overrides):
        if window_s is not None:
            overrides["end_tolerance_s"] = int(window_s)
        self.profile = (profile or PROFILE).with_(**overrides)

    def matches(self, left, right):
        return same_event_facts(left, right, self.profile)

    def anchors(self, event):
        return anchors(event)

    def decide(self, incoming: Incoming, lookup) -> Decision:
        return decide_by_confidence(incoming, lookup, self.profile)
