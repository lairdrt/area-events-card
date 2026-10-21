"""Progressive discovery: real progress, events published as each source finishes, a consistent cache, recovery."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from discovery import refresh as refresh_module
from discovery import sources as sources_module
from discovery.core import FetchError, finish_event, make_event, save_events
from discovery.engine import Engine
from discovery.publish import FeedError, validate_feed

PDT = timezone(timedelta(hours=-7))
START = (datetime.now(PDT) + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)


def record(key, title, venue="Venue One"):
    event = make_event(key, key.upper(), f"https://{key}.example", f"https://{key}.example/{title.replace(' ', '-')}", title, START,
                       local_id=title, venue=venue)
    return finish_event(event, "2030-01-01T00:00:00-08:00", [f"{key} says so"], False)


class Adapter:
    """A source that answers with events (or an error, or nothing) and can run a hook while it is 'fetching'."""

    def __init__(self, events=None, error=None, hook=None):
        self.events, self.error, self.hook = events, error, hook

    def discover(self, fetcher, previous, **_):
        if self.hook:
            self.hook()
        if self.error:
            raise FetchError(self.error)
        return self.events, {"fake": True}


class Refresh(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.paths = dict(cache_path=os.path.join(self.folder, "cache.json"), output=os.path.join(self.folder, "events.json"),
                          lock_path=os.path.join(self.folder, "lock"))
        self.config = {"retention_days": 3, "location": {"latitude": 40.0, "longitude": -100.0, "timezone": "America/Chicago", "radius_miles": 25},
                       "sources": [{"id": i, "name": i.upper(), "adapter": f"fake_{i}", "url": f"https://{i}.example", "coverage": {"type": "global"}}
                                   for i in ("a", "b", "c")]}
        self.adapters = {}
        self.calls = []

    def run_refresh(self):
        patched = {f"fake_{k}": v for k, v in self.adapters.items()}
        options = {"progress": lambda kind, **info: self.calls.append((kind, info))}
        with mock.patch.dict(sources_module.ADAPTERS, patched):
            return refresh_module.refresh(self.config, options, fetcher=object(), **self.paths)

    def feed(self):
        with open(self.paths["output"], encoding="utf-8") as handle:
            return json.load(handle)

    def test_events_are_published_as_each_source_finishes_and_always_validate(self):
        seen = []

        def observe(label):
            def hook():
                seen.append((label, sorted(e["title"] for e in self.feed()["events"]) if os.path.exists(self.paths["output"]) else None))
                if os.path.exists(self.paths["output"]):
                    validate_feed(self.feed())  # what a card could read at this instant is always a complete, valid feed
            return hook

        self.adapters = {"a": Adapter([record("a", "Alpha")], hook=observe("a running")), "b": Adapter([record("b", "Beta")], hook=observe("b running")),
                         "c": Adapter([record("c", "Gamma")], hook=observe("c running"))}
        self.assertEqual(self.run_refresh(), 0)
        self.assertEqual(seen, [("a running", None), ("b running", ["Alpha"]), ("c running", ["Alpha", "Beta"])])
        self.assertEqual(sorted(e["title"] for e in self.feed()["events"]), ["Alpha", "Beta", "Gamma"])

    def test_deduplication_and_verification_are_reconciled_when_a_later_source_finishes(self):
        states = []

        def watch():
            if os.path.exists(self.paths["output"]):
                states.append([(e["title"], e["verified"], len(e["sources"])) for e in self.feed()["events"]])

        self.adapters = {"a": Adapter([record("a", "Jazz Night")]), "b": Adapter([record("b", "Jazz Night")], hook=watch),
                         "c": Adapter([record("c", "Other Show", venue="Elsewhere")], hook=watch)}
        self.assertEqual(self.run_refresh(), 0)
        self.assertEqual(states[0], [("Jazz Night", False, 1)])  # one source: unverified
        final = self.feed()["events"]
        jazz = next(e for e in final if e["title"] == "Jazz Night")
        self.assertEqual((jazz["verified"], len(jazz["sources"])), (True, 2))  # the second source merged into it and corroborated it
        self.assertEqual(len(final), 2)  # not duplicated

    def test_pending_sources_are_not_reported_as_failing_in_a_partial_feed(self):
        captured = []
        self.adapters = {"a": Adapter([record("a", "Alpha")]), "b": Adapter([record("b", "Beta")], hook=lambda: captured.append(self.feed()["sources"])),
                         "c": Adapter([record("c", "Gamma")])}
        self.run_refresh()
        partial = captured[0]
        self.assertTrue(partial["a"]["ok"])
        self.assertEqual((partial["c"]["ok"], partial["c"]["pending"]), (None, True))

    def test_progress_notifications_describe_the_real_work(self):
        self.adapters = {"a": Adapter([record("a", "Alpha")]), "b": Adapter(error="site down"), "c": Adapter([])}
        self.assertEqual(self.run_refresh(), 2)
        kinds = [kind for kind, _ in self.calls]
        self.assertEqual(kinds[0], "start")
        self.assertEqual(self.calls[0][1], {"total": 3, "sources": ["A", "B", "C"]})
        done = [info for kind, info in self.calls if kind == "source_done"]
        self.assertEqual([(d["name"], d["ok"], d["empty"]) for d in done], [("A", True, False), ("B", False, False), ("C", False, True)])
        self.assertEqual([info for kind, info in self.calls if kind == "published"], [{"events": 1}, {"events": 1}])
        self.assertIn("source_processing", kinds)

    def test_an_interrupted_publish_leaves_the_previous_feed_whole(self):
        self.adapters = {"a": Adapter([record("a", "Alpha")]), "b": Adapter([record("b", "Beta")]), "c": Adapter([record("c", "Gamma")])}
        self.assertEqual(self.run_refresh(), 0)
        good = open(self.paths["output"], encoding="utf-8").read()
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(source, target):
            calls["n"] += 1
            if calls["n"] > 2:  # let the first source's cache and feed through, then fail
                raise OSError("disk full")
            return real_replace(source, target)

        with mock.patch("discovery.core.os.replace", side_effect=flaky):
            with self.assertRaises(OSError):
                self.run_refresh()
        validate_feed(json.loads(open(self.paths["output"], encoding="utf-8").read()))
        self.assertTrue(json.loads(good)["events"])
        self.assertEqual([f for f in os.listdir(self.folder) if f.endswith(".tmp")], [])


class EngineProgress(unittest.TestCase):
    """The engine turns refresh()'s notifications into per-profile progress a card can read."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = lambda: 1_800_000_000.0
        self.behaviour = {}
        self.during = []

    def refresh_fn(self, config, options, cache_path, output, lock_path, fetcher=None, location_cache_path=None, location_providers=None):
        script = self.behaviour.get(config["location"].get("query") or "default", [])
        notify = options["progress"]
        notify("start", total=3, sources=["KPBS", "DoSD", "Reader"])
        for step in script:
            kind, info = step[0], step[1] if len(step) > 1 else {}
            notify(kind, **info)
            if kind == "published":
                events = [{"id": f"e{i}", "title": f"Event {i}", "start": "2030-01-02T00:00:00+00:00", "url": "https://x.example"} for i in range(info["events"])]
                save_events({"generated_at": f"2030-01-0{info['events']}T00:00:00+00:00", "location": {"display_name": "Somewhere"}, "sources": {}, "events": events}, output)
            if kind == "source_done":
                self.during.append((config["location"].get("query"), self.engine.snapshot(self.pid)))
        return self.behaviour.get("code", 0)

    def make(self, query="Austin, TX"):
        self.engine = Engine(self.tmp.name, interval=3600, clock=self.clock, refresh_fn=self.refresh_fn, fetcher_factory=lambda: object())
        self.pid = self.engine.register({"area": query})
        return self.engine

    def full_run(self, source_events=(5, 12, 20)):
        return [("source_start", {"name": "KPBS", "index": 0}), ("source_processing", {"name": "KPBS"}), ("published", {"events": source_events[0]}),
                ("source_done", {"name": "KPBS", "ok": True, "empty": False, "events": 5, "error": None}),
                ("source_start", {"name": "DoSD", "index": 1}), ("published", {"events": source_events[1]}),
                ("source_done", {"name": "DoSD", "ok": True, "empty": False, "events": 9, "error": None}),
                ("source_start", {"name": "Reader", "index": 2}), ("published", {"events": source_events[2]}),
                ("source_done", {"name": "Reader", "ok": True, "empty": False, "events": 9, "error": None})]

    def test_progress_is_queued_then_real_activity_with_events_usable_before_the_end(self):
        self.behaviour["Austin, TX"] = self.full_run()
        engine = self.make()
        queued = engine.snapshot(self.pid)
        self.assertEqual((queued["state"], queued["progress"]["phase"], queued["initial"]), ("pending", "queued", True))
        engine.run_next()
        first = self.during[0][1]
        self.assertEqual(first["state"], "ready")  # events exist after the first source...
        self.assertEqual(len(first["events"]), 5)
        self.assertEqual((first["refreshing"], first["initial"]), (True, True))  # ...while discovery continues
        self.assertEqual((first["progress"]["phase"], first["progress"]["completed"], first["progress"]["total"], first["progress"]["events"]), ("searching", 1, 3, 5))
        self.assertEqual(self.during[1][1]["progress"]["completed"], 2)
        done = engine.snapshot(self.pid)
        self.assertEqual((done["progress"]["phase"], done["progress"]["completed"], done["outcome"], done["initial"], done["refreshing"]),
                         ("complete", 3, "complete", False, False))
        self.assertEqual(len(done["events"]), 20)

    def test_a_refresh_keeps_showing_the_existing_events_and_is_not_an_initial_discovery(self):
        self.behaviour["Austin, TX"] = self.full_run()
        engine = self.make()
        engine.run_next()
        self.during.clear()
        engine._enqueue(self.pid)
        self.assertEqual(engine.snapshot(self.pid)["progress"]["phase"], "queued")
        self.assertEqual(len(engine.snapshot(self.pid)["events"]), 20)  # queued refresh: tiles stay
        engine.run_next()
        during = self.during[0][1]
        self.assertEqual((during["state"], during["initial"], during["refreshing"], during["progress"]["phase"]), ("ready", False, True, "searching"))
        self.assertGreaterEqual(len(during["events"]), 5)

    def test_partial_no_results_and_complete_failure_are_distinguished(self):
        base = [("source_start", {"name": "KPBS", "index": 0})]
        ok = ("source_done", {"name": "KPBS", "ok": True, "empty": False, "events": 5, "error": None})
        down = ("source_done", {"name": "DoSD", "ok": False, "empty": False, "events": 0, "error": "down"})
        empty = ("source_done", {"name": "Reader", "ok": False, "empty": True, "events": 0, "error": "no events"})

        self.behaviour.update({"Austin, TX": base + [("published", {"events": 5}), ok, down], "code": 2})
        engine = self.make("Austin, TX")
        engine.run_next()
        partial = engine.snapshot(self.pid)
        self.assertEqual((partial["state"], partial["outcome"], partial["progress"]["failed"]), ("ready", "partial", 1))

        self.behaviour.update({"Nowhere": base + [empty, empty], "code": 1})
        none_pid = engine.register({"area": "Nowhere"})
        engine.run_next()
        nothing = engine.snapshot(none_pid)
        self.assertEqual((nothing["state"], nothing["outcome"]), ("error", "no_results"))
        self.assertIn("No upcoming events were found by the 2 sources searched", nothing["error"])

        self.behaviour.update({"Broken": base + [down, down], "code": 1})
        broken_pid = engine.register({"area": "Broken"})
        engine.run_next()
        failed = engine.snapshot(broken_pid)
        self.assertEqual((failed["state"], failed["outcome"]), ("error", "failed"))

    def test_a_failed_refresh_keeps_events_and_a_later_success_recovers(self):
        self.behaviour["Austin, TX"] = self.full_run()
        engine = self.make()
        engine.run_next()
        self.behaviour.update({"Austin, TX": [("source_start", {"name": "KPBS", "index": 0}), ("source_done", {"name": "KPBS", "ok": False, "empty": False, "events": 0, "error": "down"})], "code": 1})
        engine._enqueue(self.pid)
        engine.run_next()
        failed = engine.snapshot(self.pid)
        self.assertEqual((failed["state"], failed["outcome"], len(failed["events"])), ("ready", "failed", 20))
        self.assertTrue(failed["error"])
        self.behaviour.update({"Austin, TX": self.full_run(), "code": 0})
        engine._enqueue(self.pid)
        engine.run_next()
        recovered = engine.snapshot(self.pid)
        self.assertEqual((recovered["outcome"], recovered["error"]), ("complete", None))

    def test_progress_is_isolated_per_profile(self):
        self.behaviour["Austin, TX"] = self.full_run()
        self.behaviour["Boston, MA"] = [("source_start", {"name": "KPBS", "index": 0}), ("published", {"events": 2}),
                                        ("source_done", {"name": "KPBS", "ok": True, "empty": False, "events": 2, "error": None})]
        engine = self.make("Austin, TX")
        austin = self.pid
        boston = engine.register({"area": "Boston, MA"})
        self.assertEqual(engine.snapshot(boston)["progress"]["phase"], "queued")
        engine.run_next()  # Austin runs; Boston has not started
        self.assertEqual(engine.snapshot(boston)["progress"]["phase"], "queued")
        self.assertEqual(engine.snapshot(boston)["progress"]["events"], 0)
        engine.run_next()
        self.assertEqual(len(engine.snapshot(austin)["events"]), 20)
        self.assertEqual(len(engine.snapshot(boston)["events"]), 2)
        self.assertEqual(engine.snapshot(austin)["progress"]["completed"], 3)
        self.assertEqual(engine.snapshot(boston)["progress"]["completed"], 1)

    def test_every_feed_the_card_can_read_is_complete(self):
        self.behaviour["Austin, TX"] = self.full_run()
        engine = self.make()
        engine.run_next()
        for _, snapshot in self.during:
            validate_feed({"generated_at": snapshot["generated_at"], "events": snapshot["events"]})
        with self.assertRaises(FeedError):
            validate_feed({"generated_at": "x", "events": []})


if __name__ == "__main__":
    unittest.main()
