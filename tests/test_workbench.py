"""The Event Analysis Workbench: it runs the production functions, makes no network request on a fixture, records evidence faithfully and invents nothing."""
import json
import os
import re
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from discovery import classifiers, classify, evidence, refresh, workbench
from discovery.core import FetchError
from discovery.safe import SafeFetcher
from discovery.sources import web
from tests.test_modules import ConstantClassifier
from tests.test_site_discovery import Client, day, written
from tests.test_web import SITE, event, html, ld

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "tests", "data", "workbench")
PIZZARELLI = os.path.join(DATA, "pizzarelli-dear-mr-bennett.html")
ST_AGNES = os.path.join(DATA, "st-agnes-craft-fair.json")
READER = "https://www.sandiegoreader.com/events/2026/oct/24/john-pizzarelli-presents-dear-mr-be/"
FIXTURE_NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def analyze_pizzarelli_fixture():
    with mock.patch.object(web, "datetime") as clock:
        clock.now.return_value = FIXTURE_NOW
        return workbench.analyze(PIZZARELLI, fixture=True, base_url=READER, now=FIXTURE_NOW)


def future_fixture(directory, title="Spring Fair"):
    """A page with one structured event and one event written as prose, both in the future."""
    body = ld(event(title, 4, location={"@type": "Place", "name": "Hall", "address": {"@type": "PostalAddress", "streetAddress": "12 Oak Street", "addressLocality": "San Diego", "addressRegion": "CA"}}))
    body += f"<p>{written(9, '6:30 PM')} Harvest Dinner</p><p>Location: Parish Hall</p>"
    path = os.path.join(directory, "page.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html(body))
    return path


class Production(unittest.TestCase):
    def test_the_workbench_calls_the_production_functions_and_reports_what_they_returned(self):
        with tempfile.TemporaryDirectory() as folder:
            path = future_fixture(folder)
            with mock.patch.object(web, "crawl", wraps=web.crawl) as crawl, mock.patch.object(web, "discover", wraps=web.discover) as discover, \
                    mock.patch.object(refresh, "build_document", wraps=refresh.build_document) as build:
                report = workbench.analyze(path, fixture=True, base_url=SITE + "/events")
        self.assertTrue(crawl.called and discover.called and build.called)
        feed_events = report["stages"]["output"]["events"]
        self.assertEqual(len(feed_events), 1)
        # the classification stage is the classifier's own decision for the event the feed builder produced
        for row, event_ in zip(report["stages"]["classification"]["events"], feed_events):
            self.assertEqual(row["decision"], classify.classify(event_))
            self.assertEqual({k: row["decision"][k] for k in ("category", "strength", "reasons", "fallback")}, event_["illustration"])

    def test_a_replacement_classifier_is_what_the_workbench_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            path = future_fixture(folder)
            with classifiers.use(ConstantClassifier("halloween")):
                report = workbench.analyze(path, fixture=True, base_url=SITE + "/events")
        self.assertEqual(report["stages"]["classification"]["classifier"], "constant-halloween")
        self.assertEqual({e["illustration"]["category"] for e in report["stages"]["output"]["events"]}, {"halloween"})

    def test_every_stage_of_the_pipeline_is_present(self):
        with tempfile.TemporaryDirectory() as folder:
            report = workbench.analyze(future_fixture(folder), fixture=True, base_url=SITE + "/events")
        self.assertEqual(set(report["stages"]), {"discovery", "extraction", "classification", "geolocation", "validation", "output"})
        self.assertEqual([key for key, _, _ in workbench.STAGES], ["discovery", "extraction", "classification", "geolocation", "validation", "output"])
        extraction = report["stages"]["extraction"]
        self.assertEqual((extraction["candidates_found"], extraction["events_produced"]), (1, 1))
        self.assertEqual([c["kind"] for c in extraction["candidates"]], ["jsonld"], "text is read only when nothing structured is found")
        self.assertEqual(report["stages"]["validation"]["feed_check"], "valid")


class Offline(unittest.TestCase):
    def test_fixture_analysis_makes_no_network_request_and_builds_no_fetcher(self):
        def refuse(*args, **kwargs):
            raise AssertionError("the network was touched")

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(socket, "socket", refuse), mock.patch.object(socket, "getaddrinfo", refuse), \
                mock.patch.object(workbench, "SafeFetcher", refuse), mock.patch.object(web, "SafeFetcher", refuse):
            report = workbench.analyze(future_fixture(folder), fixture=True, base_url=SITE + "/events")
        self.assertTrue(report["tool"]["offline"])
        self.assertEqual(report["limits"]["geocoding_lookups_allowed"], 0)
        for row in report["stages"]["geolocation"]["events"]:
            self.assertEqual(row["status"], "not attempted: offline analysis")
            self.assertIsNone(row["lookup"], "no geocoding result is shown that was not obtained")

    def test_an_address_outside_the_fixture_is_refused_not_fetched(self):
        manifest = {"base_url": SITE + "/", "pages": {SITE + "/": html('<a href="/calendar">Calendar</a><a href="https://other.example/events">Other</a>')}}
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "m.json")
            with open(path, "w") as handle:
                json.dump(manifest, handle)
            report = workbench.analyze(path, fixture=True)
        errors = [p["error"] for p in report["stages"]["discovery"]["pages"] if not p["ok"]]
        self.assertTrue(errors and all("offline fixture analysis" in e for e in errors))
        self.assertTrue(all("other.example" not in p["url"] for p in report["stages"]["discovery"]["pages"]))

    def test_fixture_formats(self):
        with tempfile.TemporaryDirectory() as folder:
            node = {"@context": "https://schema.org", "@type": "Event", "name": "From JSON-LD", "startDate": event("x", 5)["startDate"], "url": SITE + "/e/x"}
            path = os.path.join(folder, "ld.json")
            with open(path, "w") as handle:
                json.dump(node, handle)
            report = workbench.analyze(path, fixture=True)
            self.assertEqual([e["title"] for e in report["stages"]["output"]["events"]], ["From JSON-LD"])
            bad = os.path.join(folder, "bad.txt")
            with open(bad, "w") as handle:
                handle.write("x")
            with self.assertRaises(ValueError):
                workbench.analyze(bad, fixture=True)
            other = os.path.join(folder, "other.json")
            with open(other, "w") as handle:
                json.dump({"hello": "world"}, handle)
            with self.assertRaises(ValueError):
                workbench.analyze(other, fixture=True)


class Limits(unittest.TestCase):
    def test_url_analysis_goes_through_the_request_limits_and_the_safe_fetcher(self):
        links = "".join(f'<a href="/events-{i}">Events {i}</a>' for i in range(30))
        pages = {SITE + "/": html(links)}
        pages.update({f"{SITE}/events-{i}": html("".join(f'<a href="/events-{i}-{j}">Events</a>' for j in range(10))) for i in range(30)})
        client = Client(pages)
        report = workbench.analyze(SITE + "/", client=client)
        self.assertLessEqual(report["stages"]["discovery"]["requests_made"], web.MAX_REQUESTS)
        self.assertEqual(report["stages"]["discovery"]["requests_made"], len(client.urls))
        self.assertEqual(report["limits"]["max_requests"], web.MAX_REQUESTS)

    def test_private_and_non_web_addresses_are_refused(self):
        report = workbench.analyze("http://127.0.0.1/events")
        self.assertIn("refused", report["stages"]["discovery"]["status"])
        self.assertEqual(report["stages"]["extraction"]["events_produced"], 0)
        for bad in ("file:///etc/passwd", "ftp://example.org/x", "javascript:alert(1)", "http://user:pass@example.org/"):
            with self.assertRaises(ValueError):
                workbench.analyze(bad)

    def test_url_mode_uses_the_production_safe_fetcher(self):
        seen = []

        class Spy(SafeFetcher):
            def fetch(self, url, *args, **kwargs):
                seen.append(url)
                raise FetchError("stopped by the test")

        with mock.patch.object(workbench, "SafeFetcher", Spy):
            report = workbench.analyze("https://example.org/events")
        self.assertEqual(seen, ["https://example.org/events"])
        self.assertIn("could not be read", report["stages"]["discovery"]["status"])


class Evidence(unittest.TestCase):
    def source_texts(self, report, pages):
        return [text for _, _, text in workbench.evidence_sources(pages, set(pages))]

    def test_a_quote_is_text_that_really_is_in_the_source(self):
        with open(PIZZARELLI, encoding="utf-8") as handle:
            page = handle.read()
        report = analyze_pizzarelli_fixture()
        texts = self.source_texts(report, {READER: page})
        refs = list(report["evidence"].values())[0]
        located = [r for r in refs if r.get("quote")]
        self.assertTrue(len(located) >= 5)
        for ref in located:
            self.assertTrue(any(ref["quote"].lower() in text.lower() for text in texts), ref)
            self.assertIn(ref["quote"].lower(), ref["context"].lower())
            self.assertIn(ref["found_in"], ("visible text", "page markup"))
        for ref in refs:
            if "quote" not in ref:
                self.assertNotIn("context", ref, "no supporting text is shown for a value that was not located")
        by_field = {r["field"]: r for r in refs if r["kind"] != "inferred" or r["field"] == "start"}
        self.assertEqual(by_field["title"]["quote"], "John Pizzarelli: Dear Mr. Bennett")
        self.assertEqual(by_field["start"]["quote"], "2026-10-24T17:30-0700")
        self.assertEqual(by_field["start"]["kind"], "inferred", "an ISO value normalised from the source's own is inferred, not extracted")
        self.assertEqual(by_field["address"]["quote"], "7600 Fay Avenue")
        self.assertIn("composed from the source's structured fields", by_field["address"]["note"])

    def test_a_value_that_is_not_in_the_source_says_so_and_has_no_invented_text(self):
        found = evidence.locate("a sentence that is nowhere", [("u", "visible text", "some other page text")])
        self.assertIsNone(found)
        self.assertIsNone(evidence.locate("", [("u", "visible text", "text")]))
        ref = evidence.reference("venue", "x", "extracted", "m")
        self.assertEqual(set(ref), {"field", "value", "kind", "recorded_by"})
        self.assertEqual(evidence.locate("Craft  FAIR", [("u", "visible text", "Annual craft fair today")])["quote"], "craft fair")

    def test_text_events_keep_the_exact_line_they_were_read_from(self):
        report = workbench.analyze(ST_AGNES, fixture=True)
        events = report["stages"]["output"]["events"]
        self.assertEqual(len(events), 1)
        refs = report["evidence"][events[0]["id"]]
        start = next(r for r in refs if r["field"] == "start")
        self.assertIn("Holiday Craft Fair Sunday, November 22, 2026, from 9:00 a.m. to 1:00 p.m.", start["quote"])
        self.assertEqual(start["kind"], "inferred")
        self.assertEqual(report["stages"]["extraction"]["candidates"][0]["source_text"], start["quote"])

    def test_classification_reasons_are_the_classifiers_own_and_are_located_by_word_stem(self):
        with tempfile.TemporaryDirectory() as folder:
            path = future_fixture(folder, title="Jazz Concert")
            report = workbench.analyze(path, fixture=True, base_url=SITE + "/events")
        first = report["stages"]["output"]["events"][0]
        refs = [r for r in report["evidence"][first["id"]] if r["field"] == "category"]
        self.assertTrue(refs)
        reasons = first["illustration"]["reasons"]
        self.assertEqual(len(refs), len(reasons))
        for ref, reason in zip(refs, reasons):
            self.assertIn(reason, ref["note"])
            self.assertEqual(ref["recorded_by"], "classifiers/keyword-lexicon")
            if "quote" in ref:
                self.assertIn(ref["quote"].lower(), first["title"].lower() + " " + (first.get("description") or "").lower() + " " + (first.get("venue") or "").lower())


class Findings(unittest.TestCase):
    """The workbench shows what the production pipeline does, shortcomings included; nothing here is tuned to look better."""

    def test_the_pizzarelli_concert_is_not_classified_and_the_word_chemistry_is_never_read(self):
        report = analyze_pizzarelli_fixture()
        row = report["stages"]["classification"]["events"][0]
        self.assertEqual(row["decision"]["category"], "general")
        self.assertTrue(row["decision"]["fallback"])
        self.assertIn("conflicting evidence", row["decision"]["reasons"][0])
        fields = row["input_fields"]
        self.assertGreater(fields["description_chars_available"], len(fields["description_read"]))
        self.assertEqual(len(fields["description_read"]), classify.DESCRIPTION_CHARS)
        self.assertNotIn("chemistry", fields["description_read"].lower())
        self.assertNotIn("science", row["decision"]["scores"])
        self.assertTrue(any(n["stage"] == "classification" for n in report["notices"]))

    def test_the_craft_fair_prose_yields_an_event_whose_weaknesses_are_visible(self):
        report = workbench.analyze(ST_AGNES, fixture=True)
        event_ = report["stages"]["output"]["events"][0]
        self.assertEqual(event_["start"][:16], "2026-11-22T09:00")
        self.assertIsNone(event_["venue"])
        self.assertIsNone(event_["address"])
        row = report["stages"]["geolocation"]["events"][0]
        self.assertTrue(row["status"].startswith("no location candidate"))
        self.assertTrue(any(n["stage"] == "geolocation" for n in report["notices"]))
        discovery = report["stages"]["discovery"]
        self.assertTrue(any(d["kind"] == "pdf" for d in discovery["documents"]))
        self.assertTrue(any(not d["extracted"] for d in discovery["documents"]))


class Reports(unittest.TestCase):
    def test_both_reports_are_written_and_the_html_is_standalone_and_escaped(self):
        with tempfile.TemporaryDirectory() as folder:
            hostile = '<script>alert(1)</script> Fish & Chips'
            data = json.dumps(event(hostile, 5)).replace("</", "<\\/")  # valid JSON that cannot close the script element early
            body = f'<script type="application/ld+json">{data}</script>' + f"<p>{written(7, '7:00 PM')} Craft <img src=x onerror=alert(1)> Night</p>"
            path = os.path.join(folder, "p.html")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(html(body))
            with mock.patch.dict(os.environ, {"HA_TOKEN": "secret-token-1234567890", "SUPERVISOR_TOKEN": "supervisor-secret-abcdef"}):
                report = workbench.analyze(path, fixture=True, base_url=SITE + "/events")
                json_path, html_path = workbench.write_reports(report, os.path.join(folder, "out"))
            with open(json_path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            with open(html_path, encoding="utf-8") as handle:
                page = handle.read()
            self.assertEqual(loaded["tool"]["mode"], "fixture")
            self.assertNotIn("<script", page.lower())
            self.assertNotIn("<img", page.lower())
            self.assertIn("&lt;script&gt;", page)
            self.assertFalse(re.search(r"(src|href)=[\"']?https?:", page), "no external resource")
            self.assertNotIn("<link", page.lower())
            for text in (page, json.dumps(loaded)):
                self.assertNotIn("secret-token-1234567890", text)
                self.assertNotIn("supervisor-secret-abcdef", text)
            self.assertLess(len(page), 400_000)

    def test_reports_go_to_the_git_ignored_review_folder_by_default(self):
        self.assertTrue(os.path.normpath(workbench.default_output()).endswith(os.path.join("review", "workbench")))
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as handle:
            self.assertIn("/review/", handle.read())

    def test_the_command_line_runs_on_a_fixture_without_a_network(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("network")):
            code = workbench.main(["fixture", ST_AGNES, "--out", folder])
            self.assertEqual(code, 0)
            self.assertEqual(sorted(os.path.splitext(f)[1] for f in os.listdir(folder)), [".html", ".json"])
        self.assertEqual(workbench.main(["url", "ftp://example.org/x", "--out", tempfile.gettempdir()]), 2)


class TextSourceRecord(unittest.TestCase):
    def test_a_text_candidate_carries_its_exact_source_line_into_the_event(self):
        pages = {SITE + "/": html(f"<ul><li>{written(6, '6:30 PM')} Harvest Dinner</li></ul>")}
        web.clear_cache()
        events, notes = web.discover(Client(pages), [], url=SITE + "/", location={"latitude": 30.3, "longitude": -97.7, "radius_miles": 25, "timezone": "America/Chicago", "name": "Austin, TX"},
                                     source_id="web_t", source_name="T")
        self.assertEqual(events[0]["extraction"]["method"], "text")
        self.assertIn("Harvest Dinner", events[0]["extraction"]["source_text"])
        self.assertIn(f"{day(6).year}", events[0]["extraction"]["source_text"])
        structured = {SITE + "/": html(ld(event("Plain", 3)))}
        web.clear_cache()
        events, _ = web.discover(Client(structured), [], url=SITE + "/", location={"latitude": 30.3, "longitude": -97.7, "radius_miles": 25, "timezone": "America/Chicago", "name": "Austin, TX"},
                                 source_id="web_t", source_name="T")
        self.assertNotIn("source_text", events[0]["extraction"], "structured events have no free text to quote")


if __name__ == "__main__":
    unittest.main()
