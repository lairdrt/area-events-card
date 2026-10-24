"""The ports of Persistence: the only way the rest of the service reaches the one SQLite database.

A port is a narrow, explicit interface in the language of the caller (a job, a run, a clean event, an area), never in the language of SQL. No connection, cursor or transaction
crosses a port in either direction: every operation that must be atomic is ONE call, and Persistence opens, runs and commits (or rolls back) the transaction behind it.
Persistence never imports the modules that use it; they never import its implementation, only these ports (and the plain records in `contracts.py`).

    CrawlStorage       claim a crawl job, read the source's previous records, record a whole run atomically, release a failed job, retention
    SchedulingStorage  keep jobs in line with the configuration (areas, settings), report what is due
    ControlStorage     areas and settings: read, create, update (revision-checked), pause; each change with its durable change record
    ProcessingStorage  lease candidates to a processing worker, and accept its validated result: lookup, deduplication decision and commit in ONE operation
    EventQueries       read normalized events
    DiscoveryReader    the read contract over the discovery data (changes, snapshot, candidates, pages, status): defined in contracts.py
    MediaWork          what media enrichment takes from, and returns to, the database
"""
from dataclasses import dataclass
from typing import Optional, Protocol


# --- records that cross the crawl port ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class ClaimedJob:
    job_id: int
    job_key: str
    source_id: int
    attempts: int
    max_attempts: int
    lease_token: str
    lease_until: int


@dataclass(frozen=True)
class SourceState:
    """What a producer needs to start a run: the source's own settings and the records (with their state) it left."""
    source_key: str
    adapter: str
    name: str
    base_url: str
    options: dict
    previous: tuple


@dataclass(frozen=True)
class PagePlan:
    """One page a run fetched, prepared outside any transaction (hashed and compressed) so that the transaction stays short."""
    url: str
    outcome: str
    role: str
    final_url: Optional[str] = None
    http_status: Optional[int] = None
    media_type: Optional[str] = None
    error: Optional[str] = None
    digest: Optional[str] = None
    packed: Optional[bytes] = None
    size: Optional[int] = None
    fingerprint: Optional[str] = None


@dataclass(frozen=True)
class CandidatePlan:
    record: object                   # producer.EventCandidate
    key: str                         # "<source_key>:<local_id>"
    evidence: tuple                  # (field, kind, quote, url, content_hash, note) rows
    extractor: str = ""
    digest: str = ""


@dataclass(frozen=True)
class RunPlan:
    pages: tuple = ()
    candidates: tuple = ()
    leads: tuple = ()                # producer.SourceLead


@dataclass(frozen=True)
class FailedFetch:
    url: str
    outcome: str
    error: Optional[str]


@dataclass(frozen=True)
class PacingFacts:
    """What a pacing policy may know to decide when a source is next due. Gathered by Persistence; the policy decides without touching the database."""
    interval_s: int
    now: int
    changed: bool
    quiet_runs: int
    mode: str
    intensity: str                   # the global intensity (manual mode)
    strongest: Optional[str]         # the strongest intensity among the enabled areas that use the source, or None


@dataclass(frozen=True)
class PacingDecision:
    next_run_at: int
    quiet_runs: Optional[int] = None  # the new count of runs without a change, or None to leave it


class PacingPolicy(Protocol):
    def next_run(self, facts: PacingFacts) -> PacingDecision: ...


class SchedulingPolicy(Protocol):
    def priority(self, intensity: str) -> int: ...

    def interval(self, base_interval_s: int, intensity: str, mode: str, quiet_runs: int) -> int: ...


# --- ports --------------------------------------------------------------------------------------------------------------

class CrawlStorage(Protocol):
    """Time is Persistence's own clock (injected once at the composition root), so a call never takes `now` from its caller."""

    def register_source(self, spec, max_attempts: int): ...

    def make_due(self, source_key: str) -> int: ...

    def recover_expired_jobs(self) -> int: ...

    def prune(self, policy) -> dict: ...

    def claim_job(self, worker: str, token: str, lease_s: int, require_area: bool) -> Optional[ClaimedJob]: ...

    def load_source(self, job: ClaimedJob) -> SourceState: ...

    def prepare_content(self, body: bytes) -> tuple: ...

    def record_run(self, job: ClaimedJob, plan: RunPlan, pacing: PacingPolicy) -> dict: ...

    def release(self, job: ClaimedJob, failed: tuple, error: str, backoff_base_s: int, backoff_cap_s: int) -> Optional[str]: ...


class SchedulingStorage(Protocol):
    def is_paused(self) -> bool: ...

    def synchronize(self, policy: SchedulingPolicy) -> dict: ...

    def next_due(self) -> Optional[int]: ...


class ControlStorage(Protocol):
    def list_areas(self) -> tuple: ...

    def get_area(self, area_key: str): ...

    def status(self, runtime: dict): ...

    def create_area(self, fields: dict, enabled: bool, request_id: Optional[str], source_keys: tuple, new_key): ...

    def update_area(self, area_key: str, expected_revision: int, wanted: dict, source_keys: Optional[tuple], settle): ...

    def set_settings(self, expected_revision: int, mode: Optional[str], intensity: Optional[str]): ...

    def set_paused(self, paused: bool): ...


class ProcessingStorage(Protocol):
    def claim_work(self, worker: str, extractor: str, version: int, limit: int, lease_s: int, new_token, config) -> tuple: ...

    def submit_result(self, candidate_key: str, extractor: str, version: int, lease_token: str, validated, worker: Optional[str]): ...

    def fail_work(self, candidate_key: str, extractor: str, version: int, lease_token: str, error: str, config) -> str: ...

    def status(self, extractor: str, version: int): ...


class EventQueries(Protocol):
    def query(self, area_key: Optional[str], source_key: Optional[str], starts_from: Optional[int], starts_to: Optional[int], after: Optional[tuple], limit: int): ...

    def get(self, event_key: str): ...


class MediaWork(Protocol):
    def pending(self, limit: int) -> list: ...

    def titles_sharing_image(self, url: str) -> int: ...

    def finish(self, results: list) -> None: ...

    def status(self) -> dict: ...


class MediaCache(Protocol):
    """File and record cache used by the imagery resolver and source-photo enrichment."""

    def get_record(self, key: str): ...

    def get_records(self, keys) -> dict: ...

    def put_record(self, key: str, record: dict) -> None: ...

    def mark_saved(self, at: int) -> None: ...

    def revision(self) -> int: ...

    def put_file(self, name: str, data: bytes) -> None: ...

    def read_file(self, name: str): ...

    def has_file(self, name: str) -> bool: ...

    def intact(self, record: dict) -> bool: ...

    def prune(self) -> None: ...

    def save(self) -> None: ...
