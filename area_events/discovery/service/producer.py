"""The standard producer-output contract: Source -> Adapter -> Standard Records.

Every source producer (an adapter) answers `crawl()` with ONE versioned, immutable envelope, `ProducerResult`, holding two kinds of record:

* `EventCandidate`: one event as a source states it. Required: an id that is unique within the source, a title, and a start time WITH a UTC offset. Everything else is
  optional and stays `None` or empty when the source did not say it; nothing is guessed. A candidate is a claim by a source, not a validated event: extraction,
  deduplication and verification happen downstream.
* `SourceLead`: a page a producer considered while looking for events, with where it was found and why it was accepted or rejected. It carries NO event facts and the
  boundary refuses to treat one as an event.

`accept()` is the one generic boundary between producers and the Discovery Service. It checks the envelope and every record, sets aside what does not conform (each with a
reason, counted in the run's notes) and passes on only valid records. Nothing downstream of it knows which producer made a record, or reads a source-specific structure.
`EventCandidate.to_payload()` is the one compatibility boundary to the stored candidate: the same JSON document the service has always stored, so stored data, payload hashes
and extraction results do not change. A producer's own working state (what it wants back as `previous` on the next run) travels in `private`, which only that producer reads.
"""
import types
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional
from urllib.parse import urlsplit

from .contracts import AdapterResult

SCHEMA = "discovery.producer.v1"
LEAD_DECISIONS = frozenset(("accepted", "rejected", "pending"))
MODELLED = frozenset(("id", "title", "start", "end", "timezone", "venue", "address", "description", "image_url", "image_source", "url", "categories", "price", "source",
                      "retrieved_at", "verified", "verification", "extraction", "organizer", "geo", "found_on"))


def _freeze(value):
    if isinstance(value, dict):
        return types.MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
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


@dataclass(frozen=True)
class Verification:
    """What the producer itself established about the record. `verified` is only ever the producer's claim; it is recomputed nowhere downstream from this."""
    status: str = "unverified"
    evidence: tuple = ()            # human-readable lines: what was read where
    missing: tuple = ()
    note: str = ""
    verified: bool = False


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
    geo: Optional[Mapping] = None   # coordinates the source states (latitude, longitude)
    provenance: Provenance = field(default_factory=Provenance)
    verification: Verification = field(default_factory=Verification)
    private: Mapping = field(default_factory=lambda: types.MappingProxyType({}))   # the producer's own state; opaque to everything downstream
    schema: str = SCHEMA

    def __post_init__(self):
        object.__setattr__(self, "categories", tuple(self.categories or ()))
        object.__setattr__(self, "geo", _freeze(self.geo) if self.geo is not None else None)
        object.__setattr__(self, "private", _freeze(dict(self.private or {})))

    @classmethod
    def from_event_dict(cls, event, source_key):
        """The record for an event in the dictionary form the producers' reading code has always used (`core.make_event`). Raises ValueError for one that cannot be."""
        prefix = f"{source_key}:"
        if not isinstance(event.get("id"), str) or not event["id"].startswith(prefix) or len(event["id"]) == len(prefix):
            raise ValueError(f"the event id {event.get('id')!r} does not belong to source {source_key!r}")
        source, verification = event.get("source") or {}, event.get("verification") or {}
        private = {}
        if event.get("extraction") is not None:
            private["extraction"] = event["extraction"]
        extras = {k: v for k, v in event.items() if k not in MODELLED}
        if extras:
            private["extras"] = extras
        return cls(local_id=event["id"][len(prefix):], title=event.get("title"), starts_at=event.get("start"), ends_at=event.get("end"), timezone=event.get("timezone"),
                   venue=event.get("venue"), address=event.get("address"), description=event.get("description"), url=event.get("url"), image_url=event.get("image_url"),
                   image_source=event.get("image_source"), categories=tuple(event.get("categories") or ()), organizer=event.get("organizer"), price=event.get("price"),
                   geo=event.get("geo"),
                   provenance=Provenance(source.get("name"), source.get("listing_url"), event.get("retrieved_at"), tuple(event.get("found_on") or ())),
                   verification=Verification(verification.get("status", "unverified"), tuple(verification.get("evidence") or ()), tuple(verification.get("missing") or ()),
                                             verification.get("note", ""), bool(event.get("verified", False))),
                   private=private)

    def to_payload(self, source_key):
        """The stored candidate document (the compatibility boundary): exactly the dictionary the service has always stored for a candidate."""
        payload = {"id": f"{source_key}:{self.local_id}", "title": self.title, "start": self.starts_at, "end": self.ends_at, "timezone": self.timezone, "venue": self.venue,
                   "address": self.address, "description": self.description, "image_url": self.image_url, "image_source": self.image_source, "url": self.url,
                   "categories": list(self.categories), "price": self.price,
                   "source": {"name": self.provenance.source_name, "listing_url": self.provenance.listing_url}, "retrieved_at": self.provenance.retrieved_at,
                   "verified": self.verification.verified,
                   "verification": {"status": self.verification.status, "evidence": list(self.verification.evidence), "missing": list(self.verification.missing),
                                    "note": self.verification.note}}
        if "extraction" in self.private:
            payload["extraction"] = _thaw(self.private["extraction"])
        if self.organizer is not None:
            payload["organizer"] = self.organizer
        if self.geo is not None:
            payload["geo"] = _thaw(self.geo)
        if self.provenance.found_on:
            payload["found_on"] = list(self.provenance.found_on)
        payload.update(_thaw(self.private.get("extras", {})))
        return payload


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

    @classmethod
    def from_dict(cls, lead):
        return cls(url=lead["url"], kind=lead["kind"], decision=lead["decision"], reasons=tuple(lead.get("reasons") or ()), score=int(lead.get("score", 0)),
                   found_on=lead.get("found_on"), link_text=lead.get("link_text"), events=int(lead.get("events", 0)), fetched=bool(lead.get("fetched", False)))


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

    @classmethod
    def from_events(cls, events, source_key, notes=None, leads=()):
        """The producers' reading code returns event dictionaries (and lead dictionaries): this is the one place they become standard records. An event that cannot
        be one is not dropped silently: it is named in the notes (`rejected_records`) and counted by the boundary."""
        candidates, rejected = [], []
        for event in events:
            try:
                candidates.append(EventCandidate.from_event_dict(event, source_key))
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                rejected.append(f"{event.get('id') if isinstance(event, dict) else type(event).__name__}: {error}"[:200])
        converted = []
        for lead in leads:
            try:
                converted.append(lead if isinstance(lead, SourceLead) else SourceLead.from_dict(lead))
            except (KeyError, TypeError, ValueError) as error:
                rejected.append(f"lead {lead.get('url') if isinstance(lead, dict) else type(lead).__name__}: {error}"[:200])
        notes = dict(notes or {})
        if rejected:
            notes["rejected_records"] = rejected
        return cls(tuple(candidates), tuple(converted), notes)


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
    for name in ("ends_at", "timezone", "venue", "address", "description", "url", "image_url", "image_source", "organizer", "price"):
        value = getattr(record, name)
        if value is not None and not isinstance(value, str):
            return f"{name} is not text"
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


def accept(output, source_key):
    """The ONE generic boundary between a producer and the Discovery Service. Takes a `ProducerResult` (or, for producers not yet moved to the standard records,
    the earlier `AdapterResult` of event dictionaries, converted here and nowhere else) and returns only valid, de-duplicated records.
    Raises TypeError for something that is neither."""
    if isinstance(output, AdapterResult):
        output = ProducerResult.from_events(output.candidates, source_key, output.notes, output.leads)
    if not isinstance(output, ProducerResult):
        raise TypeError(f"a producer must return a ProducerResult, not {type(output).__name__}")
    rejections, candidates, leads, seen = [], [], [], set()
    if output.schema != SCHEMA:
        return Accepted((), (), (Rejection("envelope", "", f"unsupported producer schema {output.schema!r}"),), {**output.notes, "records_rejected": 1})
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
    notes = _thaw(output.notes)
    notes["records_rejected"] = len(rejections) + len(notes.get("rejected_records", ()))
    return Accepted(tuple(candidates), tuple(leads), tuple(rejections), notes)
