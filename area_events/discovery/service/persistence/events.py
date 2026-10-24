"""Normalized events in the database: the SQL of events, their candidate links and revisions; the lookup deduplication is given; the application of a deduplication decision;
and the read-only event queries.

Persistence does not decide what a duplicate is. A deduplication strategy is handed a `_Lookup` (a read-only view onto the events as they are INSIDE the caller's transaction,
holding the transaction privately) and answers with a `Decision`; `apply_decision` checks that the decision is one this candidate's state allows and then writes exactly what it
says. The lookup, the decision and the write all happen inside the single write transaction of the caller, so two submissions can never both decide "no twin yet".
"""
import hashlib
import json

from ..contracts import CleanEvent, DecisionRejected, NotFound
from ..deduplication import ACTIONS, Association, Decision, ExistingEvent, Incoming, LOOKUP_PAGE_SIZE
from ..events import EVENTS_SCHEMA, MAX_PAGE, EventDetail, EventPage, EventQuery, EventRecord

FIELDS = ("title", "description", "starts_at", "ends_at", "timezone", "venue_name", "location_text", "address", "location_status", "source_url", "status")


def event_key_for(candidate_key):
    return "ev-" + hashlib.sha256(candidate_key.encode()).hexdigest()[:20]


class EventRepository:
    """The SQL of events. Write methods take the caller's transaction."""

    def __init__(self, media=None):
        self._media = media   # MediaRepository (optional): where an event's imagery is read from

    # --- what deduplication may see ------------------------------------------------------------------------------------

    @staticmethod
    def _existing(row):
        return ExistingEvent(row["event_key"], row["title"], row["starts_at"], row["ends_at"], row["venue_name"], row["address"], row["location_text"], row["revision"])

    def association(self, tx, candidate_id):
        row = tx.execute("SELECT e.event_key, l.role FROM event_candidates l JOIN events e USING (event_id) WHERE l.candidate_id=?", (candidate_id,)).fetchone()
        return Association(row["event_key"], row["role"]) if row else None

    def between(self, tx, earliest, latest, after=None, limit=LOOKUP_PAGE_SIZE):
        sql = "SELECT event_key, title, starts_at, ends_at, venue_name, address, location_text, revision FROM events WHERE starts_at BETWEEN ? AND ?"
        params = [int(earliest), int(latest)]
        if after is not None:
            sql += " AND (starts_at > ? OR (starts_at = ? AND event_key > ?))"
            params.extend((int(after[0]), int(after[0]), str(after[1])))
        sql += " ORDER BY starts_at, event_key LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return tuple(self._existing(r) for r in tx.execute(sql, params))

    # --- writes (inside the processing transaction) --------------------------------------------------------------------

    def create(self, tx, candidate_id, candidate_key, clean, extraction_id, now):
        event_key = event_key_for(candidate_key)
        event_id = tx.execute("""INSERT INTO events (event_key, title, description, starts_at, ends_at, timezone, venue_name, location_text, address,
                                   location_status, source_url, status, missing_json, revision, extraction_id, first_extracted_at, updated_at)
                                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?) RETURNING event_id""",
                              (event_key, clean.title, clean.description, clean.starts_at, clean.ends_at, clean.timezone, clean.venue_name, clean.location_text,
                               clean.address, clean.location_status, clean.source_url, clean.status, json.dumps(list(clean.missing)), extraction_id, now, now)).fetchone()[0]
        tx.execute("INSERT INTO event_candidates (candidate_id, event_id, role, linked_at) VALUES (?, ?, 'primary', ?)", (candidate_id, event_id, now))
        tx.execute("INSERT INTO event_revisions (event_id, revision, extraction_id, changed_json, created_at) VALUES (?, 1, ?, '{}', ?)", (event_id, extraction_id, now))
        return event_id, event_key, 1

    def link_duplicate(self, tx, candidate_id, event_key, now):
        row = tx.execute("SELECT event_id, event_key, revision FROM events WHERE event_key=?", (event_key,)).fetchone()
        if row is None:
            return None
        tx.execute("INSERT INTO event_candidates (candidate_id, event_id, role, linked_at) VALUES (?, ?, 'duplicate', ?)", (candidate_id, row["event_id"], now))
        return row["event_id"], row["event_key"], row["revision"]

    def row_of(self, tx, event_key):
        return tx.execute("SELECT * FROM events WHERE event_key=?", (event_key,)).fetchone()

    def update(self, tx, event_key, clean, extraction_id, now):
        """The primary description changed (or was processed again): revise the event if any fact differs. Returns (event_id, event_key, 'updated' | 'unchanged', revision)."""
        event = self.row_of(tx, event_key)
        values = {"title": clean.title, "description": clean.description, "starts_at": clean.starts_at, "ends_at": clean.ends_at, "timezone": clean.timezone,
                  "venue_name": clean.venue_name, "location_text": clean.location_text, "address": clean.address, "location_status": clean.location_status,
                  "source_url": clean.source_url, "status": clean.status}
        changed = {name: [event[name], values[name]] for name in FIELDS if event[name] != values[name]}
        if json.loads(event["missing_json"]) != list(clean.missing):
            changed["missing"] = [json.loads(event["missing_json"]), list(clean.missing)]
        if not changed:
            return event["event_id"], event["event_key"], "unchanged", event["revision"]
        revision = event["revision"] + 1
        tx.execute("""UPDATE events SET title=?, description=?, starts_at=?, ends_at=?, timezone=?, venue_name=?, location_text=?, address=?, location_status=?,
                      source_url=?, status=?, missing_json=?, revision=?, extraction_id=?, updated_at=? WHERE event_id=?""",
                   (clean.title, clean.description, clean.starts_at, clean.ends_at, clean.timezone, clean.venue_name, clean.location_text, clean.address,
                    clean.location_status, clean.source_url, clean.status, json.dumps(list(clean.missing)), revision, extraction_id, now, event["event_id"]))
        tx.execute("INSERT INTO event_revisions (event_id, revision, extraction_id, changed_json, created_at) VALUES (?, ?, ?, ?, ?)",
                   (event["event_id"], revision, extraction_id, json.dumps(changed, ensure_ascii=False), now))
        return event["event_id"], event["event_key"], "updated", revision

    def delete_before(self, tx, cutoff):
        """Events that started before `cutoff`, removed with their revisions and links; returns their keys."""
        keys = [r[0] for r in tx.execute("SELECT event_key FROM events WHERE starts_at < ?", (cutoff,))]
        tx.execute("DELETE FROM events WHERE starts_at < ?", (cutoff,))
        return keys

    # --- reads ---------------------------------------------------------------------------------------------------------

    def record(self, connection, row):
        sources = tuple(r[0] for r in connection.execute("""SELECT DISTINCT s.source_key FROM event_candidates ec JOIN candidates c USING (candidate_id)
                                                           JOIN sources s ON s.source_id = c.source_id WHERE ec.event_id=? ORDER BY 1""", (row["event_id"],)))
        candidates = tuple(r[0] for r in connection.execute("""SELECT c.candidate_key FROM event_candidates ec JOIN candidates c USING (candidate_id)
                                                              WHERE ec.event_id=? ORDER BY ec.role DESC, c.candidate_key""", (row["event_id"],)))
        media, illustration = self._media.for_event(connection, row["event_id"]) if self._media is not None else (None, None)
        return EventRecord(row["event_key"], row["title"], row["description"], row["starts_at"], row["ends_at"], row["timezone"], row["venue_name"],
                           row["location_text"], row["address"], row["location_status"], row["source_url"], row["status"], tuple(json.loads(row["missing_json"])),
                           row["revision"], row["updated_at"], sources, candidates, media, illustration)

    def query(self, connection, query, after):
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
        if after:
            clauses.append("(e.starts_at, e.event_id) > (?, ?)")
            params.extend(after)
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


class _Lookup:
    """What deduplication sees of the existing events: read-only, bound to one transaction held privately, valid for one decision."""

    def __init__(self, tx, events):
        self.__tx, self.__events = tx, events

    def starting_between(self, earliest, latest, after=None, limit=LOOKUP_PAGE_SIZE):
        return self.__events.between(self.__tx, int(earliest), int(latest), after, limit)


def decide_and_apply(tx, events, deduplicator, candidate_id, candidate_key, clean: CleanEvent, extraction_id, now):
    """Within the caller's write transaction: show the strategy the candidate and the existing events, take its decision, check it, and write exactly that. Returns
    (event_id, event_key, outcome, revision) with outcome 'created' | 'updated' | 'unchanged' | 'duplicate'."""
    association = events.association(tx, candidate_id)
    decision = deduplicator.decide(Incoming(candidate_key, clean, association), _Lookup(tx, events))
    if not isinstance(decision, Decision) or decision.action not in ACTIONS:
        raise DecisionRejected(f"{getattr(deduplicator, 'name', 'the deduplicator')} answered with something that is not a decision")
    if decision.action == "create":
        if association is not None:
            raise DecisionRejected("create: the candidate already belongs to an event")
        event_id, event_key, revision = events.create(tx, candidate_id, candidate_key, clean, extraction_id, now)
        return event_id, event_key, "created", revision
    if decision.action == "link":
        if association is not None:
            raise DecisionRejected("link: the candidate already belongs to an event")
        linked = events.link_duplicate(tx, candidate_id, decision.event_key, now) if decision.event_key else None
        if linked is None:
            raise DecisionRejected(f"link: no event '{decision.event_key}'")
        return linked[0], linked[1], "duplicate", linked[2]
    if association is None or decision.event_key != association.event_key:
        raise DecisionRejected(f"{decision.action}: the candidate is not described by event '{decision.event_key}'")
    if decision.action == "update":
        if association.role != "primary":
            raise DecisionRejected("update: only a primary description may revise its event")
        return events.update(tx, association.event_key, clean, extraction_id, now)
    row = events.row_of(tx, association.event_key)   # keep
    return row["event_id"], row["event_key"], "duplicate", row["revision"]


class SqliteEventQueries:
    """The EventQueries port: read-only transactions over normalized events; never crawls, extracts or writes."""

    def __init__(self, store, events, areas, sources, changes):
        self._store, self._events, self._areas, self._sources, self._changes = store, events, areas, sources, changes

    def query(self, query, after):
        with self._store.read() as connection:
            if query.area_key and self._areas.row_by_key(connection, query.area_key) is None:
                raise NotFound("no such area")
            if query.source_key and self._sources.by_key(connection, query.source_key) is None:
                raise NotFound("no such source")
            rows, more = self._events.query(connection, EventQuery(query.area_key, query.source_key, query.starts_from, query.starts_to, None, query.limit), after)
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

