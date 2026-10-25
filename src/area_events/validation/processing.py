"""Event processing: the Processing contract (`discovery.processing.v1`) and the validation and normalization of what a worker hands back.

An independent worker takes saved candidates (claim), extracts an event from each (extraction.py), and submits the result. This module owns the rules of that exchange: what a claim
may ask for, what a result may contain, and how a result becomes a CLEAN EVENT: validated, with its location status, missing facts and completeness derived here. It
holds no SQL and no database: leases, extractions, deduplication and writes happen in Persistence, behind the ProcessingStorage port, in one atomic operation per submission.

Work is derived, not queued: a candidate needs processing by an extractor version when it has no completed result for its CURRENT payload hash at that version. So a source
revision, or a better extractor, makes the work reappear on its own, and a crash loses nothing.

    claim    lease pending candidates to the worker (oldest first, retries only when due, no live lease) with a fresh token; return each candidate, its evidence and the stored
             text of its page (the website is never fetched again)
    submit   validate the result, then Persistence: check the lease and that the candidate still has the leased payload hash (a result made from an older revision is refused and never
             overwrites newer evidence), store the extraction, deduplicate, create or update the event and its revision, mark the work done, append the change record
    fail     the worker could not process the item: bounded retries with growing waits; after the last attempt the item waits until its source revision or the extractor version changes
"""
import hashlib
import json
import types
import uuid

from ..contracts.models import (PROCESSING_SCHEMA, CleanEvent, ProcessingConfig, ProcessingStatus, StaleResult, ValidatedResult, ValidationFailed, WorkBatch, LeaseLost)  # noqa: F401
from ..persistence.ports import ProcessingStorage

MAX_RESULT_BYTES = 60_000
MAX_TEXT = 4000
EVENT_FIELDS = ("title", "description", "starts_at", "ends_at", "timezone", "venue_name", "location_text", "address", "source_url")
SHORT_FIELDS = types.MappingProxyType({"timezone": 64, "venue_name": 300, "location_text": 600, "address": 300, "source_url": 500, "title": 300})
EVIDENCE_KEYS = ("field", "kind", "quote", "url_key", "content_hash", "note")


def result_hash(result):
    return hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def location_status(fields):
    if fields.get("address"):
        return "address"
    if fields.get("venue_name"):
        return "venue"
    return "description_only" if fields.get("location_text") else "unknown"


def missing_fields(fields):
    missing = []
    if fields.get("ends_at") is None:
        missing.append("ends_at")
    if not fields.get("description"):
        missing.append("description")
    if not (fields.get("address") or fields.get("venue_name") or fields.get("location_text")):
        missing.append("location")
    return missing


class ProcessingService:
    """The Processing contract: validates what comes in, and asks Persistence (the ProcessingStorage port) for everything that touches the database."""

    def __init__(self, storage: ProcessingStorage, clock, config=ProcessingConfig()):
        self._storage, self._clock, self._config = storage, clock, config

    def claim(self, worker, extractor, version, limit=5, lease_s=300, new_token=lambda: uuid.uuid4().hex):
        self._check_name(worker, "worker")
        self._check_name(extractor, "extractor")
        version, limit, lease_s = self._int(version, 1, 1000, "version"), self._int(limit, 1, self._config.max_batch, "limit"), self._int(lease_s, 10, self._config.max_lease_s, "lease_s")
        return WorkBatch(PROCESSING_SCHEMA, self._storage.claim_work(worker, extractor, version, limit, lease_s, new_token, self._config))

    def submit(self, candidate_key, extractor, version, lease_token, result, worker=None):
        self._check_name(extractor, "extractor")
        version = self._int(version, 1, 1000, "version")
        return self._storage.submit_result(candidate_key, extractor, version, lease_token, self.validate(result), worker)

    def fail(self, candidate_key, extractor, version, lease_token, error):
        version = self._int(version, 1, 1000, "version")
        return {"schema": PROCESSING_SCHEMA, "state": self._storage.fail_work(candidate_key, extractor, version, lease_token, str(error or "failed"), self._config)}

    def status(self, extractor, version=1):
        return self._storage.status(extractor, version)

    # --- validation and normalization ----------------------------------------------------------------------------------

    @staticmethod
    def _int(value, low, high, name):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValidationFailed(f"{name} must be an integer from {low} to {high}")
        return value

    @staticmethod
    def _check_name(value, name):
        if not isinstance(value, str) or not 1 <= len(value) <= 80 or not value.isprintable():
            raise ValidationFailed(f"{name} must be 1 to 80 printable characters")

    def validate(self, result):
        """A ValidatedResult: the clean event (or None for a failed extraction), its status and failure reason. The service recomputes status, location status and missing fields itself."""
        event, status, failure = self._validate(result)
        return ValidatedResult(event, status, failure, result, result_hash(result))

    def _validate(self, result):
        """(clean event or None, status, failure reason)."""
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
        missing = missing_fields(clean)
        status = "partial" if missing else "complete"
        return CleanEvent(clean["title"], clean["starts_at"], clean["ends_at"], clean["timezone"], clean["description"], clean["venue_name"], clean["location_text"], clean["address"],
                          clean["source_url"], location_status(clean), tuple(missing), status), status, None
