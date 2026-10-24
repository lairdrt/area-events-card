"""Test helpers: standard candidate records, and stored candidate documents, written the way the service stores them."""
import json
from datetime import datetime

from discovery.service import producer
from discovery.service.producer import EventCandidate

START = "2030-05-01T18:00:00-07:00"


def record(local_id="x1", title="Harbor Concert", starts_at=START, **fields):
    """A standard candidate with the facts a test cares about; the rest stays unsaid."""
    return EventCandidate(local_id=local_id, title=title, starts_at=starts_at, **fields)


def document(title="Harbor Concert", **fields):
    """A stored candidate document (a dictionary of the standard fields) with typical facts; `fields` override them. Used to store candidates the boundary would not let through
    (no title, no start, a start without an offset) so that extraction's own refusals can be tested on data that is already stored."""
    base = {"schema": producer.SCHEMA, "local_id": "x", "title": title, "starts_at": START, "ends_at": "2030-05-01T20:00:00-07:00", "timezone": "America/Los_Angeles",
            "venue": "Harbor Hall", "address": "500 Harbor Drive, San Diego, CA", "description": "An evening concert.", "url": "https://www.sandiego.gov/event/harbor",
            "image_url": None, "image_source": None, "categories": [], "organizer": None, "price": None, "geo": None,
            "provenance": {"source_name": "Test", "listing_url": None, "retrieved_at": None, "found_on": []},
            "verification": {"status": "unverified", "evidence": ["listing date line parsed"], "missing": [], "note": "", "verified": False}}
    base.update(fields)
    return base


def epoch(value):
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except (TypeError, ValueError):
        return None


def put_document(tx, key, source_id, page_id, doc, now, state=None):
    """Store a candidate document exactly as given (insert, or update when the key exists). Returns the candidate id."""
    text, digest = json.dumps(doc, ensure_ascii=False), producer.canonical(doc)
    tx.execute("""INSERT INTO candidates (candidate_key, source_id, page_id, method, extractor, title, starts_at, payload_json, payload_hash, state_json, first_seen_at, last_seen_at, updated_at)
                  VALUES (?, ?, ?, 'adapter', 'test', ?, ?, ?, ?, ?, ?, ?, ?)
                  ON CONFLICT(candidate_key) DO UPDATE SET page_id=excluded.page_id, title=excluded.title, starts_at=excluded.starts_at, payload_json=excluded.payload_json,
                    payload_hash=excluded.payload_hash, last_seen_at=excluded.last_seen_at, updated_at=excluded.updated_at""",
               (key, source_id, page_id, doc.get("title"), epoch(doc.get("starts_at")), text, digest, json.dumps(state or {}), now, now, now))
    return digest
