"""The events contract (`discovery.events.v1`): normalized events as records, and the read-only EventQueryService over them.

An event is one real-world occurrence. A candidate is one source's statement about it. Events are not copied per area or per card:
an area reaches an event through the sources that area uses (`area_sources`), so overlapping areas and many cards read the same
rows. An event has no coordinates until a geolocation component supplies them; a query by area is therefore by the sources that
serve the area, and every event states how its location is known (`location_status`) instead of pretending to be placed on a map.

`EventQueryService` is the read contract: it validates a request and asks Persistence (the `EventQueries` port) for plain records. It holds no SQL.
"""
import re
from dataclasses import dataclass
from typing import Optional

from ..contracts.models import ValidationFailed
from ..persistence.ports import EventQueries

EVENTS_SCHEMA = "discovery.events.v1"
MAX_PAGE = 200
MAX_EVIDENCE_LINES = 20
MAX_EVIDENCE_CHARS = 500


def verification_of(stored, candidate_key=None):
    """An event's verification, from what the producer of its current primary candidate recorded (`stored`, that candidate's `verification`), checked:

        verified       only an explicit `verified: true` with status 'verified' AND evidence lines saying what confirmed it; never inferred from completeness or an address
        corroborated   the producer recorded that its source's own statements agree, with the evidence lines (this is not independent verification)
        unverified     anything else, including missing, malformed or unsupported data

    `candidate_key` names the candidate the verification was read from (None when there is none)."""
    stored = stored if isinstance(stored, dict) else {}
    lines = stored.get("evidence") if isinstance(stored.get("evidence"), (list, tuple)) else ()
    evidence = [line.strip()[:MAX_EVIDENCE_CHARS] for line in lines if isinstance(line, str) and line.strip()][:MAX_EVIDENCE_LINES]
    verified = stored.get("verified") is True and stored.get("status") == "verified" and bool(evidence)
    status = "verified" if verified else "corroborated" if stored.get("status") == "corroborated" and evidence else "unverified"
    missing = stored.get("missing") if isinstance(stored.get("missing"), (list, tuple)) else ()
    note = stored.get("note") if isinstance(stored.get("note"), str) else ""
    return {"status": status, "verified": verified, "evidence": evidence, "missing": [m for m in missing if isinstance(m, str)], "note": note, "candidate_key": candidate_key}


@dataclass(frozen=True)
class EventRecord:
    event_key: str
    title: str
    description: Optional[str]
    starts_at: int
    ends_at: Optional[int]
    timezone: Optional[str]
    venue_name: Optional[str]
    location_text: Optional[str]
    address: Optional[str]
    location_status: str            # 'address' | 'venue' | 'description_only' | 'unknown': how the location is known; never a coordinate
    source_url: Optional[str]
    status: str                     # 'complete' | 'partial'
    missing: tuple
    revision: int
    updated_at: int
    sources: tuple = ()
    candidates: tuple = ()
    media: Optional[dict] = None            # {"image", "photo", "map"} with credits, from the media enrichment service; absent until it has run
    illustration: Optional[dict] = None     # the category illustration decision (category, strength, reasons)
    verification: Optional[dict] = None     # verification_of(...): status, verified, evidence, missing, note, candidate_key (additive)


@dataclass(frozen=True)
class EventDetail:
    schema: str
    event: EventRecord
    history: tuple                  # one dict per revision: revision, extraction_id, extractor, version, created_at, changed {field: [old, new]}
    evidence: dict                  # field -> {"origin", "evidence": [...]} from the extraction behind the current values
    candidate_links: tuple          # dicts: candidate_key, source_key, role


@dataclass(frozen=True)
class EventQuery:
    area_key: Optional[str] = None
    source_key: Optional[str] = None
    starts_from: Optional[int] = None
    starts_to: Optional[int] = None
    after: Optional[str] = None
    limit: int = 50


@dataclass(frozen=True)
class EventPage:
    schema: str
    events: tuple
    next_cursor: Optional[str]
    latest_seq: int                 # the change feed's position when the page was read: events changed later appear in /v1/changes


class EventQueryService:
    """The read contract over normalized events."""

    def __init__(self, queries: EventQueries):
        self._queries = queries

    def query(self, query):
        return self._queries.query(query, self._cursor(query.after))

    def get(self, event_key):
        return self._queries.get(event_key)

    @staticmethod
    def _cursor(value):
        if not value:
            return None
        match = re.fullmatch(r"(-?\d{1,12})\.(\d{1,12})", str(value))
        if not match:
            raise ValidationFailed("the cursor is not one this service issued")
        return int(match.group(1)), int(match.group(2))
