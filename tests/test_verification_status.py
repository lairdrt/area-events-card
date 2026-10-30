"""Verification status: an event shows what the producer of its current primary candidate recorded, checked, and only that.

Verified needs an explicit claim with the evidence behind it; corroborated is the source's own statements agreeing; everything else, including missing or malformed
data, a duplicate's statement and a candidate the event no longer reflects, is Unverified. A restated corroboration reaches a card through the change feed."""
import json
import sqlite3
import unittest
from urllib.parse import quote

from area_events.api.events import verification_of
from area_events.contracts.models import SourceSpec
from area_events.control.service import CreateAreaRequest
from area_events.discovery.producer import ProducerResult, Verification, stated
from tests.records import document, record
from tests.test_discovery_control import READ_TOKEN, request
from tests.test_extraction import Fixture
from tests.test_ha_integration import Inbox, Service, feed_module, run
from tests.test_producer_contract import ReplayProducer

START = "2030-06-01T10:00:00-07:00"
CONFIRMED = Verification("verified", ("confirmed on the county calendar at https://county.example/fair",), (), "Confirmed independently.", True)


def complete(local_id, title, verification, **fields):
    """A record with every fact a source can give: completeness alone must never make it Verified."""
    facts = {"ends_at": "2030-06-01T12:00:00-07:00", "timezone": "America/Los_Angeles", "venue": "Harbor Hall", "address": "500 Harbor Drive, San Diego, CA",
             "description": "An afternoon event.", "url": f"https://replay.test/e/{local_id}", "image_url": f"https://replay.test/e/{local_id}.jpg"}
    return record(local_id, title, START, verification=verification, **{**facts, **fields})


class TheStatusRule(unittest.TestCase):
    def test_each_state_needs_its_own_evidence(self):
        lines = ["listing date line parsed", "event page repeats the title and the event date"]
        self.assertEqual(verification_of({"status": "unverified", "evidence": lines[:1], "verified": False})["status"], "unverified")
        corroborated = verification_of({"status": "corroborated", "evidence": lines, "missing": ["end"], "note": "Single-source.", "verified": False}, "s:1")
        self.assertEqual(corroborated, {"status": "corroborated", "verified": False, "evidence": lines, "missing": ["end"], "note": "Single-source.", "candidate_key": "s:1"})
        verified = verification_of({"status": "verified", "evidence": list(CONFIRMED.evidence), "verified": True})
        self.assertEqual((verified["status"], verified["verified"]), ("verified", True))

    def test_nothing_short_of_an_explicit_supported_claim_is_verified(self):
        for stored in ({"status": "verified", "evidence": [], "verified": True}, {"status": "verified", "evidence": ["  "], "verified": True},
                       {"status": "verified", "evidence": ["x"], "verified": False}, {"status": "verified", "evidence": ["x"], "verified": "true"},
                       {"status": "corroborated", "evidence": ["a", "b"], "verified": False}, {"status": "corroborated", "evidence": ["a"], "verified": True}):
            with self.subTest(stored):
                self.assertFalse(verification_of(stored)["verified"])
                self.assertNotEqual(verification_of(stored)["status"], "verified")
        self.assertEqual(verification_of({"status": "corroborated", "evidence": [], "verified": False})["status"], "unverified", "a corroboration nothing supports is not shown")

    def test_missing_or_malformed_data_is_unverified(self):
        for stored in (None, {}, "verified", ["verified"], {"status": "confirmed", "evidence": ["x"]}, {"status": "corroborated", "evidence": "one line"},
                       {"status": None, "evidence": [1, None], "missing": "end", "note": 5}):
            with self.subTest(stored):
                result = verification_of(stored)
                self.assertEqual((result["status"], result["verified"], result["candidate_key"]), ("unverified", False, None))
                self.assertTrue(all(isinstance(line, str) for line in result["evidence"] + result["missing"]) and isinstance(result["note"], str))


class TheHomeAssistantAdapter(unittest.TestCase):
    def event(self, **fields):
        return {"event_key": "ev-1", "title": "Harbor Concert", "starts_at": 1906650000, "timezone": "America/Los_Angeles", "missing": ["ends_at"], **fields}

    def test_the_service_status_flag_and_evidence_pass_through(self):
        tile = feed_module.to_feed_event(self.event(verification=verification_of({"status": "corroborated", "evidence": ["a", "b"], "note": "Single-source."}, "s:1")))
        self.assertEqual((tile["verified"], tile["verification"]), (False, {"status": "corroborated", "evidence": ["a", "b"], "missing": ["ends_at"], "note": "Single-source."}))
        tile = feed_module.to_feed_event(self.event(verification=verification_of({"status": "verified", "evidence": list(CONFIRMED.evidence), "verified": True})))
        self.assertEqual((tile["verified"], tile["verification"]["status"]), (True, "verified"))

    def test_an_older_service_or_invalid_data_shows_unverified(self):
        for verification in (None, "corroborated", {"status": "verified", "verified": True, "evidence": []}, {"status": "verified", "verified": False, "evidence": ["x"]},
                             {"status": "corroborated", "evidence": "x"}, {"status": "nonsense", "evidence": ["x"]}):
            with self.subTest(verification):
                event = self.event() if verification is None else self.event(verification=verification)
                tile = feed_module.to_feed_event(event)
                self.assertEqual((tile["verified"], tile["verification"]["status"]), (False, "unverified"))
                self.assertTrue(tile["verification"]["note"])


class MissingVerification(Fixture):
    def test_a_stored_candidate_without_usable_verification_is_unverified(self):
        built = self.build()
        no_record = document("No Record")
        del no_record["verification"]
        self.add_candidate(built, "sandiego_gov", "s:none", no_record)
        self.add_candidate(built, "sandiego_gov", "s:bad", document("Bad Record", starts_at="2030-05-02T19:00:00-07:00", verification="corroborated"))
        self.drain(built)
        statuses = {e.title: (e.verification["status"], e.verification["verified"]) for e in self.all_events(built)}
        self.assertEqual((statuses["No Record"], statuses["Bad Record"]), (("unverified", False), ("unverified", False)))


class ThroughThePipeline(Service):
    """Producer -> stored candidate -> extraction -> event record -> API -> Home Assistant hub -> card, with a producer that replays what it is given."""
    FEED, ECHO = "https://replay.test/events", "https://echo.test/events"

    def setUp(self):
        super().setUp()
        self.pages.update({self.FEED: "<html>feed</html>", self.ECHO: "<html>echo</html>"})
        self.replays = {}
        for key, url in (("replay_feed", self.FEED), ("replay_echo", self.ECHO)):
            self.replays[key] = ReplayProducer(ProducerResult(()))
            self.built.service.adapters[f"replay_{key}"] = self.replays[key]
            self.replays[key].adapter = f"replay_{key}"
            self.built.service.register_source(SourceSpec(key, f"replay_{key}", key.replace("_", " ").title(), url, interval_s=3600))
        self.area = self.built.control.create_area(CreateAreaRequest("Replay Town", 32.7157, -117.1611, 25, sources=("replay_feed", "replay_echo"))).area

    def publish(self, key, *records):
        self.replays[key].result = ProducerResult(tuple(records))
        self.built.service.make_due(key)
        report = next(r for r in (self.built.service.run_once() for _ in range(8)) if r.job_key == f"crawl_source:{key}")
        self.assertEqual(report.outcome, "completed")
        self.drain(self.built)

    def rows(self, sql, *params):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def snapshot(self):
        return {table: self.rows(f"SELECT * FROM {table} ORDER BY 1") for table in ("events", "candidates", "event_candidates", "event_revisions", "extractions", "candidate_media")}

    def records(self):
        return {e.title: e for e in self.all_events(self.built, area_key=self.area.area_key)}

    def test_each_tile_shows_the_state_its_evidence_supports(self):
        self.publish("replay_feed", complete("u1", "Quiet Reading", stated(("Heading on https://replay.test/events: Quiet Reading",), False)),
                     complete("c1", "Harbor Concert", stated(("listing date line parsed", "event page repeats the title and the event date"), True)),
                     complete("v1", "Checked Fair", CONFIRMED),
                     complete("v2", "Bold Claim", Verification("verified", (), (), "", True)),
                     complete("v3", "Same Site Twice", Verification("corroborated", ("listing at https://replay.test/a", "listing at https://replay.test/b"), (), "", True)))
        before = self.snapshot()
        status, page = request(self.server, "GET", f"/v1/events?area={quote(self.area.area_key)}", token=READ_TOKEN)
        self.assertEqual(status, 200)
        api = {e["title"]: e["verification"] for e in page["events"]}
        self.assertEqual({t: (v["status"], v["verified"]) for t, v in api.items()},
                         {"Quiet Reading": ("unverified", False), "Harbor Concert": ("corroborated", False), "Checked Fair": ("verified", True),
                          "Bold Claim": ("unverified", False), "Same Site Twice": ("corroborated", False)})
        self.assertEqual(api["Harbor Concert"]["evidence"], ["listing date line parsed", "event page repeats the title and the event date"])
        self.assertTrue(api["Harbor Concert"]["candidate_key"].startswith("replay_feed:"))

        card = Inbox()
        run(self.hub().subscribe("Replay Town", card))
        tiles = {e["title"]: e for e in card.last["events"]}
        self.assertEqual({t: (e["verified"], e["verification"]["status"]) for t, e in tiles.items()},
                         {"Quiet Reading": (False, "unverified"), "Harbor Concert": (False, "corroborated"), "Checked Fair": (True, "verified"),
                          "Bold Claim": (False, "unverified"), "Same Site Twice": (False, "corroborated")},
                         "a complete event with an address is not Verified, two statements from one website are corroboration, not verification")
        self.assertEqual(tiles["Checked Fair"]["verification"]["evidence"], list(CONFIRMED.evidence))
        self.assertEqual(self.snapshot(), before, "reading verification changes no event, candidate, revision, extraction or media record")

    def test_a_restated_corroboration_reaches_the_card_without_changing_the_event(self):
        unverified = complete("u1", "Quiet Reading", stated(("Heading on https://replay.test/events: Quiet Reading",), False))
        self.publish("replay_feed", unverified)
        event = self.records()["Quiet Reading"]

        async def scenario():
            hub, card = self.hub(), Inbox()
            await hub.subscribe("Replay Town", card)
            first = card.last
            self.publish("replay_feed", complete("u1", "Quiet Reading", stated(("Heading on https://replay.test/events: Quiet Reading",
                                                                                  "event page repeats the title and the event date"), True)))
            await hub.refresh()
            restated = card.last
            self.publish("replay_feed", complete("u1", "Quiet Reading", stated(("Heading on https://replay.test/events: Quiet Reading",
                                                                                  "event page repeats the title and the event date"), True), image_url="https://replay.test/new.jpg"))
            self.read.calls.clear()
            await hub.refresh()
            return first, restated, card, list(self.read.calls)

        first, restated, card, quiet_calls = run(scenario())
        self.assertEqual(first["events"][0]["verification"]["status"], "unverified")
        tile = restated["events"][0]
        self.assertEqual((tile["id"], tile["revision"], tile["verification"]["status"]), (event.event_key, event.revision, "corroborated"), "same event, same revision, new status")
        self.assertEqual(self.rows("SELECT revision FROM events WHERE event_key=?", event.event_key), [(event.revision,)], "no event revision was made")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM changes WHERE kind='event.updated' AND entity_key=?", event.event_key), [(1,)],
                         "one change was announced, and none for the later restatement that left the verification as it was")
        self.assertEqual(len(card.messages), 2)
        self.assertNotIn("events", quiet_calls, "a restatement that changes nothing a card shows reads no events again")

    def test_a_duplicate_or_a_stale_candidate_never_speaks_for_the_event(self):
        self.publish("replay_feed", complete("u1", "Quiet Reading", stated(("Heading on https://replay.test/events: Quiet Reading",), False)))
        self.publish("replay_echo", complete("u1", "Quiet Reading", CONFIRMED, url="https://replay.test/e/u1"))
        roles = dict(self.rows("SELECT c.candidate_key, l.role FROM event_candidates l JOIN candidates c USING (candidate_id) WHERE c.candidate_key LIKE 'replay_%'"))
        self.assertEqual(roles, {"replay_feed:u1": "primary", "replay_echo:u1": "duplicate"}, "the echo joined the event as a duplicate")
        event = self.records()["Quiet Reading"]
        self.assertEqual((event.verification["status"], event.verification["verified"], event.verification["candidate_key"]), ("unverified", False, "replay_feed:u1"),
                         "the duplicate's claim is not the event's evidence")

        duplicate_extraction = self.rows("""SELECT x.extraction_id FROM extractions x JOIN candidates c USING (candidate_id) WHERE c.candidate_key='replay_echo:u1'""")[0][0]
        connection = sqlite3.connect(self.db)
        try:
            with connection:   # an event whose facts came from another candidate than its primary (as an earlier reconciliation could leave it)
                connection.execute("UPDATE events SET extraction_id=? WHERE event_key=?", (duplicate_extraction, event.event_key))
        finally:
            connection.close()
        event = self.records()["Quiet Reading"]
        self.assertEqual((event.verification["status"], event.verification["verified"], event.verification["candidate_key"]), ("unverified", False, None),
                         "neither the primary's nor the duplicate's statement is attributed to facts it did not produce")
        status, detail = request(self.server, "GET", f"/v1/events/{event.event_key}", token=READ_TOKEN)
        self.assertEqual((status, detail["event"]["verification"]["status"]), (200, "unverified"))


if __name__ == "__main__":
    unittest.main()
