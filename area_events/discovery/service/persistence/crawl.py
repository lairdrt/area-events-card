"""Crawl storage: the CrawlStorage port over the database. Claiming a job, reading a source's previous records, recording a whole run, releasing a failed job and retention.

`record_run` is the heart: ONE write transaction verifies the job lease (token and expiry) and then writes the pages, their source attribution, contents, fetches, candidates,
evidence, source leads and change records, marks the source and completes the job. If the lease is no longer valid, or anything fails, nothing is written.
"""
import json
import sqlite3

from ..contracts import StaleLease, StoreRejected, job_key_for
from ..urls import canonical_url, url_key
from .ports import ClaimedJob, PacingFacts, SourceState
from .repositories import compress


class SqliteCrawlStorage:
    def __init__(self, store, sources, pages, candidates, jobs, changes, retention, areas, settings, clock):
        self._store, self._sources, self._pages, self._candidates = store, sources, pages, candidates
        self._jobs, self._changes, self._retention, self._areas, self._settings, self._clock = jobs, changes, retention, areas, settings, clock

    def _now(self):
        return int(self._clock())

    # --- sources, jobs, retention --------------------------------------------------------------------------------------

    def register_source(self, spec, max_attempts):
        now = self._now()
        with self._store.write() as tx:
            source_id = self._sources.upsert(tx, spec, now)
            self._jobs.ensure(tx, job_key_for(spec.source_key), source_id, now, max_attempts=max_attempts)
        with self._store.read() as connection:
            return self._sources.record(self._sources.by_key(connection, spec.source_key))

    def make_due(self, source_key):
        with self._store.write() as tx:
            return self._jobs.make_due(tx, job_key_for(source_key), self._now())

    def recover_expired_jobs(self):
        with self._store.write() as tx:
            return self._jobs.recover_expired(tx, self._now())

    def prune(self, policy):
        with self._store.write() as tx:
            return self._retention.prune(tx, self._now(), policy)

    # --- one run ------------------------------------------------------------------------------------------------------

    def claim_job(self, worker, token, lease_s, require_area):
        now = self._now()
        with self._store.write() as tx:
            row = self._jobs.claim(tx, worker, token, now, lease_s, require_area)
            if row:
                self._sources.attempted(tx, row["source_id"], now)
        return ClaimedJob(row["job_id"], row["job_key"], row["source_id"], row["attempts"], row["max_attempts"], row["lease_token"], row["lease_until"]) if row else None

    def load_source(self, job):
        with self._store.read() as connection:
            source = self._sources.by_id(connection, job.source_id)
            previous = self._candidates.previous(connection, job.source_id)
        return SourceState(source["source_key"], source["adapter"], source["name"], source["base_url"], json.loads(source["options_json"]), previous)

    def prepare_content(self, body):
        """(sha256, compressed bytes, size): the storage format of a page body, computed by the caller before its transaction."""
        return compress(body)

    def record_run(self, job, plan, pacing):
        """Store a whole run atomically. Returns the counts. Raises StaleLease when the job's lease is no longer valid, StoreRejected when the database refuses the data."""
        try:
            with self._store.write() as tx:
                stored_at = self._now()
                if not self._jobs.lease_valid(tx, job.job_id, job.lease_token, stored_at):
                    raise StaleLease(job.job_key)
                source = self._sources.by_id(tx, job.source_id)
                counts = self._store_plan(tx, job, source, plan, stored_at)
                self._sources.succeeded(tx, job.source_id, stored_at)
                changed = bool(counts["candidates_new"] or counts["candidates_changed"] or counts["changes_appended"])
                settings = self._settings.all(tx)
                strongest = next((row[5] for row in self._areas.eligible_sources(tx) if row[0] == job.source_id), None)
                decision = pacing.next_run(PacingFacts(int(source["interval_s"]), stored_at, changed, int(source["quiet_runs"]), settings.mode, settings.intensity, strongest))
                if decision.quiet_runs is not None:
                    tx.execute("UPDATE sources SET quiet_runs=? WHERE source_id=?", (decision.quiet_runs, job.source_id))
                if not self._jobs.complete(tx, job.job_id, job.lease_token, stored_at, decision.next_run_at):
                    raise StaleLease(job.job_key)
        except sqlite3.IntegrityError as error:   # a defect in what was about to be stored: nothing was written
            raise StoreRejected(str(error)) from error
        return counts

    def release(self, job, failed, error, backoff_base_s, backoff_cap_s):
        now = self._now()
        with self._store.write() as tx:
            if self._jobs.lease_valid(tx, job.job_id, job.lease_token, now):
                for fetch in failed:   # what was tried is history worth keeping, even when the run failed
                    page_id, _ = self._pages.touch(tx, fetch.url, job.source_id, "other", now)
                    self._pages.record_fetch(tx, page_id, job.job_id, now, fetch.outcome, error=fetch.error)
            interval = int(tx.execute("SELECT interval_s FROM sources WHERE source_id=?", (job.source_id,)).fetchone()[0])
            return self._jobs.fail(tx, job.job_id, job.lease_token, now, error, backoff_base_s, backoff_cap_s, interval)

    def _store_plan(self, tx, job, source, plan, now):
        counts = dict(pages_seen=0, pages_new=0, content_new=0, content_unchanged=0, candidates_seen=0, candidates_new=0, candidates_changed=0,
                      candidates_unchanged=0, evidence_written=0, changes_appended=0)
        page_ids, stored_as = {}, {}   # stored_as: the content hash fetched -> the hash actually kept (a page whose readable text is unchanged keeps its old snapshot)
        for entry in plan.pages:
            page_id, new = self._pages.touch(tx, entry.url, source["source_id"], entry.role, now)
            page_ids[canonical_url(entry.url)] = page_id
            counts["pages_seen"] += 1
            counts["pages_new"] += int(new)
            changed, digest = False, entry.digest
            if entry.digest:
                known_fingerprint, known_digest = self._pages.fingerprint(tx, page_id)
                final_url = entry.final_url if entry.final_url != entry.url else None
                if known_fingerprint == entry.fingerprint and known_digest:
                    counts["content_unchanged"] += 1  # same readable text: keep the stored snapshot, record only the fetch
                    stored_as[entry.digest] = known_digest
                    digest = known_digest
                    self._pages.record_fetch(tx, page_id, job.job_id, now, "ok", entry.http_status, entry.size, known_digest, entry.media_type, final_url)
                else:
                    stored = self._pages.store_content(tx, entry.digest, entry.packed, entry.size, entry.media_type, now)
                    counts["content_new" if stored else "content_unchanged"] += 1
                    changed = self._pages.record_fetch(tx, page_id, job.job_id, now, "ok", entry.http_status, entry.size, entry.digest, entry.media_type,
                                                       final_url, fingerprint=entry.fingerprint)
            else:
                self._pages.record_fetch(tx, page_id, job.job_id, now, entry.outcome, entry.http_status, error=entry.error)
            if changed:
                self._changes.append(tx, "page", url_key(entry.url), digest, now)
                counts["changes_appended"] += 1
        for item in plan.candidates:
            record, key = item.record, item.key
            counts["candidates_seen"] += 1
            page_id = None
            if record.url:
                url = canonical_url(record.url)
                page_id = page_ids.get(url)
                if page_id is None:
                    page_id, new = self._pages.touch(tx, record.url, source["source_id"], "event", now)
                    page_ids[url] = page_id
                    counts["pages_new"] += int(new)
            candidate_id, outcome = self._candidates.upsert(tx, key, source["source_id"], page_id, "adapter", item.extractor, record, now)
            counts[f"candidates_{outcome}"] += 1
            if outcome != "unchanged":
                rows = [(f, k, q, page_ids.get(canonical_url(u)) if u else None, stored_as.get(d, d), n) for f, k, q, u, d, n in item.evidence]
                counts["evidence_written"] += self._candidates.replace_evidence(tx, candidate_id, rows)
                self._changes.append(tx, "candidate", key, item.digest, now)
                counts["changes_appended"] += 1
        if plan.leads:
            self._sources.save_leads(tx, source["source_id"], plan.leads, now)
        return counts
