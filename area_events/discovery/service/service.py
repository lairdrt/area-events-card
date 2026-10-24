"""DiscoveryService: registers sources, schedules and runs their discovery jobs, and hands what they find to Persistence.

One job run:

    1. claim         Persistence leases the next due job to this worker with a fresh token
    2. read          the source's settings and its previous records (so event pages already read are reused)
    3. crawl         the producer fetches through a RecordingFetcher over the shared-policy SafeFetcher and answers with standard records
    4. prepare       hash and compress bodies, compute the records' digests, locate evidence in the stored pages (no transaction)
    5. record        Persistence verifies the lease and writes the whole run atomically; the job completes
    6. on failure    Persistence releases the job with backoff (guarded by the same token)

The service holds no SQL and no connection: every database operation is a call on the CrawlStorage port, and each atomic operation is one call. It never calls classification,
geocoding, validation, imagery or Home Assistant; consumers read the change feed later. If the database cannot be written, the service pauses for a bounded, growing time and
reports it; the job's lease then expires and the work is done again, so nothing is acknowledged that was not stored.
"""
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from .. import evidence as evidence_text
from ..safe import FetchError
from . import producer
from .contracts import RetentionPolicy, RunReport, SourceSpec, StaleLease, StorageUnavailable, StoreRejected, job_key_for  # noqa: F401
from .fetching import RecordingFetcher
from .persistence.ports import CandidatePlan, CrawlStorage, FailedFetch, PacingDecision, PagePlan, RunPlan


@dataclass(frozen=True)
class DiscoveryConfig:
    lease_s: int = 900
    max_attempts: int = 4
    backoff_base_s: int = 900
    backoff_cap_s: int = 43200
    max_requests_per_job: int = 40
    max_job_seconds: int = 300
    require_area: bool = False   # True on the host: only sources that an enabled area uses are crawled
    storage_pause_s: int = 60
    storage_pause_cap_s: int = 3600
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)


class FixedPacing:
    """A source is asked for again after its own interval."""

    def next_run(self, facts):
        return PacingDecision(facts.now + int(facts.interval_s))


class DiscoveryService:
    def __init__(self, storage: CrawlStorage, fetch_client, adapters, clock, config=DiscoveryConfig(), worker_id="worker-1", new_token=lambda: uuid.uuid4().hex,
                 pacing=None, gate=None):
        self.storage, self.fetch_client, self.adapters, self.clock, self.config = storage, fetch_client, dict(adapters), clock, config
        self.worker_id, self.new_token = worker_id, new_token
        self.pacing, self.gate = pacing or FixedPacing(), gate   # gate.paused(): operator pause
        self.paused_until, self.pause_seconds, self.last_storage_error = 0, 0, None

    # --- sources and jobs --------------------------------------------------------------------------------------------

    def register_source(self, spec: SourceSpec):
        if spec.adapter not in self.adapters:
            raise ValueError(f"no adapter '{spec.adapter}' is installed")
        return self.storage.register_source(spec, self.config.max_attempts)

    def make_due(self, source_key):
        """Run a source's job at the next opportunity instead of at its scheduled time (an operator's 'run now')."""
        return self.storage.make_due(source_key)

    def recover(self):
        """Release leases that have expired (a worker that stopped mid-job). Returns how many."""
        return self.storage.recover_expired_jobs()

    def prune(self):
        return self.storage.prune(self.config.retention)

    # --- one job --------------------------------------------------------------------------------------------------

    def run_once(self, worker_id=None):
        now = self._now()
        worker = worker_id or self.worker_id
        if self.gate is not None and self.gate.paused():
            return RunReport(None, "paused", error="discovery is paused")
        if now < self.paused_until:
            return RunReport(None, "paused", error=f"storage unavailable; retrying after {self.paused_until - now} s: {self.last_storage_error}")
        token = self.new_token()
        try:
            job = self.storage.claim_job(worker, token, self.config.lease_s, self.config.require_area)
        except StorageUnavailable as error:
            return self._pause(error, None)
        if not job:
            return RunReport(None, "nothing_due")
        state = self.storage.load_source(job)
        adapter = self.adapters.get(state.adapter)
        fetcher = RecordingFetcher(self.fetch_client, self.config.max_requests_per_job, self.config.max_job_seconds)
        options = {**state.options, "source_key": state.source_key, "name": state.name, "url": state.base_url}
        try:
            if adapter is None:
                raise FetchError(f"no adapter '{state.adapter}' is installed")
            answer = adapter.crawl(fetcher, state.previous, options)
        except FetchError as error:
            return self._release(job, fetcher, error)
        try:
            result = producer.accept(answer)   # the one boundary: only valid standard records pass
        except TypeError as error:   # a producer that answers with something that is not a ProducerResult is a defect: the job is released with backoff and the reason recorded
            return self._release(job, fetcher, FetchError(f"the producer's answer is not a ProducerResult: {error}"))
        plan = self._prepare(state, adapter, fetcher, result)
        try:
            counts = self.storage.record_run(job, plan, self.pacing)
        except StaleLease:
            return RunReport(job.job_key, "stale_lease", requests=fetcher.requests, error="the lease expired or was taken over; nothing was written")
        except StorageUnavailable as error:
            return self._pause(error, job.job_key, fetcher.requests)
        except StoreRejected as error:   # a defect in what was about to be stored: nothing was written; release the job with backoff and say why
            return self._release(job, fetcher, FetchError(f"storing the results failed: {error}"), "store_failed")
        self.pause_seconds = 0
        return RunReport(job.job_key, "completed", requests=fetcher.requests, details_reused=int(result.notes.get("detail_reused", 0)),
                         notes={k: v for k, v in result.notes.items() if not isinstance(v, (list, dict))}, **counts)

    def run_until_idle(self, max_jobs=10):
        reports = []
        for _ in range(max_jobs):
            report = self.run_once()
            reports.append(report)
            if report.outcome in ("nothing_due", "paused", "storage_unavailable"):
                break
        return reports

    # --- internals --------------------------------------------------------------------------------------------------

    def _now(self):
        return int(self.clock())

    def _pause(self, error, job_key, requests=0):
        self.pause_seconds = min(max(self.pause_seconds * 2, self.config.storage_pause_s), self.config.storage_pause_cap_s)
        self.paused_until = self._now() + self.pause_seconds
        self.last_storage_error = str(error)[:200]
        return RunReport(job_key, "storage_unavailable", requests=requests, error=f"{error} (pausing {self.pause_seconds} s; the job will be repeated)")

    def _release(self, job, fetcher, error, outcome="fetch_failed"):
        failed = tuple(FailedFetch(f.url, f.outcome, f.error) for f in fetcher.fetches if f.outcome != "ok")   # its content was not stored, and an 'ok' fetch must name stored content
        try:
            state = self.storage.release(job, failed, str(error), self.config.backoff_base_s, self.config.backoff_cap_s)
        except StorageUnavailable as storage_error:
            return self._pause(storage_error, job.job_key, fetcher.requests)
        return RunReport(job.job_key, outcome, requests=fetcher.requests, error=f"{error} (job {state or 'not released: lease lost'})")

    def _prepare(self, state, adapter, fetcher, result):
        """Everything slow, before Persistence's atomic write: bodies hashed and compressed, digests, evidence located."""
        pages, texts, feeds = [], {}, set()
        for fetch in fetcher.fetches:
            role = adapter.page_role(fetch.url, state.base_url)
            if fetch.outcome == "ok" and fetch.body is not None:
                digest, packed, size = self.storage.prepare_content(fetch.body)
                fingerprint = digest
                if (fetch.media_type or "").startswith("text/html") or (fetch.media_type or "") == "":
                    text = evidence_text.visible_text(fetch.body.decode("utf-8", "replace"))
                    # pages carry random ids and tokens; their readable text decides whether they changed
                    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    texts[fetch.url] = (digest, text)
                elif (fetch.media_type or "").startswith("application/json"):
                    # a JSON feed (an API page): its string values are the readable text, searched for every event the feed lists
                    text = evidence_text.squeeze(" . ".join(self._json_strings(fetch.body)))
                    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    texts[fetch.url] = (digest, text)
                    feeds.add(fetch.url)
                pages.append(PagePlan(fetch.url, fetch.outcome, role, fetch.final_url, fetch.http_status, fetch.media_type, fetch.error, digest, packed, size, fingerprint))
            else:
                pages.append(PagePlan(fetch.url, fetch.outcome, role, fetch.final_url, fetch.http_status, fetch.media_type, fetch.error))
        extractor = getattr(adapter, "extractor", state.adapter)
        candidates = [CandidatePlan(record, f"{state.source_key}:{record.local_id}", tuple(self._evidence(record, state.base_url, texts, feeds)), extractor,
                                    producer.candidate_digest(record)) for record in result.candidates]
        return RunPlan(tuple(pages), tuple(candidates), tuple(result.leads))

    @staticmethod
    def _json_strings(body, limit=2_000_000):
        """The string values of a JSON document, in order (bounded); an unreadable document has none."""
        try:
            stack, found, size = [json.loads(body)], [], 0
        except ValueError:
            return []
        while stack and size < limit:
            value = stack.pop()
            if isinstance(value, str):
                found.append(value)
                size += len(value)
            elif isinstance(value, dict):
                stack.extend(reversed(list(value.values())))
            elif isinstance(value, list):
                stack.extend(reversed(value))
        return found

    @staticmethod
    def _evidence(record, base_url, texts, feeds=()):
        """Evidence rows (field, kind, quote, url, content_hash, note): a quote only where the value is found verbatim in a page read."""
        found_on = set(record.provenance.found_on)   # listings a producer read this event on
        listing = [(url, d, t) for url, (d, t) in texts.items() if url.split("?")[0].rstrip("/") == base_url.split("?")[0].rstrip("/") or url in feeds or url in found_on]
        own = [(url, d, t) for url, (d, t) in texts.items() if url == record.url]

        def find(value, places):
            for url, digest, text in places:
                found = evidence_text.locate(value, [(url, "visible text", text)]) if value else None
                if found:
                    return found["quote"], url, digest
            return None

        rows = []
        for name, value, places in (("title", record.title, listing + own), ("description", (record.description or "")[:60], listing + own),
                                    ("venue", record.venue, own + listing), ("address", (record.address or "").split(",")[0], own + listing)):
            hit = find(value, places)
            if hit:
                rows.append((name, "extracted", hit[0], hit[1], hit[2], None if name not in ("description", "address") else
                             "the first part of the stored value" if name == "address" else "the first 60 characters of the stored value"))
        try:
            start = datetime.fromisoformat(record.starts_at)
            hit = find(f"{start:%B} {start.day}, {start.year}", listing)
            if hit:
                rows.append(("start", "inferred", hit[0], hit[1], hit[2], f"the listing's date; normalised to {record.starts_at} with the listing's time"))
        except (KeyError, TypeError, ValueError):
            pass
        for line in record.verification.evidence:
            rows.append(("verification", "recorded", None, None, None, line[:300]))
        return rows
