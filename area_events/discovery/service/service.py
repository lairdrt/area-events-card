"""DiscoveryService: registers sources, schedules and runs their discovery jobs, and stores what they find.

One job run:

    1. claim         a write transaction: the next due job, leased to this worker with a fresh token
    2. read          a read transaction: the source and its stored candidates (so event pages already read are reused)
    3. crawl         no transaction: the adapter fetches through a RecordingFetcher over the shared-policy SafeFetcher
    4. prepare       no transaction: hash and compress bodies, compute payload hashes, locate evidence in the stored pages
    5. store         one write transaction: verify the lease (token and expiry) and, only then, write pages, source attribution,
                     contents, fetches, candidates, evidence and change records, mark the source and complete the job
    6. on failure    a write transaction releases the job with backoff (guarded by the same token)

The service never calls classification, geocoding, validation, imagery or Home Assistant; consumers read the change feed later.
If the database cannot be written, the service pauses for a bounded, growing time and reports it; the job's lease then expires
and the work is done again, so nothing is acknowledged that was not stored.
"""
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from ..core import FetchError
from .. import evidence as evidence_text
from .contracts import RunReport
from .fetching import RecordingFetcher
from .repositories import RetentionPolicy, SourceSpec, compress, payload_hash
from .scheduler import FixedPacing
from .store import StaleLease, StorageUnavailable
from .urls import canonical_url, url_key


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


def job_key_for(source_key):
    return f"crawl_source:{source_key}"


class DiscoveryService:
    def __init__(self, store, sources, pages, candidates, jobs, changes, retention, fetch_client, adapters, clock, config=DiscoveryConfig(),
                 worker_id="worker-1", new_token=lambda: uuid.uuid4().hex, pacing=None, gate=None):
        self.store, self.sources, self.pages, self.candidates = store, sources, pages, candidates
        self.jobs, self.changes, self.retention = jobs, changes, retention
        self.fetch_client, self.adapters, self.clock, self.config = fetch_client, dict(adapters), clock, config
        self.worker_id, self.new_token = worker_id, new_token
        self.pacing, self.gate = pacing or FixedPacing(), gate   # gate.paused(): operator pause
        self.paused_until, self.pause_seconds, self.last_storage_error = 0, 0, None

    # --- sources and jobs --------------------------------------------------------------------------------------------

    def register_source(self, spec: SourceSpec):
        if spec.adapter not in self.adapters:
            raise ValueError(f"no adapter '{spec.adapter}' is installed")
        now = self._now()
        with self.store.write() as tx:
            source_id = self.sources.upsert(tx, spec, now)
            self.jobs.ensure(tx, job_key_for(spec.source_key), source_id, now, max_attempts=self.config.max_attempts)
        with self.store.read() as connection:
            return self.sources.record(self.sources.by_key(connection, spec.source_key))

    def make_due(self, source_key):
        """Run a source's job at the next opportunity instead of at its scheduled time (an operator's 'run now')."""
        with self.store.write() as tx:
            return self.jobs.make_due(tx, job_key_for(source_key), self._now())

    def recover(self):
        """Release leases that have expired (a worker that stopped mid-job). Returns how many."""
        with self.store.write() as tx:
            return self.jobs.recover_expired(tx, self._now())

    def prune(self):
        with self.store.write() as tx:
            return self.retention.prune(tx, self._now(), self.config.retention)

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
            with self.store.write() as tx:
                job = self.jobs.claim(tx, worker, token, now, self.config.lease_s, self.config.require_area)
                if job:
                    self.sources.attempted(tx, job["source_id"], now)
        except StorageUnavailable as error:
            return self._pause(error, None)
        if not job:
            return RunReport(None, "nothing_due")
        with self.store.read() as connection:
            source = self.sources.by_id(connection, job["source_id"])
            previous = self.candidates.payloads(connection, job["source_id"])
        adapter = self.adapters.get(source["adapter"])
        fetcher = RecordingFetcher(self.fetch_client, self.config.max_requests_per_job, self.config.max_job_seconds)
        options = {**json.loads(source["options_json"]), "source_key": source["source_key"], "name": source["name"], "url": source["base_url"]}
        try:
            if adapter is None:
                raise FetchError(f"no adapter '{source['adapter']}' is installed")
            result = adapter.crawl(fetcher, previous, options)
        except FetchError as error:
            return self._release(job, token, fetcher, error)
        plan = self._prepare(source, adapter, fetcher, result)
        try:
            with self.store.write() as tx:
                stored_at = self._now()
                if not self.jobs.lease_valid(tx, job["job_id"], token, stored_at):
                    raise StaleLease(job["job_key"])
                counts = self._store(tx, job, source, plan, stored_at)
                self.sources.succeeded(tx, source["source_id"], stored_at)
                changed = bool(counts["candidates_new"] or counts["candidates_changed"] or counts["changes_appended"])
                next_run = self.pacing.after_run(tx, source["source_id"], int(source["interval_s"]), stored_at, changed)
                if not self.jobs.complete(tx, job["job_id"], token, stored_at, next_run):
                    raise StaleLease(job["job_key"])
        except StaleLease:
            return RunReport(job["job_key"], "stale_lease", requests=fetcher.requests, error="the lease expired or was taken over; nothing was written")
        except StorageUnavailable as error:
            return self._pause(error, job["job_key"], fetcher.requests)
        self.pause_seconds = 0
        return RunReport(job["job_key"], "completed", requests=fetcher.requests, details_reused=int(result.notes.get("detail_reused", 0)),
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

    def _release(self, job, token, fetcher, error):
        now = self._now()
        try:
            with self.store.write() as tx:
                if self.jobs.lease_valid(tx, job["job_id"], token, now):
                    for fetch in fetcher.fetches:  # what was tried is history worth keeping, even when the run failed
                        page_id, _ = self.pages.touch(tx, fetch.url, job["source_id"], "other", now)
                        self.pages.record_fetch(tx, page_id, job["job_id"], now, fetch.outcome, error=fetch.error)
                state = self.jobs.fail(tx, job["job_id"], token, now, error, self.config.backoff_base_s, self.config.backoff_cap_s, self._interval(tx, job))
        except StorageUnavailable as storage_error:
            return self._pause(storage_error, job["job_key"], fetcher.requests)
        return RunReport(job["job_key"], "fetch_failed", requests=fetcher.requests, error=f"{error} (job {state or 'not released: lease lost'})")

    def _interval(self, connection, job):
        return int(connection.execute("SELECT interval_s FROM sources WHERE source_id=?", (job["source_id"],)).fetchone()[0])

    def _prepare(self, source, adapter, fetcher, result):
        """Everything slow, before the write transaction: bodies hashed and compressed, payload hashes, evidence located."""
        pages, texts = [], {}
        for fetch in fetcher.fetches:
            entry = {"fetch": fetch, "role": adapter.page_role(fetch.url, source["base_url"]), "digest": None}
            if fetch.outcome == "ok" and fetch.body is not None:
                digest, packed, size = compress(fetch.body)
                entry.update(digest=digest, packed=packed, size=size, fingerprint=digest)
                if (fetch.media_type or "").startswith("text/html") or (fetch.media_type or "") == "":
                    text = evidence_text.visible_text(fetch.body.decode("utf-8", "replace"))
                    # pages carry random ids and tokens; their readable text decides whether they changed
                    entry["fingerprint"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    texts[fetch.url] = (digest, text)
            pages.append(entry)
        candidates = []
        for event in result.candidates:
            digest = payload_hash(event)
            try:
                starts_at = int(datetime.fromisoformat(event["start"]).timestamp())
            except (KeyError, TypeError, ValueError):
                starts_at = None
            candidates.append({"event": event, "digest": digest, "starts_at": starts_at, "evidence": self._evidence(event, source["base_url"], texts)})
        return {"pages": pages, "candidates": candidates}

    @staticmethod
    def _evidence(event, base_url, texts):
        """Evidence rows (field, kind, quote, url, content_hash, note): a quote only where the value is found verbatim in a page read."""
        listing = [(url, d, t) for url, (d, t) in texts.items() if url.split("?")[0].rstrip("/") == base_url.split("?")[0].rstrip("/")]
        own = [(url, d, t) for url, (d, t) in texts.items() if url == event.get("url")]

        def find(value, places):
            for url, digest, text in places:
                found = evidence_text.locate(value, [(url, "visible text", text)]) if value else None
                if found:
                    return found["quote"], url, digest
            return None

        rows = []
        for name, value, places in (("title", event.get("title"), listing + own), ("description", (event.get("description") or "")[:60], listing + own),
                                    ("venue", event.get("venue"), own + listing), ("address", (event.get("address") or "").split(",")[0], own + listing)):
            hit = find(value, places)
            if hit:
                rows.append((name, "extracted", hit[0], hit[1], hit[2], None if name not in ("description", "address") else
                             "the first part of the stored value" if name == "address" else "the first 60 characters of the stored value"))
        try:
            start = datetime.fromisoformat(event["start"])
            hit = find(f"{start:%B} {start.day}, {start.year}", listing)
            if hit:
                rows.append(("start", "inferred", hit[0], hit[1], hit[2], f"the listing's date; normalised to {event['start']} with the listing's time"))
        except (KeyError, TypeError, ValueError):
            pass
        for line in (event.get("verification") or {}).get("evidence", []):
            rows.append(("verification", "recorded", None, None, None, line[:300]))
        return rows

    def _store(self, tx, job, source, plan, now):
        counts = dict(pages_seen=0, pages_new=0, content_new=0, content_unchanged=0, candidates_seen=0, candidates_new=0, candidates_changed=0,
                      candidates_unchanged=0, evidence_written=0, changes_appended=0)
        page_ids = {}
        for entry in plan["pages"]:
            fetch = entry["fetch"]
            page_id, new = self.pages.touch(tx, fetch.url, source["source_id"], entry["role"], now)
            page_ids[canonical_url(fetch.url)] = page_id
            counts["pages_seen"] += 1
            counts["pages_new"] += int(new)
            changed = False
            if entry["digest"]:
                known_fingerprint, known_digest = self.pages.fingerprint(tx, page_id)
                final_url = fetch.final_url if fetch.final_url != fetch.url else None
                if known_fingerprint == entry["fingerprint"] and known_digest:
                    counts["content_unchanged"] += 1  # same readable text: keep the stored snapshot, record only the fetch
                    entry["digest"] = known_digest
                    self.pages.record_fetch(tx, page_id, job["job_id"], now, "ok", fetch.http_status, entry["size"], known_digest, fetch.media_type, final_url)
                else:
                    stored = self.pages.store_content(tx, entry["digest"], entry["packed"], entry["size"], fetch.media_type, now)
                    counts["content_new" if stored else "content_unchanged"] += 1
                    changed = self.pages.record_fetch(tx, page_id, job["job_id"], now, "ok", fetch.http_status, entry["size"], entry["digest"], fetch.media_type,
                                                      final_url, fingerprint=entry["fingerprint"])
            else:
                self.pages.record_fetch(tx, page_id, job["job_id"], now, fetch.outcome, fetch.http_status, error=fetch.error)
            if changed:
                self.changes.append(tx, "page", url_key(fetch.url), entry["digest"], now)
                counts["changes_appended"] += 1
        for item in plan["candidates"]:
            event = item["event"]
            counts["candidates_seen"] += 1
            page_id = None
            if event.get("url"):
                key = canonical_url(event["url"])
                page_id = page_ids.get(key)
                if page_id is None:
                    page_id, new = self.pages.touch(tx, event["url"], source["source_id"], "event", now)
                    page_ids[key] = page_id
                    counts["pages_new"] += int(new)
            candidate_id, outcome = self.candidates.upsert(tx, event["id"], source["source_id"], page_id, "adapter", self._extractor(source), event.get("title"),
                                                           item["starts_at"], event, item["digest"], now)
            counts[f"candidates_{outcome}"] += 1
            if outcome != "unchanged":
                rows = [(f, k, q, page_ids.get(canonical_url(u)) if u else None, d, n) for f, k, q, u, d, n in item["evidence"]]
                counts["evidence_written"] += self.candidates.replace_evidence(tx, candidate_id, rows)
                self.changes.append(tx, "candidate", event["id"], item["digest"], now)
                counts["changes_appended"] += 1
        return counts

    def _extractor(self, source):
        adapter = self.adapters.get(source["adapter"])
        return getattr(adapter, "extractor", source["adapter"])
