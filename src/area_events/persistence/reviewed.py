"""Operator-reviewed reconciliation of listed event pairs: the narrow path for duplicates the automatic matcher intentionally refuses (a scraped field label left in a title,
a variant that states no place, a time that was later corrected).

It is NOT a force-merge. The operator names, for each pair, the event that survives and the event that is removed; this module then PROVES from the stored records that the
two describe one occurrence of one source's event, and refuses the whole manifest if any pair cannot be proven. It never searches for pairs, never consults the matcher, and
never lets the removed event's facts overwrite the survivor's: the survivor keeps its event key, its facts (including a corrected time or a venue the variant lacks) and its
revisions; every candidate association, extraction and revision of the removed event is moved to the survivor, and one final revision records the reconciliation.

Evidence required for a pair (all of it, from the database):
  * both events exist, are different, and each key appears once in the manifest;
  * every candidate of both events belongs to the manifest's source, and every one of them was found on one listing page (provenance.listing_url);
  * each event has exactly one primary candidate and a complete revision history (1..revision);
  * the removed event's titles are a survivor title followed only by date/time label words ("Date", "Date & Time", "Date: Sat.", "Date: Thurs., Sept. 17 to Sat.");
  * the primary candidates state the same local calendar day;
  * a different start or end on the removed event is allowed only when the survivor's own revision history records the correction FROM that value;
  * the survivor would lose no fact: a removed event's end, place, address or description that the survivor lacks, or a place that conflicts, refuses the pair.
Preview is read-only. Apply runs in one transaction (rolled back whole on any mismatch), is idempotent (a pair already reconciled is reported and skipped), and writes change records
for the survivor and the removed event so the API and card update.
"""
import hashlib
import json
import re
from datetime import datetime

from .events import DuplicateMaintenanceError, _maintenance_connection

MANIFEST_SCHEMA = "discovery.reconciliation.v1"
MAX_PAIRS = 50
_LABEL_WORDS = frozenset(("date", "time", "when", "day", "dates", "to", "through", "thru", "and", "until", "till", "am", "pm",
                          "mon", "monday", "tue", "tues", "tuesday", "wed", "wednesday", "thu", "thur", "thurs", "thursday", "fri", "friday", "sat", "saturday", "sun", "sunday",
                          "jan", "january", "feb", "february", "mar", "march", "apr", "april", "may", "jun", "june", "jul", "july", "aug", "august",
                          "sep", "sept", "september", "oct", "october", "nov", "november", "dec", "december"))
_FACTS = ("venue_name", "location_text", "address", "timezone")
_FIELDS = ("title", "description", "starts_at", "ends_at", "timezone", "venue_name", "location_text", "address", "location_status", "source_url", "status")


def _tokens(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").casefold()).split()


def _is_labelled_variant(core, variant):
    """`variant` is `core` followed only by date/time label words, numbers and ordinals (or equal to it)."""
    a, b = _tokens(core), _tokens(variant)
    return bool(a) and b[:len(a)] == a and all(t in _LABEL_WORDS or t.isdigit() or re.fullmatch(r"\d{1,2}(?:st|nd|rd|th)", t) for t in b[len(a):])


def _local_day(iso):
    try:
        return datetime.fromisoformat(iso).date().isoformat()
    except (TypeError, ValueError):
        return None


def load_manifest(text):
    """Parse and shape-check a manifest: {"schema", "source_key", "pairs": [{"label", "survivor", "remove"}], optional "expect_events_after"}. Returns (manifest, sha256)."""
    try:
        manifest = json.loads(text)
    except ValueError as error:
        raise DuplicateMaintenanceError(f"the manifest is not valid JSON: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA or not isinstance(manifest.get("source_key"), str) or not manifest["source_key"]:
        raise DuplicateMaintenanceError(f"the manifest needs schema {MANIFEST_SCHEMA!r} and a source_key")
    pairs = manifest.get("pairs")
    if not isinstance(pairs, list) or not 1 <= len(pairs) <= MAX_PAIRS:
        raise DuplicateMaintenanceError(f"the manifest must list 1..{MAX_PAIRS} explicit pairs")
    keys = []
    for pair in pairs:
        if not isinstance(pair, dict) or not all(isinstance(pair.get(name), str) and pair[name] for name in ("survivor", "remove")):
            raise DuplicateMaintenanceError("every pair needs a full 'survivor' and 'remove' event key")
        if pair["survivor"] == pair["remove"]:
            raise DuplicateMaintenanceError("a pair's survivor and removed event must differ")
        keys.extend((pair["survivor"], pair["remove"]))
    if len(set(keys)) != len(keys):
        raise DuplicateMaintenanceError("an event key appears more than once in the manifest")
    expected = manifest.get("expect_events_after")
    if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool) or expected < 0):
        raise DuplicateMaintenanceError("expect_events_after must be a non-negative integer")
    return manifest, hashlib.sha256(text.encode("utf-8")).hexdigest()


class ReviewedReconciliation:
    load_manifest = staticmethod(load_manifest)

    def __init__(self, path, schema_version, clock):
        self.path, self.schema_version, self.clock = path, schema_version, clock

    # --- evidence ------------------------------------------------------------------------------------------------------

    @staticmethod
    def _event(tx, key, role):
        row = tx.execute("SELECT * FROM events WHERE event_key=?", (key,)).fetchone()
        if row is None:
            return None
        links = tx.execute("""SELECT ec.candidate_id, ec.role, c.candidate_key, c.source_id, c.payload_json FROM event_candidates ec JOIN candidates c USING (candidate_id)
                              WHERE ec.event_id=? ORDER BY ec.candidate_id""", (row["event_id"],)).fetchall()
        candidates = []
        for link in links:
            try:
                doc = json.loads(link["payload_json"])
            except ValueError:
                doc = {}
            doc = doc if isinstance(doc, dict) else {}
            provenance = doc.get("provenance") if isinstance(doc.get("provenance"), dict) else {}
            candidates.append({"candidate_id": link["candidate_id"], "role": link["role"], "key": link["candidate_key"], "source_id": link["source_id"],
                               "title": doc.get("title"), "starts_at": doc.get("starts_at"), "listing_url": provenance.get("listing_url")})
        revisions = [(r["revision"], json.loads(r["changed_json"])) for r in tx.execute("SELECT revision, changed_json FROM event_revisions WHERE event_id=? ORDER BY revision", (row["event_id"],))]
        return {"row": row, "candidates": candidates, "revisions": revisions, "role": role}

    @staticmethod
    def _already(tx, survivor_key, removed_key):
        row = tx.execute("SELECT event_id FROM events WHERE event_key=?", (survivor_key,)).fetchone()
        if row is None:
            return False
        for (changed,) in tx.execute("SELECT changed_json FROM event_revisions WHERE event_id=?", (row["event_id"],)):
            reconciled = json.loads(changed).get("reconciled_from")
            if isinstance(reconciled, dict) and reconciled.get("event_key") == removed_key:
                return True
        return False

    def _verify(self, tx, source_id, survivor_key, removed_key):
        """The evidence lines for one pair, or DuplicateMaintenanceError naming what could not be proven."""
        survivor, removed = self._event(tx, survivor_key, "survivor"), self._event(tx, removed_key, "removed")
        if survivor is None:
            raise DuplicateMaintenanceError(f"survivor {survivor_key} does not exist")
        if removed is None:
            raise DuplicateMaintenanceError(f"removed event {removed_key} does not exist")
        s, r = survivor["row"], removed["row"]
        evidence = []
        for name, event in (("survivor", survivor), ("removed event", removed)):
            if not event["candidates"]:
                raise DuplicateMaintenanceError(f"the {name} has no candidate ownership")
            if sum(c["role"] == "primary" for c in event["candidates"]) != 1:
                raise DuplicateMaintenanceError(f"the {name} must have exactly one primary candidate")
            if [n for n, _ in event["revisions"]] != list(range(1, event["row"]["revision"] + 1)):
                raise DuplicateMaintenanceError(f"the {name}'s revision history is incomplete or inconsistent")
            if any(c["source_id"] != source_id for c in event["candidates"]):
                raise DuplicateMaintenanceError(f"the {name} has a candidate that the manifest's source does not own")
        evidence.append(f"all {len(survivor['candidates']) + len(removed['candidates'])} candidates belong to the manifest's source")
        pages = {c["listing_url"] for event in (survivor, removed) for c in event["candidates"]}
        if len(pages) != 1 or None in pages:
            raise DuplicateMaintenanceError(f"the candidates were not all found on one listing page: {sorted(map(str, pages))}")
        evidence.append(f"one listing page: {next(iter(pages))}")
        cores = {s["title"], *(c["title"] for c in survivor["candidates"] if c["title"])}
        variants = {r["title"], *(c["title"] for c in removed["candidates"] if c["title"])}
        for variant in sorted(variants):
            if not any(_is_labelled_variant(core, variant) for core in cores):
                raise DuplicateMaintenanceError(f"title {variant!r} is not a survivor title followed only by date/time label words")
        evidence.append(f"title {r['title']!r} is {s['title']!r} plus field-label words")
        days = {_local_day(c["starts_at"]) for event in (survivor, removed) for c in event["candidates"] if c["role"] == "primary"}
        if len(days) != 1 or None in days:
            raise DuplicateMaintenanceError(f"the primary candidates state different or unreadable days: {sorted(map(str, days))}")
        evidence.append(f"one occurrence day: {next(iter(days))}")
        corrections = {name: {change[0] for _, changed in survivor["revisions"] for field, change in changed.items() if field == name and isinstance(change, list) and change}
                       for name in ("starts_at", "ends_at")}
        if r["starts_at"] != s["starts_at"]:
            if r["starts_at"] not in corrections["starts_at"]:
                raise DuplicateMaintenanceError("the start differs and the survivor's history records no correction from the removed event's start")
            evidence.append(f"start {r['starts_at']} was corrected to {s['starts_at']} by the survivor's own history")
        if r["ends_at"] != s["ends_at"]:
            if r["ends_at"] is not None and s["ends_at"] is None:
                raise DuplicateMaintenanceError("the survivor would lose the end the removed event states")
            if r["ends_at"] is not None and r["ends_at"] not in corrections["ends_at"]:
                raise DuplicateMaintenanceError("the end differs and the survivor's history records no correction from the removed event's end")
        for name in _FACTS + ("description",):
            if r[name] is not None and s[name] is None:
                raise DuplicateMaintenanceError(f"the survivor would lose its {name}: only the removed event states {r[name]!r}")
            if name in _FACTS and r[name] is not None and s[name] != r[name]:
                raise DuplicateMaintenanceError(f"the {name} conflicts: survivor {s[name]!r}, removed event {r[name]!r}")
        evidence.append("the survivor loses no fact and no place conflicts")
        return survivor, removed, evidence

    def _source(self, tx, source_key):
        row = tx.execute("SELECT source_id FROM sources WHERE source_key=?", (source_key,)).fetchone()
        if row is None:
            raise DuplicateMaintenanceError(f"source {source_key!r} does not exist")
        return row["source_id"]

    def _plan(self, tx, manifest):
        source_id = self._source(tx, manifest["source_key"])
        plan = []
        for pair in manifest["pairs"]:
            survivor_key, removed_key = pair["survivor"], pair["remove"]
            if tx.execute("SELECT 1 FROM events WHERE event_key=?", (removed_key,)).fetchone() is None:
                if self._already(tx, survivor_key, removed_key):
                    plan.append({"label": pair.get("label"), "survivor": survivor_key, "removed": removed_key, "state": "already reconciled", "evidence": []})
                    continue
                raise DuplicateMaintenanceError(f"removed event {removed_key} does not exist and was not reconciled into {survivor_key}")
            survivor, removed, evidence = self._verify(tx, source_id, survivor_key, removed_key)
            plan.append({"label": pair.get("label"), "survivor": survivor_key, "removed": removed_key, "state": "ready", "evidence": evidence,
                         "survivor_facts": {k: survivor["row"][k] for k in _FIELDS}, "removed_facts": {k: removed["row"][k] for k in _FIELDS},
                         "candidates_moved": len(removed["candidates"]), "revisions_moved": len(removed["revisions"]), "_pair": (survivor, removed)})
        events = tx.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        pending = sum(item["state"] == "ready" for item in plan)
        expected = manifest.get("expect_events_after")
        if expected is not None and events - pending != expected:
            raise DuplicateMaintenanceError(f"expected {expected} events after reconciliation, but {events} events minus {pending} removals is {events - pending}")
        return plan, events, events - pending

    # --- operations ----------------------------------------------------------------------------------------------------

    def _check_schema(self, connection):
        if connection.execute("PRAGMA user_version").fetchone()[0] != self.schema_version:
            raise DuplicateMaintenanceError("database schema does not match this maintenance tool; no migration was run")

    def preview(self, manifest):
        """Read-only: per pair, whether it is proven (with its evidence) or why it is refused; the event counts before and after. Raises if any pair cannot be proven."""
        connection = _maintenance_connection(self.path, "ro")
        try:
            self._check_schema(connection)
            plan, before, after = self._plan(connection, manifest)
            return {"pairs": [{k: v for k, v in item.items() if k != "_pair"} for item in plan], "events_before": before, "events_after": after}
        finally:
            connection.close()

    def apply(self, manifest):
        """Reconcile every pair in ONE transaction. Any mismatch, or any failure while writing, rolls the whole manifest back."""
        connection = _maintenance_connection(self.path, "rw")
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._check_schema(connection)
            plan, before, after = self._plan(connection, manifest)
            now = int(self.clock())
            results = []
            for item in plan:
                if item["state"] == "ready":
                    results.append(self._reconcile(connection, *item.pop("_pair"), now))
                else:
                    results.append({"survivor": item["survivor"], "removed": item["removed"], "state": item["state"]})
            if connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] != after:
                raise DuplicateMaintenanceError("the event count after reconciliation is not the planned count")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise DuplicateMaintenanceError(f"foreign-key integrity failed ({len(violations)} violations)")
            connection.execute("COMMIT")
            return {"pairs": results, "events_before": before, "events_after": after}
        except BaseException as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if isinstance(error, DuplicateMaintenanceError):
                raise
            raise DuplicateMaintenanceError(f"reviewed reconciliation failed and was rolled back: {error}") from error
        finally:
            connection.close()

    @staticmethod
    def _reconcile(tx, survivor, removed, now):
        s, r = survivor["row"], removed["row"]
        appended = []
        for number, _ in removed["revisions"]:
            new = s["revision"] + number
            tx.execute("UPDATE event_revisions SET event_id=?, revision=? WHERE event_id=? AND revision=?", (s["event_id"], new, r["event_id"], number))
            appended.append(new)
        tx.execute("UPDATE extractions SET event_id=? WHERE event_id=?", (s["event_id"], r["event_id"]))
        for candidate in removed["candidates"]:
            tx.execute("UPDATE event_candidates SET event_id=?, role='duplicate' WHERE candidate_id=?", (s["event_id"], candidate["candidate_id"]))
        revision = s["revision"] + len(appended) + 1
        changed = {"reconciled_from": {"event_key": r["event_key"], "event_id": r["event_id"], "revision": r["revision"], "title": r["title"],
                                       "appended_revisions": appended, "candidates": [c["key"] for c in removed["candidates"]]}}
        text = json.dumps(changed, ensure_ascii=False, sort_keys=True)
        tx.execute("INSERT INTO event_revisions(event_id, revision, extraction_id, changed_json, created_at) VALUES(?,?,?,?,?)", (s["event_id"], revision, s["extraction_id"], text, now))
        tx.execute("UPDATE events SET revision=?, updated_at=? WHERE event_id=?", (revision, now, s["event_id"]))
        left = tx.execute("SELECT COUNT(*) FROM event_candidates WHERE event_id=?", (r["event_id"],)).fetchone()[0]
        if left:
            raise DuplicateMaintenanceError("candidate associations remained on the removed event")
        tx.execute("DELETE FROM events WHERE event_id=?", (r["event_id"],))
        digest = hashlib.sha256(text.encode()).hexdigest()
        tx.execute("INSERT INTO changes(entity,entity_key,op,kind,payload_hash,created_at) VALUES('event',?,'upsert','event.reconciled',?,?)", (s["event_key"], digest, now))
        tx.execute("INSERT INTO changes(entity,entity_key,op,kind,payload_hash,created_at) VALUES('event',?,'delete','event.reconciled',?,?)", (r["event_key"], digest, now))
        return {"survivor": s["event_key"], "removed": r["event_key"], "state": "reconciled", "candidates_preserved": len(removed["candidates"]) + len(survivor["candidates"]),
                "revisions_preserved": len(removed["revisions"]) + len(survivor["revisions"]), "revision": revision}
