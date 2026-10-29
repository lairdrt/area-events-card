"""The trashcan in the database: deleting an event an administrator chose, the record kept of it, and the lookup the crawl asks before it stores a new candidate.

`trash` is ONE write transaction: the record is saved (the event's own facts, its candidates' standard records and its revision history; no picture bytes), every candidate of
the event is removed with what only it owns (evidence, extractions, processing work, media decisions, its association), then the event and its revisions; shared rows (pages,
page contents, the photograph cache, sources, other events) are untouched. The usual change records follow: `candidate` deletes and `event.removed`. An event whose candidates'
extractions are part of ANOTHER event's history is refused, because removing them would erase that history. Deleting an event that is already in the trashcan changes nothing.
"""
import hashlib
import json
from datetime import datetime

from ..contracts.models import ControlError, NotFound, ValidationFailed
from ..discovery.urls import canonical_url
from ..trashcan import DAY, REASONS, normalize, suppresses


class TrashConflict(ControlError):
    code, status = "shared_history", 409


def _url(value):
    try:
        return canonical_url(value) if value else None
    except ValueError:
        return None


def _detail_url(url, listing_url):
    """The page about this one event: not a listing page, which names many events."""
    url = _url(url)
    return url if url and url != _url(listing_url) else None


class SqliteTrashcan:
    def __init__(self, store, changes, clock):
        self._store, self._changes, self._clock = store, changes, clock

    # --- delete -----------------------------------------------------------------------------------------------------

    def trash(self, event_key, reason):
        if reason not in REASONS:
            raise ValidationFailed(f"reason must be one of: {', '.join(REASONS)}")
        with self._store.write() as tx:
            now = int(self._clock())
            kept = tx.execute("SELECT * FROM trashcan WHERE event_key=?", (event_key,)).fetchone()
            event = tx.execute("SELECT * FROM events WHERE event_key=?", (event_key,)).fetchone()
            if event is None:
                if kept is None:
                    raise NotFound("no such event")
                return self._result(kept, "already_deleted", None)
            if kept is not None:
                raise TrashConflict("this event key is already in the trashcan")
            links = tx.execute("""SELECT c.candidate_id, c.candidate_key, c.payload_json, c.payload_hash, c.starts_at, c.title, ec.role, s.source_key
                                  FROM event_candidates ec JOIN candidates c USING (candidate_id) JOIN sources s ON s.source_id = c.source_id
                                  WHERE ec.event_id=? ORDER BY ec.role DESC, c.candidate_key""", (event["event_id"],)).fetchall()
            ids = [link["candidate_id"] for link in links]
            marks = ",".join("?" * len(ids))
            if ids and tx.execute(f"""SELECT (SELECT COUNT(*) FROM event_revisions r JOIN extractions x USING (extraction_id) WHERE x.candidate_id IN ({marks}) AND r.event_id <> ?)
                                           + (SELECT COUNT(*) FROM events e JOIN extractions x USING (extraction_id) WHERE x.candidate_id IN ({marks}) AND e.event_id <> ?)""",
                                  (*ids, event["event_id"], *ids, event["event_id"])).fetchone()[0]:
                raise TrashConflict("another event's history refers to this event's candidates; it was not deleted")
            record, facts = self._snapshot(tx, event, links)
            trash_id = tx.execute("""INSERT INTO trashcan (event_key, title, starts_at, ends_at, reason, record_json, match_json, deleted_at)
                                     VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING trash_id""",
                                  (event_key, event["title"], event["starts_at"], event["ends_at"], reason, json.dumps(record, ensure_ascii=False, sort_keys=True),
                                   json.dumps(facts, ensure_ascii=False, sort_keys=True), now)).fetchone()[0]
            tx.executemany("INSERT OR IGNORE INTO trashcan_occurrences (trash_id, source_key, starts_at) VALUES (?, ?, ?)",
                           [(trash_id, source, start) for source in facts["sources"] for start in facts["starts"]])
            for link in links:
                self._changes.append(tx, "candidate", link["candidate_key"], link["payload_hash"], now, op="delete")
            if ids:
                tx.execute(f"DELETE FROM candidates WHERE candidate_id IN ({marks})", ids)
            tx.execute("DELETE FROM events WHERE event_id=?", (event["event_id"],))
            digest = hashlib.sha256(record["event"]["event_key"].encode() + str(now).encode()).hexdigest()
            seq = self._changes.append(tx, "event", event_key, digest, now, op="delete", kind="event.removed")
            violations = tx.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise ControlError(f"foreign-key integrity failed ({len(violations)} violations); nothing was deleted")
            return self._result(tx.execute("SELECT * FROM trashcan WHERE trash_id=?", (trash_id,)).fetchone(), "deleted", seq)

    @staticmethod
    def _snapshot(tx, event, links):
        """(the record kept, the facts rediscovery is compared with)."""
        candidates, starts, titles, details, places = [], {event["starts_at"]}, {normalize(event["title"])}, set(), set()
        for value in (event["venue_name"], event["address"]):
            places.add(normalize(value))
        for link in links:
            doc = json.loads(link["payload_json"])
            provenance = doc.get("provenance") or {}
            candidates.append({"candidate_key": link["candidate_key"], "source_key": link["source_key"], "role": link["role"], "payload": doc})
            if link["starts_at"] is not None:
                starts.add(int(link["starts_at"]))
            titles.add(normalize(link["title"]))
            details.add(_detail_url(doc.get("url"), provenance.get("listing_url")))
            places.update((normalize(doc.get("venue")), normalize(doc.get("address"))))
        if not any(_url(event["source_url"]) == _url((c["payload"].get("provenance") or {}).get("listing_url")) for c in candidates):
            details.add(_url(event["source_url"]))
        revisions = [{"revision": r["revision"], "created_at": r["created_at"], "changed": json.loads(r["changed_json"])}
                     for r in tx.execute("SELECT revision, created_at, changed_json FROM event_revisions WHERE event_id=? ORDER BY revision", (event["event_id"],))]
        record = {"event": {k: event[k] for k in event.keys() if k != "event_id"}, "candidates": candidates, "revisions": revisions}
        facts = {"sources": sorted({link["source_key"] for link in links}), "candidate_keys": sorted(link["candidate_key"] for link in links), "starts": sorted(starts),
                 "titles": sorted(titles - {""}), "detail_urls": sorted(details - {None}), "places": sorted(places - {""})}
        return record, facts

    # --- rediscovery ------------------------------------------------------------------------------------------------

    def suppressed(self, tx, source_key, candidate_key, record, now):
        """Is this new candidate a deleted event found again? Called by the crawl storage inside its run's transaction, BEFORE the candidate is stored; a match is
        counted on the trashcan record and the candidate is not stored (so no event, extraction, photograph or work can come of it)."""
        try:
            start = int(datetime.fromisoformat(record.starts_at).timestamp())
        except (TypeError, ValueError):
            return False
        rows = tx.execute("""SELECT DISTINCT t.trash_id, t.match_json FROM trashcan_occurrences o JOIN trashcan t USING (trash_id)
                             WHERE o.source_key=? AND o.starts_at BETWEEN ? AND ?""", (source_key, start - DAY, start + DAY)).fetchall()
        incoming = {"source_key": source_key, "candidate_key": candidate_key, "starts_at": start, "title": record.title,
                    "detail_url": _detail_url(record.url, record.provenance.listing_url), "venue": record.venue, "address": record.address}
        for row in rows:
            if suppresses(json.loads(row["match_json"]), incoming):
                tx.execute("UPDATE trashcan SET suppressed_count = suppressed_count + 1, last_suppressed_at=? WHERE trash_id=?", (now, row["trash_id"]))
                return True
        return False

    # --- reads ------------------------------------------------------------------------------------------------------

    @staticmethod
    def _result(row, state, seq):
        return {"event_key": row["event_key"], "state": state, "reason": row["reason"], "deleted_at": row["deleted_at"], "change_seq": seq}

    def listing(self, limit=100):
        """The trashcan's records (newest first) and its totals."""
        limit = max(1, min(int(limit), 500))
        with self._store.read() as connection:
            names = dict(connection.execute("SELECT source_key, name FROM sources").fetchall())
            totals = connection.execute("SELECT COUNT(*), COALESCE(SUM(suppressed_count), 0) FROM trashcan").fetchone()
            by_reason = dict(connection.execute("SELECT reason, COUNT(*) FROM trashcan GROUP BY reason").fetchall())
            rows = connection.execute("SELECT * FROM trashcan ORDER BY deleted_at DESC, trash_id DESC LIMIT ?", (limit,)).fetchall()
        events = []
        for row in rows:
            sources = json.loads(row["match_json"])["sources"]
            events.append({"event_key": row["event_key"], "title": row["title"], "starts_at": row["starts_at"], "ends_at": row["ends_at"],
                           "sources": [{"key": key, "name": names.get(key) or key} for key in sources], "reason": row["reason"], "reason_label": REASONS.get(row["reason"]),
                           "deleted_at": row["deleted_at"], "suppressed_count": row["suppressed_count"], "last_suppressed_at": row["last_suppressed_at"]})
        return {"total": totals[0], "suppressed": totals[1], "by_reason": {key: by_reason.get(key, 0) for key in REASONS}, "events": events}
