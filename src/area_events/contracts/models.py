"""Records and protocols of the Discovery Service, independent of how they travel (in process now, HTTP later).

Every record carries stable string keys (never local integer ids), so a consumer on another computer can rely on them:
`source_key` (stable producer-instance id), `url_key` (sha256 of the canonical URL), `candidate_key` (`<source_key>:<local id>`, the existing event id),
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
class ConfiguredSource:
    """A persisted producer instance as exposed by the control API."""
    source_key: str
    adapter: str
    name: str
    enabled: bool
    interval_minutes: int
    options: tuple
    revision: int

    def options_dict(self):
        return dict(self.options)


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
    """A producer: a source's own reading logic. `crawl` fetches only through the client it is given and returns a `producer.ProducerResult` (the standard,
    versioned records: EventCandidate and SourceLead); it never touches the database. `page_role(url, base_url)` names the role of a page it fetched."""

    adapter: str
    extractor: str

    def crawl(self, fetcher, previous: tuple, options: dict) -> "ProducerResult": ...  # noqa: F821  (`previous`: the source's stored EventCandidates, with the state it left)


# --- errors every module of the service shares --------------------------------------------------------------------------

class ControlError(Exception):
    code = "error"
    status = 400


class ValidationFailed(ControlError):
    code, status = "invalid_request", 400


class NotFound(ControlError):
    code, status = "not_found", 404


class SourceExists(ControlError):
    code, status = "source_exists", 409


class RevisionConflict(ControlError):
    code, status = "revision_conflict", 409

    def __init__(self, message, current_revision):
        super().__init__(message)
        self.current_revision = current_revision


class LeaseLost(ControlError):
    code, status = "lease_lost", 409


class StaleResult(ControlError):
    code, status = "stale_result", 409


class StorageUnavailable(Exception):
    """The database cannot take a write now (locked beyond the retries, full, or failing)."""


class StaleLease(Exception):
    """The worker's job lease is no longer valid: another worker may own the job, so nothing is written."""


def job_key_for(source_key):
    return f"crawl_source:{source_key}"


MAX_FILE_BYTES = 2_000_000
MEDIA_SCHEMA = "discovery.media.v1"
ENRICHMENT_VERSION = 2   # 2: maps carry a short place label; candidates enriched under an older version are enriched again (places and pictures stay cached)


class DecisionRejected(Exception):
    """A deduplication strategy answered with a decision the candidate's state does not allow; the operation was rolled back."""


class StoreRejected(Exception):
    """What was about to be stored was refused by the database's own constraints (a defect in the data); nothing was written."""


# --- configuration records ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceSpec:
    source_key: str
    adapter: str
    name: str
    base_url: str
    options: tuple = ()             # (key, value) pairs, so the spec stays immutable
    interval_s: int = 3600

    def options_dict(self):
        return dict(self.options)


@dataclass(frozen=True)
class RetentionPolicy:
    fetch_history_days: int = 30
    fetches_per_page: int = 20
    candidate_days_after_start: int = 3
    unreferenced_content_days: int = 1
    content_budget_bytes: int = 200_000_000
    change_min_age_s: int = 86400
    change_max_rows: int = 500_000
    page_days: int = 30                       # pages no source has seen for this long, and that nothing refers to


INTENSITIES = ("low", "normal", "high")
MODES = ("automatic", "manual")
UNITS = ("mi", "km")


@dataclass(frozen=True)
class AreaRecord:
    area_key: str
    name: str
    latitude: float
    longitude: float
    radius: float
    unit: str
    enabled: bool
    intensity: str
    revision: int
    created_at: int
    updated_at: int
    sources: tuple = ()             # source keys


@dataclass(frozen=True)
class SettingsRecord:
    mode: str
    intensity: str
    paused: bool
    revision: int
    updated_at: int


# --- the Processing contract's records (discovery.processing.v1) and the clean event processing produces ------------------

PROCESSING_SCHEMA = "discovery.processing.v1"


@dataclass(frozen=True)
class ProcessingConfig:
    max_attempts: int = 4
    backoff_base_s: int = 300
    backoff_cap_s: int = 21600
    max_lease_s: int = 3600
    max_batch: int = 20
    page_text_chars: int = 20000


@dataclass(frozen=True)
class WorkItem:
    lease_token: str
    lease_until: int
    candidate_key: str
    source_key: str
    payload_hash: str
    payload: dict
    evidence: tuple                 # dicts: field, kind, quote, url_key, content_hash, note
    page: Optional[dict]            # {"url_key", "url", "media_type", "text"}: the stored page's readable text, bounded; None when none is stored


@dataclass(frozen=True)
class WorkBatch:
    schema: str
    items: tuple


@dataclass(frozen=True)
class SubmitAck:
    schema: str
    candidate_key: str
    extraction_id: int
    status: str                     # 'complete' | 'partial' | 'failed'
    outcome: str                    # 'created' | 'updated' | 'unchanged' | 'duplicate' | 'recorded' (failed result: no event written)
    event_key: Optional[str]
    event_revision: Optional[int]
    replayed: bool = False


@dataclass(frozen=True)
class ProcessingStatus:
    schema: str
    extractor: str
    version: int
    candidates: int
    pending: int
    leased: int
    retrying: int
    failed: int
    done: int
    events: int
    extractions: dict = field(default_factory=dict)   # status -> count
    latest_seq: int = 0


@dataclass(frozen=True)
class CleanEvent:
    """An event as processing leaves it after extraction, normalization and validation: every fact is stated by the candidate or absent, and the derived words
    (how the location is known, what is missing, whether it is complete) are processing's own. This, and nothing source-specific, is what deduplication judges and
    persistence stores."""
    title: str
    starts_at: int
    ends_at: Optional[int]
    timezone: Optional[str]
    description: Optional[str]
    venue_name: Optional[str]
    location_text: Optional[str]
    address: Optional[str]
    source_url: Optional[str]
    location_status: str            # 'address' | 'venue' | 'description_only' | 'unknown'
    missing: tuple
    status: str                     # 'complete' | 'partial'


@dataclass(frozen=True)
class ValidatedResult:
    """A worker's result after validation: the clean event (None for a failed extraction), the status and failure reason, and the exact result for the record."""
    event: Optional[CleanEvent]
    status: str
    failure: Optional[str]
    result: dict
    digest: str


# --- the control plane's results ---------------------------------------------------------------------------------------

CONTROL_SCHEMA = "discovery.control.v1"


@dataclass(frozen=True)
class ControlResult:
    schema: str
    revision: int                   # the revision of the thing that changed (area or settings)
    change_seq: Optional[int]       # the durable change record; None when nothing changed
    replayed: bool = False
    area: Optional[AreaRecord] = None
    settings: Optional[SettingsRecord] = None


@dataclass(frozen=True)
class SourceControlResult:
    schema: str
    revision: int
    change_seq: Optional[int]
    replayed: bool
    source: ConfiguredSource


@dataclass(frozen=True)
class SourceStatus:
    source_key: str
    enabled: bool
    eligible: bool                  # an enabled area uses it
    interval_s: int
    last_success_at: Optional[int]
    last_attempt_at: Optional[int]
    job_state: Optional[str]
    next_run_at: Optional[int]
    attempts: int
    last_error: Optional[str]


@dataclass(frozen=True)
class ControlStatus:
    schema: str
    settings: SettingsRecord
    areas: int
    areas_enabled: int
    sources: tuple
    jobs: JobCounts
    latest_change_seq: int
    database_bytes: int
    wal_bytes: int
    runtime: dict = field(default_factory=dict)


def digest_of(record):
    """The identity of a configuration record in the change log: its fields without the times."""
    import hashlib
    import json
    data = {k: v for k, v in record.__dict__.items() if k not in ("created_at", "updated_at")}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
