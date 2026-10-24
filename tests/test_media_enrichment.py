"""Milestone 2F: event imagery in the new architecture. The accepted imagery code (discovery/media.py: verified places, OpenFreeMap maps, Wikimedia Commons venue
photographs) runs inside the independent MediaEnrichmentService on top of the one database; the service API serves the cached files; the Home Assistant
integration relays them by name. No website is contacted: real stored-event samples (UT Austin Localist API, saved; City-of-San-Diego-shaped pages) and the
accepted imagery tests' fakes for OpenStreetMap, OpenFreeMap tiles and Wikimedia stand in for the network."""
import json
import os
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor

from discovery import classify
from discovery.core import FetchError
from discovery.safe import BlockedError
from discovery.service import app as app_module
from discovery.service.api import ApiTokens
from discovery.service.control import CreateAreaRequest
from discovery.service.enrichment import EnrichmentConfig
from discovery.service.events import EventQuery
from discovery.service.media_store import SqliteMediaStore, content_type_of, inside_area
from discovery.service.repositories import payload_hash
from tests.test_austin import API, JsonClient, sample_pages
from tests.test_discovery_control import CONTROL_TOKEN, READ_TOKEN, request
from tests.test_discovery_service import Clock, SPEC, city_pages
from tests.test_extraction import PROCESS_TOKEN
from tests.test_media import FakeFetcher, VIEJAS, Wikimedia, nominatim

JPEG = b"\xff\xd8\xff\xe0" + b"photo" * 600
PNG = b"\x89PNG\r\n\x1a\n" + b"png" * 600


class Router:
    """One fetch client for everything: saved API/City pages, event photographs by address, and the accepted imagery tests' fake OpenStreetMap/OpenFreeMap/Wikimedia."""

    def __init__(self, pages, pictures, imagery):
        self.pages, self.pictures, self.imagery, self.json, self.urls, self.picture_urls = pages, pictures, imagery, JsonClient(pages), [], []

    def fetch(self, url, check_robots=True, binary=None, timeout=None):
        self.urls.append(url)
        if url in self.pages:
            return self.json.fetch(url, check_robots)
        if not any(host in url for host in ("nominatim", "overpass", "openfreemap", "wikipedia", "wikidata", "wikimedia")) and url not in self.pictures:
            raise FetchError(f"{url}: HTTP 404")   # a page the saved sample does not have
        if url in self.pictures:
            self.picture_urls.append(url)
            entry = self.pictures[url]
            if isinstance(entry, Exception):
                raise entry
            return types.SimpleNamespace(body=entry, text="")
        return self.imagery.fetch(url, check_robots, binary, timeout)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = os.path.join(self.folder.name, "discovery.db")
        self.clock = Clock()
        self.pages = {API + "1": sample_pages()[0], API + "2": sample_pages()[1], **city_pages()}
        self.pictures = {}
        for page in sample_pages():
            for wrapper in page["events"]:
                self.pictures[wrapper["event"]["photo_url"]] = JPEG
        self.imagery = FakeFetcher({"Viejas Arena": [nominatim("Viejas Arena", *VIEJAS, house="5500", road="Canyon Crest Drive", category="leisure", kind="sports_centre", wikidata="Q1")]},
                                   wiki=Wikimedia())
        self.router = Router(self.pages, self.pictures, self.imagery)
        self.built = self.build()
        self.sd = self.built.control.create_area(CreateAreaRequest("San Diego", 32.7157, -117.1611, 25, sources=("sandiego_gov",))).area
        self.austin = self.built.control.create_area(CreateAreaRequest("Austin", 30.2672, -97.7431, 35, sources=("ut_austin",))).area

    def build(self, **kwargs):
        built = app_module.build(self.db, clock=self.clock, fetch_client=self.router, nominatim_interval=0, enrichment_config=EnrichmentConfig(places_per_pass=50), **kwargs)
        self.addCleanup(built.close)
        built.service.register_source(SPEC)
        built.register_known_sources()
        return built

    def count(self, table, where="1=1"):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
        finally:
            connection.close()

    def discover_and_extract(self, built=None):
        built = built or self.built
        built.service.run_until_idle(10)
        while built.extraction.run_once().claimed:
            pass

    def enrich(self, built=None, passes=30):
        built = built or self.built
        total = 0
        for _ in range(passes):
            handled = built.enrichment.run_once()
            total += handled
            if not handled:
                break
        return total

    def events(self, area, built=None):
        return {e.title: e for e in (built or self.built).events.query(EventQuery(area_key=area.area_key, limit=200)).events}


class Enrichment(Fixture):
    def test_the_event_s_own_photograph_is_cached_credited_and_comes_first(self):
        self.discover_and_extract()
        self.assertEqual(self.enrich(), 11)
        fort = self.events(self.austin)["Fortlandia"]
        image = fort.media["image"]
        self.assertRegex(image["url"], r"^/area_events/media/p[a-f0-9]{24}\.jpg$")
        self.assertEqual((image["credit"], image["license"]), ("UT Austin Events", "as published by the source"))
        self.assertTrue(image["source_url"].startswith("https://calendar.utexas.edu/event/"), "the credit links to the event's own page")
        self.assertEqual(self.built.media_files.read(image["url"].rsplit("/", 1)[1]), (JPEG, "image/jpeg"))
        self.assertEqual(self.count("media_files"), self.count("media_files", "media_type = 'image/jpeg' OR media_type = 'image/svg+xml'"))

    def test_a_map_exists_only_for_coordinates_the_source_gave_inside_the_area(self):
        self.discover_and_extract()
        self.enrich()
        austin = self.events(self.austin)
        fort = austin["Fortlandia"].media["map"]
        self.assertEqual((fort["confidence"], fort["precision"], fort["approximate"]), ("source", "venue", False))
        for theme in ("light", "dark"):
            self.assertTrue(self.built.media_files.read(fort[theme].rsplit("/", 1)[1]), theme)
        self.assertIsNone((austin["Student Affairs Meeting"].media or {}).get("map"), "no location given: no map, nothing invented")
        self.assertIsNone((self.events(self.sd)["Event 0"].media or {}).get("map"), "an address the geocoder could not verify gets no map")
        record = [r for r in self.built.enrichment._resolver.store.index["records"].values() if not r["ok"]]
        self.assertTrue(record and all(r.get("reason") for r in record), "refusals are cached with their reasons")

    def test_source_coordinates_outside_the_area_are_not_trusted_for_a_map(self):
        area = {"latitude": 30.2672, "longitude": -97.7431, "radius_miles": 35}
        self.assertTrue(inside_area({"latitude": 30.28, "longitude": -97.73}, area))
        self.assertFalse(inside_area({"latitude": 32.7, "longitude": -117.1}, area))
        self.assertFalse(inside_area({"latitude": "x", "longitude": None}, area))
        self.assertFalse(inside_area(None, area))

    def test_a_verified_venue_photograph_is_shown_with_its_licence_and_a_cached_one_is_not_fetched_again(self):
        self.discover_and_extract()
        self.pages["https://www.sandiego.gov/event/e0"] = self.pages["https://www.sandiego.gov/event/e0"]   # (unchanged) the City pages stay as they are
        with self.built.store.write() as tx:   # a stored San Diego candidate that names Viejas Arena
            source = self.built.service.sources.by_key(tx, "sandiego_gov")
            event = {"id": "sandiego_gov:weezer", "title": "Weezer", "start": time.strftime("%Y-%m-%dT20:00:00-07:00", time.gmtime(time.time() + 3 * 86400)), "venue": "Viejas Arena",
                     "address": "5500 Canyon Crest Dr., San Diego", "timezone": "America/Los_Angeles", "url": "https://example.org/weezer", "categories": []}
            self.built.service.candidates.upsert(tx, event["id"], source["source_id"], None, "adapter", "test", event["title"], int(time.time() + 3 * 86400), event, payload_hash(event), int(self.clock()))
        self.enrich()
        weezer = self.events(self.sd, self.built) if False else None
        with self.built.store.read() as connection:
            row = connection.execute("SELECT media_json FROM candidate_media m JOIN candidates c USING (candidate_id) WHERE c.candidate_key='sandiego_gov:weezer'").fetchone()
        media = json.loads(row["media_json"])
        self.assertEqual((media["photo"]["license"], media["photo"]["creator"]), ("CC BY-SA 3.0", "A. Photographer"))
        self.assertEqual(media["photo"]["source_url"], "https://commons.wikimedia.org/wiki/File:F.jpg")
        self.assertTrue(media["map"]["approximate"], "an address pin only says somewhere on this property")
        requests = list(self.imagery.urls)
        self.clock.t += 7200
        self.enrich()
        self.assertEqual(self.imagery.urls[len(requests):], [], "nothing about a verified place is looked up twice")
        del weezer

    def test_every_event_gets_a_category_illustration_decision_from_the_accepted_classifier(self):
        self.discover_and_extract()
        self.enrich()
        decisions = [e.illustration for e in [*self.events(self.austin).values(), *self.events(self.sd).values()]]
        self.assertTrue(all(d and d["category"] in classify.CATEGORIES for d in decisions))
        self.assertTrue(all("reasons" in d for d in decisions), "each decision says why")

    def test_a_picture_shared_by_unrelated_events_is_a_default_not_a_photograph_of_any_of_them(self):
        self.discover_and_extract()
        shared = "https://localist-images.azureedge.net/photos/1/huge/shared.jpg"
        self.pictures[shared] = JPEG
        with self.built.store.write() as tx:
            source = self.built.service.sources.by_key(tx, "ut_austin")
            for n in range(4):
                event = {"id": f"ut_austin:shared{n}", "title": f"Different event {n}", "start": time.strftime("%Y-%m-%dT10:00:00-05:00", time.gmtime(time.time() + 86400 * (n + 2))),
                         "image_url": shared, "image_source": "Localist API photo_url", "url": f"https://calendar.utexas.edu/event/s{n}", "categories": []}
                self.built.service.candidates.upsert(tx, event["id"], source["source_id"], None, "adapter", "test", event["title"], int(time.time() + 86400 * (n + 2)), event,
                                                     payload_hash(event), int(self.clock()))
        self.enrich()
        self.assertNotIn(shared, self.router.picture_urls, "the shared picture was never even downloaded")
        with self.built.store.read() as connection:
            rows = connection.execute("SELECT media_json FROM candidate_media m JOIN candidates c USING (candidate_id) WHERE c.candidate_key LIKE 'ut_austin:shared%'").fetchall()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all("image" not in json.loads(r["media_json"]) for r in rows))

    def test_broken_pictures_are_refused_once_and_temporary_failures_are_retried_later(self):
        self.discover_and_extract()
        urls = sorted(self.pictures)
        gone, junk, huge, flaky = urls[0], urls[1], urls[2], urls[3]
        self.pictures[gone] = FetchError("https://x: HTTP 404")
        self.pictures[junk] = b"<html>not a picture</html>" * 50
        self.pictures[huge] = JPEG + b"x" * 3_000_000
        self.pictures[flaky] = FetchError("timed out")
        self.enrich()
        first_pass = {u: self.router.picture_urls.count(u) for u in urls[:4]}
        with self.built.store.read() as connection:
            open_candidates = connection.execute("SELECT COUNT(*) FROM candidate_media WHERE complete = 0").fetchone()[0]
        self.assertGreaterEqual(open_candidates, 1, "the temporary failure leaves its candidate open")
        for url in (gone, junk, huge):
            record = self.built.enrichment._resolver.store.index["records"]["e:" + __import__("hashlib").sha1(url.encode()).hexdigest()[:20]]
            self.assertFalse(record["ok"])
            self.assertTrue(record["reason"])
        self.pictures[flaky] = JPEG
        self.clock.t += 1000
        self.enrich()
        self.assertEqual({u: self.router.picture_urls.count(u) for u in (gone, junk, huge)}, {u: first_pass[u] for u in (gone, junk, huge)}, "refusals are remembered, not retried")
        self.assertGreater(self.router.picture_urls.count(flaky), first_pass[flaky])
        with self.built.store.read() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM candidate_media WHERE complete = 0").fetchone()[0], 0)

    def test_a_picture_host_that_forbids_fetching_is_respected_and_the_source_s_own_address_is_offered_instead(self):
        self.discover_and_extract()
        urls = sorted(self.pictures)
        blocked = urls[0]
        self.pictures[blocked] = BlockedError("robots.txt of localist-images.azureedge.net does not allow this page")
        self.enrich()
        first = self.router.picture_urls.count(blocked)
        self.assertEqual(first, 1)
        shown = [e for e in self.events(self.austin).values() if e.media and (e.media.get("image") or {}).get("original_url") == blocked]
        self.assertEqual(len(shown), 1)
        image = shown[0].media["image"]
        self.assertEqual((image["url"], image["cached"], image["credit"]), (blocked, False, "UT Austin Events"))
        self.assertTrue(image["url"].startswith("https://"), "never a plain-http address (mixed content)")
        self.clock.t += 5000
        self.built.enrichment._work.retry_after_s = 0
        self.enrich()
        self.assertEqual(self.router.picture_urls.count(blocked), first, "a host's refusal is remembered, not asked again")

    def test_enrichment_is_repeatable_and_a_changed_event_is_enriched_again(self):
        self.discover_and_extract()
        self.enrich()
        files, rows, fetched = self.count("media_files"), self.count("candidate_media"), len(self.router.picture_urls)
        self.assertEqual(self.enrich(), 0, "nothing pending")
        self.assertEqual((self.count("media_files"), self.count("candidate_media"), len(self.router.picture_urls)), (files, rows, fetched))
        page = json.loads(json.dumps(self.pages[API + "2"]))
        target = [w for w in page["events"] if w["event"]["title"] == "Fortlandia"][0]["event"]
        target["photo_url"] = "https://localist-images.azureedge.net/photos/2/huge/replacement.jpg"
        self.pictures[target["photo_url"]] = PNG
        self.pages[API + "2"] = page
        self.clock.t += 3601
        self.built.service.make_due("ut_austin")
        self.discover_and_extract()
        self.assertEqual(self.enrich(), 1)
        image = self.events(self.austin)["Fortlandia"].media["image"]
        self.assertTrue(image["url"].endswith(".png"))
        self.assertEqual(self.count("candidate_media"), rows, "one row per candidate, replaced in place")

    def test_a_restart_of_the_worker_continues_from_the_database(self):
        self.discover_and_extract()
        self.built.enrichment.config = EnrichmentConfig(batch=3)
        self.assertEqual(self.built.enrichment.run_once(), 3)
        self.built.close()
        again = self.build()
        self.assertEqual(self.enrich(again), 8, "the rest, not all over again")
        self.assertEqual(self.count("candidate_media"), 11)

    def test_the_worker_starts_stops_promptly_and_survives_errors(self):
        self.discover_and_extract()
        enrichment = self.built.enrichment
        enrichment.config = EnrichmentConfig(idle_wait_s=30, busy_wait_s=0.01, error_wait_s=0.01, error_wait_cap_s=0.02)
        calls = iter([RuntimeError("boom"), RuntimeError("boom")])
        real = enrichment._work.pending

        def flaky(limit):
            error = next(calls, None)
            if error:
                raise error
            return real(limit)
        enrichment._work.pending = flaky
        enrichment.start()
        deadline = time.time() + 15
        while time.time() < deadline and self.count("candidate_media") < 11:
            time.sleep(0.05)
        started = time.time()
        self.assertTrue(enrichment.stop(5))
        self.assertLess(time.time() - started, 3)
        status = enrichment.status()
        self.assertEqual((status["state"], status["errors"] >= 2, self.count("candidate_media")), ("stopped", True, 11))
        self.assertIn("RuntimeError", status["last_error"])

    def test_a_stalled_enrichment_does_not_slow_discovery_or_queries(self):
        release = threading.Event()
        real = self.imagery.fetch

        def stalled(url, check_robots=True, binary=None, timeout=None):
            release.wait(20)
            return real(url, check_robots, binary, timeout)
        self.imagery.fetch = stalled
        self.discover_and_extract()
        enrichment = self.built.enrichment
        enrichment.config = EnrichmentConfig(batch=2, busy_wait_s=0.01)
        enrichment.start()
        try:
            time.sleep(0.3)
            started = time.perf_counter()
            self.clock.t += 3601
            self.built.service.make_due("ut_austin")
            reports = self.built.service.run_until_idle(10)
            page = self.built.events.query(EventQuery(area_key=self.austin.area_key, limit=200))
            took = time.perf_counter() - started
            self.assertIn("completed", [r.outcome for r in reports])
            self.assertEqual(len(page.events), 7)
            self.assertLess(took, 5, "discovery and queries ran while enrichment waited on a slow lookup")
        finally:
            release.set()
            enrichment.stop(10)

    def test_the_cache_is_bounded(self):
        self.discover_and_extract()
        self.enrich()
        store = self.built.enrichment._resolver.store
        self.assertIsInstance(store, SqliteMediaStore)
        total = sum(os.path.getsize(os.path.join(store.files, n)) for n in os.listdir(store.files))
        self.assertGreater(total, 0)
        store.max_bytes = total // 3
        store.prune()
        remaining = sum(os.path.getsize(os.path.join(store.files, n)) for n in os.listdir(store.files))
        self.assertLessEqual(remaining, total // 3)
        store.save()
        self.assertEqual(self.count("media_files"), len(os.listdir(store.files)), "file metadata follows the folder")
        for name in ("../x.jpg", "a/b.jpg", "X.jpg", "p1.exe", "", "p" * 70 + ".jpg"):
            with self.assertRaises(ValueError):
                store.put_file(name, JPEG)
        with self.assertRaises(ValueError):
            store.put_file("pabcd1234.jpg", b"")

    def test_the_known_content_types(self):
        self.assertEqual([content_type_of(n) for n in ("a1234.jpg", "a1234.png", "a1234.webp", "a1234.svg", "a1234.gif", "../a.jpg", None)],
                         ["image/jpeg", "image/png", "image/webp", "image/svg+xml", None, None, None])


class Serving(Fixture):
    def setUp(self):
        super().setUp()
        self.discover_and_extract()
        self.enrich()
        self.server = app_module.build_api(self.built, ApiTokens(CONTROL_TOKEN, READ_TOKEN, PROCESS_TOKEN), clock=self.clock, port=0).start()
        self.addCleanup(self.server.stop)
        self.image = self.events(self.austin)["Fortlandia"].media["image"]["url"].rsplit("/", 1)[1]
        self.map = self.events(self.austin)["Fortlandia"].media["map"]["light"].rsplit("/", 1)[1]

    def raw(self, path, token=READ_TOKEN):
        import http.client
        connection = http.client.HTTPConnection(*self.server.address[:2], timeout=10)
        try:
            connection.request("GET", path, headers={"Authorization": f"Bearer {token}"} if token else {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_a_picture_is_served_by_name_with_its_real_type_and_long_lived_caching(self):
        status, headers, body = self.raw(f"/v1/media/{self.image}")
        self.assertEqual((status, body, headers["Content-Type"]), (200, JPEG, "image/jpeg"))
        self.assertIn("immutable", headers["Cache-Control"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, headers, body = self.raw(f"/v1/media/{self.map}")
        self.assertEqual((status, headers["Content-Type"]), (200, "image/svg+xml"))
        self.assertTrue(body.startswith(b"<svg"))

    def test_only_authenticated_readers_get_pictures_and_no_name_can_reach_a_path(self):
        self.assertEqual(self.raw(f"/v1/media/{self.image}", token=None)[0], 401)
        self.assertEqual(self.raw(f"/v1/media/{self.image}", token=PROCESS_TOKEN)[0], 403)
        self.assertEqual(self.raw(f"/v1/media/{self.image}", token=CONTROL_TOKEN)[0], 200)
        for bad in ("../../etc/passwd", "..%2f..%2fdiscovery.db", "pABC.jpg", "p0000000000000000000000000.jpg", f"{self.image}.exe", f"{self.image}/", "discovery.db", "%00.jpg",
                    "p" * 90 + ".jpg"):
            self.assertIn(self.raw(f"/v1/media/{bad}")[0], (404, 400), bad)
        self.assertEqual(request(self.server, "POST", f"/v1/media/{self.image}", body={})[0], 405)

    def test_twenty_clients_fetch_pictures_at_once(self):
        def one(i):
            status, _, body = self.raw(f"/v1/media/{self.image if i % 2 else self.map}")
            return status, len(body)
        with ThreadPoolExecutor(20) as pool:
            results = list(pool.map(one, range(60)))
        self.assertEqual({s for s, _ in results}, {200})
        self.assertEqual(len({n for i, (_, n) in enumerate(results) if i % 2}), 1)

    def test_events_over_the_api_carry_media_and_the_illustration(self):
        status, page = request(self.server, "GET", f"/v1/events?area={self.austin.area_key}&limit=200", token=READ_TOKEN)
        fort = [e for e in page["events"] if e["title"] == "Fortlandia"][0]
        self.assertEqual(status, 200)
        self.assertTrue(fort["media"]["image"]["url"].startswith("/area_events/media/p"))
        self.assertIn("category", fort["illustration"])
        self.assertNotIn(READ_TOKEN, json.dumps(page))


class Schema(unittest.TestCase):
    def test_the_media_tables_exist_and_cascade_with_their_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            built = app_module.build(os.path.join(folder, "discovery.db"), clock=Clock(), fetch_client=JsonClient({}))
            try:
                with built.store.write() as tx:
                    source = built.service.sources.upsert(tx, SPEC, 1)
                    built.service.candidates.upsert(tx, "sandiego_gov:x", source, None, "adapter", "t", "T", 1, {"title": "T"}, "0" * 64, 1)
                    cid = tx.execute("SELECT candidate_id FROM candidates").fetchone()[0]
                    tx.execute("INSERT INTO candidate_media (candidate_id, payload_hash, media_json, complete, updated_at) VALUES (?, ?, '{}', 1, 1)", (cid, "0" * 64))
                with built.store.write() as tx:
                    tx.execute("DELETE FROM candidates")
                with built.store.read() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM candidate_media").fetchone()[0], 0)
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
            finally:
                built.close()


if __name__ == "__main__":
    unittest.main()
