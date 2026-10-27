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
import re
from typing import Optional, Protocol

from ..contracts.models import CleanEvent

DEDUP_SCHEMA = "discovery.dedup.v1"
ACTIONS = ("create", "update", "link", "keep")
LOOKUP_PAGE_SIZE = 100


def normalize(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def anchors(event):
    return tuple((field, value) for field in ("address", "venue_name", "location_text")
                 if (value := normalize(getattr(event, field))))


def _url_identity(value):
    if not value:
        return ""
    clean = value.strip().split("#", 1)[0]
    before_query, separator, query = clean.partition("?")
    scheme, marker, rest = before_query.partition("://")
    if not marker:
        return normalize(value)
    host, slash, path = rest.partition("/")
    query_parts = []
    if separator:
        for pair in query.split("&"):
            key, equal, item = pair.partition("=")
            if key.lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}:
                query_parts.append((key, item if equal else ""))
    normalized_query = "&".join(f"{key}={item}" for key, item in sorted(query_parts))
    return f"{scheme.lower()}://{host.lower()}/{path.rstrip('/')}?{normalized_query}"


def _specific_url(value):
    identity = _url_identity(value)
    if not identity or "://" not in identity:
        return False
    path = identity.split("://", 1)[1].split("/", 1)
    segments = path[1].split("?", 1)[0].lower().split("/") if len(path) > 1 else []
    generic = {"", "event", "events", "calendar", "calendars", "listing", "listings", "home", "index"}
    return any(segment not in generic for segment in segments)


def same_event_facts(left, right, start_window_s=0, allow_venue_only=False, end_window_s=0):
    """High-confidence event match. Every stated comparable fact must agree."""
    a, b = normalize(left.title), normalize(right.title)
    if not a or not b:
        return False
    title_changed = a != b
    if title_changed:
        a_words, b_words = set(a.split()), set(b.split())
        shared = a_words & b_words
        overlap = len(shared) / len(a_words | b_words) if a_words | b_words else 0
        if len(shared) < 4 or overlap < 0.75:
            return False
    if abs(int(left.starts_at) - int(right.starts_at)) > int(start_window_s):
        return False
    if left.ends_at is not None and right.ends_at is not None and abs(int(left.ends_at) - int(right.ends_at)) > int(end_window_s):
        return False
    if left.source_url and right.source_url and _url_identity(left.source_url) != _url_identity(right.source_url):
        return False
    if title_changed and not (left.source_url and right.source_url and _specific_url(left.source_url) and
                              _url_identity(left.source_url) == _url_identity(right.source_url)):
        return False
    agrees = False
    for field in ("address", "venue_name", "location_text"):
        left_value, right_value = normalize(getattr(left, field)), normalize(getattr(right, field))
        if left_value and right_value:
            if left_value != right_value:
                return False
            if field != "venue_name" or allow_venue_only:
                agrees = True
    if title_changed:
        venue_agrees = normalize(left.venue_name) and normalize(left.venue_name) == normalize(right.venue_name)
        address_agrees = normalize(left.address) and normalize(left.address) == normalize(right.address)
        location_agrees = normalize(left.location_text) and normalize(left.location_text) == normalize(right.location_text)
        return bool(agrees and venue_agrees and (address_agrees or location_agrees))
    return agrees


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

    def matches(self, left: ExistingEvent, right: ExistingEvent) -> bool: ...

    def anchors(self, event: ExistingEvent) -> tuple: ...


def retain(incoming: Incoming) -> Optional[Decision]:
    """The decision every strategy shares for a candidate that already has a place: its primary event is updated, a secondary stays secondary. None for a new candidate."""
    association = incoming.association
    if association is None:
        return None
    return Decision("update", association.event_key, reason="the candidate is this event's primary description") if association.role == "primary" \
        else Decision("keep", association.event_key, reason="the candidate is already a secondary description")
