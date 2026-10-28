"""Renaming a producer: the source_key is its identity, the display name is a label. A rename must reach every operational copy of the name, change no identity and cause no
reprocessing."""
import json
import sqlite3
import unittest

from area_events.control.service import UpdateSourceRequest
from tests.test_media_enrichment import Fixture

SOURCE = "ut_austin"


class Rename(Fixture):
    def rows(self, sql, *args):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def identity(self):
        return {"events": self.rows("SELECT event_key, revision, extraction_id FROM events ORDER BY event_key"),
                "candidates": self.rows("SELECT candidate_key, source_id FROM candidates ORDER BY candidate_key"),
                "links": self.rows("SELECT candidate_id, event_id, role FROM event_candidates ORDER BY candidate_id"),
                "extractions": self.rows("SELECT extraction_id, candidate_id, candidate_payload_hash, result_json FROM extractions ORDER BY extraction_id")}

    def rename(self, name):
        revision = self.built.control.get_source(SOURCE).revision
        return self.built.control.update_source(UpdateSourceRequest(SOURCE, revision, name=name))

    def names(self):
        """Every stored copy of the source's display name: candidate provenance and image credit."""
        provenance = {json.loads(p)["provenance"]["source_name"] for (p,) in self.rows(
            "SELECT payload_json FROM candidates c JOIN sources s USING (source_id) WHERE s.source_key=?", SOURCE)}
        credits = {json.loads(m)["image"]["credit"] for (m,) in self.rows(
            "SELECT media_json FROM candidate_media m JOIN candidates c USING (candidate_id) JOIN sources s USING (source_id) WHERE s.source_key=?", SOURCE) if "image" in json.loads(m)}
        return provenance, credits

    def prepare(self):
        self.discover_and_extract()
        self.enrich()
        return self.built.control.get_source(SOURCE).name

    def test_a_rename_reaches_every_operational_copy_and_changes_no_identity(self):
        old = self.prepare()
        provenance, credits = self.names()
        self.assertEqual((provenance, credits), ({old}, {old}), "before the rename every copy carries the old name")
        before = self.identity()
        changes_before = self.count("changes")

        result = self.rename("Texas Campus Calendar")

        self.assertFalse(result.replayed)
        self.assertEqual(self.names(), ({"Texas Campus Calendar"}, {"Texas Campus Calendar"}))
        self.assertEqual(self.identity(), before, "events, candidates, associations and the recorded extractions are exactly as they were")
        self.assertEqual(self.built.control.get_source(SOURCE).source_key, SOURCE)
        self.assertGreater(self.count("changes"), changes_before)
        # nothing is stale for the pipeline: no extraction and no enrichment is due because of the rename
        self.assertEqual(self.built.enrichment._work.pending(50), [])
        self.assertEqual(self.built.extraction.run_once().claimed, 0)

    def test_the_api_presents_the_new_name_and_the_same_events(self):
        old = self.prepare()
        before = {e.event_key: e for e in self.events(self.austin).values()}
        self.rename("Texas Campus Calendar")
        after = {e.event_key: e for e in self.events(self.austin).values()}
        self.assertEqual(set(after), set(before))
        credits = {e.media["image"]["credit"] for e in after.values() if e.media and "image" in e.media}
        self.assertEqual(credits, {"Texas Campus Calendar"})
        self.assertNotIn(old, credits)
        self.assertEqual({e.revision for e in after.values()}, {e.revision for e in before.values()}, "no event was revised")

    def test_future_renames_work_without_manual_cleanup_and_a_crawl_after_a_rename_changes_nothing(self):
        self.prepare()
        identity = self.identity()
        for name in ("Texas Campus Calendar", "Longhorn Events", "UT Austin Events"):
            self.rename(name)
            self.assertEqual(self.names(), ({name}, {name}), name)
        self.assertEqual(self.identity(), identity)
        self.clock.t += 3601
        self.built.service.make_due(SOURCE)
        self.built.service.run_until_idle(10)
        self.discover_and_extract()
        self.enrich()
        self.assertEqual(self.identity()["candidates"], identity["candidates"], "a crawl under the new name creates no candidate")
        self.assertEqual([e[0] for e in self.identity()["events"]], [e[0] for e in identity["events"]], "and no event")
        self.assertEqual(self.names(), ({"UT Austin Events"}, {"UT Austin Events"}))

    def test_a_new_photograph_after_a_rename_is_credited_to_the_new_name(self):
        self.prepare()
        self.rename("Texas Campus Calendar")
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("DELETE FROM candidate_media")   # force every candidate to be enriched again, from the cached photograph records
            connection.commit()
        finally:
            connection.close()
        self.enrich()
        self.assertEqual(self.names()[1], {"Texas Campus Calendar"}, "a cached record made under the old name is credited under the current one")

    def test_historical_evidence_keeps_the_name_it_was_recorded_under(self):
        self.prepare()
        recorded = self.rows("SELECT extraction_id, result_json FROM extractions ORDER BY extraction_id")
        self.rename("Texas Campus Calendar")
        self.assertEqual(self.rows("SELECT extraction_id, result_json FROM extractions ORDER BY extraction_id"), recorded)
        self.assertTrue(recorded)

    def test_a_rename_that_does_not_change_the_name_touches_nothing(self):
        old = self.prepare()
        before = self.rows("SELECT candidate_id, payload_hash FROM candidates ORDER BY candidate_id")
        revision = self.built.control.get_source(SOURCE).revision
        result = self.built.control.update_source(UpdateSourceRequest(SOURCE, revision, name=old, interval_minutes=75))
        self.assertFalse(result.replayed)
        self.assertEqual(self.rows("SELECT candidate_id, payload_hash FROM candidates ORDER BY candidate_id"), before)


class RenameWhileBusy(Rename):
    """D29/D30/D33: a rename in the middle of extraction or enrichment work, and a rename that fails half way."""

    def consistent(self):
        stale_media = self.rows("SELECT COUNT(*) FROM candidate_media m JOIN candidates c USING (candidate_id) WHERE m.payload_hash<>c.payload_hash")[0][0]
        stale_work = self.rows("SELECT COUNT(*) FROM processing_work w JOIN candidates c USING (candidate_id) WHERE w.state='done' AND w.done_hash<>c.payload_hash")[0][0]
        return stale_media, stale_work

    def test_D29_a_result_made_before_a_rename_is_discarded_safely_and_the_work_is_done_again(self):
        from area_events.contracts.models import StaleResult
        self.built.service.run_until_idle(10)
        client, extractor = self.built.extraction._client, self.built.extraction._extractor
        items = []
        while True:
            batch = client.claim("worker-1", extractor.name, extractor.version, 20, 600)
            if not batch:
                break
            items.extend(batch)
        self.assertGreater(len(items), 0)
        results = [(item, extractor.extract(item)) for item in items]
        self.rename("Texas Campus Calendar")   # while every item is leased
        stale = accepted = 0
        for item, result in results:
            try:
                client.submit("worker-1", extractor.name, extractor.version, item["candidate_key"], item["lease_token"], result)
                accepted += 1
            except StaleResult:
                stale += 1
        self.assertGreater(stale, 0, "the renamed source's candidates changed after they were leased")
        self.assertGreater(accepted, 0, "other sources' results are unaffected")
        while self.built.extraction.run_once().claimed:
            pass
        self.assertEqual(self.rows("SELECT COUNT(*) FROM processing_work WHERE state<>'done'"), [(0,)], "no work was lost")
        self.assertEqual(self.consistent(), (0, 0))
        events, linked = self.rows("SELECT COUNT(*) FROM events")[0][0], self.rows("SELECT COUNT(DISTINCT event_id) FROM event_candidates")[0][0]
        self.assertEqual(events, linked)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM (SELECT title, starts_at FROM events GROUP BY title, starts_at HAVING COUNT(*) > 1)"), self.duplicate_groups_baseline)

    def setUp(self):
        super().setUp()
        self.duplicate_groups_baseline = [(0,)]

    def test_D29_an_enrichment_result_made_before_a_rename_is_replaced_not_trusted(self):
        self.discover_and_extract()
        work = self.built.enrichment._work
        item = next(i for i in work.pending(500) if i["source_key"] == SOURCE)
        self.rename("Texas Campus Calendar")
        old_hash = item["payload_hash"]
        work.finish([{"candidate_id": item["candidate_id"], "payload_hash": old_hash, "media": {"image": {"url": "/area_events/media/pxxxxxxxxxxxxxxxxxxxxxxxx.jpg", "credit": "Old Name"}},
                      "illustration": None, "complete": True, "version": 99}])
        self.assertNotEqual(self.consistent()[0], 0, "the late result is keyed to the old hash, so the candidate is due again")
        self.enrich()
        self.assertEqual(self.consistent(), (0, 0))
        credits = self.names()[1]
        self.assertEqual(credits - {"Texas Campus Calendar"}, set(), credits)

    def test_D33_a_rename_that_fails_part_way_changes_nothing(self):
        self.prepare()
        before = {"sources": self.rows("SELECT source_key, name, revision FROM sources ORDER BY 1"), "candidates": self.rows("SELECT candidate_id, payload_json, payload_hash FROM candidates ORDER BY 1"),
                  "media": self.rows("SELECT candidate_id, payload_hash, media_json FROM candidate_media ORDER BY 1"), "work": self.rows("SELECT candidate_id, done_hash FROM processing_work ORDER BY 1"),
                  "changes": self.count("changes")}
        control = self.built.persistence.control
        real = control._changes

        class Failing:
            calls = 0

            def append(self, tx, *args, **kwargs):
                Failing.calls += 1
                if Failing.calls == 3:
                    raise RuntimeError("the disk filled up")
                return real.append(tx, *args, **kwargs)
        control._changes = Failing()
        try:
            with self.assertRaises(RuntimeError):
                self.rename("Texas Campus Calendar")
        finally:
            control._changes = real
        after = {"sources": self.rows("SELECT source_key, name, revision FROM sources ORDER BY 1"), "candidates": self.rows("SELECT candidate_id, payload_json, payload_hash FROM candidates ORDER BY 1"),
                 "media": self.rows("SELECT candidate_id, payload_hash, media_json FROM candidate_media ORDER BY 1"), "work": self.rows("SELECT candidate_id, done_hash FROM processing_work ORDER BY 1"),
                 "changes": self.count("changes")}
        self.assertEqual(after, before, "the whole rename rolled back")
        self.rename("Texas Campus Calendar")   # and it can simply be done again
        self.assertEqual(self.names(), ({"Texas Campus Calendar"}, {"Texas Campus Calendar"}))
        self.assertEqual(self.consistent(), (0, 0))

    def test_D30_the_hash_keyed_tables_agree_after_every_rename(self):
        self.prepare()
        for name in ("A Calendar", "B Calendar"):
            self.rename(name)
            self.assertEqual(self.consistent(), (0, 0), name)

    def test_D32_the_api_credit_follows_the_current_name_even_for_a_cached_record_made_under_an_older_one(self):
        self.prepare()
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("UPDATE media_records SET record_json=REPLACE(record_json, ?, 'Ancient Name')", (self.built.control.get_source(SOURCE).name,))
            connection.execute("DELETE FROM candidate_media")
            connection.commit()
        finally:
            connection.close()
        self.enrich()
        self.assertEqual(self.names()[1], {self.built.control.get_source(SOURCE).name})


class ExistingRename(Rename):
    """A source that was renamed BEFORE renames reconciled their records: sources.name is new, the stored copies are not."""

    OLD = "Mission Trails Marketplace"

    def legacy_rename(self, key=SOURCE, name="Texas Campus Calendar"):
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("UPDATE sources SET name=?, revision=revision+1 WHERE source_key=?", (name, key))
            connection.commit()
        finally:
            connection.close()
        return name

    def state(self):
        return {"candidates": self.rows("SELECT candidate_id, payload_json, payload_hash FROM candidates ORDER BY candidate_id"),
                "media": self.rows("SELECT candidate_id, payload_hash, media_json FROM candidate_media ORDER BY candidate_id"),
                "work": self.rows("SELECT candidate_id, done_hash, last_fail_hash FROM processing_work ORDER BY candidate_id"),
                "changes": self.count("changes")}

    def test_the_dry_run_reports_the_stale_copies_and_writes_nothing(self):
        old = self.prepare()
        name = self.legacy_rename()
        before = self.state()
        report = {item["source_key"]: item for item in self.built.persistence.display_names.preview()}
        self.assertEqual(self.state(), before, "a dry run writes nothing")
        self.assertEqual(report[SOURCE]["name"], name)
        self.assertGreater(report[SOURCE]["candidates"], 0)
        self.assertGreater(report[SOURCE]["image_credits"], 0)
        self.assertGreater(report[SOURCE]["bookkeeping_rebased"], 0)
        self.assertEqual(report[SOURCE]["stale_names"], [old])
        self.assertTrue(all(item["candidates"] == 0 for key, item in report.items() if key != SOURCE), "other sources have nothing stale")

    def test_reconciliation_restores_consistent_names_without_touching_identity_or_reprocessing(self):
        self.prepare()
        name = self.legacy_rename()
        identity = self.identity()
        self.assertNotEqual(self.names(), ({name}, {name}))
        result = {item["source_key"]: item for item in self.built.persistence.display_names.apply()}
        self.assertGreater(result[SOURCE]["candidates"], 0)
        self.assertEqual(self.names(), ({name}, {name}))
        self.assertEqual(self.identity(), identity, "events, candidates, associations and recorded extractions are untouched")
        self.assertEqual(self.built.enrichment._work.pending(50), [], "no enrichment is due")
        self.assertEqual(self.built.extraction.run_once().claimed, 0, "no extraction is due")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM candidate_media m JOIN candidates c USING (candidate_id) WHERE m.payload_hash<>c.payload_hash"), [(0,)])

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self.prepare()
        self.legacy_rename()
        self.built.persistence.display_names.apply()
        settled = self.state()
        again = self.built.persistence.display_names.apply()
        self.assertEqual(self.state(), settled)
        self.assertTrue(all(item["candidates"] == item["image_credits"] == item["bookkeeping_rebased"] == 0 and item["stale_names"] == [] for item in again))
        self.assertTrue(all(item["candidates"] == 0 for item in self.built.persistence.display_names.preview()))

    def test_one_source_can_be_selected_and_an_unknown_one_is_refused(self):
        self.prepare()
        self.legacy_rename()
        only = self.built.persistence.display_names.preview(SOURCE)
        self.assertEqual([item["source_key"] for item in only], [SOURCE])
        with self.assertRaises(ValueError):
            self.built.persistence.display_names.preview("no_such_source")

    def test_ownership_follows_the_source_not_the_name_found_in_a_record(self):
        self.prepare()
        sandiego = self.rows("SELECT name FROM sources WHERE source_key='sandiego_gov'")[0][0]
        self.legacy_rename()
        self.built.persistence.display_names.apply()
        stale = self.rows("SELECT COUNT(*) FROM candidates c JOIN sources s USING (source_id) WHERE s.source_key='sandiego_gov'")[0][0]
        self.assertGreater(stale, 0)
        names = {json.loads(p)["provenance"]["source_name"] for (p,) in self.rows("SELECT payload_json FROM candidates c JOIN sources s USING (source_id) WHERE s.source_key='sandiego_gov'")}
        self.assertEqual(names, {sandiego}, "another source's records keep their own name")

    def test_the_command_reports_by_default_and_applies_only_after_a_verified_backup(self):
        import contextlib
        import io
        import os
        import tempfile
        from area_events import cli
        self.prepare()
        name = self.legacy_rename()
        self.built.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["sources", "--db", self.db, "reconcile-names", SOURCE]), 0)
        self.assertIn("dry run (nothing written)", out.getvalue())
        self.assertIn("stale candidates", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["sources", "--db", self.db, "reconcile-names", SOURCE, "--apply"]), 2)
        self.assertIn("--backup-dir", err.getvalue())
        with tempfile.TemporaryDirectory() as backups, tempfile.TemporaryDirectory() as root:
            os.environ["AREA_EVENTS_APP_ROOT"] = root
            self.addCleanup(os.environ.pop, "AREA_EVENTS_APP_ROOT", None)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["sources", "--db", self.db, "reconcile-names", SOURCE, "--apply", "--backup-dir", backups]), 0)
            self.assertIn("verified backup:", out.getvalue())
            self.assertTrue(any(f.endswith(".sqlite3") for f in os.listdir(backups)))
        self.assertEqual(self.names(), ({name}, {name}))


if __name__ == "__main__":
    unittest.main()
