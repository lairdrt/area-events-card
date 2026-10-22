"""The Processing contract (`discovery.processing.v1`): how an independent worker takes saved candidates, hands back results, and how the
database-owning service records them. The worker never touches SQLite; `LocalProcessing` is the in-process implementation and the API
exposes the same calls to a remote worker.

Work is derived, not queued: a candidate needs processing by an extractor version when it has no completed result for its CURRENT
payload hash at that version. So a source revision, or a better extractor, makes the work reappear on its own, and a crash loses nothing.

    claim    one write transaction: choose pending candidates (oldest first, retries only when due, no live lease), lease each to the
             worker with a fresh token, remember the payload hash it was leased at, and return the candidate, its evidence and the
             stored text of its page (the website is never fetched again)
    submit   one write transaction: check the lease, check the candidate still has the leased payload hash (a result made from an older
             revision is refused and never overwrites newer evidence), validate the result, store the extraction, create or update the
             event and its revision, mark the work done and append the change record. All of it commits together or none of it does.
    fail     the worker could not process the item: bounded retries with growing waits; after the last attempt the item waits until its
             source revision or the extractor version changes.
"""
import hashlib
import json
import types
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .. import evidence as evidence_text
from .control import ControlError, ValidationFailed
from .events import location_status, missing_fields

PROCESSING_SCHEMA = "discovery.processing.v1"
MAX_RESULT_BYTES = 60_000
MAX_TEXT = 4000
EVENT_FIELDS = ("title", "description", "starts_at", "ends_at", "timezone", "venue_name", "location_text", "address", "source_url")
SHORT_FIELDS = types.MappingProxyType({"timezone": 64, "venue_name": 300, "location_text": 600, "address": 300, "source_url": 500, "title": 300})
EVIDENCE_KEYS = ("field", "kind", "quote", "url_key", "content_hash", "note")


class LeaseLost(ControlError):
    code, status = "lease_lost", 409


class StaleResult(ControlError):
    code, status = "stale_result", 409


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


def result_hash(result):
    return hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


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


class LocalProcessing:
    """The Processing contract over the local database (the database-owning service's side)."""

    def __init__(self, store, work, events, candidates, pages, changes, clock, config=ProcessingConfig()):
        self._store, self._work, self._events, self._candidates, self._pages = store, work, events, candidates, pages
        self._changes, self._clock, self._config = changes, clock, config

    def claim(self, worker, extractor, version, limit=5, lease_s=300, new_token=lambda: uuid.uuid4().hex):
        self._check_name(worker, "worker")
        self._check_name(extractor, "extractor")
        version, limit, lease_s = self._int(version, 1, 1000, "version"), self._int(limit, 1, self._config.max_batch, "limit"), self._int(lease_s, 10, self._config.max_lease_s, "lease_s")
        now = int(self._clock())
        leased = []
        with self._store.write() as tx:
            self._work.expire(tx, now)
            for row in self._work.pending(tx, extractor, version, now, limit):
                token = new_token()
                self._work.lease(tx, row["candidate_id"], extractor, worker, token, row["payload_hash"], now + lease_s, now)
                leased.append((token, row))
            items = tuple(self._item(tx, token, now + lease_s, row) for token, row in leased)
        return WorkBatch(PROCESSING_SCHEMA, items)

    def submit(self, candidate_key, extractor, version, lease_token, result, worker=None):
        self._check_name(extractor, "extractor")
        version = self._int(version, 1, 1000, "version")
        fields, status, failure = self._validate(result)
        digest = result_hash(result)
        now = int(self._clock())
        stale, ack = False, None
        with self._store.write() as tx:
            held = self._work.held(tx, candidate_key, extractor, str(lease_token), now)
            if held is None:
                replay = self._work.replay(tx, candidate_key, extractor, version, digest)
                if replay:  # the same result already committed (a retried request): acknowledge it again, change nothing
                    return SubmitAck(PROCESSING_SCHEMA, candidate_key, replay["extraction_id"], replay["status"], "recorded" if replay["event_key"] is None else "unchanged",
                                     replay["event_key"], replay["revision"], replayed=True)
            elif held["current_hash"] != held["claimed_hash"]:
                stale = True
                self._work.release(tx, held["cid"], extractor, now)  # committed with the block: the item is pending again at once
            else:  # the lease is held and the evidence is the revision the work was leased at: store everything together
                extraction_id, replayed = self._work.store_extraction(tx, held["cid"], None, held["claimed_hash"], extractor, version, status, failure, result, digest,
                                                                      worker, now)
                event_key = revision = None
                outcome = "recorded"
                if fields is not None:
                    event_id, event_key, outcome, revision = self._events.apply(tx, held["cid"], candidate_key, fields, extraction_id, now)
                    tx.execute("UPDATE extractions SET event_id=? WHERE extraction_id=?", (event_id, extraction_id))
                    if outcome in ("created", "updated"):
                        self._changes.append(tx, "event", event_key, digest, now, kind="event.created" if outcome == "created" else "event.updated")
                self._work.done(tx, held["cid"], extractor, held["claimed_hash"], version, now)
                ack = SubmitAck(PROCESSING_SCHEMA, candidate_key, extraction_id, status, outcome, event_key, revision, replayed=replayed)
        if held is None:
            raise LeaseLost("the lease on this work is not held (it expired, was taken over, or was already used)")
        if stale:
            raise StaleResult("the candidate changed after this work was leased; the result was made from an older revision and was discarded")
        return ack

    def fail(self, candidate_key, extractor, version, lease_token, error):
        version = self._int(version, 1, 1000, "version")
        now = int(self._clock())
        with self._store.write() as tx:
            held = self._work.held(tx, candidate_key, extractor, str(lease_token), now)
            if held is None:
                raise LeaseLost("the lease on this work is not held")
            return {"schema": PROCESSING_SCHEMA, "state": self._work.failed(tx, held, version, str(error or "failed"), now, self._config)}

    def status(self, extractor, version=1):
        now = int(self._clock())
        with self._store.read() as connection:
            counts = self._work.counts(connection, extractor, version, now)
            latest = self._changes.latest(connection)
        return ProcessingStatus(PROCESSING_SCHEMA, extractor, version, latest_seq=latest, **counts)

    # --- internals --------------------------------------------------------------------------------------------------

    def _item(self, connection, token, until, row):
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
                page = {"url_key": record["url_key"], "url": record["url"], "media_type": record["media_type"], "text": (text or "")[:self._config.page_text_chars]}
        return WorkItem(token, until, row["candidate_key"], source_key, row["payload_hash"], json.loads(row["payload_json"]), evidence, page)

    @staticmethod
    def _int(value, low, high, name):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValidationFailed(f"{name} must be an integer from {low} to {high}")
        return value

    @staticmethod
    def _check_name(value, name):
        if not isinstance(value, str) or not 1 <= len(value) <= 80 or not value.isprintable():
            raise ValidationFailed(f"{name} must be 1 to 80 printable characters")

    def _validate(self, result):
        """(event fields or None, status, failure reason). The service recomputes status, location status and missing fields itself."""
        if not isinstance(result, dict) or len(json.dumps(result, default=str)) > MAX_RESULT_BYTES:
            raise ValidationFailed("the result must be a JSON object of at most 60 KB")
        unknown = sorted(set(result) - {"event", "failure_reason", "fields", "issues"})
        if unknown:
            raise ValidationFailed(f"unknown result fields: {', '.join(unknown)[:100]}")
        for name in ("issues",):
            values = result.get(name, [])
            if not isinstance(values, list) or len(values) > 20 or any(not isinstance(v, str) or len(v) > 100 for v in values):
                raise ValidationFailed(f"{name} must be a list of up to 20 short strings")
        fields = result.get("fields", {})
        if not isinstance(fields, dict) or len(fields) > 20:
            raise ValidationFailed("fields must be an object of up to 20 entries")
        for name, entry in fields.items():
            refs = entry.get("evidence") if isinstance(entry, dict) else None
            if (not isinstance(name, str) or not isinstance(entry, dict) or set(entry) - {"origin", "evidence"} or not isinstance(entry.get("origin", ""), str)
                    or not isinstance(refs, list) or len(refs) > 10):
                raise ValidationFailed("fields must map names to {origin, evidence[]}")
            for ref in refs:
                if not isinstance(ref, dict) or set(ref) - set(EVIDENCE_KEYS) or any(v is not None and not isinstance(v, str) or (isinstance(v, str) and len(v) > 400) for v in ref.values()):
                    raise ValidationFailed("an evidence reference has unexpected content")
        event = result.get("event")
        if event is None:
            reason = result.get("failure_reason")
            if not isinstance(reason, str) or not 1 <= len(reason) <= 300:
                raise ValidationFailed("a result without an event needs a failure_reason of up to 300 characters")
            return None, "failed", reason
        if not isinstance(event, dict) or set(event) - set(EVENT_FIELDS):
            raise ValidationFailed("the event has unexpected fields")
        clean = {}
        for name in EVENT_FIELDS:
            value = event.get(name)
            if value is None:
                clean[name] = None
            elif name in ("starts_at", "ends_at"):
                if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 4_102_444_800:
                    raise ValidationFailed(f"{name} must be a UTC epoch in seconds")
                clean[name] = value
            elif not isinstance(value, str) or len(value) > (MAX_TEXT if name == "description" else SHORT_FIELDS[name]):
                raise ValidationFailed(f"{name} is not a string of allowed length")
            else:
                clean[name] = value.strip() or None
        if not clean["title"] or clean["starts_at"] is None:
            raise ValidationFailed("an event needs a title and a start")
        if clean["ends_at"] is not None and clean["ends_at"] < clean["starts_at"]:
            raise ValidationFailed("the end is before the start")
        if clean["source_url"] and not clean["source_url"].startswith(("http://", "https://")):
            raise ValidationFailed("source_url must be a web address")
        clean["location_status"] = location_status(clean)
        clean["missing"] = missing_fields(clean)
        clean["status"] = "partial" if clean["missing"] else "complete"
        return clean, clean["status"], None
