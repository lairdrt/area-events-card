"""The standard producer-to-processing interface: Source -> Producer -> Standard Records -> Processing.

A producer (a source's adapter) answers `crawl(fetcher, previous, options)` with ONE immutable, versioned envelope, `ProducerResult`, built directly from two record types:

* `EventCandidate`: one event as a source states it. Required: an id that is unique within the source, a title, and a start time WITH a UTC offset. Everything else is optional and
  stays `None` or empty when the source did not say it; nothing is guessed. A candidate is a source's CLAIM. It is stored as evidence and checkpointed, but it becomes an event only
  by passing processing (extraction and validation); no candidate is ever an event.
* `SourceLead`: a page a producer considered while looking for events, with where it was found and why it was accepted or rejected. It has no event facts and cannot be turned
  into an event.

The records are closed: there is no place for source-specific fields. The one free-form field, `EventCandidate.state`, is the producer's own working state (what it wants handed back
as `previous` on its next run, e.g. which detail pages it has read). It is stored apart from the record, is not part of the record's identity (`candidate_digest`), and no processing
module reads it.

`accept()` is the one validation boundary: it checks the envelope and every record, sets invalid ones aside with a reason (counted in the run's notes) and passes on only valid,
de-duplicated records. The stored form of a candidate is `document()`, a versioned JSON document of the standard fields; `candidate_from_document()` reads it back.
"""
import hashlib
import json
import types
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional
from urllib.parse import urlsplit

SCHEMA = "discovery.producer.v1"
LEAD_DECISIONS = frozenset(("accepted", "rejected", "pending"))
MAX_STATE_BYTES = 20_000
TEXT_FIELDS = ("ends_at", "timezone", "venue", "address", "description", "url", "image_url", "image_source", "organizer", "price")


def _freeze(value):
    if isinstance(value, dict):
        return types.MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def thaw(value):
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(v) for v in value]
    return value


def _offset_time(value):
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except (TypeError, ValueError):
        return False


def _web_url(value):
    parts = urlsplit(value) if isinstance(value, str) else None
    return bool(parts and parts.scheme in ("http", "https") and parts.hostname)


@dataclass(frozen=True)
class Provenance:
    """Where a record came from: the source's display name, the listing it was read from, when, and every listing that carried it."""
    source_name: Optional[str] = None
    listing_url: Optional[str] = None
    retrieved_at: Optional[str] = None
    found_on: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "found_on", tuple(self.found_on or ()))


@dataclass(frozen=True)
class Verification:
    """What the producer itself established about a record. `verified` is only the producer's claim; processing does not take it as fact."""
    status: str = "unverified"
    evidence: tuple = ()            # human-readable lines: what was read where
    missing: tuple = ()
    note: str = ""
    verified: bool = False

    def __post_init__(self):
        object.__setattr__(self, "evidence", tuple(self.evidence or ()))
        object.__setattr__(self, "missing", tuple(self.missing or ()))


GAP_FIELDS = ("end", "venue", "address", "description", "image_url")
SINGLE_SOURCE_NOTE = "Single-source; verified stays false until independently confirmed."


def stated(evidence, corroborated, ends_at=None, venue=None, address=None, description=None, image_url=None):
    """The producer's own account of a record, the same for every producer: what it read, whether a second statement corroborates it, and which facts the source left out."""
    facts = (ends_at, venue, address, description, image_url)
    return Verification("corroborated" if corroborated else "unverified", tuple(evidence), tuple(name for name, value in zip(GAP_FIELDS, facts) if not value), SINGLE_SOURCE_NOTE)


@dataclass(frozen=True)
class EventCandidate:
    local_id: str                   # unique within the source; the service keys the stored candidate "<source_key>:<local_id>"
    title: str
    starts_at: str                  # ISO 8601 WITH a UTC offset: a time without one is not placed in time by guessing
    ends_at: Optional[str] = None
    timezone: Optional[str] = None  # IANA name, as the source gives it
    venue: Optional[str] = None
    address: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None       # the page about this event
    image_url: Optional[str] = None
    image_source: Optional[str] = None
    categories: tuple = ()
    organizer: Optional[str] = None
    price: Optional[str] = None
    geo: Optional[Mapping] = None   # coordinates the source states: {"latitude", "longitude"} and optionally "source"
    provenance: Provenance = field(default_factory=Provenance)
    verification: Verification = field(default_factory=Verification)
    state: Mapping = field(default_factory=lambda: types.MappingProxyType({}))   # the producer's own working state; opaque to processing, outside the record's identity
    schema: str = SCHEMA

    def __post_init__(self):
        object.__setattr__(self, "categories", tuple(self.categories or ()))
        object.__setattr__(self, "geo", _freeze(dict(self.geo)) if isinstance(self.geo, Mapping) else self.geo)
        object.__setattr__(self, "state", _freeze(dict(self.state)) if isinstance(self.state, Mapping) else self.state)


@dataclass(frozen=True)
class SourceLead:
    """A page considered while looking for events. It says nothing about any event's facts."""
    url: str
    kind: str                       # 'listing', 'organizer', 'event', 'social', 'other', 'seed': what the producer took the page to be
    decision: str                   # 'accepted' | 'rejected' | 'pending'
    reasons: tuple = ()
    score: int = 0
    found_on: Optional[str] = None  # the page whose link led here (None for a seed)
    link_text: Optional[str] = None
    events: int = 0                 # how many candidates this page supports
    fetched: bool = False
    schema: str = SCHEMA

    def __post_init__(self):
        object.__setattr__(self, "reasons", tuple(self.reasons or ()))

    @property
    def host(self):
        return (urlsplit(self.url).hostname or "").lower().removeprefix("www.")


@dataclass(frozen=True)
class ProducerResult:
    """What a producer's `crawl()` returns: records and scalar notes about the run."""
    candidates: tuple = ()
    leads: tuple = ()
    notes: Mapping = field(default_factory=lambda: types.MappingProxyType({}))
    schema: str = SCHEMA

    def __post_init__(self):
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "leads", tuple(self.leads))
        object.__setattr__(self, "notes", _freeze(dict(self.notes or {})))


@dataclass(frozen=True)
class Rejection:
    kind: str                       # 'candidate' | 'lead' | 'envelope'
    ref: str
    reason: str


@dataclass(frozen=True)
class Accepted:
    """What the boundary lets through: valid records only, the rejections with their reasons, and the run's notes (with `records_rejected`)."""
    candidates: tuple
    leads: tuple
    rejections: tuple
    notes: Mapping


# --- the stored form of a candidate ----------------------------------------------------------------------------------

def document(candidate):
    """The stored candidate: a versioned JSON document of the standard fields (never the producer's state)."""
    return {"schema": candidate.schema, "local_id": candidate.local_id, "title": candidate.title, "starts_at": candidate.starts_at, "ends_at": candidate.ends_at,
            "timezone": candidate.timezone, "venue": candidate.venue, "address": candidate.address, "description": candidate.description, "url": candidate.url,
            "image_url": candidate.image_url, "image_source": candidate.image_source, "categories": list(candidate.categories), "organizer": candidate.organizer,
            "price": candidate.price, "geo": thaw(candidate.geo) if candidate.geo is not None else None,
            "provenance": {"source_name": candidate.provenance.source_name, "listing_url": candidate.provenance.listing_url,
                           "retrieved_at": candidate.provenance.retrieved_at, "found_on": list(candidate.provenance.found_on)},
            "verification": {"status": candidate.verification.status, "evidence": list(candidate.verification.evidence), "missing": list(candidate.verification.missing),
                             "note": candidate.verification.note, "verified": candidate.verification.verified}}


def candidate_from_document(doc, state=None):
    """The candidate a stored document describes (with the producer's state, when given). Raises ValueError for a document that is not a version-1 candidate."""
    if not isinstance(doc, Mapping) or doc.get("schema") != SCHEMA:
        raise ValueError("not a stored version-1 candidate")
    provenance, verification = doc.get("provenance") or {}, doc.get("verification") or {}
    try:
        return EventCandidate(local_id=doc["local_id"], title=doc["title"], starts_at=doc["starts_at"], ends_at=doc.get("ends_at"), timezone=doc.get("timezone"), venue=doc.get("venue"),
                              address=doc.get("address"), description=doc.get("description"), url=doc.get("url"), image_url=doc.get("image_url"), image_source=doc.get("image_source"),
                              categories=tuple(doc.get("categories") or ()), organizer=doc.get("organizer"), price=doc.get("price"), geo=doc.get("geo"),
                              provenance=Provenance(provenance.get("source_name"), provenance.get("listing_url"), provenance.get("retrieved_at"), tuple(provenance.get("found_on") or ())),
                              verification=Verification(verification.get("status", "unverified"), tuple(verification.get("evidence") or ()), tuple(verification.get("missing") or ()),
                                                        verification.get("note", ""), bool(verification.get("verified", False))),
                              state=state or {})
    except KeyError as error:
        raise ValueError(f"the stored candidate has no {error.args[0]}") from error


def canonical(doc):
    """The identity of a stored document: canonical JSON without the field that changes on every read without the source changing."""
    stable = {k: v for k, v in doc.items() if k != "provenance"}
    stable["provenance"] = {k: v for k, v in (doc.get("provenance") or {}).items() if k != "retrieved_at"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def candidate_digest(candidate):
    """sha256 of a candidate's comparable facts: not its retrieval time, not the producer's state."""
    return canonical(document(candidate))


# --- the boundary ----------------------------------------------------------------------------------------------------

def _check_geo(geo):
    try:
        return -90 <= float(geo["latitude"]) <= 90 and -180 <= float(geo["longitude"]) <= 180
    except (KeyError, TypeError, ValueError):
        return False


def _check_candidate(record):
    if not isinstance(record, EventCandidate):
        return f"not an EventCandidate ({type(record).__name__})"
    if record.schema != SCHEMA:
        return f"unsupported record schema {record.schema!r}"
    if not isinstance(record.local_id, str) or not record.local_id.strip():
        return "no id"
    if not isinstance(record.title, str) or not record.title.strip():
        return "no title"
    if not isinstance(record.starts_at, str) or not _offset_time(record.starts_at):
        return "the start time is missing or has no UTC offset"
    for name in TEXT_FIELDS:
        value = getattr(record, name)
        if value is not None and not isinstance(value, str):
            return f"{name} is not text"
    if not all(isinstance(c, str) for c in record.categories):
        return "categories are not text"
    if record.geo is not None and not (isinstance(record.geo, Mapping) and _check_geo(record.geo)):
        return "coordinates are not a latitude and longitude"
    if not isinstance(record.provenance, Provenance) or not isinstance(record.verification, Verification):
        return "provenance or verification is not a standard record"
    if not isinstance(record.state, Mapping):
        return "state is not a mapping"
    try:
        if len(json.dumps(thaw(record.state), ensure_ascii=False)) > MAX_STATE_BYTES:
            return "state is over the size limit"
    except (TypeError, ValueError):
        return "state is not JSON"
    return None


def _check_lead(record):
    if not isinstance(record, SourceLead):
        return f"not a SourceLead ({type(record).__name__})"
    if record.schema != SCHEMA:
        return f"unsupported record schema {record.schema!r}"
    if not _web_url(record.url):
        return "no web address"
    if record.decision not in LEAD_DECISIONS:
        return f"unknown decision {record.decision!r}"
    if not isinstance(record.kind, str) or not record.kind:
        return "no kind"
    return None


def accept(output):
    """The ONE validation boundary between a producer and the Discovery Service: returns only valid, de-duplicated standard records.
    Raises TypeError when the producer's answer is not a `ProducerResult` (anything else, including dictionaries, is a defect of the producer)."""
    if not isinstance(output, ProducerResult):
        raise TypeError(f"a producer must return a ProducerResult, not {type(output).__name__}")
    if output.schema != SCHEMA:
        return Accepted((), (), (Rejection("envelope", "", f"unsupported producer schema {output.schema!r}"),), {**thaw(output.notes), "records_rejected": 1})
    rejections, candidates, leads, seen = [], [], [], set()
    for record in output.candidates:
        reason = _check_candidate(record)
        if reason is None and record.local_id in seen:
            reason = "duplicate id within this run"
        if reason is not None:
            rejections.append(Rejection("candidate", getattr(record, "local_id", None) or type(record).__name__, reason + (" (a lead is not an event)" if isinstance(record, SourceLead) else "")))
            continue
        seen.add(record.local_id)
        candidates.append(record)
    seen_urls = set()
    for record in output.leads:
        reason = _check_lead(record)
        if reason is None and record.url in seen_urls:
            reason = "duplicate lead address within this run"
        if reason is not None:
            rejections.append(Rejection("lead", getattr(record, "url", None) or type(record).__name__, reason))
            continue
        seen_urls.add(record.url)
        leads.append(record)
    notes = thaw(output.notes)
    notes["records_rejected"] = len(rejections)
    return Accepted(tuple(candidates), tuple(leads), tuple(rejections), notes)
