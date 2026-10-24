"""The standard producer-output contract (Source -> Adapter -> Standard Records).

Three groups: the three existing producers conform without changing what they say; one generic boundary admits valid records, keeps incomplete ones incomplete and sets invalid ones
aside; and an independent producer, written only against the contract, replaces a real one without any change downstream (processing, deduplication, persistence, API, Home Assistant)."""
import copy
import dataclasses
import importlib.util
import json
import os
import sqlite3
import types
import unittest

from discovery.service import app as app_module
from discovery.service import producer
from discovery.service.adapters import CityOfSanDiegoAdapter, CraftFairAcquisitionAdapter, LocalistAdapter
from discovery.service.api import ApiTokens
from discovery.service.control import CreateAreaRequest
from discovery.service.contracts import AdapterResult
from discovery.service.fetching import RecordingFetcher
from discovery.service.producer import EventCandidate, ProducerResult, Provenance, SourceLead, Verification
from discovery.service.repositories import SourceSpec, payload_hash
from discovery.sources import localist, sandiego_gov
from tests.test_austin import API, Recorder, options as localist_options, sample_pages
from tests.test_craft_fair_acquisition import DRAGON, SEVEN, SQ, TODAY, web
from tests.test_discovery_control import CONTROL_TOKEN, READ_TOKEN, request
from tests.test_discovery_service import FakeClient, city_pages
from tests.test_extraction import PROCESS_TOKEN, Fixture

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START = "2030-06-01T18:00:00-07:00"


def candidate(local_id="x1", **fields):
    return EventCandidate(local_id=local_id, **{"title": "Harbor Concert", "starts_at": START, **fields})


def accepted(*records, leads=(), source_key="src", notes=None):
    return producer.accept(ProducerResult(tuple(records), tuple(leads), notes or {}), source_key)


def city_run():
    client = FakeClient(city_pages())
    opts = {"source_key": "sandiego_gov", "name": sandiego_gov.NAME, "url": sandiego_gov.LISTING_URL, "pages": 2, "max_details_per_run": 10}
    result = CityOfSanDiegoAdapter().crawl(RecordingFetcher(client, 40), [], opts)
    legacy, _ = sandiego_gov.discover(RecordingFetcher(FakeClient(city_pages()), 40), [], details=True, pages=2, max_details_per_run=10, source_id="sandiego_gov",
                                      source_name=sandiego_gov.NAME, url=sandiego_gov.LISTING_URL)
    return "sandiego_gov", result, legacy


def austin_run():
    opts = localist_options()
    pages = sample_pages()
    result = LocalistAdapter().crawl(Recorder({API + "1": pages[0], API + "2": pages[1]}), [], opts)
    legacy, _ = localist.discover(Recorder({API + "1": pages[0], API + "2": pages[1]}), [], details=False, days=int(opts.get("days", 14)), max_pages=int(opts.get("max_pages", 5)),
                                  source_id=opts["source_key"], source_name=opts["name"], url=opts["url"], location={"timezone": opts.get("timezone")})
    return opts["source_key"], result, legacy


def craft_run():
    adapter = CraftFairAcquisitionAdapter(today=lambda: TODAY)
    opts = {"source_key": "sd_craft_fairs", "name": "San Diego craft fairs", "url": SQ, "seeds": (SQ, DRAGON), "seed_region": "San Diego County"}
    first = adapter.crawl(RecordingFetcher(FakeClient(web()), 40), [], opts)
    second = adapter.crawl(RecordingFetcher(FakeClient(web()), 40), [], opts)
    return "sd_craft_fairs", first, [c.to_payload("sd_craft_fairs") for c in second.candidates]


RUNS = {"City of San Diego": city_run, "UT Austin (Localist)": austin_run, "San Diego craft fairs": craft_run}


class ExistingProducers(unittest.TestCase):
    def test_every_existing_producer_answers_with_the_standard_records(self):
        for name, run in RUNS.items():
            with self.subTest(name):
                key, result, _ = run()
                self.assertIsInstance(result, ProducerResult)
                self.assertEqual(result.schema, producer.SCHEMA)
                self.assertTrue(result.candidates, "the fixtures hold real events")
                self.assertTrue(all(isinstance(c, EventCandidate) and c.schema == producer.SCHEMA for c in result.candidates))
                self.assertTrue(all(not c.local_id.startswith(key + ":") for c in result.candidates), "the record carries the source's own id, not the service's key")
                boundary = producer.accept(result, key)
                self.assertEqual((boundary.rejections, len(boundary.candidates)), ((), len(result.candidates)), "the boundary admits every record a real producer makes")
                self.assertEqual(boundary.notes["records_rejected"], 0)

    def test_the_stored_document_is_exactly_what_the_service_stored_before(self):
        """The compatibility boundary: a record turned back into a stored candidate equals the event dictionary the reading code produced, so stored data and hashes are unchanged."""
        for name, run in RUNS.items():
            with self.subTest(name):
                key, result, legacy = run()
                payloads = [c.to_payload(key) for c in result.candidates]
                self.assertEqual(len(payloads), len(legacy))
                for new, old in zip(sorted(payloads, key=lambda p: p["id"]), sorted(legacy, key=lambda p: p["id"])):
                    self.assertEqual(payload_hash(new), payload_hash(old), new["id"])
                    self.assertEqual({k: v for k, v in new.items() if k != "retrieved_at"}, {k: v for k, v in old.items() if k != "retrieved_at"})

    def test_only_the_acquisition_producer_returns_leads_and_they_carry_no_event_facts(self):
        _, craft, _ = craft_run()
        self.assertTrue(craft.leads and all(isinstance(lead, SourceLead) for lead in craft.leads))
        self.assertFalse({"title", "starts_at", "start"} & {f.name for f in dataclasses.fields(SourceLead)}, "a lead has no event fields to masquerade with")
        self.assertEqual(city_run()[1].leads, ())
        self.assertEqual({lead.url: lead.decision for lead in craft.leads if lead.url == SEVEN}, {SEVEN: "accepted"})

    def test_records_are_immutable(self):
        _, result, _ = austin_run()
        record = result.candidates[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.title = "changed"
        with self.assertRaises(TypeError):
            record.private["extraction"] = {}
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.candidates = ()
        with self.assertRaises(AttributeError):
            result.candidates.append(record)
        with self.assertRaises(TypeError):
            result.notes["x"] = 1
        self.assertIsInstance(record.categories, tuple)


class Boundary(unittest.TestCase):
    def test_a_valid_record_passes_and_becomes_the_stored_document(self):
        record = candidate(ends_at="2030-06-01T20:00:00-07:00", timezone="America/Los_Angeles", venue="Harbor Hall", address="500 Harbor Drive, San Diego, CA", description="An evening.",
                           url="https://example.org/e", categories=("Music",), provenance=Provenance("Example", "https://example.org/list", "2030-05-01T00:00:00+00:00", ("https://example.org/list",)),
                           verification=Verification("unverified", ("read from the listing",), ("image_url",), "single source"))
        result = accepted(record, source_key="src")
        self.assertEqual((len(result.candidates), result.rejections), (1, ()))
        stored = result.candidates[0].to_payload("src")
        self.assertEqual((stored["id"], stored["start"], stored["venue"], stored["categories"], stored["found_on"], stored["verification"]["evidence"]),
                         ("src:x1", START, "Harbor Hall", ["Music"], ["https://example.org/list"], ["read from the listing"]))
        json.dumps(stored)   # a plain JSON document: no record types leak into storage

    def test_an_incomplete_record_stays_incomplete(self):
        stored = accepted(candidate()).candidates[0].to_payload("src")
        for name in ("end", "timezone", "venue", "address", "description", "url", "image_url", "price"):
            self.assertIsNone(stored[name], name)
        self.assertNotIn("organizer", stored)
        self.assertNotIn("geo", stored)
        self.assertEqual((stored["categories"], stored["verified"], stored["verification"]["status"]), ([], False, "unverified"))

    def test_invalid_records_are_set_aside_with_their_reasons_and_valid_ones_still_pass(self):
        bad = [candidate("a1", title=""), candidate("a2", title=None), candidate("a3", starts_at="2030-06-01T18:00:00"), candidate("a4", starts_at="soon"), candidate("a5", starts_at=None),
               candidate("   "), candidate("a7", venue=5), candidate("a8", schema="discovery.producer.v0"), {"id": "src:legacy", "title": "dict"}, SourceLead("https://x.org/", "listing", "accepted")]
        good = candidate("ok1")
        duplicate = candidate("ok1", title="Same id again")
        result = accepted(good, *bad, duplicate)
        self.assertEqual([c.local_id for c in result.candidates], ["ok1"])
        self.assertEqual(result.notes["records_rejected"], len(bad) + 1)
        reasons = {r.ref: r.reason for r in result.rejections}
        self.assertIn("no title", reasons["a1"])
        self.assertIn("UTC offset", reasons["a3"])
        self.assertIn("UTC offset", reasons["a4"])
        self.assertIn("duplicate id", reasons["ok1"])
        self.assertTrue(any("not an EventCandidate" in r.reason for r in result.rejections if r.ref == "dict"))
        self.assertTrue(any("a lead is not an event" in r.reason for r in result.rejections))

    def test_a_lead_is_never_an_event_and_an_event_is_never_a_lead(self):
        lead = SourceLead("https://example.org/craft-fair", "event", "accepted", ("page names a craft fair",), 5, "https://example.org/list", "Craft Fair", 1, True)
        only_leads = accepted(leads=[lead])
        self.assertEqual((only_leads.candidates, len(only_leads.leads)), ((), 1), "leads alone create no event")
        confused = accepted(lead, leads=[candidate()])
        self.assertEqual((confused.candidates, confused.leads, len(confused.rejections)), ((), (), 2))
        invalid = accepted(leads=[SourceLead("ftp://x.org/", "listing", "accepted"), SourceLead("https://x.org/", "listing", "maybe"), SourceLead("https://x.org/a", "", "accepted"), lead, lead])
        self.assertEqual(len(invalid.leads), 1)
        self.assertEqual(len(invalid.rejections), 4)

    def test_the_envelope_is_checked(self):
        with self.assertRaises(TypeError):
            producer.accept({"candidates": []}, "src")
        with self.assertRaises(TypeError):
            producer.accept([candidate()], "src")
        wrong = producer.accept(ProducerResult((candidate(),), schema="discovery.producer.v9"), "src")
        self.assertEqual((wrong.candidates, wrong.rejections[0].kind), ((), "envelope"))

    def test_producers_not_yet_moved_to_the_records_pass_through_the_one_compatibility_conversion(self):
        legacy = AdapterResult([{"id": "src:stub@1", "title": "Event", "start": START, "venue": None, "url": None, "description": None}, {"id": "other:wrong", "title": "x", "start": START},
                                {"title": "no id", "start": START}], {"pages": 1})
        result = producer.accept(legacy, "src")
        self.assertEqual([c.local_id for c in result.candidates], ["stub@1"])
        self.assertEqual(result.notes["records_rejected"], 2)
        self.assertEqual(result.candidates[0].to_payload("src")["id"], "src:stub@1")

    def test_an_event_that_cannot_become_a_record_is_counted_not_hidden(self):
        result = ProducerResult.from_events([{"id": "src:fine", "title": "Fine", "start": START}, {"id": "elsewhere:1", "title": "Wrong", "start": START}], "src")
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(len(result.notes["rejected_records"]), 1)
        self.assertEqual(producer.accept(result, "src").notes["records_rejected"], 1)


class IndependentProducer:
    """A producer written only against the contract (no reading code, no source module, no dictionaries): it reads a JSON feed through the fetcher it is given."""

    adapter = "independent"
    extractor = "tests/independent@v1"
    method = "adapter"

    def __init__(self, records):
        self.records = records

    def crawl(self, fetcher, previous, options):
        fetcher.fetch(options["url"])
        rows = [EventCandidate(**row, provenance=Provenance("Independent feed", options["url"], None, ())) if isinstance(row, dict) else row for row in self.records]
        lead = SourceLead(options["url"], "listing", "accepted", ("the feed",), 1, None, None, len(rows), True)
        return ProducerResult(tuple(rows), (lead,), {"records": len(rows)})

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

    def run_jobs(self, built, key):
        reports = [built.service.run_once() for _ in range(8)]
        return next(r for r in reports if r.job_key == f"crawl_source:{key}")

    def test_a_producer_written_against_the_contract_runs_the_whole_pipeline_unchanged(self):
        built = self.build()
        independent = IndependentProducer([])
        built.service.adapters["independent"] = independent
        built.service.register_source(SourceSpec("independent_feed", "independent", "Independent feed", self.FEED, interval_s=3600))
        sd = built.control.create_area(CreateAreaRequest("San Diego", 32.7157, -117.1611, 25, sources=("sandiego_gov", "independent_feed"))).area
        self.run_jobs(built, "sandiego_gov")
        self.drain(built)
        shadow = next(e for e in self.all_events(built, area_key=sd.area_key) if e.title == "Event 0")

        # the independent producer now speaks: an event the City source already has (same title, start and address), a full event, a minimal one, and two invalid records
        independent.records = [EventCandidate(local_id="i1", title=shadow.title, starts_at=_iso(shadow.starts_at), address=shadow.address),
                               EventCandidate(local_id="i2", title="Independent Fair", starts_at="2030-06-01T10:00:00-07:00", ends_at="2030-06-01T14:00:00-07:00",
                                              timezone="America/Los_Angeles", venue="Independent Hall", address="700 Independent Way, San Diego, CA",
                                              description="Written against the contract.", url="https://independent.test/e/2"),
                               EventCandidate(local_id="i3", title="Bare Minimum", starts_at="2030-06-02T10:00:00-07:00"),
                               EventCandidate(local_id="bad1", title="", starts_at="2030-06-03T10:00:00-07:00"),
                               EventCandidate(local_id="bad2", title="No offset", starts_at="2030-06-03T10:00:00")]
        built.service.make_due("independent_feed")
        report = self.run_jobs(built, "independent_feed")
        self.assertEqual((report.outcome, report.candidates_new, report.notes["records_rejected"]), ("completed", 3, 2), "the invalid records were set aside and counted; the rest stored")
        self.drain(built)

        self.assertEqual(self.count("candidates", "candidate_key LIKE 'independent_feed:%'"), 3)
        self.assertEqual(self.count("source_leads", "url = '" + self.FEED + "'"), 1, "its lead is stored like any producer's")
        self.assertEqual(self.count("events", "title = 'Event 0'"), 1, "deduplication: the independent producer's copy joined the existing event")
        self.assertEqual(self.count("event_candidates", "role = 'duplicate' AND candidate_id IN (SELECT candidate_id FROM candidates WHERE candidate_key LIKE 'independent_feed:%')"), 1)

        connection = sqlite3.connect(self.db)
        try:
            stored = {r[0]: json.loads(r[1]) for r in connection.execute("SELECT candidate_key, payload_json FROM candidates")}
        finally:
            connection.close()
        city_doc = next(v for k, v in stored.items() if k.startswith("sandiego_gov:"))
        core = {"id", "title", "start", "end", "timezone", "venue", "address", "description", "image_url", "image_source", "url", "categories", "price", "source", "retrieved_at",
                "verified", "verification"}
        for key in ("independent_feed:i2", "independent_feed:i3"):
            self.assertTrue(core <= set(stored[key]) and core <= set(city_doc), "every producer's candidate has the same stored shape")

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
        self.assertEqual(by_title["Independent Fair"]["sources"], ["independent_feed"])

        feed_events = {e["title"]: e for e in self.feed.to_feed_events(page)}
        self.assertEqual(set(feed_events["Independent Fair"]), set(feed_events["Event 1"]), "and the Home Assistant feed adapter converts them alike")
        self.assertEqual((feed_events["Independent Fair"]["source"]["key"], feed_events["Independent Fair"]["timezone"]), ("independent_feed", "America/Los_Angeles"))

    def test_the_independent_producer_replaces_the_city_producer_under_its_own_name(self):
        built = self.build()
        sd, _ = self.two_areas(built)
        rows = [dict(local_id=f"s{i}", title=f"Stand-in {i}", starts_at=f"2030-07-0{i + 1}T10:00:00-07:00", address=f"{i + 1}00 Oak Street, San Diego, CA", venue="Oak Hall") for i in range(3)]
        built.service.adapters["sandiego_gov"] = IndependentProducer([EventCandidate(**r) for r in rows])
        self.pages["https://www.sandiego.gov/events"] = "<html></html>"
        built.service.make_due("sandiego_gov")
        outcomes = [r for r in (built.service.run_once() for _ in range(6)) if r.job_key == "crawl_source:sandiego_gov"]
        self.assertEqual((outcomes[0].outcome, outcomes[0].candidates_new), ("completed", 3))
        self.drain(built)
        events = [e for e in self.all_events(built, area_key=sd.area_key) if e.title.startswith("Stand-in")]
        self.assertEqual([(e.title, e.location_status, e.status, "sandiego_gov" in e.sources) for e in events], [(f"Stand-in {i}", "address", "partial", True) for i in range(3)])

    def test_a_producer_that_answers_with_the_wrong_thing_fails_its_own_job_only(self):
        built = self.build()
        built.service.adapters["independent"] = IndependentProducer([EventCandidate(local_id="fine", title="Fine", starts_at=START)])
        built.service.register_source(SourceSpec("independent_feed", "independent", "Independent feed", self.FEED, interval_s=3600))
        built.control.create_area(CreateAreaRequest("San Diego", 32.7157, -117.1611, 25, sources=("sandiego_gov", "independent_feed")))
        built.service.adapters["sandiego_gov"] = types.SimpleNamespace(adapter="sandiego_gov", extractor="x", method="adapter", crawl=lambda fetcher, previous, options: {"events": []},
                                                                    page_role=lambda url, base: "listing")
        built.service.make_due("sandiego_gov")
        reports = [r for r in (built.service.run_once() for _ in range(6)) if r.job_key]
        failed = [r for r in reports if r.job_key == "crawl_source:sandiego_gov"]
        self.assertEqual(failed[0].outcome, "fetch_failed")
        self.assertIn("not a ProducerResult", failed[0].error)
        self.assertTrue(any(r.job_key == "crawl_source:independent_feed" and r.outcome == "completed" for r in reports), "another producer's crawl is unaffected")

    def test_the_contract_module_depends_on_no_source_and_no_source_depends_on_the_pipeline(self):
        import ast
        with open(os.path.join(HERE, "area_events", "discovery", "service", "producer.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertFalse([m for m in imported if "sources" in m or "adapters" in m], "the contract imports nothing source-specific")
        self.assertEqual(copy.copy(producer.SCHEMA), "discovery.producer.v1")


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


if __name__ == "__main__":
    unittest.main()
