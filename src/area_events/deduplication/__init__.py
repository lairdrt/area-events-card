"""Deduplication: deciding whether a clean event is one the system already knows. The interface (`discovery.dedup.v1`) and its records.

Deduplication owns the matching rules and nothing else. It never touches a database: it is handed one `Incoming` (the clean event a candidate produced, and what is already
known about that candidate's place among events) and a read-only `EventLookup` through which Persistence lets it see existing events, and it answers with one explicit
`Decision`. Persistence runs the lookup, the decision and the write as ONE atomic operation, validates the decision, and applies it; the strategy never sees a connection or a
transaction, and Persistence never knows how the decision was reached. A different matching algorithm is therefore a different object registered at the composition root,
with no change to Persistence, processing, producers or presentation.

    action   meaning
    create   a new canonical event with a stable event_key
    update   the candidate is the primary description of its event: the event takes the new facts
    link     the candidate describes an existing event (`event_key`), which keeps its own facts; the candidate becomes a secondary description
    keep     the candidate is already a secondary description of its event; nothing changes

The strategies in this package are the initial default (`exact`, conservative) and an alternative (`tolerant`); both implement the same `Deduplicator`.
"""
from dataclasses import dataclass
from typing import Optional, Protocol

from ..contracts.models import CleanEvent
from .scoring import MatchProfile, best_match, compare, normalize, title_keys

DEDUP_SCHEMA = "discovery.dedup.v1"
DEDUP_MAINTENANCE_SCHEMA = "discovery.dedup.maintenance.v1"  # optional extension for database duplicate scans
ACTIONS = ("create", "update", "link", "keep")
LOOKUP_PAGE_SIZE = 100
CONSERVATIVE = MatchProfile(name="exact", no_place_title_min=0.97)


def anchors(event):
    """What a maintenance scan may bucket an event by (together with its start): its stated places, or, when it states none, its title (such events can only ever match on an
    exceptionally strong identity, and the title is that identity)."""
    places = tuple((field, value) for field in ("address", "venue_name", "location_text") if (value := normalize(getattr(event, field))))
    return places or tuple(("title", key) for key in title_keys(event.title))


def same_event_facts(left, right, profile=None):
    """High-confidence event match under a `MatchProfile` (default: the conservative one). Weighted fuzzy comparison with hard vetoes: see `scoring`."""
    return compare(left, right, profile or CONSERVATIVE).matched


def decide_by_confidence(incoming: "Incoming", lookup: "EventLookup", profile: MatchProfile) -> "Decision":
    """The shared decision loop: an existing place is retained; otherwise every known event inside the start tolerance is scored (bounded pages, bounded count) and the best
    UNAMBIGUOUS match is linked. An ambiguous or unmatched candidate becomes a new event: never merging is the safe error."""
    known = retain(incoming)
    if known is not None:
        return known
    event = incoming.event
    earliest, latest = event.starts_at - profile.start_tolerance_s, event.starts_at + profile.start_tolerance_s
    seen, after, examined, truncated = [], None, 0, False
    while True:
        page = lookup.starting_between(earliest, latest, after, LOOKUP_PAGE_SIZE)
        seen.extend(page)
        examined += len(page)
        if len(page) < LOOKUP_PAGE_SIZE:
            break
        if examined >= profile.max_compared:
            truncated = True
            break
        last = page[-1]
        after = (last.starts_at, last.event_key)
    match, why = best_match(event, seen, profile)
    if match is not None and not truncated:
        return Decision("link", match.event_key, why)
    if truncated:
        return Decision("create", reason=f"more than {profile.max_compared} events share this time; no unambiguous match can be certified")
    return Decision("create", reason=why)


@dataclass(frozen=True)
class ExistingEvent:
    """An event already known, as much of it as matching may use. Plain facts: no key of the database, no row."""
    event_key: str
    title: str
    starts_at: int
    ends_at: Optional[int]
    venue_name: Optional[str]
    address: Optional[str]
    location_text: Optional[str]
    revision: int
    source_url: Optional[str] = None
    description: Optional[str] = None
    timezone: Optional[str] = None


@dataclass(frozen=True)
class Association:
    """Where a candidate already stands: the event it describes and whether it is that event's primary or a secondary description."""
    event_key: str
    role: str                       # 'primary' | 'duplicate'


@dataclass(frozen=True)
class Incoming:
    candidate_key: str
    event: CleanEvent
    association: Optional[Association]


@dataclass(frozen=True)
class Decision:
    action: str
    event_key: Optional[str] = None     # the event concerned (update, link, keep)
    reason: str = ""


class EventLookup(Protocol):
    """Read-only access to existing events, valid only during one `decide` call."""

    def starting_between(self, earliest: int, latest: int, after=None, limit: int = LOOKUP_PAGE_SIZE) -> tuple: ...


class Deduplicator(Protocol):
    """Baseline discovery.dedup.v1 plugin: deciding where one candidate belongs."""
    name: str
    version: int

    def decide(self, incoming: Incoming, lookup: EventLookup) -> Decision: ...


class MaintenanceMatcher(Deduplicator, Protocol):
    """Optional discovery.dedup.maintenance.v1 extension for scanning stored events."""
    def matches(self, left: ExistingEvent, right: ExistingEvent) -> bool: ...

    def anchors(self, event: ExistingEvent) -> tuple: ...


def retain(incoming: Incoming) -> Optional[Decision]:
    """The decision every strategy shares for a candidate that already has a place: its primary event is updated, a secondary stays secondary. None for a new candidate."""
    association = incoming.association
    if association is None:
        return None
    return Decision("update", association.event_key, reason="the candidate is this event's primary description") if association.role == "primary" \
        else Decision("keep", association.event_key, reason="the candidate is already a secondary description")
