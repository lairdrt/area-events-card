"""The standard producer-to-processing interface (Source -> Producer -> Standard Records -> Processing).

Three groups: the existing producers answer with standard records, and say what the readers of their sources always said; one generic boundary admits valid records, keeps incomplete
ones incomplete and sets invalid ones aside; and an independent producer, written only against the contract, takes the place of a real one with no change to processing,
deduplication, persistence, the API or the Home Assistant feed."""
import dataclasses
import importlib.util
import json
import os
import sqlite3
import types
import unittest
from datetime import datetime, timezone

from discovery.service import app as app_module
from discovery.service import producer
from discovery.service.api import ApiTokens
from discovery.service.control import CreateAreaRequest
from discovery.service.fetching import RecordingFetcher
from discovery.service.producer import EventCandidate, ProducerResult, Provenance, SourceLead, Verification
from discovery.service.producers.city_of_san_diego import CityOfSanDiegoProducer
from discovery.service.producers.craft_fairs import CraftFairProducer
from discovery.service.producers.localist import LocalistProducer
from discovery.service.repositories import SourceSpec
from discovery.sources import localist, sandiego_gov
from tests.records import record
from tests.test_austin import API, Recorder, sample_pages
from tests.test_austin import options as localist_options
from tests.test_craft_fair_acquisition import DRAGON, SEVEN, SQ, TODAY, web
from tests.test_discovery_control import CONTROL_TOKEN, READ_TOKEN, request
from tests.test_discovery_service import FakeClient, city_pages
from tests.test_extraction import PROCESS_TOKEN, Fixture

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START = "2030-06-01T18:00:00-07:00"


def accepted(*records, leads=(), notes=None):
    return producer.accept(ProducerResult(tuple(records), tuple(leads), notes or {}))


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def city_run():
    opts = {"source_key": "sandiego_gov", "name": sandiego_gov.NAME, "url": sandiego_gov.LISTING_URL, "pages": 2, "max_details_per_run": 10}
    result = CityOfSanDiegoProducer().crawl(RecordingFetcher(FakeClient(city_pages()), 40), (), opts)
    legacy, _ = sandiego_gov.discover(RecordingFetcher(FakeClient(city_pages()), 40), [], details=True, pages=2, max_details_per_run=10, source_id="sandiego_gov",
                                      source_name=sandiego_gov.NAME, url=sandiego_gov.LISTING_URL)
    return "sandiego_gov", result, legacy


def austin_run():
    opts = localist_options()
    pages = sample_pages()
    result = LocalistProducer().crawl(Recorder({API + "1": pages[0], API + "2": pages[1]}), (), opts)
    legacy, _ = localist.discover(Recorder({API + "1": pages[0], API + "2": pages[1]}), [], details=False, days=int(opts.get("days", 14)), max_pages=int(opts.get("max_pages", 5)),
                                  source_id=opts["source_key"], source_name=opts["name"], url=opts["url"], location={"timezone": opts.get("timezone")})
    return opts["source_key"], result, legacy


def craft_run():
    opts = {"source_key": "sd_craft_fairs", "name": "San Diego craft fairs", "url": SQ, "seeds": (SQ, DRAGON), "seed_region": "San Diego County"}
    return "sd_craft_fairs", CraftFairProducer(today=lambda: TODAY).crawl(RecordingFetcher(FakeClient(web()), 40), (), opts), None


RUNS = {"City of San Diego": city_run, "UT Austin (Localist)": austin_run, "San Diego craft fairs": craft_run}


class ExistingProducers(unittest.TestCase):
    def test_every_existing_producer_answers_with_standard_records_the_boundary_admits(self):
        for name, run in RUNS.items():
            with self.subTest(name):
                key, result, _ = run()
                self.assertIsInstance(result, ProducerResult)
                self.assertTrue(result.candidates, "the fixtures hold real events")
                self.assertTrue(all(type(c) is EventCandidate and c.schema == producer.SCHEMA for c in result.candidates))
                self.assertTrue(all(not c.local_id.startswith(key + ":") for c in result.candidates), "a record carries the source's own id, not the service's key")
                boundary = producer.accept(result)
                self.assertEqual((boundary.rejections, len(boundary.candidates), boundary.notes["records_rejected"]), ((), len(result.candidates), 0))

    def test_the_city_and_localist_producers_say_what_their_sources_readers_always_said(self):
        """Behaviour is preserved: each record states the same facts, evidence and bookkeeping as the event dictionary the source's reading code builds."""
        for name in ("City of San Diego", "UT Austin (Localist)"):
            with self.subTest(name):
                key, result, legacy = RUNS[name]()
                self.assertEqual(len(result.candidates), len(legacy))
                by_id = {f"{key}:{c.local_id}": c for c in result.candidates}
                for old in legacy:
                    new = by_id[old["id"]]
                    self.assertEqual((new.title, new.starts_at, new.ends_at, new.timezone, new.venue, new.address, new.description, new.url, new.image_url, new.image_source),
                                     (old["title"], old["start"], old["end"], old["timezone"], old["venue"], old["address"], old["description"], old["url"], old["image_url"], old["image_source"]))
                    self.assertEqual((new.categories, new.price, new.organizer, dict(new.geo) if new.geo else None), (tuple(old["categories"]), old["price"], old.get("organizer"), old.get("geo")))
                    self.assertEqual((new.verification.status, list(new.verification.evidence), list(new.verification.missing), new.verification.verified),
                                     (old["verification"]["status"], old["verification"]["evidence"], old["verification"]["missing"], old["verified"]))
                    self.assertEqual((new.provenance.source_name, new.provenance.listing_url), (old["source"]["name"], old["source"]["listing_url"]))
                    self.assertEqual(dict(new.state), old["extraction"] if name == "City of San Diego" else {})

    def test_only_the_acquisition_producer_returns_leads_and_they_carry_no_event_facts(self):
        _, craft, _ = craft_run()
        self.assertTrue(craft.leads and all(isinstance(lead, SourceLead) for lead in craft.leads))
        self.assertFalse({"title", "starts_at", "start"} & {f.name for f in dataclasses.fields(SourceLead)}, "a lead has no event fields to masquerade with")
        self.assertEqual({lead.url: lead.decision for lead in craft.leads if lead.url == SEVEN}, {SEVEN: "accepted"})
        self.assertEqual(city_run()[1].leads, ())

    def test_records_are_immutable_and_closed(self):
        _, result, _ = city_run()
        first = result.candidates[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.title = "changed"
        with self.assertRaises(TypeError):
            first.state["detail_page"] = False
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.candidates = ()
        with self.assertRaises(TypeError):
            result.notes["x"] = 1
        with self.assertRaises(TypeError):
            EventCandidate(local_id="a", title="t", starts_at=START, neighborhood="Downtown")   # there is no place for a source-specific field
        self.assertEqual(sorted(f.name for f in dataclasses.fields(EventCandidate)),
                         sorted(("local_id", "title", "starts_at", "ends_at", "timezone", "venue", "address", "description", "url", "image_url", "image_source", "categories", "organizer",
                                 "price", "geo", "provenance", "verification", "state", "schema")))


class Boundary(unittest.TestCase):
    def test_a_valid_record_passes_and_is_stored_as_a_closed_versioned_document(self):
        full = record("x1", ends_at="2030-06-01T20:00:00-07:00", timezone="America/Los_Angeles", venue="Harbor Hall", address="500 Harbor Drive, San Diego, CA", description="An evening.",
                      url="https://example.org/e", categories=("Music",), geo={"latitude": 32.7, "longitude": -117.1},
                      provenance=Provenance("Example", "https://example.org/list", "2030-05-01T00:00:00+00:00", ("https://example.org/list",)),
                      verification=Verification("unverified", ("read from the listing",), ("image_url",), "single source"), state={"cursor": 3})
        result = accepted(full)
        self.assertEqual((len(result.candidates), result.rejections), (1, ()))
        stored = producer.document(result.candidates[0])
        self.assertEqual(set(stored), {"schema", "local_id", "title", "starts_at", "ends_at", "timezone", "venue", "address", "description", "url", "image_url", "image_source", "categories",
                                       "organizer", "price", "geo", "provenance", "verification"}, "the stored document holds standard fields only: no state, no source-specific keys")
        self.assertEqual((stored["local_id"], stored["categories"], stored["provenance"]["found_on"], stored["verification"]["evidence"]), ("x1", ["Music"], ["https://example.org/list"], ["read from the listing"]))
        json.dumps(stored)
        again = producer.candidate_from_document(stored, {"cursor": 3})
        self.assertEqual(again, result.candidates[0])
        self.assertEqual(producer.candidate_digest(again), producer.candidate_digest(full))

    def test_an_incomplete_record_stays_incomplete(self):
        stored = producer.document(accepted(record("x1")).candidates[0])
        for name in ("ends_at", "timezone", "venue", "address", "description", "url", "image_url", "price", "organizer", "geo"):
            self.assertIsNone(stored[name], name)
        self.assertEqual((stored["categories"], stored["verification"]["verified"], stored["verification"]["status"]), ([], False, "unverified"))

    def test_the_identity_of_a_record_is_its_facts_not_its_retrieval_time_or_its_state(self):
        a = record("x1", provenance=Provenance("S", "https://a.example/", "2030-01-01T00:00:00+00:00"), state={"cursor": 1})
        b = record("x1", provenance=Provenance("S", "https://a.example/", "2031-01-01T00:00:00+00:00"), state={"cursor": 2})
        self.assertEqual(producer.candidate_digest(a), producer.candidate_digest(b))
        self.assertNotEqual(producer.candidate_digest(a), producer.candidate_digest(record("x1", venue="Elsewhere")))

    def test_invalid_records_are_set_aside_with_their_reasons_and_valid_ones_still_pass(self):
        bad = [record("a1", title=""), record("a2", title=None), record("a3", starts_at="2030-06-01T18:00:00"), record("a4", starts_at="soon"), record("a5", starts_at=None),
               record("   "), record("a7", venue=5), record("a8", schema="discovery.producer.v0"), record("a9", geo={"latitude": "north", "longitude": 1}),
               record("b1", state={"x": object()}), record("b2", state={"big": "x" * 30000}), record("b3", categories=(1,)),
               {"id": "src:legacy", "title": "dict"}, SourceLead("https://x.org/", "listing", "accepted")]
        good, duplicate = record("ok1"), record("ok1", title="Same id again")
        result = accepted(good, *bad, duplicate)
        self.assertEqual([c.local_id for c in result.candidates], ["ok1"])
        self.assertEqual(result.notes["records_rejected"], len(bad) + 1)
        reasons = {r.ref: r.reason for r in result.rejections}
        self.assertIn("no title", reasons["a1"])
        self.assertIn("UTC offset", reasons["a3"])
        self.assertIn("coordinates", reasons["a9"])
        self.assertIn("not JSON", reasons["b1"])
        self.assertIn("size limit", reasons["b2"])
        self.assertIn("duplicate id", reasons["ok1"])
        self.assertTrue(any("not an EventCandidate" in r.reason for r in result.rejections if r.ref == "dict"))
        self.assertTrue(any("a lead is not an event" in r.reason for r in result.rejections))

    def test_a_lead_is_never_an_event_and_an_event_is_never_a_lead(self):
        lead = SourceLead("https://example.org/craft-fair", "event", "accepted", ("page names a craft fair",), 5, "https://example.org/list", "Craft Fair", 1, True)
        only_leads = accepted(leads=[lead])
        self.assertEqual((only_leads.candidates, len(only_leads.leads)), ((), 1), "leads alone create no event")
        confused = accepted(lead, leads=[record("x1")])
        self.assertEqual((confused.candidates, confused.leads, len(confused.rejections)), ((), (), 2))
        invalid = accepted(leads=[SourceLead("ftp://x.org/", "listing", "accepted"), SourceLead("https://x.org/", "listing", "maybe"), SourceLead("https://x.org/a", "", "accepted"), lead, lead])
        self.assertEqual((len(invalid.leads), len(invalid.rejections)), (1, 4))

    def test_only_a_producer_result_crosses_the_boundary(self):
        for wrong in ({"candidates": []}, [record("x1")], (record("x1"),), None, types.SimpleNamespace(candidates=[record("x1")], notes={}, leads=[])):
            with self.subTest(type(wrong).__name__):
                with self.assertRaises(TypeError):
                    producer.accept(wrong)
        envelope = producer.accept(ProducerResult((record("x1"),), schema="discovery.producer.v9"))
        self.assertEqual((envelope.candidates, envelope.rejections[0].kind), ((), "envelope"))


class IndependentProducer:
    """A producer written only against the contract: no reading code from any source, no dictionaries, nothing from the service except the contract. Its working state is a
    cursor it keeps for itself."""

    adapter = "independent"
    extractor = "tests/independent@v1"
    method = "adapter"

    def __init__(self, rows=()):
        self.rows, self.previous_seen = list(rows), []

    def crawl(self, fetcher, previous, options):
        self.previous_seen.append({c.local_id: producer.thaw(c.state) for c in previous})
        fetcher.fetch(options["url"])
        run = len(self.previous_seen)
        records = tuple(dataclasses.replace(row, state={"cursor": run, "nested": {"pages": [1, 2], "note": "écrit"}}) if isinstance(row, EventCandidate) else row for row in self.rows)
        return ProducerResult(records, (SourceLead(options["url"], "listing", "accepted", ("the feed",), 1, None, None, len(records), True),), {"records": len(records)})

    @staticmethod
    def page_role(url, base_url):
        return "listing"


class Substitution(Fixture):
    """The independent producer takes the place of a real one; the pipeline from the service to the Home Assistant feed has no change and no special case for it."""
    FEED = "https://independent.test/events"

    def setUp(self):
        super().setUp()
        self.pages[self.FEED] = "<html>feed</html>"
        spec = importlib.util.spec_from_file_location("feed", os.path.join(HERE, "custom_components", "area_events", "feed.py"))
        self.feed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.feed)

    def crawl(self, built, key):
        built.service.make_due(key)
        return next(r for r in (built.service.run_once() for _ in range(8)) if r.job_key == f"crawl_source:{key}")

    def test_a_producer_written_against_the_contract_runs_the_whole_pipeline_unchanged(self):
        built = self.build()
        independent = IndependentProducer()
        built.service.adapters["independent"] = independent
        built.service.register_source(SourceSpec("independent_feed", "independent", "Independent feed", self.FEED, interval_s=3600))
        sd = built.control.create_area(CreateAreaRequest("San Diego", 32.7157, -117.1611, 25, sources=("sandiego_gov", "independent_feed"))).area
        self.crawl(built, "sandiego_gov")
        self.drain(built)
        shadow = next(e for e in self.all_events(built, area_key=sd.area_key) if e.title == "Event 0")

        # a complete event, a partial one, a duplicate of an event the City source already has, a duplicate id, an invalid record, and a source lead
        independent.rows = [record("i1", shadow.title, iso(shadow.starts_at), address=shadow.address),
                            record("i2", "Independent Fair", "2030-06-01T10:00:00-07:00", ends_at="2030-06-01T14:00:00-07:00", timezone="America/Los_Angeles", venue="Independent Hall",
                                   address="700 Independent Way, San Diego, CA", description="Written against the contract.", url="https://independent.test/e/2",
                                   provenance=Provenance("Independent feed", self.FEED)),
                            record("i3", "Bare Minimum", "2030-06-02T10:00:00-07:00"),
                            record("i3", "The same id twice", "2030-06-02T11:00:00-07:00"),
                            record("bad1", "", "2030-06-03T10:00:00-07:00"),
                            record("bad2", "No offset", "2030-06-03T10:00:00")]
        report = self.crawl(built, "independent_feed")
        self.assertEqual((report.outcome, report.candidates_new, report.notes["records_rejected"]), ("completed", 3, 3), "invalid records and the duplicate id were set aside and counted; the rest stored")
        self.drain(built)

        self.assertEqual(self.count("candidates", "candidate_key LIKE 'independent_feed:%'"), 3)
        self.assertEqual(self.count("source_leads", f"url = '{self.FEED}'"), 1, "its lead is stored like any producer's")
        self.assertEqual(self.count("events", "title = 'Event 0'"), 1, "deduplication: the independent producer's copy joined the existing event")
        self.assertEqual(self.count("event_candidates", "role = 'duplicate' AND candidate_id IN (SELECT candidate_id FROM candidates WHERE candidate_key LIKE 'independent_feed:%')"), 1)
        self.assertEqual(self.count("events", "title = 'The same id twice' OR title = '' OR title = 'No offset'"), 0, "no rejected record became an event")

        events = {e.title: e for e in self.all_events(built, area_key=sd.area_key)}
        full, bare = events["Independent Fair"], events["Bare Minimum"]
        self.assertEqual((full.sources, full.venue_name, full.address, full.status, full.revision), (("independent_feed",), "Independent Hall", "700 Independent Way, San Diego, CA", "complete", 1))
        self.assertEqual((bare.location_status, bare.status, bare.missing, bare.address, bare.venue_name), ("unknown", "partial", ("ends_at", "description", "location"), None, None),
                         "nothing is invented for a record that says only the required facts")
        self.assertEqual(set(dataclasses.asdict(full)), set(dataclasses.asdict(shadow)), "the same event record, whichever producer made it")

        server = app_module.build_api(built, ApiTokens(CONTROL_TOKEN, READ_TOKEN, PROCESS_TOKEN), clock=self.clock, port=0).start()
        self.addCleanup(server.stop)
        status, page = request(server, "GET", f"/v1/events?area={sd.area_key}&limit=200", token=READ_TOKEN)
        self.assertEqual(status, 200)
        by_title = {e["title"]: e for e in page["events"]}
        self.assertEqual(set(by_title["Independent Fair"]), set(by_title["Event 1"]), "the API serves both producers' events with one shape")
        feed_events = {e["title"]: e for e in self.feed.to_feed_events(page)}
        self.assertEqual(set(feed_events["Independent Fair"]), set(feed_events["Event 1"]), "and the Home Assistant feed adapter converts them alike")
        self.assertEqual((feed_events["Independent Fair"]["source"]["key"], feed_events["Independent Fair"]["timezone"]), ("independent_feed", "America/Los_Angeles"))

    def test_producer_state_is_kept_for_the_producer_and_never_read_by_processing(self):
        built = self.build()
        independent = IndependentProducer([record("s1", "Stateful", "2030-06-05T10:00:00-07:00", venue="Hall", address="1 Main Street, San Diego, CA")])
        built.service.adapters["independent"] = independent
        built.service.register_source(SourceSpec("independent_feed", "independent", "Independent feed", self.FEED, interval_s=3600))
        built.control.create_area(CreateAreaRequest("San Diego", 32.7157, -117.1611, 25, sources=("independent_feed",)))
        self.assertEqual(self.crawl(built, "independent_feed").candidates_new, 1)
        self.drain(built)
        extractions = self.count("extractions")
        self.clock.t += 7200
        again = self.crawl(built, "independent_feed")
        self.assertEqual((again.candidates_new, again.candidates_changed, again.candidates_unchanged), (0, 0, 1), "a changed cursor alone is not a change in the event")
        self.drain(built)
        self.assertEqual(self.count("extractions"), extractions, "so processing did nothing again")
        self.assertEqual(independent.previous_seen[0], {})
        self.assertEqual(independent.previous_seen[1], {"s1": {"cursor": 1, "nested": {"pages": [1, 2], "note": "écrit"}}}, "handed back to the producer, exactly as it left it")
        connection = sqlite3.connect(self.db)
        try:
            doc, state = connection.execute("SELECT payload_json, state_json FROM candidates WHERE candidate_key='independent_feed:s1'").fetchone()
        finally:
            connection.close()
        self.assertEqual(json.loads(state)["cursor"], 2, "the newest state replaced the old")
        self.assertNotIn("cursor", doc, "and it is not part of the stored record")

    def test_the_independent_producer_replaces_the_city_producer_under_its_own_name(self):
        built = self.build()
        sd, _ = self.two_areas(built)
        rows = [record(f"s{i}", f"Stand-in {i}", f"2030-07-0{i + 1}T10:00:00-07:00", address=f"{i + 1}00 Oak Street, San Diego, CA", venue="Oak Hall") for i in range(3)]
        built.service.adapters["sandiego_gov"] = IndependentProducer(rows)
        self.pages["https://www.sandiego.gov/events"] = "<html></html>"
        outcome = self.crawl(built, "sandiego_gov")
        self.assertEqual((outcome.outcome, outcome.candidates_new), ("completed", 3))
        self.drain(built)
        events = [e for e in self.all_events(built, area_key=sd.area_key) if e.title.startswith("Stand-in")]
        self.assertEqual([(e.title, e.location_status, e.status, "sandiego_gov" in e.sources) for e in events], [(f"Stand-in {i}", "address", "partial", True) for i in range(3)])

    def test_a_producer_that_answers_with_the_wrong_thing_fails_its_own_job_only(self):
        built = self.build()
        built.service.adapters["independent"] = IndependentProducer([record("fine", "Fine", START)])
        built.service.register_source(SourceSpec("independent_feed", "independent", "Independent feed", self.FEED, interval_s=3600))
        built.control.create_area(CreateAreaRequest("San Diego", 32.7157, -117.1611, 25, sources=("sandiego_gov", "independent_feed")))
        built.service.adapters["sandiego_gov"] = types.SimpleNamespace(adapter="sandiego_gov", extractor="x", method="adapter", crawl=lambda fetcher, previous, options: {"events": []},
                                                                    page_role=lambda url, base: "listing")
        reports = [r for r in (built.service.run_once() for _ in range(6)) if r.job_key]
        failed = [r for r in reports if r.job_key == "crawl_source:sandiego_gov"]
        self.assertEqual(failed[0].outcome, "fetch_failed")
        self.assertIn("not a ProducerResult", failed[0].error)
        self.assertTrue(any(r.job_key == "crawl_source:independent_feed" and r.outcome == "completed" for r in reports), "another producer's crawl is unaffected")

    def test_there_is_no_compatibility_reader_for_the_earlier_stored_form(self):
        """A stored candidate that is not a version-1 record is skipped (and logged), never interpreted."""
        built = self.build()
        built.service.adapters["independent"] = IndependentProducer()
        built.service.register_source(SourceSpec("independent_feed", "independent", "Independent feed", self.FEED, interval_s=3600))
        with built.store.write() as tx:
            source_id = built.service.sources.by_key(tx, "independent_feed")["source_id"]
            tx.execute("INSERT INTO candidates (candidate_key, source_id, method, extractor, payload_json, payload_hash, first_seen_at, last_seen_at, updated_at) "
                       "VALUES ('independent_feed:old', ?, 'adapter', 'x', ?, ?, 1, 1, 1)", (source_id, json.dumps({"id": "independent_feed:old", "title": "Old", "start": START}), "0" * 64))
        with built.store.read() as connection:
            self.assertEqual(built.service.candidates.previous(connection, source_id), ())


if __name__ == "__main__":
    unittest.main()
