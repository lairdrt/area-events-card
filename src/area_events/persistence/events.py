"""Normalized events in the database: the SQL of events, their candidate links and revisions; the lookup deduplication is given; the application of a deduplication decision;
and the read-only event queries.

Persistence does not decide what a duplicate is. A deduplication strategy is handed a `_Lookup` (a read-only view onto the events as they are INSIDE the caller's transaction,
holding the transaction privately) and answers with a `Decision`; `apply_decision` checks that the decision is one this candidate's state allows and then writes exactly what it
says. The lookup, the decision and the write all happen inside the single write transaction of the caller, so two submissions can never both decide "no twin yet".
"""
import hashlib
import json
import os
import sqlite3
import time
from collections import defaultdict
from urllib.parse import quote

from ..contracts.models import CleanEvent, DecisionRejected, NotFound
from ..deduplication import ACTIONS, Association, DEDUP_MAINTENANCE_SCHEMA, Decision, ExistingEvent, Incoming, LOOKUP_PAGE_SIZE
from ..api.events import EVENTS_SCHEMA, MAX_PAGE, EventDetail, EventPage, EventQuery, EventRecord, verification_of

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
        return ExistingEvent(row["event_key"], row["title"], row["starts_at"], row["ends_at"], row["venue_name"], row["address"], row["location_text"], row["revision"], row["source_url"],
                             row["description"], row["timezone"])

    def association(self, tx, candidate_id):
        row = tx.execute("SELECT e.event_key, l.role FROM event_candidates l JOIN events e USING (event_id) WHERE l.candidate_id=?", (candidate_id,)).fetchone()
        return Association(row["event_key"], row["role"]) if row else None

    def between(self, tx, earliest, latest, after=None, limit=LOOKUP_PAGE_SIZE):
        sql = "SELECT event_key, title, starts_at, ends_at, venue_name, address, location_text, revision, source_url, description, timezone FROM events WHERE starts_at BETWEEN ? AND ?"
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
                           row["revision"], row["updated_at"], sources, candidates, media, illustration, self._verification(connection, row))

    _PRIMARY = """SELECT e.event_key, c.candidate_key, c.payload_json FROM events e JOIN event_candidates ec ON ec.event_id = e.event_id AND ec.role = 'primary'
                  JOIN candidates c ON c.candidate_id = ec.candidate_id JOIN extractions x ON x.extraction_id = e.extraction_id AND x.candidate_id = c.candidate_id"""

    @staticmethod
    def _verified_by(rows):
        if len(rows) != 1:
            return verification_of(None)
        try:
            stored = json.loads(rows[0]["payload_json"]).get("verification")
        except (TypeError, ValueError, AttributeError):
            stored = None
        return verification_of(stored, rows[0]["candidate_key"])

    def _verification(self, connection, row):
        """What the producer recorded for the event's current primary candidate, and only when the event's current facts were extracted from that candidate:
        a duplicate's statement, or a candidate the event no longer reflects, is never taken as the event's evidence."""
        return self._verified_by(connection.execute(self._PRIMARY + " WHERE e.event_id = ?", (row["event_id"],)).fetchall())

    def verification_through(self, tx, candidate_key):
        """(event_key, verification) for the event whose verification is read from this candidate, or None: what a restatement of the candidate can change."""
        rows = tx.execute(self._PRIMARY + " WHERE c.candidate_key = ?", (candidate_key,)).fetchall()
        return (rows[0]["event_key"], self._verified_by(rows)) if len(rows) == 1 else None

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


class DuplicateMaintenanceError(ValueError):
    pass


def _maintenance_connection(path, mode):
    absolute = os.path.abspath(path).replace("\\", "/")
    connection = sqlite3.connect(f"file:{quote(absolute, safe='/:')}?mode={mode}", uri=True, isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


class DuplicateMaintenance:
    """Read-only duplicate discovery and transactional reconciliation through the injected deduplicator."""
    MAX_SCAN_EVENTS = 20000
    MAX_ANCHOR_BUCKET = 50
    MAX_PAIR_COMPARISONS = 50000

    def __init__(self, path, matcher, schema_version, clock=time.time):
        if not callable(getattr(matcher, "matches", None)) or not callable(getattr(matcher, "anchors", None)):
            raise DuplicateMaintenanceError(f"duplicate maintenance requires {DEDUP_MAINTENANCE_SCHEMA} (matches and anchors); "
                                            "decide-only discovery.dedup.v1 strategies remain valid for normal processing")
        self.path, self.matcher, self.schema_version, self.clock = os.path.abspath(path), matcher, schema_version, clock

    @staticmethod
    def _facts(row):
        return ExistingEvent(row["event_key"], row["title"], row["starts_at"], row["ends_at"], row["venue_name"], row["address"],
                             row["location_text"], row["revision"], row["source_url"],
                             row["description"], row["timezone"])

    def _eligible_pairs(self, rows):
        anchors = defaultdict(set)
        by_id = {row["event_id"]: row for row in rows}
        for row in rows:
            for field, value in self.matcher.anchors(self._facts(row)):
                anchors[(row["starts_at"], field, value)].add(row["event_id"])
        pairs = set()
        for ids in anchors.values():
            ordered = sorted(ids)
            if len(ordered) > self.MAX_ANCHOR_BUCKET:
                raise DuplicateMaintenanceError("ambiguous high-density event bucket; narrow the scan before cleaning")
            for index, left in enumerate(ordered):
                for right in ordered[index + 1:]:
                    pairs.add((left, right))
                    if len(pairs) > self.MAX_PAIR_COMPARISONS:
                        raise DuplicateMaintenanceError("duplicate scan pair limit exceeded; narrow the scan")
        links = defaultdict(set)
        for left_id, right_id in pairs:
            if self.matcher.matches(self._facts(by_id[left_id]), self._facts(by_id[right_id])):
                links[left_id].add(right_id)
                links[right_id].add(left_id)
        groups, seen = [], set()
        for start in links:
            if start in seen:
                continue
            component, pending = set(), [start]
            while pending:
                item = pending.pop()
                if item in component:
                    continue
                component.add(item)
                pending.extend(links[item] - component)
            seen.update(component)
            group = tuple(by_id[event_id] for event_id in sorted(component))
            groups.append({"event_keys": tuple(row["event_key"] for row in group), "event_ids": tuple(row["event_id"] for row in group),
                           "eligible": len(group) == 2, "reason": "" if len(group) == 2 else "ambiguous match component has more than two events",
                           "events": tuple(dict(row) for row in group)})
        return tuple(sorted(groups, key=lambda group: group["event_ids"]))

    def _scan(self, connection, days=180, limit=100):
        if days < 1 or days > 730 or limit < 1 or limit > 1000:
            raise DuplicateMaintenanceError("days must be 1..730 and limit must be 1..1000")
        now = int(self.clock())
        rows = connection.execute("SELECT * FROM events WHERE starts_at>=? AND starts_at<=? ORDER BY starts_at,event_id LIMIT ?",
                                  (now, now + int(days) * 86400, self.MAX_SCAN_EVENTS + 1)).fetchall()
        if len(rows) > self.MAX_SCAN_EVENTS:
            raise DuplicateMaintenanceError("duplicate scan event limit exceeded; narrow the scan")
        groups = self._eligible_pairs(rows)
        for group in groups:
            ids = group["event_ids"]
            placeholders = ",".join("?" for _ in ids)
            group["candidate_associations"] = connection.execute(
                f"SELECT COUNT(*) FROM event_candidates WHERE event_id IN ({placeholders})", ids).fetchone()[0]
        return groups[:limit]

    def scan(self, days=180, limit=100):
        connection = _maintenance_connection(self.path, "ro")
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] != self.schema_version:
                raise DuplicateMaintenanceError("database schema does not match this maintenance tool; no migration was run")
            return self._scan(connection, days, limit)
        except sqlite3.Error as error:
            raise DuplicateMaintenanceError(f"read-only duplicate scan failed: {error}") from error
        finally:
            connection.close()

    @staticmethod
    def _snapshot(row):
        return {key: row[key] for key in ("event_id", "event_key", "title", "description", "starts_at", "ends_at", "timezone", "venue_name",
                                           "location_text", "address", "location_status", "source_url", "status", "missing_json", "revision",
                                           "extraction_id", "first_extracted_at", "updated_at")}

    def clean(self, selected_groups, days=180, limit=10):
        selected = tuple(tuple(sorted(set(keys))) for keys in selected_groups)
        if not selected or len(selected) > limit or limit < 1 or limit > 100:
            raise DuplicateMaintenanceError("select 1..limit duplicate groups explicitly")
        if any(len(group) != 2 or not all(group) for group in selected) or len(set(selected)) != len(selected):
            raise DuplicateMaintenanceError("each selected group must contain two unique event keys")
        connection = _maintenance_connection(self.path, "rw")
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA user_version").fetchone()[0] != self.schema_version:
                raise DuplicateMaintenanceError("database schema does not match this maintenance tool")
            groups = self._scan(connection, days, 1000)
            available = {tuple(sorted(group["event_keys"])): group for group in groups if group["eligible"]}
            if any(group not in available for group in selected):
                raise DuplicateMaintenanceError("a selected group is no longer an unambiguous duplicate; transaction refused")
            changed = [self._reconcile(connection, available[keys]["events"]) for keys in selected]
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise DuplicateMaintenanceError(f"foreign-key integrity failed ({len(violations)} violations)")
            connection.execute("COMMIT")
            return tuple(changed)
        except BaseException as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if isinstance(error, sqlite3.Error):
                raise DuplicateMaintenanceError(f"duplicate cleanup transaction failed and was rolled back: {error}") from error
            raise
        finally:
            connection.close()

    def _reconcile(self, tx, snapshots):
        keys = tuple(row["event_key"] for row in snapshots)
        placeholders = ",".join("?" for _ in keys)
        rows = tx.execute(f"SELECT * FROM events WHERE event_key IN ({placeholders}) ORDER BY event_id", keys).fetchall()
        if len(rows) != 2 or {row["event_key"] for row in rows} != set(keys):
            raise DuplicateMaintenanceError("selected event identity changed during cleanup")
        current, prior = {row["event_key"]: row for row in rows}, {row["event_key"]: row for row in snapshots}
        if any(self._snapshot(current[key]) != self._snapshot(prior[key]) for key in keys):
            raise DuplicateMaintenanceError("selected event facts changed during cleanup")
        if not self.matcher.matches(self._facts(current[keys[0]]), self._facts(current[keys[1]])):
            raise DuplicateMaintenanceError("selected events no longer match")
        survivor = min(rows, key=lambda row: row["event_id"])
        loser = next(row for row in rows if row["event_id"] != survivor["event_id"])
        latest = max(rows, key=lambda row: (row["updated_at"], row["event_id"]))
        candidate_rows = tx.execute("SELECT candidate_id,role,linked_at FROM event_candidates WHERE event_id=? ORDER BY candidate_id", (survivor["event_id"],)).fetchall()
        loser_candidates = tx.execute("SELECT candidate_id,role,linked_at FROM event_candidates WHERE event_id=? ORDER BY candidate_id", (loser["event_id"],)).fetchall()
        if not candidate_rows or not loser_candidates:
            raise DuplicateMaintenanceError("both events must have candidate ownership; refusing ambiguous reconciliation")
        if any(sum(link["role"] == "primary" for link in links) != 1 for links in (candidate_rows, loser_candidates)):
            raise DuplicateMaintenanceError("each event must have exactly one primary candidate; refusing ambiguous reconciliation")
        revision_rows = tx.execute("SELECT rowid,event_id,revision,extraction_id,changed_json,created_at FROM event_revisions WHERE event_id IN (?,?) ORDER BY created_at,event_id,revision",
                                   (survivor["event_id"], loser["event_id"])).fetchall()
        revisions_by_event = {event["event_id"]: [item["revision"] for item in revision_rows if item["event_id"] == event["event_id"]] for event in rows}
        if any(sorted(revisions_by_event[event["event_id"]]) != list(range(1, event["revision"] + 1)) for event in rows):
            raise DuplicateMaintenanceError("event revision history is incomplete or inconsistent; refusing to discard history")
        now = int(self.clock())
        for link in loser_candidates:
            tx.execute("UPDATE event_candidates SET event_id=? WHERE candidate_id=?", (survivor["event_id"], link["candidate_id"]))
        all_links = tx.execute("SELECT candidate_id,role FROM event_candidates WHERE event_id=? ORDER BY candidate_id", (survivor["event_id"],)).fetchall()
        primaries = [link["candidate_id"] for link in all_links if link["role"] == "primary"]
        primary = primaries[0] if primaries else all_links[0]["candidate_id"]
        tx.execute("UPDATE event_candidates SET role='duplicate' WHERE event_id=?", (survivor["event_id"],))
        tx.execute("UPDATE event_candidates SET role='primary' WHERE event_id=? AND candidate_id=?", (survivor["event_id"], primary))
        tx.execute("UPDATE extractions SET event_id=? WHERE event_id=?", (survivor["event_id"], loser["event_id"]))
        for index, revision in enumerate(revision_rows, 1):
            tx.execute("UPDATE event_revisions SET revision=? WHERE rowid=?", (-index, revision["rowid"]))
        for revision_number, revision in enumerate(revision_rows, 1):
            tx.execute("UPDATE event_revisions SET event_id=?, revision=? WHERE rowid=?", (survivor["event_id"], revision_number, revision["rowid"]))
        fields = ("title", "description", "starts_at", "ends_at", "timezone", "venue_name", "location_text", "address", "location_status",
                  "source_url", "status", "missing_json")
        changed = {field: [survivor[field], latest[field]] for field in fields if survivor[field] != latest[field]}
        revision = len(revision_rows) + 1
        changed["reconciled_from"] = {"event_key": loser["event_key"], "event_id": loser["event_id"], "revision": loser["revision"]}
        assignments = ",".join(f"{field}=?" for field in fields)
        tx.execute(f"UPDATE events SET {assignments},revision=?,extraction_id=?,first_extracted_at=?,updated_at=? WHERE event_id=?",
                   tuple(latest[field] for field in fields) + (revision, latest["extraction_id"], min(row["first_extracted_at"] for row in rows), now, survivor["event_id"]))
        tx.execute("INSERT INTO event_revisions(event_id,revision,extraction_id,changed_json,created_at) VALUES(?,?,?,?,?)",
                   (survivor["event_id"], revision, latest["extraction_id"] or revision_rows[-1]["extraction_id"],
                    json.dumps(changed, ensure_ascii=False, sort_keys=True), now))
        tx.execute("DELETE FROM events WHERE event_id=?", (loser["event_id"],))
        digest = hashlib.sha256(json.dumps(changed, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        tx.execute("INSERT INTO changes(entity,entity_key,op,kind,payload_hash,created_at) VALUES('event',?,'upsert','event.reconciled',?,?)", (survivor["event_key"], digest, now))
        tx.execute("INSERT INTO changes(entity,entity_key,op,kind,payload_hash,created_at) VALUES('event',?,'delete','event.reconciled',?,?)", (loser["event_key"], digest, now))
        return {"survivor": survivor["event_key"], "removed": loser["event_key"], "candidates_preserved": len(all_links),
                "revisions_preserved": len(revision_rows)}

