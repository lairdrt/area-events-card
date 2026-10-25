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

DEDUP_SCHEMA = "discovery.dedup.v1"
ACTIONS = ("create", "update", "link", "keep")
LOOKUP_PAGE_SIZE = 100


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
    name: str
    version: int

    def decide(self, incoming: Incoming, lookup: EventLookup) -> Decision: ...


def retain(incoming: Incoming) -> Optional[Decision]:
    """The decision every strategy shares for a candidate that already has a place: its primary event is updated, a secondary stays secondary. None for a new candidate."""
    association = incoming.association
    if association is None:
        return None
    return Decision("update", association.event_key, reason="the candidate is this event's primary description") if association.role == "primary" \
        else Decision("keep", association.event_key, reason="the candidate is already a secondary description")
