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


if __name__ == "__main__":
    unittest.main()
