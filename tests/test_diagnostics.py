"""The shared issue classifier (custom_components/area_events/diagnostics.py) and the two views that use it: a card's area view (the issue badge)
and the management snapshot (the Overview). Pure data in, issues out; the hub tests use an in-memory read client, so no service and no live API."""
import importlib
import unittest

from tests.test_ha_integration import hub_module, run   # installs the Home Assistant stand-ins and the area_events_ha package

diagnostics = importlib.import_module("area_events_ha.diagnostics")

NOW = 1_800_000_000
HOUR = 3600


def source(key, enabled=True, last_success_at=NOW - HOUR, last_error=None):
    return {"source_key": key, "enabled": enabled, "eligible": enabled, "last_success_at": last_success_at, "last_error": last_error}


def area(key, name, sources, enabled=True):
    return {"area_key": key, "name": name, "enabled": enabled, "sources": list(sources), "intensity": "normal", "revision": 1, "radius": 20, "unit": "mi"}


NAMES = {"cityweb": "City Calendar", "library": "Library Events", "serp_crossville": "Google Crossville", "serp_cookeville": "Google Cookeville"}
TYPES = {"cityweb": "localist", "library": "localist", "serp_crossville": "serpapi_google_events", "serp_cookeville": "serpapi_google_events"}


def classify(sources, areas, paused=False, usage=None, now=NOW, connected=True, error=None):
    status = {"settings": {"paused": paused, "revision": 3}, "runtime": {"state": "running"}, "sources": sources}
    return diagnostics.classify(connected, error, status, areas, NAMES, TYPES, usage, now)


def kinds(found):
    return [i["kind"] for i in found["issues"]]


class Classifier(unittest.TestCase):
    def test_a_one_failed_source_and_a_stale_area_are_two_issues(self):
        found = classify([source("cityweb", last_success_at=NOW - 20 * HOUR, last_error="HTTP 503 from the calendar"),
                          source("library", last_success_at=NOW - 13 * HOUR)], [area("jt", "Jamestown", ["cityweb", "library"])])
        self.assertEqual(sorted(kinds(found)), ["area_stale", "source_failing"])
        failing = next(i for i in found["issues"] if i["kind"] == "source_failing")
        self.assertEqual((failing["summary"], failing["detail"], failing["source_key"], failing["severity"]),
                         ("City Calendar — last scan failed", "HTTP 503 from the calendar", "cityweb", "error"))
        self.assertEqual(failing["last_success_at"], NOW - 20 * HOUR)
        stale = next(i for i in found["issues"] if i["kind"] == "area_stale")
        self.assertEqual((stale["summary"], stale["area_keys"], stale["last_success_at"]), ("Jamestown — event information is stale", ["jt"], NOW - 13 * HOUR))

    def test_b_two_failed_sources_and_a_stale_area_are_three_issues_each_source_counted_once(self):
        found = classify([source("cityweb", last_success_at=NOW - 20 * HOUR, last_error="timeout"),
                          source("library", last_success_at=NOW - 14 * HOUR, last_error="HTTP 404")], [area("jt", "Jamestown", ["cityweb", "library"])])
        self.assertEqual(sorted(kinds(found)), ["area_stale", "source_failing", "source_failing"])
        self.assertEqual(len({i["id"] for i in found["issues"]}), 3)

    def test_c_a_healthy_area_has_no_issue_and_d_a_cleared_failure_removes_its_issue(self):
        areas = [area("sd", "San Diego", ["cityweb", "library"])]
        self.assertEqual(kinds(classify([source("cityweb", last_error="timeout"), source("library")], areas)), ["source_failing"])
        healthy = classify([source("cityweb"), source("library")], areas)   # the next successful scan cleared the job's error
        self.assertEqual(healthy["issues"], [])
        self.assertEqual([i["kind"] for i in healthy["upcoming"]], ["area_stale"], "staleness is only due later")

    def test_e_a_disabled_source_or_a_switched_off_area_is_not_counted(self):
        self.assertEqual(classify([source("cityweb", enabled=False, last_error="old failure"), source("library")],
                                  [area("sd", "San Diego", ["cityweb", "library"])])["issues"], [])
        self.assertEqual(classify([source("cityweb", last_success_at=NOW - 99 * HOUR, last_error="down")],
                                  [area("sd", "San Diego", ["cityweb"], enabled=False), area("nb", "Nobody", [], enabled=False)])["issues"], [])

    def test_f_paused_discovery_is_one_accurate_issue_and_staleness_is_not_counted_again(self):
        found = classify([source("cityweb", last_success_at=NOW - 30 * HOUR)], [area("sd", "San Diego", ["cityweb"]), area("au", "Austin", ["cityweb"])], paused=True)
        self.assertEqual(kinds(found), ["discovery_paused"])
        paused = found["issues"][0]
        self.assertEqual((paused["summary"], paused["area_keys"], paused["severity"]), ("Discovery is paused", [], "warning"))
        self.assertIn("No source is being scanned", paused["detail"])
        self.assertEqual(found["upcoming"], [])

    def test_g_an_area_without_sources_says_what_to_do(self):
        found = classify([], [area("nw", "Nowhere", [])])
        self.assertEqual(kinds(found), ["area_no_sources"])
        self.assertEqual(found["issues"][0]["action"], "Assign at least one producer to Nowhere under Areas.")

    def test_h_a_serpapi_usage_hold_is_one_account_issue_with_its_own_guidance_never_a_failure(self):
        usage = {"serpapi": {"state": "blocked_free_tier", "reason": "5 free searches remain; the free-tier safety stop keeps them", "resumes": "2026-11-20",
                             "account": {"renewal": "2026-11-20"},
                             "sources": {"serp_crossville": {"state": "blocked_free_tier", "reason": "5 free searches remain; the free-tier safety stop keeps them"},
                                         "serp_cookeville": {"state": "blocked_free_tier", "reason": "5 free searches remain; the free-tier safety stop keeps them"}}}}
        error = "SerpApi search not made: 5 free searches remain"
        found = classify([source("serp_crossville", last_error=error), source("serp_cookeville", last_error=error), source("cityweb")],
                         [area("cv", "Crossville", ["serp_crossville", "cityweb"]), area("ck", "Cookeville", ["serp_cookeville"])], usage=usage)
        self.assertEqual(kinds(found), ["serpapi_held"], "one account hold, not one failure per SerpApi source")
        held = found["issues"][0]
        self.assertEqual(held["summary"], "Google Events searches stopped at the free-tier safety limit")
        self.assertIn("renewal 2026-11-20", held["action"])
        self.assertIn("Affected: Google Cookeville, Google Crossville", held["detail"])
        self.assertEqual(held["area_keys"], ["ck", "cv"])
        self.assertEqual(kinds(diagnostics.for_area(found, "cv")), ["serpapi_held"])
        unread = classify([source("serp_crossville", last_error=error)], [area("cv", "Crossville", ["serp_crossville"])], usage=None)
        self.assertEqual(kinds(unread), ["source_failing"], "without a usage report the recorded error is shown as it is, never guessed to be a quota")

    def test_i_an_unreachable_service_is_reported_and_nothing_older_is_presented_as_current(self):
        found = classify([source("cityweb", last_error="timeout")], [area("sd", "San Diego", ["cityweb"])], connected=False, error="connection refused")
        self.assertEqual(kinds(found), ["service_unreachable"])
        self.assertEqual((found["issues"][0]["detail"], found["issues"][0]["severity"]), ("connection refused", "error"))

    def test_p_a_stale_area_becomes_active_at_its_moment_without_a_new_reading(self):
        found = classify([source("cityweb", last_success_at=NOW - 11 * HOUR)], [area("sd", "San Diego", ["cityweb"])])
        self.assertEqual(diagnostics.active(found, NOW), [])
        moment = found["upcoming"][0]["active_from"]
        self.assertEqual(moment, NOW - 11 * HOUR + 12 * HOUR + 1)
        self.assertEqual([i["kind"] for i in diagnostics.active(found, moment)], ["area_stale"])
        self.assertEqual(kinds(classify([source("cityweb", last_success_at=NOW - 11 * HOUR)], [area("sd", "San Diego", ["cityweb"])], now=moment)), ["area_stale"],
                         "a later reading lists it as active: the same rule")

    def test_one_source_in_two_areas_is_one_issue_overall_and_one_in_each_area(self):
        found = classify([source("cityweb", last_error="timeout")], [area("sd", "San Diego", ["cityweb"]), area("au", "Austin", ["cityweb"])])
        self.assertEqual(len(found["issues"]), 1)
        self.assertEqual(found["issues"][0]["area_keys"], ["au", "sd"])
        for key in ("sd", "au"):
            self.assertEqual(len(diagnostics.for_area(found, key)["issues"]), 1)
        self.assertEqual(diagnostics.for_area(found, "elsewhere")["issues"], [])

    def test_details_are_bounded(self):
        found = classify([source("cityweb", last_error="x" * 5000)], [area("sd", "San Diego", ["cityweb"])])
        self.assertLessEqual(len(found["issues"][0]["detail"]), diagnostics.DETAIL_LIMIT)

    def test_a_robots_txt_refusal_says_the_site_prohibits_automated_access(self):
        error = "all configured websites failed: robots.txt of www.facebook.com does not allow this page (2 URL reads)"
        found = classify([source("cityweb", last_error=error)], [area("jt", "Jamestown", ["cityweb"])])
        failing = found["issues"][0]
        self.assertEqual((failing["kind"], failing["detail"]), ("source_failing", error), "the recorded failure is shown as it is")
        self.assertEqual(failing["action"], "This website prohibits automated access. Use another event source that permits collection.")
        for advice in ("retries", "URL", "settings"):
            self.assertNotIn(advice, failing["action"], "never suggests retrying, changing addresses or working around robots.txt")

    def test_an_ordinary_source_error_keeps_its_guidance(self):
        for error in ("HTTP 503 from the calendar", "all configured websites failed: example.org: timed out", "robots.txt could not be read: timed out"):
            with self.subTest(error):
                failing = classify([source("cityweb", last_error=error)], [area("jt", "Jamestown", ["cityweb"])])["issues"][0]
                self.assertEqual(failing["action"],
                                 "Open Sources and check this producer's settings and the site it reads. The next scheduled scan retries by itself.")


class MemoryRead:
    """The read client's interface over plain data: one area per test, its sources' status, and an optional usage report."""

    def __init__(self, sources, areas, usage=None, paused=False):
        self.source_status, self.area_list, self.report, self.paused, self.calls = sources, areas, usage, paused, []

    async def areas(self):
        return [dict(a) for a in self.area_list]

    async def status(self):
        self.calls.append("status")
        return {"settings": {"paused": self.paused, "revision": 3}, "runtime": {"state": "running"}, "sources": [dict(s) for s in self.source_status],
                "areas": len(self.area_list), "areas_enabled": len(self.area_list)}

    async def sources(self):
        return [{"id": key, "name": NAMES[key], "type": TYPES[key], "enabled": True, "revision": 1} for key in NAMES]

    async def processing(self):
        return {"pending": 0}

    async def events(self, area, after=None, limit=200, **window):
        return {"events": [], "next_cursor": None, "latest_seq": 1}

    async def changes(self, after, limit):
        return {"changes": [], "next_seq": after}

    async def usage(self):
        self.calls.append("usage")
        return self.report

    async def producer_types(self):
        return {"types": []}

    async def trashcan(self):
        return None


class Views(unittest.TestCase):
    def scenario(self, read, steps=()):
        clock = [NOW]
        hub = hub_module.DiscoveryHub(read, clock=lambda: clock[0])
        messages = []

        async def go():
            await hub.subscribe("Jamestown", messages.append)
            for step in steps:
                step(read, clock)
                await hub.refresh()
            return await hub.management_snapshot()
        return hub, messages, run(go())

    def test_j_the_card_view_and_the_overview_count_the_same_issues(self):
        read = MemoryRead([source("cityweb", last_success_at=NOW - 20 * HOUR, last_error="timeout"), source("library", last_success_at=NOW - 13 * HOUR)],
                          [area("jt", "Jamestown", ["cityweb", "library"])])
        _, messages, snapshot = self.scenario(read)
        card = diagnostics.active(messages[-1]["diagnostics"], NOW)
        overview = diagnostics.active(diagnostics.for_area(snapshot["diagnostics"], "jt"), NOW)
        self.assertEqual(len(card), 2)
        self.assertEqual([i["id"] for i in card], [i["id"] for i in overview])
        self.assertEqual(read.calls.count("usage"), 1, "only the snapshot's own read, as before: no SerpApi source has an error, so the card's view never asks")

    def test_o_a_status_only_change_reaches_the_card_without_any_new_event(self):
        read = MemoryRead([source("cityweb", last_error="timeout"), source("library")], [area("jt", "Jamestown", ["cityweb", "library"])])

        def recovered(read, clock):
            read.source_status[0] = source("cityweb")
        _, messages, _ = self.scenario(read, [recovered])
        self.assertEqual([len(m["diagnostics"]["issues"]) for m in messages], [1, 0], "one push when the failure clears, with the same (empty) events")
        self.assertEqual(messages[-1]["events"], [])

    def test_paused_discovery_is_an_issue_and_no_longer_a_notice(self):
        read = MemoryRead([source("cityweb")], [area("jt", "Jamestown", ["cityweb"])], paused=True)
        _, messages, _ = self.scenario(read)
        self.assertEqual([i["kind"] for i in messages[-1]["diagnostics"]["issues"]], ["discovery_paused"])
        self.assertNotIn("paused", messages[-1]["notice"] or "")

    def test_the_usage_report_is_read_only_to_explain_a_serpapi_error_and_at_most_every_five_minutes(self):
        usage = {"serpapi": {"state": "paused_threshold", "reason": "200 searches used", "resumes": "2026-11-20",
                             "sources": {"serp_crossville": {"state": "paused_threshold", "reason": "200 searches used"}}}}
        read = MemoryRead([source("serp_crossville", last_error="SerpApi search not made: 200 searches used")], [area("jt", "Jamestown", ["serp_crossville"])], usage)

        def later(seconds):
            def step(read, clock):
                clock[0] += seconds
            return step
        _, messages, _ = self.scenario(read, [later(10), later(10), later(300)])
        self.assertEqual(read.calls.count("usage"), 3, "subscribe, then once more after five minutes (refreshes in between reuse it), then the snapshot")
        self.assertEqual([i["kind"] for i in messages[-1]["diagnostics"]["issues"]], ["serpapi_held"])
        self.assertIn("raise “Pause at”", messages[-1]["diagnostics"]["issues"][0]["action"])


if __name__ == "__main__":
    unittest.main()
