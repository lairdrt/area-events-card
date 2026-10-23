"""Normalized events in the discovery database, and the read-only EventQueryService over them.

An event is one real-world occurrence. A candidate is one source's statement about it. Events are not copied per area or per card:
an area reaches an event through the sources that area uses (`area_sources`), so overlapping areas and many cards read the same
rows. An event has no coordinates until a geolocation component supplies them; a query by area is therefore by the sources that
serve the area, and every event states how its location is known (`location_status`) instead of pretending to be placed on a map.

`EventRepository` holds the SQL (its write methods are used only inside the processing transaction); `EventQueryService` is the
read contract (`discovery.events.v1`): each call opens its own read-only transaction and returns plain records.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional

from .control import NotFound, ValidationFailed

EVENTS_SCHEMA = "discovery.events.v1"
MAX_PAGE = 200
FIELDS = ("title", "description", "starts_at", "ends_at", "timezone", "venue_name", "location_text", "address", "location_status", "source_url", "status")


@dataclass(frozen=True)
class EventRecord:
    event_key: str
    title: str
    description: Optional[str]
    starts_at: int
    ends_at: Optional[int]
    timezone: Optional[str]
    venue_name: Optional[str]
    location_text: Optional[str]
    address: Optional[str]
    location_status: str            # 'address' | 'venue' | 'description_only' | 'unknown': how the location is known; never a coordinate
    source_url: Optional[str]
    status: str                     # 'complete' | 'partial'
    missing: tuple
    revision: int
    updated_at: int
    sources: tuple = ()
    candidates: tuple = ()
    media: Optional[dict] = None            # {"image", "photo", "map"} with credits, from the media enrichment service; absent until it has run
    illustration: Optional[dict] = None     # the category illustration decision (category, strength, reasons)


@dataclass(frozen=True)
class EventDetail:
    schema: str
    event: EventRecord
    history: tuple                  # one dict per revision: revision, extraction_id, extractor, version, created_at, changed {field: [old, new]}
    evidence: dict                  # field -> {"origin", "evidence": [...]} from the extraction behind the current values
    candidate_links: tuple          # dicts: candidate_key, source_key, role


@dataclass(frozen=True)
class EventQuery:
    area_key: Optional[str] = None
    source_key: Optional[str] = None
    starts_from: Optional[int] = None
    starts_to: Optional[int] = None
    after: Optional[str] = None
    limit: int = 50


@dataclass(frozen=True)
class EventPage:
    schema: str
    events: tuple
    next_cursor: Optional[str]
    latest_seq: int                 # the change feed's position when the page was read: events changed later appear in /v1/changes


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def match_key(title, starts_at, address):
    """Identity for merging two candidates into one event: the same title, start and street address. Without an address there is no match."""
    if not address:
        return None
    return hashlib.sha256(f"{_norm(title)}|{starts_at}|{_norm(address)}".encode()).hexdigest()


def event_key_for(candidate_key):
    return "ev-" + hashlib.sha256(candidate_key.encode()).hexdigest()[:20]


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


class EventRepository:
    def __init__(self, media=None):
        self._media = media   # MediaRepository (optional): where an event's imagery is read from

    # --- writes (inside the processing transaction) ------------------------------------------------------------------

    def apply(self, tx, candidate_id, candidate_key, fields, extraction_id, now):
        """Create or update the event for a candidate's extraction. Returns (event_id, event_key, outcome, revision) where outcome is
        'created' | 'updated' | 'unchanged' | 'duplicate' (a second candidate for an event it did not define: linked, event untouched)."""
        link = tx.execute("SELECT event_id, role FROM event_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        key = match_key(fields["title"], fields["starts_at"], fields.get("address"))
        if link is None:
            twin = tx.execute("SELECT event_id, event_key, revision FROM events WHERE match_key=? ORDER BY event_id LIMIT 1", (key,)).fetchone() if key else None
            if twin is not None:
                tx.execute("INSERT INTO event_candidates (candidate_id, event_id, role, linked_at) VALUES (?, ?, 'duplicate', ?)", (candidate_id, twin["event_id"], now))
                return twin["event_id"], twin["event_key"], "duplicate", twin["revision"]
            event_key = event_key_for(candidate_key)
            event_id = tx.execute("""INSERT INTO events (event_key, match_key, title, description, starts_at, ends_at, timezone, venue_name, location_text, address,
                                       location_status, source_url, status, missing_json, revision, extraction_id, first_extracted_at, updated_at)
                                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?) RETURNING event_id""",
                                  (event_key, key, fields["title"], fields.get("description"), fields["starts_at"], fields.get("ends_at"), fields.get("timezone"),
                                   fields.get("venue_name"), fields.get("location_text"), fields.get("address"), fields["location_status"], fields.get("source_url"),
                                   fields["status"], json.dumps(fields["missing"]), extraction_id, now, now)).fetchone()[0]
            tx.execute("INSERT INTO event_candidates (candidate_id, event_id, role, linked_at) VALUES (?, ?, 'primary', ?)", (candidate_id, event_id, now))
            tx.execute("INSERT INTO event_revisions (event_id, revision, extraction_id, changed_json, created_at) VALUES (?, 1, ?, '{}', ?)", (event_id, extraction_id, now))
            return event_id, event_key, "created", 1
        event = tx.execute("SELECT * FROM events WHERE event_id=?", (link["event_id"],)).fetchone()
        if link["role"] != "primary":
            return event["event_id"], event["event_key"], "duplicate", event["revision"]
        changed = {}
        for name in FIELDS:
            old, new = event[name], fields.get(name)
            if old != new:
                changed[name] = [old, new]
        if json.loads(event["missing_json"]) != fields["missing"]:
            changed["missing"] = [json.loads(event["missing_json"]), fields["missing"]]
        if not changed:
            return event["event_id"], event["event_key"], "unchanged", event["revision"]
        revision = event["revision"] + 1
        tx.execute("""UPDATE events SET match_key=?, title=?, description=?, starts_at=?, ends_at=?, timezone=?, venue_name=?, location_text=?, address=?, location_status=?,
                      source_url=?, status=?, missing_json=?, revision=?, extraction_id=?, updated_at=? WHERE event_id=?""",
                   (key, fields["title"], fields.get("description"), fields["starts_at"], fields.get("ends_at"), fields.get("timezone"), fields.get("venue_name"),
                    fields.get("location_text"), fields.get("address"), fields["location_status"], fields.get("source_url"), fields["status"],
                    json.dumps(fields["missing"]), revision, extraction_id, now, event["event_id"]))
        tx.execute("INSERT INTO event_revisions (event_id, revision, extraction_id, changed_json, created_at) VALUES (?, ?, ?, ?, ?)",
                   (event["event_id"], revision, extraction_id, json.dumps(changed, ensure_ascii=False), now))
        return event["event_id"], event["event_key"], "updated", revision

    def delete_before(self, tx, cutoff):
        """Events that started before `cutoff`, removed with their revisions and links; returns their keys."""
        keys = [r[0] for r in tx.execute("SELECT event_key FROM events WHERE starts_at < ?", (cutoff,))]
        tx.execute("DELETE FROM events WHERE starts_at < ?", (cutoff,))
        return keys

    # --- reads --------------------------------------------------------------------------------------------------------

    def record(self, connection, row):
        sources = tuple(r[0] for r in connection.execute("""SELECT DISTINCT s.source_key FROM event_candidates ec JOIN candidates c USING (candidate_id)
                                                           JOIN sources s ON s.source_id = c.source_id WHERE ec.event_id=? ORDER BY 1""", (row["event_id"],)))
        candidates = tuple(r[0] for r in connection.execute("""SELECT c.candidate_key FROM event_candidates ec JOIN candidates c USING (candidate_id)
                                                              WHERE ec.event_id=? ORDER BY ec.role DESC, c.candidate_key""", (row["event_id"],)))
        media, illustration = self._media.for_event(connection, row["event_id"]) if self._media is not None else (None, None)
        return EventRecord(row["event_key"], row["title"], row["description"], row["starts_at"], row["ends_at"], row["timezone"], row["venue_name"],
                           row["location_text"], row["address"], row["location_status"], row["source_url"], row["status"], tuple(json.loads(row["missing_json"])),
                           row["revision"], row["updated_at"], sources, candidates, media, illustration)

    def query(self, connection, query):
        clauses, params = [], []
        if query.area_key:
            clauses.append("""EXISTS (SELECT 1 FROM event_candidates ec JOIN candidates c USING (candidate_id) JOIN area_sources x ON x.source_id = c.source_id
                                      JOIN areas a ON a.area_id = x.area_id WHERE ec.event_id = e.event_id AND a.area_key = ?)""")
            params.append(query.area_key)
        if query.source_key:
            clauses.append("""EXISTS (SELECT 1 FROM event_candidates ec JOIN candidates c USING (candidate_id) JOIN sources s ON s.source_id = c.source_id
                                      WHERE ec.event_id = e.event_id AND s.source_key = ?)""")
            params.append(query.source_key)
        if query.starts_from is not None:
            clauses.append("e.starts_at >= ?")
            params.append(query.starts_from)
        if query.starts_to is not None:
            clauses.append("e.starts_at < ?")
            params.append(query.starts_to)
        if query.after:
            clauses.append("(e.starts_at, e.event_id) > (?, ?)")
            params.extend(query.after)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(int(query.limit), MAX_PAGE))
        rows = connection.execute(f"SELECT e.* FROM events e {where} ORDER BY e.starts_at, e.event_id LIMIT ?", params + [limit + 1]).fetchall()
        return rows[:limit], len(rows) > limit

    def by_key(self, connection, event_key):
        return connection.execute("SELECT * FROM events WHERE event_key=?", (event_key,)).fetchone()

    def history(self, connection, event_id):
        return tuple({"revision": r["revision"], "extraction_id": r["extraction_id"], "extractor": r["extractor"], "version": r["version"], "created_at": r["created_at"],
                      "changed": json.loads(r["changed_json"])} for r in connection.execute(
            """SELECT r.*, x.extractor, x.version FROM event_revisions r JOIN extractions x USING (extraction_id) WHERE r.event_id=? ORDER BY r.revision""", (event_id,)))

    def evidence(self, connection, extraction_id):
        row = connection.execute("SELECT result_json FROM extractions WHERE extraction_id=?", (extraction_id,)).fetchone()
        return json.loads(row[0]).get("fields", {}) if row else {}

    def links(self, connection, event_id):
        return tuple({"candidate_key": r["candidate_key"], "source_key": r["source_key"], "role": r["role"]} for r in connection.execute(
            """SELECT c.candidate_key, s.source_key, ec.role FROM event_candidates ec JOIN candidates c USING (candidate_id)
               JOIN sources s ON s.source_id = c.source_id WHERE ec.event_id=? ORDER BY ec.role DESC, c.candidate_key""", (event_id,)))


class EventQueryService:
    """The read contract over normalized events. Opens only read-only transactions; never crawls, extracts or writes."""

    def __init__(self, store, events, areas, sources, changes):
        self._store, self._events, self._areas, self._sources, self._changes = store, events, areas, sources, changes

    def query(self, query):
        after = self._cursor(query.after)
        with self._store.read() as connection:
            if query.area_key and self._areas.row_by_key(connection, query.area_key) is None:
                raise NotFound("no such area")
            if query.source_key and self._sources.by_key(connection, query.source_key) is None:
                raise NotFound("no such source")
            rows, more = self._events.query(connection, EventQuery(query.area_key, query.source_key, query.starts_from, query.starts_to, after, query.limit))
            events = tuple(self._events.record(connection, row) for row in rows)
            latest = self._changes.latest(connection)
        return EventPage(EVENTS_SCHEMA, events, f"{rows[-1]['starts_at']}.{rows[-1]['event_id']}" if more and rows else None, latest)

    def get(self, event_key):
        with self._store.read() as connection:
            row = self._events.by_key(connection, event_key)
            if row is None:
                raise NotFound("no such event")
            return EventDetail(EVENTS_SCHEMA, self._events.record(connection, row), self._events.history(connection, row["event_id"]),
                               self._events.evidence(connection, row["extraction_id"]), self._events.links(connection, row["event_id"]))

    @staticmethod
    def _cursor(value):
        if not value:
            return None
        match = re.fullmatch(r"(-?\d{1,12})\.(\d{1,12})", str(value))
        if not match:
            raise ValidationFailed("the cursor is not one this service issued")
        return int(match.group(1)), int(match.group(2))
