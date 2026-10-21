"""Websites added to a card supplement the automatic sources; they never replace them, and a failing one never removes their events."""
import json
import os
import sys
import tempfile
import unittest

from discovery.core import save_events
from discovery.engine import Engine
from discovery.profiles import normalize_profile, profile_id
from tests.test_engine import Clock, fake_geocode

GOOD = "https://www.example-events.org/calendar"
OTHER = "https://calendar.other-city.org"
ST_MICHAELS = "https://www.smpoway.org/calendar"
BLOCKED = "https://blocked.example/events"
DOWN = "https://down.example/events"
EMPTY = "https://empty.example/events"
AUTOMATIC = {"ut_austin"}  # the registry sources that cover the test area


def event(source, title):
    return {"id": f"{source}:{title}", "title": title, "start": "2030-01-02T00:00:00+00:00", "url": f"https://x.example/{source}/{title}", "source": {"name": source}}


class SourceRefresh:
    """Behaves like discovery.refresh for what matters here: a cache per profile folder, `only`/`offline`, per-source failure that keeps cached
    events, and no change to the feed when no source succeeded. `results` maps a source id to its titles, or to an exception."""

    def __init__(self, results):
        self.results, self.calls, self.hook = results, [], None

    def __call__(self, config, options, cache_path, output, lock_path, fetcher=None, location_cache_path=None, location_providers=None):
        self.calls.append((config, options, output))
        if self.hook:
            self.hook(len(self.calls), output)
        try:
            with open(cache_path, encoding="utf-8") as handle:
                cache = json.load(handle)
        except (OSError, ValueError):
            cache = {}
        chosen = [e["id"] for e in config["sources"] if e["coverage"]["type"] == "global" or e["id"] in AUTOMATIC]
        only = set(options.get("only") or [])
        active = [] if options.get("offline") else [sid for sid in chosen if not only or sid in only]
        failed = succeeded = 0
        for sid in active:
            answer = self.results.get(sid, [])
            entry = cache.setdefault(sid, {"events": []})
            if isinstance(answer, Exception) or not answer:
                entry.update(ok=False, error=str(answer) if isinstance(answer, Exception) else "extraction produced no events")
                failed += 1
            else:
                cache[sid] = {"ok": True, "error": None, "events": [event(sid, title) for title in answer]}
                succeeded += 1
        if active and not succeeded:
            for sid in active:
                print(f"[{sid}] FAILED: {cache[sid].get('error')}", file=sys.stderr)
            return 1  # nothing refreshed: the published feed is left exactly as it was
        events = [e for sid in chosen for e in cache.get(sid, {}).get("events", [])]
        if not events:
            return 1
        save_events(cache, cache_path)
        status = {sid: {"ok": cache.get(sid, {}).get("ok"), "events": len(cache.get(sid, {}).get("events", [])), "error": cache.get(sid, {}).get("error")} for sid in chosen}
        save_events({"generated_at": "2030-01-01T00:00:00+00:00", "location": {"display_name": "Test"}, "sources": status, "events": events}, output)
        return 2 if failed else 0


PROBES = {GOOD: {"status": "supported", "format": "jsonld", "events": 4, "name": "Good Events"},
          OTHER: {"status": "supported", "format": "ics", "events": 2, "name": "Other City"},
          ST_MICHAELS: {"status": "unsupported", "reason": "the page builds its events with JavaScript, which Area Events does not run, and it offers no calendar feed"},
          BLOCKED: {"status": "blocked", "reason": "robots.txt of blocked.example does not allow this page"},
          DOWN: {"status": "unavailable", "reason": "could not be read: HTTP 503"},
          EMPTY: {"status": "no_events", "reason": "no upcoming events were found"}}
RESULTS = {"ut_austin": ["Campus Lecture", "Library Talk"], "web_good": ["Good One", "Good Two"], "web_other": ["Other One"]}


class Additive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.refresh = SourceRefresh(dict(RESULTS))
        self.clock = Clock()
        self.engine = self.make()

    def make(self, folder=None):
        return Engine(folder or self.tmp.name, interval=3600, clock=self.clock, refresh_fn=self.refresh, fetcher_factory=lambda: object(),
                      host_check=lambda host: True, probe=lambda fetcher, url: False, geocode=fake_geocode, web_probe=lambda fetcher, url: PROBES[url])

    def ids(self, url):
        import hashlib
        return "web_" + hashlib.sha1(url.encode()).hexdigest()[:8]

    def use_ids(self):
        """The generated source ids depend on the address, so the canned results are keyed by them."""
        self.refresh.results.update({self.ids(GOOD): RESULTS["web_good"], self.ids(OTHER): RESULTS["web_other"]})

    def search(self, sources=None, engine=None):
        engine = engine or self.engine
        profile = {"area": "Austin, TX", **({"sources": sources} if sources else {})}
        pid = engine.register(profile)
        while engine.run_next():
            pass
        return pid, engine.snapshot(pid)

    def titles(self, snapshot):
        return sorted(e["title"] for e in snapshot["events"])

    def test_automatic_sources_only(self):
        _, snapshot = self.search()
        self.assertEqual(self.titles(snapshot), ["Campus Lecture", "Library Talk"])
        self.assertEqual((snapshot["state"], snapshot["error"], snapshot["report"]), ("ready", None, []))
        self.assertEqual(len(self.refresh.calls), 1)

    def test_one_successful_added_website_is_merged_with_the_automatic_events(self):
        self.use_ids()
        _, snapshot = self.search([GOOD])
        self.assertEqual(self.titles(snapshot), ["Campus Lecture", "Good One", "Good Two", "Library Talk"])
        self.assertEqual(snapshot["outcome"], "complete")
        self.assertEqual([(r["url"], r["status"], r["adapter"]) for r in snapshot["report"]], [(GOOD, "supported", "web")])
        automatic_call, added_call = self.refresh.calls
        self.assertEqual(automatic_call[1].get("only"), None)
        self.assertEqual(added_call[1]["only"], [self.ids(GOOD)])  # only the new website is read; the automatic sources are not scanned again

    def test_several_added_websites(self):
        self.use_ids()
        _, snapshot = self.search([GOOD, OTHER])
        self.assertEqual(len(snapshot["events"]), 5)
        self.assertEqual(sorted(snapshot["sources"]), sorted(["ut_austin", self.ids(GOOD), self.ids(OTHER)]))
        self.assertEqual(sorted(self.refresh.calls[1][1]["only"]), sorted([self.ids(GOOD), self.ids(OTHER)]))

    def test_an_unsupported_website_does_not_remove_the_automatic_events(self):
        _, snapshot = self.search([ST_MICHAELS])
        self.assertEqual(self.titles(snapshot), ["Campus Lecture", "Library Talk"])
        self.assertEqual((snapshot["state"], snapshot["error"], snapshot["outcome"]), ("ready", None, "complete"))
        report = snapshot["report"][0]
        self.assertEqual((report["url"], report["status"]), (ST_MICHAELS, "unsupported"))
        self.assertIn("JavaScript", report["reason"])
        self.assertEqual(len(self.refresh.calls), 1)

    def test_blocked_unavailable_and_empty_websites_are_warnings_not_failures(self):
        self.use_ids()
        _, snapshot = self.search([BLOCKED, DOWN, EMPTY, GOOD])
        self.assertEqual(self.titles(snapshot), ["Campus Lecture", "Good One", "Good Two", "Library Talk"])
        self.assertEqual((snapshot["state"], snapshot["error"]), ("ready", None))
        self.assertEqual({r["url"]: r["status"] for r in snapshot["report"]},
                         {BLOCKED: "blocked", DOWN: "unavailable", EMPTY: "no_events", GOOD: "supported"})

    def test_an_added_website_that_turns_out_to_have_no_events_keeps_everything_else(self):
        self.refresh.results[self.ids(GOOD)] = []  # readable by the probe, nothing once it is searched
        _, snapshot = self.search([GOOD])
        self.assertEqual(self.titles(snapshot), ["Campus Lecture", "Library Talk"])
        self.assertEqual(snapshot["state"], "ready")
        self.assertEqual(snapshot["outcome"], "partial")  # one source had nothing; the rest are fine

    def test_an_added_website_that_errors_keeps_the_automatic_events(self):
        self.refresh.results[self.ids(GOOD)] = RuntimeError("HTTP 503")
        _, snapshot = self.search([GOOD])
        self.assertEqual(self.titles(snapshot), ["Campus Lecture", "Library Talk"])
        self.assertEqual((snapshot["state"], snapshot["error"]), ("ready", None))

    def test_events_never_disappear_while_added_websites_are_being_discovered(self):
        self.use_ids()
        seen = []

        def hook(number, output):
            if os.path.exists(output):
                seen.append((number, sorted(e["title"] for e in json.load(open(output, encoding="utf-8"))["events"])))
        self.refresh.hook = hook
        self.search([GOOD])
        self.assertEqual(seen, [(2, ["Campus Lecture", "Library Talk"])])  # when the added website's run starts, the automatic events are already published
        # a later edit that adds a website starts from the earlier results, so the card keeps showing them
        pid = self.engine.register({"area": "Austin, TX", "sources": [GOOD, OTHER]})
        early = self.engine.snapshot(pid)
        self.assertEqual((early["state"], early["initial"]), ("ready", False))
        self.assertEqual(self.titles(early), ["Campus Lecture", "Good One", "Good Two", "Library Talk"])

    def test_adding_a_website_later_reuses_the_earlier_results(self):
        self.use_ids()
        self.search()
        calls_before = len(self.refresh.calls)
        _, snapshot = self.search([GOOD])
        later = self.refresh.calls[calls_before:]
        self.assertEqual([c[1].get("offline") for c in later], [True, None])  # rebuild from the sibling's results, then read only the new website
        self.assertEqual(later[1][1]["only"], [self.ids(GOOD)])
        self.assertEqual(self.titles(snapshot), ["Campus Lecture", "Good One", "Good Two", "Library Talk"])

    def test_removing_an_added_website_removes_only_its_events(self):
        self.use_ids()
        self.search([GOOD, OTHER])
        calls_before = len(self.refresh.calls)
        _, snapshot = self.search([GOOD])
        self.assertEqual(self.titles(snapshot), ["Campus Lecture", "Good One", "Good Two", "Library Talk"])
        _, snapshot = self.search()
        self.assertEqual(self.titles(snapshot), ["Campus Lecture", "Library Talk"])
        self.assertEqual(sorted(snapshot["sources"]), ["ut_austin"])
        new_calls = self.refresh.calls[calls_before:]
        self.assertTrue(all(c[1].get("offline") for c in new_calls))  # removal rebuilt the feed from stored results: nothing was fetched
        self.assertEqual(snapshot["report"], [])

    def test_a_website_that_failed_before_is_probed_again_and_can_start_working(self):
        self.use_ids()
        probes = dict(PROBES)
        probes[GOOD] = {"status": "unavailable", "reason": "could not be read: HTTP 503"}
        engine = Engine(self.tmp.name, interval=3600, clock=self.clock, refresh_fn=self.refresh, fetcher_factory=lambda: object(), host_check=lambda h: True,
                        probe=lambda f, u: False, geocode=fake_geocode, web_probe=lambda f, u: probes[u])
        pid, snapshot = self.search([GOOD], engine)
        self.assertEqual(self.titles(snapshot), ["Campus Lecture", "Library Talk"])
        probes[GOOD] = PROBES[GOOD]
        self.clock.now += 7200  # the next scheduled refresh
        engine.tick()
        while engine.run_next():
            pass
        self.assertEqual(self.titles(engine.snapshot(pid)), ["Campus Lecture", "Good One", "Good Two", "Library Talk"])

    def test_a_website_that_worked_stays_in_every_scheduled_run_so_its_events_never_drop_out(self):
        self.use_ids()
        pid, _ = self.search([GOOD])
        self.clock.now += 7200
        self.engine.tick()
        while self.engine.run_next():
            pass
        ran = self.refresh.calls[-1][0]["sources"]
        self.assertIn(self.ids(GOOD), [e["id"] for e in ran])
        self.assertEqual(self.titles(self.engine.snapshot(pid)), ["Campus Lecture", "Good One", "Good Two", "Library Talk"])

    def test_a_registered_source_that_is_also_typed_in_is_scanned_once(self):
        _, snapshot = self.search(["https://calendar.utexas.edu/events"])
        sources = self.refresh.calls[0][0]["sources"]
        self.assertEqual([e["id"] for e in sources].count("ut_austin"), 1)
        self.assertEqual(len(self.refresh.calls), 1)  # nothing extra to read
        self.assertEqual((snapshot["report"][0]["adapter"], snapshot["report"][0]["id"]), ("localist", "ut_austin"))
        self.assertEqual(self.titles(snapshot), ["Campus Lecture", "Library Talk"])

    def test_naming_a_registered_source_outside_its_area_still_reads_it(self):
        self.refresh.results["kpbs"] = ["Public Media Show"]
        _, snapshot = self.search(["https://www.kpbs.org/events"])
        self.assertEqual(self.titles(snapshot), ["Campus Lecture", "Library Talk", "Public Media Show"])

    def test_the_same_address_in_two_spellings_is_one_website(self):
        self.use_ids()
        a = normalize_profile({"area": "Austin, TX", "sources": [GOOD + "/", GOOD.upper().replace("HTTPS", "https", 1)]})
        self.assertEqual(len(a["sources"]), 2)  # paths are case sensitive, so only the host is folded
        b = normalize_profile({"area": "Austin, TX", "sources": [GOOD, GOOD + "/"]})
        self.assertEqual(b["sources"], [GOOD])

    def test_existing_saved_profiles_keep_working_after_a_restart(self):
        self.use_ids()
        pid, _ = self.search([GOOD])
        folder = os.path.join(self.tmp.name, "profiles", pid)
        saved = json.load(open(os.path.join(folder, "profile.json"), encoding="utf-8"))
        self.assertEqual(saved["id"], profile_id(normalize_profile({"area": "Austin, TX", "sources": [GOOD]})))  # identity is unchanged
        restarted = self.make()
        before = restarted.snapshot(pid)
        self.assertEqual((before["state"], self.titles(before)), ("ready", ["Campus Lecture", "Good One", "Good Two", "Library Talk"]))
        self.assertEqual([r["url"] for r in before["report"]], [GOOD])
        self.clock.now += 7200
        restarted.tick()
        while restarted.run_next():
            pass
        self.assertEqual(self.titles(restarted.snapshot(pid)), ["Campus Lecture", "Good One", "Good Two", "Library Talk"])

    def test_a_profile_saved_before_this_change_with_no_stored_report_still_runs(self):
        self.use_ids()
        pid, _ = self.search([GOOD])
        path = os.path.join(self.tmp.name, "profiles", pid, "profile.json")
        saved = json.load(open(path, encoding="utf-8"))
        saved["report"] = []  # older versions did not keep a usable report for working websites
        json.dump(saved, open(path, "w", encoding="utf-8"))
        restarted = self.make()
        self.clock.now += 7200
        restarted.tick()
        while restarted.run_next():
            pass
        self.assertEqual(len(restarted.snapshot(pid)["events"]), 4)
        self.assertEqual(restarted.snapshot(pid)["report"][0]["status"], "supported")

    def saved_profile(self, sources, **fields):
        """Write a profile.json as an earlier version of the app left it."""
        profile = normalize_profile({"area": "Austin, TX", "sources": sources})
        pid = profile_id(profile)
        folder = os.path.join(self.tmp.name, "profiles", pid)
        os.makedirs(folder)
        record = {"id": pid, "profile": profile, "requested_at": self.clock(), "state": "pending", "error": None, "attempt_at": None, "success_at": None,
                  "report": [], "outcome": None, "permanent": False, "place_key": None, **fields}
        json.dump(record, open(os.path.join(folder, "profile.json"), "w", encoding="utf-8"))
        return pid

    def test_an_error_left_by_the_old_source_only_behaviour_is_not_reported_while_the_new_run_publishes_events(self):
        old = "none of the requested websites can be read by an available adapter"
        pid = self.saved_profile([ST_MICHAELS, GOOD], state="error", error=old, outcome="failed", attempt_at=self.clock())
        self.use_ids()
        engine = self.make()  # loading the saved profile queues it again
        seen = []
        self.refresh.hook = lambda number, output: seen.append((number, engine.snapshot(pid)))
        while engine.run_next():
            pass
        first, second = seen
        self.assertEqual((first[1]["error"], first[1]["outcome"], first[1]["state"]), (None, None, "discovering"))  # the old message is gone as soon as the run starts
        self.assertEqual((second[1]["error"], second[1]["state"], len(second[1]["events"])), (None, "ready", 2))  # events visible, no stale failure, while the added site is read
        done = engine.snapshot(pid)
        self.assertEqual((done["error"], done["state"], done["outcome"]), (None, "ready", "complete"))
        self.assertEqual(self.titles(done), ["Campus Lecture", "Good One", "Good Two", "Library Talk"])
        self.assertEqual({r["url"]: r["status"] for r in done["report"]}, {ST_MICHAELS: "unsupported", GOOD: "supported"})

    def test_a_genuine_failure_is_reported_and_cleared_by_the_next_successful_run(self):
        self.refresh.results["ut_austin"] = RuntimeError("all sources are down")
        pid, snapshot = self.search([ST_MICHAELS])
        self.assertEqual((snapshot["state"], snapshot["outcome"]), ("error", "failed"))
        self.assertIn("all sources are down", snapshot["error"])
        self.refresh.results["ut_austin"] = ["Campus Lecture"]
        self.clock.now += 7200
        self.engine.register({"area": "Austin, TX", "sources": [ST_MICHAELS]})
        while self.engine.run_next():
            pass
        recovered = self.engine.snapshot(pid)
        self.assertEqual((recovered["state"], recovered["error"], recovered["outcome"]), ("ready", None, "complete"))
        self.assertEqual(self.titles(recovered), ["Campus Lecture"])

    def test_a_failed_refresh_keeps_the_events_and_a_later_run_clears_the_message(self):
        pid, _ = self.search([ST_MICHAELS])
        self.refresh.results["ut_austin"] = RuntimeError("site down")
        self.clock.now += 7200
        self.engine.tick()
        while self.engine.run_next():
            pass
        failed = self.engine.snapshot(pid)
        self.assertEqual((failed["state"], self.titles(failed)), ("ready", ["Campus Lecture", "Library Talk"]))
        self.assertIn("site down", failed["error"])
        self.refresh.results["ut_austin"] = ["Campus Lecture"]
        self.clock.now += 7200
        self.engine.tick()
        while self.engine.run_next():
            pass
        self.assertEqual((self.engine.snapshot(pid)["error"], self.titles(self.engine.snapshot(pid))), (None, ["Campus Lecture"]))


if __name__ == "__main__":
    unittest.main()
