"""Deduplication, persistence and event processing as three separate capabilities.

* Deduplication decides (explicit `Decision`s) through one interface; two interchangeable strategies implement it, and choosing between them is the composition root's argument.
* Processing runs against a second, dictionary-backed Persistence that imports no database.
* Persistence runs the lookup, the decision and the write as one atomic operation: concurrent twins, strategy failures, invalid decisions, stale leases and replays.
"""
import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from discovery.service.contracts import CleanEvent, DecisionRejected, LeaseLost
from discovery.service.deduplication import Association, Decision, ExistingEvent, Incoming
from discovery.service.deduplication.exact import ExactMatch
from discovery.service.deduplication.tolerant import TolerantMatch
from discovery.service.extraction import VERSION, ExtractionService, LocalProcessingClient, StoredCandidateExtractor
from discovery.service.events import EventQuery
from discovery.service.processing import ProcessingService
from tests.db import database
from tests.inmemory import InMemoryProcessing
from tests.records import document
from tests.test_extraction import Fixture

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = StoredCandidateExtractor.name
START = 1893477600   # 2030-01-01T06:00:00Z


def clean(title="Harbor Concert", starts_at=START, address="500 Harbor Drive, San Diego, CA", venue="Harbor Hall", ends_at=None, location_text=None):
    return CleanEvent(title, starts_at, ends_at, "America/Los_Angeles", None, venue, location_text, address, None, "address" if address else "venue" if venue else "unknown", (), "partial")


def known(event_key, event):
    return ExistingEvent(event_key, event.title, event.starts_at, event.ends_at, event.venue_name, event.address, event.location_text, 1)


class Lookup:
    def __init__(self, *events):
        self.events = events

    def starting_between(self, earliest, latest, after=None, limit=100):
        events = sorted((e for e in self.events if earliest <= e.starts_at <= latest), key=lambda e: (e.starts_at, e.event_key))
        if after is not None:
            events = [e for e in events if (e.starts_at, e.event_key) > after]
        return tuple(events[:limit])


class NoLookup:
    def starting_between(self, *args, **kwargs):
        raise AssertionError("a candidate that already has a place needs no lookup")


STRATEGIES = {"exact": ExactMatch, "tolerant": TolerantMatch}


class Interface(unittest.TestCase):
    def test_both_strategies_implement_the_same_interface(self):
        for name, make in STRATEGIES.items():
            with self.subTest(name):
                strategy = make()
                self.assertEqual((strategy.name, strategy.version), (name, 2))
                decision = strategy.decide(Incoming("src:a", clean(), None), Lookup())
                self.assertIsInstance(decision, Decision)
                self.assertEqual((decision.action, decision.event_key), ("create", None))
                self.assertIsNone(decision.event_key)

    def test_a_candidate_that_already_has_a_place_keeps_it_under_either_strategy(self):
        for name, make in STRATEGIES.items():
            with self.subTest(name):
                primary = make().decide(Incoming("src:a", clean(), Association("ev-1", "primary")), NoLookup())
                self.assertEqual((primary.action, primary.event_key), ("update", "ev-1"), "the primary description revises its event")
                secondary = make().decide(Incoming("src:b", clean(), Association("ev-1", "duplicate")), NoLookup())
                self.assertEqual((secondary.action, secondary.event_key), ("keep", "ev-1"), "a secondary description never rewrites the event")


class Exact(unittest.TestCase):
    def test_the_same_title_start_and_address_are_one_event_however_they_are_written(self):
        first = clean()
        existing = known("ev-1", first)
        for variant in (clean(title="HARBOR concert!", address="500 harbor drive, san diego, ca"), clean(title=" Harbor  Concert ", address="500 Harbor Drive San Diego CA")):
            decision = ExactMatch().decide(Incoming("other:b", variant, None), Lookup(existing))
            self.assertEqual((decision.action, decision.event_key), ("link", "ev-1"))

    def test_a_different_date_place_or_title_is_a_different_event(self):
        first = clean()
        existing = known("ev-1", first)
        for other in (clean(starts_at=START + 86400), clean(address="900 Other Street, San Diego, CA"), clean(title="Harbor Concert II")):
            self.assertEqual(ExactMatch().decide(Incoming("other:b", other, None), Lookup(existing)).action, "create")

    def test_incomplete_location_evidence_is_never_matched(self):
        first = clean(address=None, venue=None)
        existing = known("ev-1", first)
        decision = ExactMatch().decide(Incoming("other:b", clean(address=None, venue=None), None), Lookup(existing))
        self.assertEqual(decision.action, "create", "nothing is invented to make a placeless description comparable")


class Tolerant(unittest.TestCase):
    def existing(self, **kwargs):
        event = clean(**kwargs)
        return known("ev-1", event)

    def test_a_start_within_the_window_and_the_same_place_is_one_event(self):
        decision = TolerantMatch(window_s=900).decide(Incoming("o:b", clean(starts_at=START + 600), None), Lookup(self.existing()))
        self.assertEqual((decision.action, decision.event_key), ("link", "ev-1"))
        self.assertEqual(TolerantMatch(window_s=900).decide(Incoming("o:b", clean(starts_at=START + 3600), None), Lookup(self.existing())).action, "create")

    def test_contradicting_places_are_different_events_and_a_missing_address_needs_the_same_venue(self):
        strategy = TolerantMatch()
        self.assertEqual(strategy.decide(Incoming("o:b", clean(address="900 Other Street, San Diego, CA"), None), Lookup(self.existing())).action, "create")
        self.assertEqual(strategy.decide(Incoming("o:b", clean(address=None, venue="Harbor Hall"), None), Lookup(self.existing())).action, "link")
        self.assertEqual(strategy.decide(Incoming("o:b", clean(address=None, venue="Elsewhere Hall"), None), Lookup(self.existing())).action, "create")
        self.assertEqual(strategy.decide(Incoming("o:b", clean(address=None, venue=None), None), Lookup(self.existing())).action, "create", "no place at all: not matched")


class LookupPagination(unittest.TestCase):
    def test_both_strategies_search_past_the_first_200_events(self):
        unrelated = [known(f"ev-{i:03}", clean(title=f"Other {i}")) for i in range(250)]
        target = known("zz-target", clean())
        lookup = Lookup(*(unrelated + [target]))
        for strategy in (ExactMatch(), TolerantMatch()):
            with self.subTest(strategy=strategy.name):
                decision = strategy.decide(Incoming("new:candidate", clean(), None), lookup)
                self.assertEqual((decision.action, decision.event_key), ("link", "zz-target"))


def run_in_memory(strategy, candidates):
    storage = InMemoryProcessing(strategy, candidates)
    clock = lambda: 1.0   # noqa: E731
    worker = ExtractionService(LocalProcessingClient(ProcessingService(storage, clock)), StoredCandidateExtractor(), clock)
    while worker.run_once().claimed:
        pass
    return storage


def stored(key, source, **fields):
    return {"key": key, "source_key": source, "payload": document(**fields)}


class ProcessingOnAnotherPersistence(unittest.TestCase):
    """Event processing and extraction run on a Persistence that is plain Python: they know no database."""

    def candidates(self):
        return [stored("a:1", "a", title="Harbor Concert"),
                stored("b:1", "b", title="HARBOR CONCERT", address="500 harbor drive, san diego, ca"),                      # another source, the same event
                stored("c:1", "c", title="Harbor Concert", starts_at="2030-01-02T18:00:00-08:00"),                          # same title, another day
                stored("d:1", "d", title="Harbor Concert", address="900 Other Street, San Diego, CA"),                      # same title and day, another place
                stored("e:1", "e", title="Harbor Concert", address=None, venue="Harbor Hall"),                              # no address
                stored("f:1", "f", title="Harbor Concert", address=None, venue="Harbor Hall")]                              # no address, same venue

    def test_duplicates_within_one_batch_and_against_existing_events(self):
        exact = run_in_memory(ExactMatch(), self.candidates())
        self.assertEqual(len(exact.events), 5, "a:1 and b:1 are one event; the other day, address, and descriptions with no address stay separate")
        self.assertEqual(sorted(role for _, role in exact.links.values()), ["duplicate", "primary", "primary", "primary", "primary", "primary"])
        self.assertEqual(exact.links["b:1"], exact.links["a:1"][:1] + ("duplicate",))

    def test_replacing_the_strategy_changes_the_outcome_and_nothing_else(self):
        tolerant = run_in_memory(TolerantMatch(), self.candidates())
        groups = {}
        for key, (event_key, _) in tolerant.links.items():
            groups.setdefault(event_key, []).append(key)
        self.assertEqual(sorted(sorted(g) for g in groups.values()), [["a:1", "b:1", "e:1", "f:1"], ["c:1"], ["d:1"]],
                         "the alternative also links the placeless descriptions that name the same venue; other days and places stay separate")
        self.assertEqual(tolerant.extractions, run_in_memory(ExactMatch(), self.candidates()).extractions, "the same extraction work was done for the same candidates")

    def test_processing_and_the_in_memory_persistence_import_no_database(self):
        code = ("import sys, json\n"
                "sys.path[:0] = [%r, %r]\n"
                "from discovery.service.processing import ProcessingService\n"
                "from discovery.service.extraction import ExtractionService, LocalProcessingClient, StoredCandidateExtractor\n"
                "from discovery.service.deduplication.exact import ExactMatch\n"
                "from tests.inmemory import InMemoryProcessing\n"
                "from tests.records import document\n"
                "storage = InMemoryProcessing(ExactMatch(), [{'key': 'a:1', 'source_key': 'a', 'payload': document()}])\n"
                "clock = lambda: 1.0\n"
                "worker = ExtractionService(LocalProcessingClient(ProcessingService(storage, clock)), StoredCandidateExtractor(), clock)\n"
                "worker.run_once()\n"
                "print(json.dumps({'events': len(storage.events), 'sqlite3': 'sqlite3' in sys.modules, 'persistence': [m for m in sys.modules if 'service.persistence' in m and m != 'discovery.service.persistence.ports' and m != 'discovery.service.persistence']}))\n"
                % (os.path.join(ROOT, "area_events"), ROOT))
        done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        self.assertEqual(json.loads(done.stdout), {"events": 1, "sqlite3": False, "persistence": []})


class ThroughRealPersistence(Fixture):
    """The same pipeline with SQLite behind it: the strategy is a composition-root argument, nothing else differs."""

    def test_the_composition_root_selects_the_strategy_and_the_pipeline_is_otherwise_identical(self):
        outcomes = {}
        for name in ("exact", "tolerant"):
            self.folder = tempfile.TemporaryDirectory()
            self.addCleanup(self.folder.cleanup)
            self.db = os.path.join(self.folder.name, "discovery.db")
            self.pages = {}
            built = self.build(deduplication=name)
            self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
            self.add_candidate(built, "austin_fixture", "austin_fixture:two", document("Harbor Concert", address=None, venue="Harbor Hall"))
            self.drain(built)
            outcomes[name] = (self.count("events"), self.count("event_candidates", "role='duplicate'"), len(self.all_events(built)))
            built.close()
        self.assertEqual(outcomes["exact"], (2, 0, 2), "a description with no address is never matched by the conservative default")
        self.assertEqual(outcomes["tolerant"], (1, 1, 1), "the alternative links it to the event that states the same venue; Persistence, processing and the API are unchanged")

    def test_a_strategy_object_can_be_registered_instead_of_a_name(self):
        built = self.build(deduplication=TolerantMatch(window_s=3600))
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
        self.add_candidate(built, "austin_fixture", "austin_fixture:two", document("Harbor Concert", starts_at="2030-05-01T18:30:00-07:00"))
        self.drain(built)
        self.assertEqual((self.count("events"), self.count("event_candidates", "role='duplicate'")), (1, 1))

    def test_switching_strategies_reuses_the_populated_database_and_stable_associations(self):
        exact = self.build(deduplication="exact")
        primary_key = "sandiego_gov:strategy-switch-primary"
        self.add_candidate(exact, "sandiego_gov", primary_key, document("Harbor Concert"))
        self.drain(exact)
        original = exact.events.query(EventQuery(limit=20)).events[0]
        stable_key = original.event_key
        exact.close()

        # Recreate the version-7 schema shape and historical fingerprint so this
        # populated database exercises the actual version-8 removal migration.
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("ALTER TABLE events ADD COLUMN match_key TEXT")
            connection.execute("UPDATE events SET match_key='legacy-fingerprint'")
            connection.execute("DROP INDEX events_start_key")
            connection.execute("CREATE INDEX events_match ON events(match_key) WHERE match_key IS NOT NULL")
            connection.execute("DELETE FROM schema_migrations WHERE version=8")
            connection.execute("PRAGMA user_version=7")
            connection.commit()
        finally:
            connection.close()

        tolerant = self.build(deduplication="tolerant")
        secondary_key = "austin_fixture:strategy-switch-secondary"
        self.add_candidate(tolerant, "austin_fixture", secondary_key,
                           document("HARBOR CONCERT!", starts_at="2030-05-01T18:10:00-07:00"))
        self.drain(tolerant)
        self.assertEqual((self.count("events"), self.count("event_candidates", "role='duplicate'")), (1, 1))
        event = tolerant.events.get(stable_key).event
        self.assertEqual(event.event_key, stable_key)
        with database(tolerant).read() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)")}
            association = connection.execute("""SELECT e.event_key FROM event_candidates ec JOIN candidates c USING(candidate_id)
                                                   JOIN events e USING(event_id) WHERE c.candidate_key=?""", (secondary_key,)).fetchone()[0]
        self.assertNotIn("match_key", columns)
        self.assertEqual(association, stable_key)

    def test_existing_primary_and_secondary_associations_reprocessing_and_revisions(self):
        built = self.build()
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
        self.add_candidate(built, "austin_fixture", "austin_fixture:two", document("Harbor Concert"))
        self.drain(built)
        self.assertEqual((self.count("events"), self.count("event_candidates", "role='primary'"), self.count("event_candidates", "role='duplicate'")), (1, 1, 1))
        self.add_candidate(built, "austin_fixture", "austin_fixture:two", document("Harbor Concert", venue="A different wording of the venue", description="Changed by the secondary source"))
        self.drain(built)
        event = self.all_events(built)[0]
        self.assertEqual((event.revision, event.description), (1, "An evening concert."), "a secondary description never rewrites the event")
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert", description="Changed by the primary source"))
        self.drain(built)
        event = self.all_events(built)[0]
        self.assertEqual((event.revision, event.description, self.count("events"), self.count("event_candidates", "role='duplicate'")), (2, "Changed by the primary source", 1, 1))
        detail = built.events.get(event.event_key)
        self.assertEqual([h["revision"] for h in detail.history], [1, 2])
        self.assertEqual(sorted(link["role"] for link in detail.candidate_links), ["duplicate", "primary"])

    def test_different_sources_describing_one_event_and_the_same_title_elsewhere(self):
        built = self.build()
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
        self.add_candidate(built, "austin_fixture", "austin_fixture:two", document("Harbor Concert", address="9 Other Road, Austin, TX"))
        self.add_candidate(built, "austin_fixture", "austin_fixture:three", document("Harbor Concert", starts_at="2030-06-01T18:00:00-07:00"))
        self.add_candidate(built, "austin_fixture", "austin_fixture:four", document("harbor concert", address="500 Harbor Drive San Diego CA"))
        self.drain(built)
        self.assertEqual((self.count("events"), self.count("event_candidates", "role='duplicate'")), (3, 1))

    # --- atomicity --------------------------------------------------------------------------------------------------

    def claim_results(self, built, count):
        items = built.processing.claim("w1", NAME, VERSION, limit=count, lease_s=300).items
        return [(item, StoredCandidateExtractor().extract(dataclasses.asdict(item))) for item in items]

    def test_concurrent_twins_are_decided_one_after_the_other_never_both_as_new(self):
        intervals, lock = [], threading.Lock()

        class Slow(ExactMatch):
            name = "exact-slowed"

            def decide(self, incoming, lookup):
                started = time.monotonic()
                decision = super().decide(incoming, lookup)   # the lookup has happened ...
                time.sleep(0.3)                               # ... and the decision is still pending while the other submission tries to go
                with lock:
                    intervals.append((started, time.monotonic()))
                return decision

        built = self.build(deduplication=Slow())
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
        self.add_candidate(built, "austin_fixture", "austin_fixture:two", document("Harbor Concert"))
        work = self.claim_results(built, 2)
        self.assertEqual(len(work), 2)
        acks, errors, start = [], [], threading.Barrier(2)

        def submit(item, result):
            try:
                start.wait(5)
                acks.append(built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, result))
            except Exception as error:   # pragma: no cover - reported below
                errors.append(repr(error))

        threads = [threading.Thread(target=submit, args=pair) for pair in work]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(a.outcome for a in acks), ["created", "duplicate"])
        self.assertEqual((self.count("events"), self.count("event_candidates", "role='primary'"), self.count("event_candidates", "role='duplicate'")), (1, 1, 1))
        first, second = sorted(intervals)
        self.assertGreaterEqual(second[0], first[1] - 0.01, "the second decision began only after the first had committed")

    def test_a_strategy_that_fails_leaves_nothing_behind_and_the_work_can_be_retried(self):
        class Flaky(ExactMatch):
            name, broken = "flaky", True

            def decide(self, incoming, lookup):
                if self.broken:
                    raise RuntimeError("the strategy failed")
                return super().decide(incoming, lookup)

        strategy = Flaky()
        built = self.build(deduplication=strategy)
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
        (item, result), = self.claim_results(built, 1)
        with self.assertRaises(RuntimeError):
            built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, result)
        self.assertEqual((self.count("extractions"), self.count("events"), self.count("event_candidates")), (0, 0, 0))
        self.assertEqual(self.count("processing_work", "state='leased'"), 1, "still leased, so the same worker can retry")
        strategy.broken = False
        ack = built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, result)
        self.assertEqual((ack.outcome, self.count("events"), self.count("event_candidates")), ("created", 1, 1))

    def test_a_decision_the_candidates_state_does_not_allow_is_refused_and_rolled_back(self):
        class Wrong(ExactMatch):
            name, answer = "wrong", None

            def decide(self, incoming, lookup):
                return self.answer

        strategy = Wrong()
        built = self.build(deduplication=strategy)
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
        (item, result), = self.claim_results(built, 1)
        for answer in (Decision("link", "ev-nope"), Decision("update", "ev-nope"), Decision("keep", "ev-nope"), Decision("merge"), "create", None):
            strategy.answer = answer
            with self.assertRaises(DecisionRejected, msg=str(answer)):
                built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, result)
            self.assertEqual((self.count("extractions"), self.count("events"), self.count("event_candidates")), (0, 0, 0), "nothing of the refused attempt remains")

    def test_a_stale_lease_and_a_replayed_submission_change_nothing_more(self):
        built = self.build()
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
        self.add_candidate(built, "austin_fixture", "austin_fixture:two", document("Harbor Concert"))
        (first, first_result), (second, second_result) = self.claim_results(built, 2)
        ack = built.processing.submit(first.candidate_key, NAME, VERSION, first.lease_token, first_result)
        again = built.processing.submit(first.candidate_key, NAME, VERSION, first.lease_token, first_result)
        self.assertEqual((ack.outcome, again.replayed), ("created", True))
        self.assertEqual((self.count("events"), self.count("extractions")), (1, 1))
        built.processing.submit(second.candidate_key, NAME, VERSION, second.lease_token, second_result)
        replay = built.processing.submit(second.candidate_key, NAME, VERSION, second.lease_token, second_result)
        self.assertEqual((replay.replayed, self.count("events"), self.count("event_candidates", "role='duplicate'")), (True, 1, 1), "a replay never links a second time")
        with self.assertRaises(LeaseLost):
            built.processing.submit(second.candidate_key, NAME, VERSION, "not-the-token", dict(second_result, issues=["a different result"]))   # (an identical result is a replay, acknowledged again)
        with database(built).read() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM event_candidates").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
