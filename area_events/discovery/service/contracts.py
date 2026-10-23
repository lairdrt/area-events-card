"""Records and protocols of the Discovery Service, independent of how they travel (in process now, HTTP later).

Every record carries stable string keys (never local integer ids), so a consumer on another computer can rely on them:
`source_key` (registry id), `url_key` (sha256 of the canonical URL), `candidate_key` (`<source_key>:<local id>`, the existing event id),
`content_hash` (sha256 of the stored bytes) and `payload_hash` (sha256 of the candidate's comparable fields).
"""
from dataclasses import dataclass, field
from typing import Optional, Protocol

SCHEMA = "discovery.v1"


@dataclass(frozen=True)
class SourceRecord:
    source_key: str
    adapter: str
    name: str
    base_url: str
    enabled: bool
    interval_s: int
    last_success_at: Optional[int]
    last_attempt_at: Optional[int]


@dataclass(frozen=True)
class PageRecord:
    url_key: str
    url: str
    status: str                     # 'ok', 'not_fetched', or the failure ('http_error', 'blocked', ...)
    content_hash: Optional[str]
    media_type: Optional[str]
    bytes: Optional[int]
    first_seen_at: int
    fetched_at: Optional[int]
    changed_at: Optional[int]
    sources: tuple = ()             # (source_key, role) pairs: every source that discovered this page
    body: Optional[bytes] = None    # only when asked for


@dataclass(frozen=True)
class EvidenceRecord:
    field: str
    kind: str                       # 'extracted' | 'inferred' | 'recorded'
    quote: Optional[str]
    url_key: Optional[str]
    content_hash: Optional[str]
    note: Optional[str]


@dataclass(frozen=True)
class CandidateRecord:
    candidate_key: str
    source_key: str
    url_key: Optional[str]
    method: str
    extractor: str
    title: Optional[str]
    starts_at: Optional[int]
    payload: dict
    payload_hash: str
    first_seen_at: int
    last_seen_at: int
    evidence: tuple = ()


@dataclass(frozen=True)
class Change:
    seq: int
    entity: str                     # 'candidate' | 'page' | 'area' | 'settings' | 'discovery'
    entity_key: str
    op: str                         # 'upsert' | 'delete'
    payload_hash: str
    created_at: int
    kind: str = ""                  # 'candidate.changed', 'page.changed', 'area.configuration_changed', 'discovery.settings_changed', 'discovery.paused', 'discovery.resumed'


@dataclass(frozen=True)
class ChangeBatch:
    """`changes` after the consumer's checkpoint, in order. When the checkpoint is older than the retained history,
    `resync_required` is true and `changes` is empty: the consumer must take a snapshot (see DiscoveryReader.snapshot)."""
    schema: str
    changes: tuple
    next_seq: int
    latest_seq: int
    resync_required: bool = False
    oldest_retained_seq: int = 0


@dataclass(frozen=True)
class SnapshotPage:
    """One page of current candidates, in candidate_key order. `as_of_seq` is the newest change at the start of the snapshot:
    after the last page the consumer continues with changes(after=as_of_seq); records changed meanwhile appear again
    there, which an idempotent consumer (keyed by candidate_key and payload_hash) absorbs."""
    schema: str
    candidates: tuple
    as_of_seq: int
    next_after_key: Optional[str]


@dataclass(frozen=True)
class CandidateQuery:
    source_key: Optional[str] = None
    starts_from: Optional[int] = None
    starts_to: Optional[int] = None
    title_contains: Optional[str] = None
    limit: int = 50


@dataclass(frozen=True)
class JobCounts:
    ready: int = 0
    leased: int = 0
    done: int = 0
    failed: int = 0
    retrying: int = 0


@dataclass(frozen=True)
class DiscoveryStatus:
    schema: str
    schema_version: int
    state: str                      # 'idle' | 'paused: storage' | ...
    sources: tuple
    pages: int
    pages_fetched: int
    candidates: int
    evidence: int
    contents: int
    content_bytes: int
    changes_latest_seq: int
    jobs: JobCounts
    last_success_at: Optional[int]
    database_bytes: int
    wal_bytes: int
    write_stats: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RunReport:
    """What one job run did. Counts are of this run only."""
    job_key: Optional[str]
    outcome: str                    # 'completed' | 'nothing_due' | 'fetch_failed' | 'stale_lease' | 'storage_unavailable' | 'paused' | 'store_failed'
    requests: int = 0
    pages_seen: int = 0
    pages_new: int = 0
    content_new: int = 0
    content_unchanged: int = 0
    candidates_seen: int = 0
    candidates_new: int = 0
    candidates_changed: int = 0
    candidates_unchanged: int = 0
    evidence_written: int = 0
    changes_appended: int = 0
    details_reused: int = 0
    error: Optional[str] = None
    notes: dict = field(default_factory=dict)


class DiscoveryReader(Protocol):
    """What other components may ask of Discovery. A local implementation reads the database; a remote one (later) calls the
    Discovery API. Both return the same records, so consumers do not change."""

    def changes(self, after_seq: int, limit: int = 200) -> ChangeBatch: ...

    def snapshot(self, after_key: Optional[str] = None, limit: int = 200, as_of_seq: Optional[int] = None) -> SnapshotPage: ...

    def candidate(self, candidate_key: str) -> Optional[CandidateRecord]: ...

    def candidates(self, query: CandidateQuery) -> tuple: ...

    def page(self, url_key: str, include_body: bool = False) -> Optional[PageRecord]: ...

    def acknowledge(self, consumer: str, seq: int) -> None: ...

    def status(self) -> DiscoveryStatus: ...


class SourceAdapter(Protocol):
    """A source's own reading logic. `crawl` fetches only through the client it is given and returns the candidates it found;
    it never touches the database."""

    adapter: str
    extractor: str

    def crawl(self, fetcher, previous: list, options: dict) -> "AdapterResult": ...


@dataclass
class AdapterResult:
    candidates: list
    notes: dict
