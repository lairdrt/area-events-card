"""The operator-reviewed reconciliation of listed event pairs: proof of each pair, preservation, atomicity, idempotence and refusal."""
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO

from area_events import app as app_module, cli
from area_events.persistence.events import DuplicateMaintenanceError
from area_events.persistence.reviewed import load_manifest
from tests.records import epoch
from tests.test_discovery_service import Clock, FakeClient

PAGE = "https://example-farm.test/events"
SOURCE = "sandiego_gov"


class Seeded(unittest.TestCase):
    """Three reviewed pairs in the shapes the Bates data has, plus events the manifest never names."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = os.path.join(self.temp.name, "discovery.db")
        app = app_module.build(self.db_path, clock=Clock(), fetch_client=FakeClient({}))
        app.register_known_sources()
        app.close()
        self.counter = 0
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            self.sources = {row[1]: row[0] for row in db.execute("SELECT source_id, source_key FROM sources")}
            self.keys = {}
            self.keys["finale"] = (self.event(db, "Halloween Finale", "2026-10-31T09:00:00-07:00", "2026-10-31T16:30:00-07:00"),
                                   self.event(db, "Halloween Finale Date", "2026-10-31T09:00:00-07:00", "2026-10-31T16:30:00-07:00"))
            self.keys["cruisers"] = (self.event(db, "East County Cruisers Car Show", "2026-11-07T09:00:00-08:00", "2026-11-07T14:00:00-08:00", venue="Bates Nut Farm", address="15954 Woods Valley Road, Valley Center, CA"),
                                     self.event(db, "East County Cruisers Car Show Date", "2026-11-07T09:00:00-08:00", "2026-11-07T14:00:00-08:00"))
            # Dec 12: the survivor's time was corrected (revision 2 records it); the variant still states the old time
            self.keys["dec12"] = (self.event(db, "Christmas in Valley Center", "2026-12-12T09:30:00-08:00", "2026-12-12T14:30:00-08:00", corrected_from=("2026-12-12T16:00:00-08:00", "2026-12-12T20:00:00-08:00")),
                                  self.event(db, "Christmas in Valley Center Date & Time", "2026-12-12T16:00:00-08:00", "2026-12-12T20:00:00-08:00"))
            self.keys["dec11"] = (self.event(db, "Christmas in Valley Center", "2026-12-11T16:00:00-08:00", "2026-12-11T20:00:00-08:00"), None)   # the distinct Dec 11 occurrence: not in the manifest
            self.keys["other"] = (self.event(db, "Unrelated Concert", "2026-11-07T09:00:00-08:00", None, source="ut_austin", listing="https://other.test/events"), None)
            db.commit()
        self.manifest = {"schema": "discovery.reconciliation.v1", "source_key": SOURCE, "expect_events_after": 5,
                         "pairs": [{"label": name, "survivor": self.keys[name][0], "remove": self.keys[name][1]} for name in ("finale", "cruisers", "dec12")]}
        self.tool = app_module.reviewed_reconciliation(self.db_path, clock=lambda: epoch("2026-10-28T00:00:00+00:00"))

    def event(self, db, title, start, end, venue=None, address=None, source=SOURCE, listing=PAGE, corrected_from=None):
        self.counter += 1
        n = self.counter
        payload = json.dumps({"title": title, "starts_at": start, "provenance": {"source_name": "Local Craft Fairs", "listing_url": listing}})
        candidate_id = db.execute("""INSERT INTO candidates(candidate_key, source_id, method, extractor, title, starts_at, payload_json, payload_hash, first_seen_at, last_seen_at, updated_at)
                                     VALUES(?,?,'adapter','test',?,?,?,?,1,1,?) RETURNING candidate_id""",
                                  (f"{source}:cand-{n}", self.sources[source], title, epoch(start), payload, hashlib.sha256(payload.encode()).hexdigest(), n)).fetchone()[0]
        result = json.dumps({"fields": {"title": {"value": title}}})
        extraction_id = db.execute("""INSERT INTO extractions(candidate_id, event_id, candidate_payload_hash, extractor, version, status, result_json, result_hash, created_at)
                                      VALUES(?,NULL,?,'test',1,'complete',?,?,?) RETURNING extraction_id""", (candidate_id, "h", result, hashlib.sha256(result.encode()).hexdigest(), n)).fetchone()[0]
        first_start, first_end = (epoch(corrected_from[0]), epoch(corrected_from[1])) if corrected_from else (epoch(start), epoch(end) if end else None)
        status = "address" if address else ("venue" if venue else "unknown")
        revision = 2 if corrected_from else 1
        key = f"ev-test-{n:04d}"
        event_id = db.execute("""INSERT INTO events(event_key, title, description, starts_at, ends_at, timezone, venue_name, location_text, address, location_status, source_url, status,
                                 missing_json, revision, extraction_id, first_extracted_at, updated_at) VALUES(?,?,NULL,?,?,NULL,?,NULL,?,?,?,'partial','[]',?,?,?,?) RETURNING event_id""",
                              (key, title, epoch(start), epoch(end) if end else None, venue, address, status, listing, revision, extraction_id, n, n)).fetchone()[0]
        db.execute("UPDATE extractions SET event_id=? WHERE extraction_id=?", (event_id, extraction_id))
        db.execute("INSERT INTO event_candidates(candidate_id, event_id, role, linked_at) VALUES(?,?,'primary',?)", (candidate_id, event_id, n))
        db.execute("INSERT INTO event_revisions(event_id, revision, extraction_id, changed_json, created_at) VALUES(?,1,?,'{}',?)", (event_id, extraction_id, n))
        if corrected_from:
            changed = json.dumps({"starts_at": [first_start, epoch(start)], "ends_at": [first_end, epoch(end)]})
            db.execute("INSERT INTO event_revisions(event_id, revision, extraction_id, changed_json, created_at) VALUES(?,2,?,?,?)", (event_id, extraction_id, changed, n + 1))
        return key

    def snapshot(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            return {"events": db.execute("SELECT * FROM events ORDER BY event_id").fetchall(), "links": db.execute("SELECT * FROM event_candidates ORDER BY candidate_id").fetchall(),
                    "revisions": db.execute("SELECT * FROM event_revisions ORDER BY event_id, revision").fetchall(), "extractions": db.execute("SELECT extraction_id, event_id FROM extractions").fetchall(),
                    "changes": db.execute("SELECT COUNT(*) FROM changes").fetchone()[0], "candidates": db.execute("SELECT candidate_id, payload_hash FROM candidates").fetchall()}

    def mutate(self, statement):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(statement)
            db.commit()


class Proof(Seeded):
    def test_preview_proves_each_pair_and_writes_nothing(self):
        before, mtime = self.snapshot(), os.stat(self.db_path).st_mtime_ns
        report = self.tool.preview(self.manifest)
        self.assertEqual((report["events_before"], report["events_after"]), (8, 5))
        self.assertEqual([item["state"] for item in report["pairs"]], ["ready"] * 3)
        dec12 = report["pairs"][2]
        self.assertTrue(any("was corrected to" in line for line in dec12["evidence"]), dec12["evidence"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(os.stat(self.db_path).st_mtime_ns, mtime)

    def refused(self, pattern, manifest=None):
        before = self.snapshot()
        for operation in (self.tool.preview, self.tool.apply):
            with self.assertRaisesRegex(DuplicateMaintenanceError, pattern):
                operation(manifest or self.manifest)
        self.assertEqual(self.snapshot(), before, "a refusal writes nothing, not even for the pairs that were provable")

    def test_a_pair_that_cannot_be_proven_refuses_the_whole_manifest(self):
        cases = (
            ("UPDATE events SET venue_name='Elsewhere Hall' WHERE event_key='%s'" % self.keys["cruisers"][1], "conflicts"),
            ("UPDATE events SET address='9 Variant St' WHERE event_key='%s'" % self.keys["finale"][1], "would lose its address"),
            ("UPDATE events SET address='9 Variant St' WHERE event_key='%s'" % self.keys["cruisers"][1], "address conflicts"),
            ("UPDATE events SET description='Only the variant says this' WHERE event_key='%s'" % self.keys["finale"][1], "would lose its description"),
            ("UPDATE events SET ends_at=NULL WHERE event_key='%s'" % self.keys["finale"][0], "lose the end"),
            ("UPDATE events SET starts_at=starts_at+3600 WHERE event_key='%s'" % self.keys["finale"][1], "records no correction"),
            ("UPDATE events SET title='Halloween Finale Party' WHERE event_key='%s'" % self.keys["finale"][1], "not a survivor title"),
            ("UPDATE candidates SET payload_json=replace(payload_json,'2026-10-31','2026-11-01') WHERE candidate_key IN "
             "(SELECT c.candidate_key FROM candidates c JOIN event_candidates ec USING(candidate_id) JOIN events e USING(event_id) WHERE e.event_key='%s')" % self.keys["finale"][1], "different or unreadable days"),
            ("UPDATE candidates SET payload_json=replace(payload_json,'%s','https://elsewhere.test/events') WHERE candidate_key IN "
             "(SELECT c.candidate_key FROM candidates c JOIN event_candidates ec USING(candidate_id) JOIN events e USING(event_id) WHERE e.event_key='%s')" % (PAGE, self.keys["finale"][1]), "one listing page"),
            ("UPDATE candidates SET source_id=%d WHERE candidate_key IN (SELECT c.candidate_key FROM candidates c JOIN event_candidates ec USING(candidate_id) JOIN events e USING(event_id) "
             "WHERE e.event_key='%s')" % (self.sources["ut_austin"], self.keys["finale"][1]), "does not own"),
            ("UPDATE event_candidates SET role='duplicate' WHERE event_id=(SELECT event_id FROM events WHERE event_key='%s')" % self.keys["finale"][1], "exactly one primary"),
            ("UPDATE events SET revision=3 WHERE event_key='%s'" % self.keys["finale"][1], "revision history"),
        )
        for statement, pattern in cases:
            with self.subTest(statement=statement[:70]):
                clone = os.path.join(self.temp.name, "clone.db")
                with closing(sqlite3.connect(self.db_path)) as source, closing(sqlite3.connect(clone)) as target:
                    source.backup(target)
                original = self.db_path
                self.db_path = clone
                self.tool = app_module.reviewed_reconciliation(clone, clock=lambda: 1)
                try:
                    self.mutate(statement)
                    self.refused(pattern)
                finally:
                    self.db_path = original
                    os.remove(clone)

    def test_distinct_occurrences_are_never_paired(self):
        wrong = {**self.manifest, "pairs": [{"survivor": self.keys["dec11"][0], "remove": self.keys["dec12"][1]}], "expect_events_after": 7}
        self.refused("different or unreadable days", wrong)

    def test_unknown_missing_and_wrong_source_are_refused(self):
        self.refused("does not exist", {**self.manifest, "pairs": [{"survivor": "ev-nope", "remove": self.keys["finale"][1]}]})
        self.refused("does not exist and was not reconciled", {**self.manifest, "pairs": [{"survivor": self.keys["finale"][0], "remove": "ev-nope"}]})
        self.refused("source 'nope' does not exist", {**self.manifest, "source_key": "nope"})
        self.refused("does not own", {**self.manifest, "source_key": "ut_austin"})

    def test_an_unexpected_event_count_is_refused(self):
        self.refused("expected 6 events", {**self.manifest, "expect_events_after": 6})

    def test_manifest_shape_is_checked(self):
        good = json.dumps(self.manifest)
        self.assertEqual(load_manifest(good)[1], hashlib.sha256(good.encode()).hexdigest())
        pair = self.manifest["pairs"][0]
        for bad in ("not json", "[]", json.dumps({**self.manifest, "schema": "other"}), json.dumps({**self.manifest, "pairs": []}), json.dumps({**self.manifest, "pairs": [{"survivor": "a"}]}),
                    json.dumps({**self.manifest, "pairs": [{"survivor": "a", "remove": "a"}]}), json.dumps({**self.manifest, "pairs": [pair, pair]}),
                    json.dumps({**self.manifest, "pairs": [pair, {"survivor": pair["remove"], "remove": "ev-x"}]}), json.dumps({**self.manifest, "expect_events_after": "5"})):
            with self.subTest(bad=bad[:60]), self.assertRaises(DuplicateMaintenanceError):
                load_manifest(bad)


class Application(Seeded):
    def test_apply_keeps_the_survivor_its_facts_and_all_history(self):
        before = self.snapshot()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            originals = {row["event_key"]: dict(row) for row in db.execute("SELECT * FROM events")}
        report = self.tool.apply(self.manifest)
        self.assertEqual((report["events_before"], report["events_after"]), (8, 5))
        after = self.snapshot()
        self.assertEqual(len(after["events"]), 5)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            for name in ("finale", "cruisers", "dec12"):
                survivor, removed = self.keys[name]
                row = db.execute("SELECT * FROM events WHERE event_key=?", (survivor,)).fetchone()
                self.assertIsNone(db.execute("SELECT 1 FROM events WHERE event_key=?", (removed,)).fetchone())
                for field in ("event_key", "title", "starts_at", "ends_at", "venue_name", "address", "location_status", "source_url", "extraction_id", "first_extracted_at"):
                    self.assertEqual(row[field], originals[survivor][field], field)
                links = db.execute("SELECT c.candidate_key, ec.role FROM event_candidates ec JOIN candidates c USING(candidate_id) WHERE ec.event_id=? ORDER BY c.candidate_key", (row["event_id"],)).fetchall()
                self.assertEqual(len(links), 2, "both candidates stay associated")
                self.assertEqual(sorted(link["role"] for link in links), ["duplicate", "primary"])
                revisions = db.execute("SELECT revision, changed_json FROM event_revisions WHERE event_id=? ORDER BY revision", (row["event_id"],)).fetchall()
                before_revisions = [r for r in before["revisions"] if r[0] == originals[survivor]["event_id"]]
                self.assertEqual([tuple(r) for r in revisions[:len(before_revisions)]], [(r[1], r[3]) for r in before_revisions], "the survivor's own revisions are untouched")
                self.assertEqual(row["revision"], revisions[-1]["revision"])
                reconciled = json.loads(revisions[-1]["changed_json"])["reconciled_from"]
                self.assertEqual(reconciled["event_key"], removed)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM extractions WHERE event_id=?", (row["event_id"],)).fetchone()[0], 2)
            dec12 = db.execute("SELECT starts_at, ends_at FROM events WHERE event_key=?", (self.keys["dec12"][0],)).fetchone()
            self.assertEqual((dec12[0], dec12[1]), (epoch("2026-12-12T09:30:00-08:00"), epoch("2026-12-12T14:30:00-08:00")), "the corrected time survives")
            self.assertEqual(db.execute("SELECT venue_name, address FROM events WHERE event_key=?", (self.keys["cruisers"][0],)).fetchone()[:], ("Bates Nut Farm", "15954 Woods Valley Road, Valley Center, CA"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE event_key IN (?,?)", (self.keys["dec11"][0], self.keys["other"][0])).fetchone()[0], 2, "events outside the manifest stay")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], len(before["candidates"]), "no candidate is removed")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_revisions").fetchone()[0], len(before["revisions"]) + 3, "every revision stays; one reconciliation revision per pair")
            changes = db.execute("SELECT op, entity_key FROM changes WHERE kind='event.reconciled' ORDER BY seq").fetchall()
            self.assertEqual(len(changes), 6)
            self.assertEqual({(op, key) for op, key in changes if op == "delete"}, {("delete", self.keys[n][1]) for n in ("finale", "cruisers", "dec12")})
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_a_second_run_changes_nothing(self):
        self.tool.apply(self.manifest)
        once = self.snapshot()
        report = self.tool.apply(self.manifest)
        self.assertEqual([item["state"] for item in report["pairs"]], ["already reconciled"] * 3)
        self.assertEqual(self.snapshot(), once)
        self.assertEqual([item["state"] for item in self.tool.preview(self.manifest)["pairs"]], ["already reconciled"] * 3)

    def test_a_failure_while_writing_rolls_back_every_pair(self):
        self.mutate("CREATE TRIGGER refuse_delete BEFORE DELETE ON events WHEN OLD.event_key='%s' BEGIN SELECT RAISE(ABORT,'injected failure'); END" % self.keys["dec12"][1])
        before = self.snapshot()
        with self.assertRaisesRegex(DuplicateMaintenanceError, "rolled back"):
            self.tool.apply(self.manifest)
        self.assertEqual(self.snapshot(), before, "the two pairs reconciled before the failure are rolled back too")

    def test_a_partly_reconciled_manifest_finishes_the_rest(self):
        partial = {**self.manifest, "pairs": self.manifest["pairs"][:1], "expect_events_after": 7}
        self.tool.apply(partial)
        report = self.tool.apply(self.manifest)
        self.assertEqual([item["state"] for item in report["pairs"]], ["already reconciled", "reconciled", "reconciled"])
        self.assertEqual(report["events_after"], 5)

    def test_the_schema_version_is_checked_and_nothing_is_migrated(self):
        self.mutate("PRAGMA user_version=1")
        with self.assertRaisesRegex(DuplicateMaintenanceError, "schema"):
            self.tool.preview(self.manifest)


class Command(Seeded):
    def run_cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["duplicates", "reconcile", "--db", self.db_path, *args])
        return code, out.getvalue(), err.getvalue()

    def test_default_is_a_dry_run_and_apply_needs_a_backup_directory(self):
        path = os.path.join(self.temp.name, "manifest.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.manifest, handle)
        before = self.snapshot()
        code, out, _ = self.run_cli("--manifest", path)
        self.assertEqual(code, 0)
        self.assertIn("dry run (nothing written)", out)
        self.assertIn("events 8 -> 5", out)
        code, _, err = self.run_cli("--manifest", path, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("--backup-dir", err)
        self.assertEqual(self.snapshot(), before)

    def test_an_unprovable_pair_stops_before_any_backup_or_write(self):
        self.mutate("UPDATE events SET venue_name='Elsewhere Hall' WHERE event_key='%s'" % self.keys["cruisers"][1])
        path = os.path.join(self.temp.name, "manifest.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.manifest, handle)
        backups = os.path.join(self.temp.name, "backups")
        before = self.snapshot()
        code, _, err = self.run_cli("--manifest", path, "--apply", "--backup-dir", backups)
        self.assertEqual(code, 2)
        self.assertIn("conflicts", err)
        self.assertFalse(os.path.exists(backups), "no backup is taken for a manifest that is refused")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
