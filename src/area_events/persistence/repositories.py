"""Repositories: the explicit SQL for each entity of the discovery database.

Write methods take the transaction connection `tx` from `DiscoveryStore.write()`, so the Discovery Service decides what one
transaction contains (for example: lease check, pages, candidates, evidence, changes and job completion together). Read methods
take a connection from `DiscoveryStore.read()`. No repository makes a network call, and none keeps state between calls.
"""
import hashlib
import json
import logging
import zlib
from datetime import datetime

from ..discovery import producer
from ..contracts.models import (CandidateRecord, Change, ConfiguredSource, EvidenceRecord, JobCounts, PageRecord, RetentionPolicy, SourceRecord, SourceSpec)  # noqa: F401
from ..discovery.urls import canonical_url, url_key

log = logging.getLogger("area_events.discovery.repositories")
MAX_CONTENT_BYTES = 2_000_000


def content_hash(body):
    return hashlib.sha256(body).hexdigest()


class SourceRepository:
    def upsert(self, tx, spec, now):
        """Install a built-in instance once; later service starts must preserve persisted configuration."""
        tx.execute("""INSERT OR IGNORE INTO sources (source_key, adapter, name, base_url, options_json, interval_s, created_at, updated_at)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                   (spec.source_key, spec.adapter, spec.name, spec.base_url, json.dumps(spec.options_dict(), sort_keys=True), int(spec.interval_s), now, now))
        return tx.execute("SELECT source_id FROM sources WHERE source_key=?", (spec.source_key,)).fetchone()[0]

    def create_configured(self, tx, spec, enabled, now):
        return tx.execute("""INSERT INTO sources (source_key, adapter, name, base_url, options_json, enabled, interval_s, revision, created_at, updated_at)
                             VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?) RETURNING source_id""",
                          (spec.source_key, spec.adapter, spec.name, spec.base_url, json.dumps(spec.options_dict(), sort_keys=True), int(enabled), int(spec.interval_s), now, now)).fetchone()[0]

    def update_configured(self, tx, source_id, spec, enabled, now):
        tx.execute("""UPDATE sources SET name=?, base_url=?, options_json=?, enabled=?, interval_s=?, revision=revision+1, updated_at=?
                      WHERE source_id=?""",
                   (spec.name, spec.base_url, json.dumps(spec.options_dict(), sort_keys=True), int(enabled), int(spec.interval_s), now, source_id))

    def set_enabled(self, tx, source_id, enabled, now):
        """State-only transition: a retired adapter need not be installed to stop it."""
        tx.execute("""UPDATE sources SET enabled=?, revision=revision+1, updated_at=?
                      WHERE source_id=?""", (int(enabled), now, source_id))

    def by_id(self, connection, source_id):
        return connection.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()

    def by_key(self, connection, source_key):
        return connection.execute("SELECT * FROM sources WHERE source_key=?", (source_key,)).fetchone()

    def all(self, connection):
        return [self.record(row) for row in connection.execute("SELECT * FROM sources ORDER BY source_key")]

    def all_configured(self, connection):
        return connection.execute("SELECT * FROM sources ORDER BY source_key").fetchall()

    def save_leads(self, tx, source_id, leads, now):
        """Record the `SourceLead`s a producer returned (one row per page, updated each run). Returns how many rows were written."""
        for lead in leads:
            tx.execute("""INSERT INTO source_leads (source_id, url_key, url, host, kind, decision, reasons_json, score, found_on, link_text, events, fetched, first_seen_at, last_seen_at)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                          ON CONFLICT(source_id, url_key) DO UPDATE SET kind=excluded.kind, decision=excluded.decision, reasons_json=excluded.reasons_json, score=excluded.score,
                            found_on=COALESCE(source_leads.found_on, excluded.found_on), link_text=COALESCE(source_leads.link_text, excluded.link_text), events=excluded.events,
                            fetched=excluded.fetched, last_seen_at=excluded.last_seen_at""",
                       (source_id, url_key(lead.url), lead.url, lead.host, lead.kind, lead.decision, json.dumps(list(lead.reasons), ensure_ascii=False), int(lead.score),
                        lead.found_on, (lead.link_text or None), int(lead.events), 1 if lead.fetched else 0, now, now))
        return len(leads)

    def attempted(self, tx, source_id, now):
        tx.execute("UPDATE sources SET last_attempt_at=?, updated_at=? WHERE source_id=?", (now, now, source_id))

    def succeeded(self, tx, source_id, now):
        tx.execute("UPDATE sources SET last_success_at=?, last_attempt_at=?, updated_at=? WHERE source_id=?", (now, now, now, source_id))

    @staticmethod
    def record(row):
        return SourceRecord(row["source_key"], row["adapter"], row["name"], row["base_url"], bool(row["enabled"]), row["interval_s"],
                            row["last_success_at"], row["last_attempt_at"])

    @staticmethod
    def configuration(row):
        options = json.loads(row["options_json"])
        options["url"] = row["base_url"]
        return ConfiguredSource(row["source_key"], row["adapter"], row["name"], bool(row["enabled"]), row["interval_s"] // 60,
                                tuple(sorted(options.items())), row["revision"])


class PageRepository:
    def touch(self, tx, url, source_id, role, now):
        """The page_id of a URL (created on first sight) and its attribution to `source_id`. Returns (page_id, new)."""
        canonical = canonical_url(url)
        key = url_key(canonical)
        row = tx.execute("SELECT page_id FROM pages WHERE url_key=?", (key,)).fetchone()
        new = row is None
        if new:
            page_id = tx.execute("INSERT INTO pages (url_key, url, first_seen_at) VALUES (?, ?, ?) RETURNING page_id", (key, canonical, now)).fetchone()[0]
        else:
            page_id = row[0]
        tx.execute("""INSERT INTO source_pages (source_id, page_id, role, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)
                      ON CONFLICT(source_id, page_id, role) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                   (source_id, page_id, role, now, now))
        return page_id, new

    def store_content(self, tx, digest, compressed, size, media_type, now):
        """Store one body (already hashed and compressed by the caller, outside the transaction). Returns True when it was new."""
        if size > MAX_CONTENT_BYTES:
            raise ValueError("content over the stored-size limit")
        cursor = tx.execute("INSERT OR IGNORE INTO contents (content_hash, media_type, bytes, body, stored_at) VALUES (?, ?, ?, ?, ?)",
                            (digest, media_type or "application/octet-stream", size, compressed, now))
        return cursor.rowcount == 1

    def fingerprint(self, tx, page_id):
        row = tx.execute("SELECT fingerprint, content_hash FROM pages WHERE page_id=?", (page_id,)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def record_fetch(self, tx, page_id, job_id, now, outcome, http_status=None, size=None, digest=None, media_type=None, final_url=None, error=None,
                     fingerprint=None):
        """Record one request and update the page's current state. Returns True when the page's content changed."""
        tx.execute("INSERT INTO fetches (page_id, job_id, fetched_at, outcome, http_status, bytes, content_hash, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (page_id, job_id, now, outcome, http_status, size, digest, (error or None) and str(error)[:300]))
        if outcome != "ok":
            tx.execute("UPDATE pages SET status=?, fetched_at=? WHERE page_id=?", (outcome, now, page_id))
            return False
        previous = tx.execute("SELECT content_hash FROM pages WHERE page_id=?", (page_id,)).fetchone()[0]
        changed = previous != digest
        tx.execute("""UPDATE pages SET status='ok', content_hash=?, media_type=?, bytes=?, final_url=?, fetched_at=?, fingerprint=COALESCE(?, fingerprint),
                      changed_at=CASE WHEN ? THEN ? ELSE changed_at END WHERE page_id=?""",
                   (digest, media_type, size, final_url, now, fingerprint, 1 if changed else 0, now, page_id))
        return changed

    def get(self, connection, key, include_body=False):
        row = connection.execute("SELECT * FROM pages WHERE url_key=?", (key,)).fetchone()
        if row is None:
            return None
        attributions = tuple((r["source_key"], r["role"]) for r in connection.execute(
            "SELECT s.source_key, sp.role FROM source_pages sp JOIN sources s USING (source_id) WHERE sp.page_id=? ORDER BY s.source_key, sp.role", (row["page_id"],)))
        body = None
        if include_body and row["content_hash"]:
            stored = connection.execute("SELECT body FROM contents WHERE content_hash=?", (row["content_hash"],)).fetchone()
            body = zlib.decompress(stored[0]) if stored else None
        return PageRecord(row["url_key"], row["url"], row["status"], row["content_hash"], row["media_type"], row["bytes"], row["first_seen_at"],
                          row["fetched_at"], row["changed_at"], attributions, body)

    def body(self, connection, digest):
        stored = connection.execute("SELECT body FROM contents WHERE content_hash=?", (digest,)).fetchone()
        return zlib.decompress(stored[0]) if stored else None

    def key_of(self, connection, page_id):
        row = connection.execute("SELECT url_key FROM pages WHERE page_id=?", (page_id,)).fetchone()
        return row[0] if row else None

    def recent(self, connection, limit=20):
        return [row["url_key"] for row in connection.execute("SELECT url_key FROM pages ORDER BY COALESCE(fetched_at, first_seen_at) DESC, page_id DESC LIMIT ?", (limit,))]


class CandidateRepository:
    def upsert(self, tx, key, source_id, page_id, method, extractor, record, now):
        """Insert or update one candidate from a standard `EventCandidate`. Its identity is the record's comparable facts (`producer.candidate_digest`); the producer's state is kept
        in its own column, updated on every run and never part of the identity. Returns (candidate_id, 'new' | 'changed' | 'unchanged')."""
        doc, digest = json.dumps(producer.document(record), ensure_ascii=False), producer.candidate_digest(record)
        state = json.dumps(producer.thaw(record.state), ensure_ascii=False)
        starts_at = int(datetime.fromisoformat(record.starts_at).timestamp())
        row = tx.execute("SELECT candidate_id, payload_hash FROM candidates WHERE candidate_key=?", (key,)).fetchone()
        if row is None:
            candidate_id = tx.execute("""INSERT INTO candidates (candidate_key, source_id, page_id, method, extractor, title, starts_at, payload_json, payload_hash, state_json,
                                          first_seen_at, last_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING candidate_id""",
                                      (key, source_id, page_id, method, extractor, record.title, starts_at, doc, digest, state, now, now, now)).fetchone()[0]
            return candidate_id, "new"
        if row["payload_hash"] == digest:
            tx.execute("UPDATE candidates SET last_seen_at=?, state_json=? WHERE candidate_id=?", (now, state, row["candidate_id"]))
            return row["candidate_id"], "unchanged"
        tx.execute("""UPDATE candidates SET page_id=?, method=?, extractor=?, title=?, starts_at=?, payload_json=?, payload_hash=?, state_json=?, last_seen_at=?, updated_at=?
                      WHERE candidate_id=?""", (page_id, method, extractor, record.title, starts_at, doc, digest, state, now, now, row["candidate_id"]))
        return row["candidate_id"], "changed"

    def replace_evidence(self, tx, candidate_id, rows):
        """Replace a candidate's evidence with `rows` [(field, kind, quote, page_id, content_hash, note)]. Returns how many were written."""
        tx.execute("DELETE FROM evidence WHERE candidate_id=?", (candidate_id,))
        for field, kind, quote, page_id, digest, note in rows:
            tx.execute("INSERT INTO evidence (candidate_id, field, kind, quote, page_id, content_hash, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (candidate_id, field, kind, quote[:400] if quote else None, page_id, digest, note))
        return len(rows)

    def previous(self, connection, source_id):
        """The source's stored candidates as standard records, with the state its producer left (what the producer is handed as `previous`). A row that is not a readable
        version-1 record is skipped and logged, never guessed at."""
        records = []
        for row in connection.execute("SELECT candidate_key, payload_json, state_json FROM candidates WHERE source_id=? ORDER BY candidate_key", (source_id,)):
            try:
                records.append(producer.candidate_from_document(json.loads(row["payload_json"]), json.loads(row["state_json"] or "{}")))
            except (ValueError, TypeError) as error:
                log.warning("stored candidate %s is not a readable standard record: %s", row["candidate_key"], error)
        return tuple(records)

    def get(self, connection, key):
        row = connection.execute("""SELECT c.*, s.source_key, p.url_key FROM candidates c JOIN sources s USING (source_id)
                                    LEFT JOIN pages p ON p.page_id = c.page_id WHERE c.candidate_key=?""", (key,)).fetchone()
        return self._record(connection, row) if row else None

    def query(self, connection, query):
        clauses, params = [], []
        if query.source_key:
            clauses.append("s.source_key = ?")
            params.append(query.source_key)
        if query.starts_from is not None:
            clauses.append("c.starts_at >= ?")
            params.append(query.starts_from)
        if query.starts_to is not None:
            clauses.append("c.starts_at < ?")
            params.append(query.starts_to)
        if query.title_contains:
            clauses.append("c.title LIKE ? ESCAPE '\\'")
            params.append("%" + query.title_contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(query.limit), 500)))
        rows = connection.execute(f"""SELECT c.*, s.source_key, p.url_key FROM candidates c JOIN sources s USING (source_id)
                                      LEFT JOIN pages p ON p.page_id = c.page_id {where} ORDER BY c.starts_at, c.candidate_key LIMIT ?""", params).fetchall()
        return tuple(self._record(connection, row, with_evidence=False) for row in rows)

    def page_after(self, connection, after_key, limit):
        rows = connection.execute("""SELECT c.*, s.source_key, p.url_key FROM candidates c JOIN sources s USING (source_id)
                                     LEFT JOIN pages p ON p.page_id = c.page_id WHERE c.candidate_key > ? ORDER BY c.candidate_key LIMIT ?""",
                                  (after_key or "", limit)).fetchall()
        return tuple(self._record(connection, row, with_evidence=False) for row in rows)

    def _record(self, connection, row, with_evidence=True):
        evidence = ()
        if with_evidence:
            evidence = tuple(EvidenceRecord(r["field"], r["kind"], r["quote"], r["url_key"], r["content_hash"], r["note"]) for r in connection.execute(
                "SELECT e.*, p.url_key FROM evidence e LEFT JOIN pages p ON p.page_id = e.page_id WHERE e.candidate_id=? ORDER BY e.evidence_id", (row["candidate_id"],)))
        return CandidateRecord(row["candidate_key"], row["source_key"], row["url_key"], row["method"], row["extractor"], row["title"], row["starts_at"],
                               json.loads(row["payload_json"]), row["payload_hash"], row["first_seen_at"], row["last_seen_at"], evidence)


class JobRepository:
    """Durable jobs. A job is claimable when it is due and either waiting ('ready'), completed and due again ('done'), given up
    until its next regular run ('failed'), or leased with an expired lease. Claiming is one UPDATE ... RETURNING inside a write
    transaction, so two workers can never claim the same job."""

    def ensure(self, tx, job_key, source_id, now, priority=50, max_attempts=4):
        tx.execute("""INSERT INTO jobs (job_key, kind, source_id, priority, state, next_run_at, max_attempts, created_at, updated_at)
                      VALUES (?, 'crawl_source', ?, ?, 'ready', ?, ?, ?, ?) ON CONFLICT(job_key) DO NOTHING""",
                   (job_key, source_id, priority, now, max_attempts, now, now))
        return tx.execute("SELECT job_id FROM jobs WHERE job_key=?", (job_key,)).fetchone()[0]

    def claim(self, tx, worker, token, now, lease_s, require_area=False):
        """`require_area`: only jobs of sources that an enabled area uses (the control plane decides what is crawled)."""
        gate = ("AND EXISTS (SELECT 1 FROM area_sources x JOIN areas a USING (area_id) WHERE x.source_id = j.source_id AND a.enabled = 1)" if require_area else "")
        row = tx.execute(f"""UPDATE jobs SET state='leased', lease_owner=?, lease_token=?, lease_until=?, attempts=attempts+1, updated_at=?
                            WHERE job_id = (SELECT j.job_id FROM jobs j JOIN sources s USING (source_id)
                                            WHERE s.enabled=1 AND j.next_run_at <= ? {gate}
                                              AND (j.state IN ('ready', 'done', 'failed') OR (j.state='leased' AND j.lease_until <= ?))
                                            ORDER BY j.priority DESC, j.next_run_at, j.job_id LIMIT 1)
                            RETURNING job_id, job_key, source_id, attempts, max_attempts, lease_token, lease_until""",
                         (worker, token, now + lease_s, now, now, now)).fetchone()
        return dict(row) if row else None

    def lease_valid(self, tx, job_id, token, now):
        return tx.execute("SELECT 1 FROM jobs WHERE job_id=? AND state='leased' AND lease_token=? AND lease_until > ?", (job_id, token, now)).fetchone() is not None

    def complete(self, tx, job_id, token, now, next_run_at):
        cursor = tx.execute("""UPDATE jobs SET state='done', lease_owner=NULL, lease_token=NULL, lease_until=NULL, attempts=0, last_error=NULL,
                               runs=runs+1, next_run_at=?, updated_at=? WHERE job_id=? AND state='leased' AND lease_token=? AND lease_until > ?""",
                            (next_run_at, now, job_id, token, now))
        return cursor.rowcount == 1

    def fail(self, tx, job_id, token, now, error, backoff_base_s, backoff_cap_s, interval_s):
        """Release a failed job: retry after a growing wait, or, after its last attempt, wait for the next regular run.
        A stale token changes nothing. Returns the new state, or None."""
        row = tx.execute("SELECT attempts, max_attempts FROM jobs WHERE job_id=? AND state='leased' AND lease_token=?", (job_id, token)).fetchone()
        if row is None:
            return None
        if row["attempts"] >= row["max_attempts"]:
            state, next_run, attempts = "failed", now + interval_s, 0
        else:
            state, next_run, attempts = "ready", now + min(backoff_base_s * 2 ** (row["attempts"] - 1), backoff_cap_s), row["attempts"]
        tx.execute("""UPDATE jobs SET state=?, next_run_at=?, attempts=?, lease_owner=NULL, lease_token=NULL, lease_until=NULL, last_error=?, updated_at=?
                      WHERE job_id=? AND lease_token=?""", (state, next_run, attempts, str(error)[:300], now, job_id, token))
        return state

    def recover_expired(self, tx, now):
        """Return expired leases to 'ready' (a worker died or was stopped). Returns how many."""
        return tx.execute("""UPDATE jobs SET state='ready', lease_owner=NULL, lease_token=NULL, lease_until=NULL, updated_at=?
                             WHERE state='leased' AND lease_until <= ?""", (now, now)).rowcount

    def make_due(self, tx, job_key, now):
        return tx.execute("UPDATE jobs SET next_run_at=?, updated_at=? WHERE job_key=? AND state <> 'leased'", (now, now, job_key)).rowcount

    def get(self, connection, job_key):
        row = connection.execute("SELECT * FROM jobs WHERE job_key=?", (job_key,)).fetchone()
        return dict(row) if row else None

    def counts(self, connection):
        row = connection.execute("""SELECT SUM(state='ready' AND attempts=0), SUM(state='leased'), SUM(state='done'), SUM(state='failed'),
                                           SUM(state='ready' AND attempts>0) FROM jobs""").fetchone()
        return JobCounts(*[int(v or 0) for v in row])


class ChangeLog:
    def append(self, tx, entity, key, digest, now, op="upsert", kind=None):
        """Append one change. `kind` names what happened ('candidate.changed', 'area.configuration_changed', ...); it defaults to '<entity>.changed'."""
        return tx.execute("INSERT INTO changes (entity, entity_key, op, kind, payload_hash, created_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING seq",
                          (entity, key, op, kind or f"{entity}.changed", digest, now)).fetchone()[0]

    def latest(self, connection):
        row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='changes'").fetchone()
        return int(row[0]) if row else 0

    def pruned_through(self, connection):
        return int(connection.execute("SELECT value FROM store_meta WHERE key='pruned_through_seq'").fetchone()[0])

    def after(self, connection, seq, limit):
        return tuple(Change(r["seq"], r["entity"], r["entity_key"], r["op"], r["payload_hash"], r["created_at"], r["kind"])
                     for r in connection.execute("SELECT * FROM changes WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit)))

    def acknowledge(self, tx, consumer, seq, now):
        latest = self.latest(tx)
        seq = max(0, min(int(seq), latest))
        tx.execute("""INSERT INTO consumers (consumer, acked_seq, acked_at) VALUES (?, ?, ?)
                      ON CONFLICT(consumer) DO UPDATE SET acked_seq=MAX(consumers.acked_seq, excluded.acked_seq), acked_at=excluded.acked_at""",
                   (consumer, seq, now))

    def prune(self, tx, now, min_age_s, max_rows):
        """Remove changes every registered consumer has acknowledged and that are older than `min_age_s`; whatever the
        acknowledgements, keep at most `max_rows`. Records the highest removed sequence, so a consumer behind it is told to resync."""
        latest = self.latest(tx)
        acked = tx.execute("SELECT MIN(acked_seq) FROM consumers").fetchone()[0]
        floor = latest if acked is None else int(acked)
        removed_through = tx.execute("SELECT MAX(seq) FROM changes WHERE seq <= ? AND created_at < ?", (floor, now - min_age_s)).fetchone()[0] or 0
        count = int(tx.execute("SELECT COUNT(*) FROM changes").fetchone()[0])
        if count - (tx.execute("SELECT COUNT(*) FROM changes WHERE seq <= ?", (removed_through,)).fetchone()[0]) > max_rows:
            removed_through = max(removed_through, int(tx.execute("SELECT seq FROM changes ORDER BY seq DESC LIMIT 1 OFFSET ?", (max_rows,)).fetchone()[0]))
        if not removed_through:
            return 0
        deleted = tx.execute("DELETE FROM changes WHERE seq <= ?", (removed_through,)).rowcount
        tx.execute("UPDATE store_meta SET value=? WHERE key='pruned_through_seq' AND CAST(value AS INTEGER) < ?", (str(removed_through), removed_through))
        return deleted


class Retention:
    """Bounded growth. Never removes content that a page's current state or a retained candidate's evidence refers to."""

    def __init__(self, changes, events):
        self.changes, self.events = changes, events

    def prune(self, tx, now, policy):
        removed = {}
        removed["fetches"] = tx.execute("DELETE FROM fetches WHERE fetched_at < ?", (now - policy.fetch_history_days * 86400,)).rowcount
        removed["fetches"] += tx.execute("""DELETE FROM fetches WHERE fetch_id IN (SELECT fetch_id FROM (SELECT fetch_id,
                                              ROW_NUMBER() OVER (PARTITION BY page_id ORDER BY fetched_at DESC, fetch_id DESC) AS n FROM fetches) WHERE n > ?)""",
                                         (policy.fetches_per_page,)).rowcount
        ended = tx.execute("SELECT candidate_key, payload_hash FROM candidates WHERE starts_at IS NOT NULL AND starts_at < ?",
                           (now - policy.candidate_days_after_start * 86400,)).fetchall()
        for row in ended:
            self.changes.append(tx, "candidate", row["candidate_key"], row["payload_hash"], now, op="delete")
        removed["candidates"] = tx.execute("DELETE FROM candidates WHERE starts_at IS NOT NULL AND starts_at < ?",
                                           (now - policy.candidate_days_after_start * 86400,)).rowcount
        for key in self.events.delete_before(tx, now - policy.candidate_days_after_start * 86400):
            self.changes.append(tx, "event", key, "0" * 64, now, op="delete", kind="event.removed")
        removed["pages"] = tx.execute("""DELETE FROM pages WHERE page_id NOT IN (SELECT page_id FROM candidates WHERE page_id IS NOT NULL)
                                           AND page_id NOT IN (SELECT page_id FROM evidence WHERE page_id IS NOT NULL)
                                           AND COALESCE((SELECT MAX(last_seen_at) FROM source_pages sp WHERE sp.page_id = pages.page_id), first_seen_at) < ?""",
                                        (now - policy.page_days * 86400,)).rowcount
        referenced = "SELECT content_hash FROM pages WHERE content_hash IS NOT NULL UNION SELECT content_hash FROM evidence WHERE content_hash IS NOT NULL"
        removed["contents"] = tx.execute(f"DELETE FROM contents WHERE stored_at < ? AND content_hash NOT IN ({referenced})",
                                         (now - policy.unreferenced_content_days * 86400,)).rowcount
        total = int(tx.execute("SELECT COALESCE(SUM(bytes), 0) FROM contents").fetchone()[0])
        if total > policy.content_budget_bytes:
            for row in tx.execute(f"SELECT content_hash, bytes FROM contents WHERE content_hash NOT IN ({referenced}) ORDER BY stored_at").fetchall():
                if total <= policy.content_budget_bytes:
                    break
                tx.execute("DELETE FROM contents WHERE content_hash=?", (row[0],))
                total -= row[1]
                removed["contents"] += 1
        removed["changes"] = self.changes.prune(tx, now, policy.change_min_age_s, policy.change_max_rows)
        removed["content_bytes_after"] = total
        return removed


def compress(body):
    """(sha256, zlib bytes, size): done by the caller before its transaction."""
    return content_hash(body), zlib.compress(body, 6), len(body)
