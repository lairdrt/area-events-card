"""Processing storage: the leases, work records and extractions of event processing, and the atomic acceptance of a validated result.

`SqliteProcessingStorage` implements the ProcessingStorage port. A submission is ONE write transaction that checks the lease and the candidate's revision, stores the extraction, hands
the clean event to the deduplication strategy together with a lookup onto the existing events, applies the (checked) decision, marks the work done and appends the change record.
All of it commits together or none of it does; the single write connection (`BEGIN IMMEDIATE`) serializes concurrent submissions, so a decision based on existing events can never
be overtaken by another submission.
"""
import json

from ... import evidence as evidence_text
from ..contracts import PROCESSING_SCHEMA, LeaseLost, ProcessingStatus, StaleResult, SubmitAck, WorkItem
from .events import decide_and_apply


class ProcessingRepository:
    PENDING = """c.payload_hash IS NOT NULL AND (w.candidate_id IS NULL OR (
                    (w.done_hash IS NOT c.payload_hash OR w.done_version < :version)
                    AND (w.lease_token IS NULL OR w.lease_until <= :now)
                    AND w.next_attempt_at <= :now
                    AND NOT (w.state = 'failed' AND w.last_fail_hash = c.payload_hash AND w.fail_version >= :version)))"""

    def pending(self, connection, extractor, version, now, limit):
        return connection.execute(f"""SELECT c.candidate_id, c.candidate_key, c.payload_hash, c.payload_json, c.source_id, c.page_id FROM candidates c
                                      LEFT JOIN processing_work w ON w.candidate_id = c.candidate_id AND w.extractor = :extractor
                                      WHERE {self.PENDING} ORDER BY c.updated_at, c.candidate_id LIMIT :limit""",
                                  {"extractor": extractor, "version": version, "now": now, "limit": limit}).fetchall()

    def lease(self, tx, candidate_id, extractor, worker, token, claimed_hash, until, now):
        tx.execute("""INSERT INTO processing_work (candidate_id, extractor, state, claimed_hash, lease_owner, lease_token, lease_until, updated_at)
                      VALUES (?, ?, 'leased', ?, ?, ?, ?, ?)
                      ON CONFLICT(candidate_id, extractor) DO UPDATE SET state='leased', claimed_hash=excluded.claimed_hash, lease_owner=excluded.lease_owner,
                        lease_token=excluded.lease_token, lease_until=excluded.lease_until, updated_at=excluded.updated_at""",
                   (candidate_id, extractor, claimed_hash, worker, token, until, now))

    def held(self, tx, candidate_key, extractor, token, now):
        """The work row when this token holds a live lease on the candidate, else None."""
        return tx.execute("""SELECT w.*, c.payload_hash AS current_hash, c.candidate_id AS cid FROM processing_work w JOIN candidates c USING (candidate_id)
                             WHERE c.candidate_key=? AND w.extractor=? AND w.state='leased' AND w.lease_token=? AND w.lease_until >= ?""",
                          (candidate_key, extractor, token, now)).fetchone()

    def release(self, tx, candidate_id, extractor, now):
        tx.execute("""UPDATE processing_work SET state='ready', lease_owner=NULL, lease_token=NULL, lease_until=NULL, updated_at=? WHERE candidate_id=? AND extractor=?""",
                   (now, candidate_id, extractor))

    def done(self, tx, candidate_id, extractor, hash_, version, now):
        tx.execute("""UPDATE processing_work SET state='done', done_hash=?, done_version=?, attempts=0, next_attempt_at=0, lease_owner=NULL, lease_token=NULL,
                      lease_until=NULL, last_error=NULL, updated_at=? WHERE candidate_id=? AND extractor=?""", (hash_, version, now, candidate_id, extractor))

    def failed(self, tx, row, version, error, now, config):
        attempts = row["attempts"] + 1 if row["last_fail_hash"] == row["claimed_hash"] else 1
        exhausted = attempts >= config.max_attempts
        wait = min(config.backoff_base_s * 2 ** (attempts - 1), config.backoff_cap_s)
        tx.execute("""UPDATE processing_work SET state=?, attempts=?, next_attempt_at=?, last_fail_hash=?, fail_version=?, lease_owner=NULL, lease_token=NULL,
                      lease_until=NULL, last_error=?, updated_at=? WHERE candidate_id=? AND extractor=?""",
                   ("failed" if exhausted else "ready", attempts, now + wait, row["claimed_hash"], version, error[:300], now, row["candidate_id"], row["extractor"]))
        return "failed" if exhausted else "retrying"

    def expire(self, tx, now):
        return tx.execute("""UPDATE processing_work SET state='ready', lease_owner=NULL, lease_token=NULL, lease_until=NULL, updated_at=?
                             WHERE state='leased' AND lease_until < ?""", (now, now)).rowcount

    def store_extraction(self, tx, candidate_id, event_id, candidate_hash, extractor, version, status, failure, result, digest, worker, now):
        """Insert the extraction; returns (extraction_id, replayed). The same result for the same source revision is stored once."""
        row = tx.execute("""SELECT extraction_id, event_id FROM extractions WHERE candidate_id=? AND candidate_payload_hash=? AND extractor=? AND version=? AND result_hash=?""",
                         (candidate_id, candidate_hash, extractor, version, digest)).fetchone()
        if row:
            return row["extraction_id"], True
        extraction_id = tx.execute("""INSERT INTO extractions (candidate_id, event_id, candidate_payload_hash, extractor, version, status, failure_reason, result_json,
                                        result_hash, worker, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING extraction_id""",
                                   (candidate_id, event_id, candidate_hash, extractor, version, status, failure, json.dumps(result, ensure_ascii=False), digest, worker, now)).fetchone()[0]
        return extraction_id, False

    def replay(self, connection, candidate_key, extractor, version, digest):
        return connection.execute("""SELECT x.extraction_id, x.status, x.candidate_payload_hash, x.event_id, e.event_key, e.revision FROM extractions x
                                     JOIN candidates c USING (candidate_id) LEFT JOIN events e ON e.event_id = x.event_id
                                     WHERE c.candidate_key=? AND x.extractor=? AND x.version=? AND x.result_hash=? ORDER BY x.extraction_id DESC LIMIT 1""",
                                  (candidate_key, extractor, version, digest)).fetchone()

    def counts(self, connection, extractor, version, now):
        def one(sql, **params):
            return int(connection.execute(sql, {"extractor": extractor, "version": version, "now": now, **params}).fetchone()[0])
        base = "FROM candidates c LEFT JOIN processing_work w ON w.candidate_id = c.candidate_id AND w.extractor = :extractor"
        return {"candidates": one("SELECT COUNT(*) FROM candidates"),
                "pending": one(f"SELECT COUNT(*) {base} WHERE {self.PENDING}"),
                "leased": one("SELECT COUNT(*) FROM processing_work WHERE extractor=:extractor AND state='leased' AND lease_until > :now"),
                "retrying": one("SELECT COUNT(*) FROM processing_work WHERE extractor=:extractor AND state='ready' AND attempts > 0"),
                "failed": one("SELECT COUNT(*) FROM processing_work WHERE extractor=:extractor AND state='failed'"),
                "done": one(f"SELECT COUNT(*) {base} WHERE w.done_hash = c.payload_hash AND w.done_version >= :version"),
                "events": one("SELECT COUNT(*) FROM events"),
                "extractions": {r[0]: r[1] for r in connection.execute("SELECT status, COUNT(*) FROM extractions GROUP BY status")}}


class SqliteProcessingStorage:
    def __init__(self, store, work, events, candidates, pages, changes, clock, deduplicator):
        self._store, self._work, self._events, self._candidates, self._pages = store, work, events, candidates, pages
        self._changes, self._clock, self._deduplicator = changes, clock, deduplicator

    def claim_work(self, worker, extractor, version, limit, lease_s, new_token, config):
        now = int(self._clock())
        leased = []
        with self._store.write() as tx:
            self._work.expire(tx, now)
            for row in self._work.pending(tx, extractor, version, now, limit):
                token = new_token()
                self._work.lease(tx, row["candidate_id"], extractor, worker, token, row["payload_hash"], now + lease_s, now)
                leased.append((token, row))
            return tuple(self._item(tx, token, now + lease_s, row, config) for token, row in leased)

    def submit_result(self, candidate_key, extractor, version, lease_token, validated, worker):
        now = int(self._clock())
        stale, ack = False, None
        with self._store.write() as tx:
            held = self._work.held(tx, candidate_key, extractor, str(lease_token), now)
            if held is None:
                replay = self._work.replay(tx, candidate_key, extractor, version, validated.digest)
                if replay:  # the same result already committed (a retried request): acknowledge it again, change nothing
                    return SubmitAck(PROCESSING_SCHEMA, candidate_key, replay["extraction_id"], replay["status"], "recorded" if replay["event_key"] is None else "unchanged",
                                     replay["event_key"], replay["revision"], replayed=True)
            elif held["current_hash"] != held["claimed_hash"]:
                stale = True
                self._work.release(tx, held["cid"], extractor, now)  # committed with the block: the item is pending again at once
            else:  # the lease is held and the evidence is the revision the work was leased at: store everything together
                extraction_id, replayed = self._work.store_extraction(tx, held["cid"], None, held["claimed_hash"], extractor, version, validated.status, validated.failure,
                                                                      validated.result, validated.digest, worker, now)
                event_key = revision = None
                outcome = "recorded"
                if validated.event is not None:
                    event_id, event_key, outcome, revision = decide_and_apply(tx, self._events, self._deduplicator, held["cid"], candidate_key, validated.event, extraction_id, now)
                    tx.execute("UPDATE extractions SET event_id=? WHERE extraction_id=?", (event_id, extraction_id))
                    if outcome in ("created", "updated"):
                        self._changes.append(tx, "event", event_key, validated.digest, now, kind="event.created" if outcome == "created" else "event.updated")
                self._work.done(tx, held["cid"], extractor, held["claimed_hash"], version, now)
                ack = SubmitAck(PROCESSING_SCHEMA, candidate_key, extraction_id, validated.status, outcome, event_key, revision, replayed=replayed)
        if held is None:
            raise LeaseLost("the lease on this work is not held (it expired, was taken over, or was already used)")
        if stale:
            raise StaleResult("the candidate changed after this work was leased; the result was made from an older revision and was discarded")
        return ack

    def fail_work(self, candidate_key, extractor, version, lease_token, error, config):
        now = int(self._clock())
        with self._store.write() as tx:
            held = self._work.held(tx, candidate_key, extractor, str(lease_token), now)
            if held is None:
                raise LeaseLost("the lease on this work is not held")
            return self._work.failed(tx, held, version, str(error or "failed"), now, config)

    def status(self, extractor, version):
        now = int(self._clock())
        with self._store.read() as connection:
            counts = self._work.counts(connection, extractor, version, now)
            latest = self._changes.latest(connection)
        return ProcessingStatus(PROCESSING_SCHEMA, extractor, version, latest_seq=latest, **counts)

    def _item(self, connection, token, until, row, config):
        evidence = tuple({"field": r["field"], "kind": r["kind"], "quote": r["quote"], "url_key": r["url_key"], "content_hash": r["content_hash"], "note": r["note"]}
                         for r in connection.execute("SELECT e.*, p.url_key FROM evidence e LEFT JOIN pages p ON p.page_id = e.page_id WHERE e.candidate_id=? ORDER BY e.evidence_id",
                                                     (row["candidate_id"],)))
        source_key = connection.execute("SELECT source_key FROM sources WHERE source_id=?", (row["source_id"],)).fetchone()[0]
        page = None
        if row["page_id"] is not None:
            record = connection.execute("SELECT url_key, url, media_type, content_hash FROM pages WHERE page_id=?", (row["page_id"],)).fetchone()
            if record and record["content_hash"]:
                body = self._pages.body(connection, record["content_hash"])
                text = evidence_text.visible_text(body.decode("utf-8", "replace")) if body is not None and "html" in (record["media_type"] or "html") else None
                page = {"url_key": record["url_key"], "url": record["url"], "media_type": record["media_type"], "text": (text or "")[:config.page_text_chars]}
        return WorkItem(token, until, row["candidate_key"], source_key, row["payload_hash"], json.loads(row["payload_json"]), evidence, page)

