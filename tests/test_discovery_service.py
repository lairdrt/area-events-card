"""The Discovery Service and its SQLite store: schema, identity, jobs and leases, atomic and idempotent writes, recovery, retention,
the reader contract, the shared host policy, and concurrent readers. Deterministic: a fixed clock and saved pages, no network."""
import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from discovery import safe
from discovery.core import FetchError
from discovery.safe import BlockedError, HostPolicy, Response, SafeFetcher
from discovery.service import app as app_module
from discovery.service import repositories, store as store_module
from discovery.service.contracts import CandidateQuery
from discovery.service.repositories import JobRepository, PageRepository, RetentionPolicy, SourceRepository, SourceSpec
from discovery.service.schema import LATEST
from discovery.service.service import DiscoveryConfig, job_key_for
from discovery.service.store import DiscoveryStore, StorageUnavailable
from discovery.service.urls import canonical_url, url_key
from tests.test_site_discovery import CITY_URL, city_detail, city_listing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE_DIR = os.path.join(ROOT, "area_events", "discovery", "service")
def build_plain(*args, **kwargs):
    """The 2A behaviour: no area is needed for a source to be crawled."""
    return app_module.build(*args, service_gate=False, **kwargs)


SPEC = SourceSpec("sandiego_gov", "sandiego_gov", "City of San Diego", CITY_URL, options=(("pages", 1), ("max_details_per_run", 10)), interval_s=3600)


class Clock:
    def __init__(self, start=None):
        start = int(time.time()) if start is None else start  # the saved pages' dates are relative to today
        self.t = start

    def __call__(self):
        return self.t


class FakeClient:
    """Stands in for SafeFetcher: saved pages by address; an Exception value is raised; `on_fetch` runs before each answer."""

    def __init__(self, pages, on_fetch=None):
        self.pages, self.urls, self.on_fetch = pages, [], on_fetch

    def fetch(self, url, check_robots=True, binary=None, timeout=None):
        self.urls.append(url)
        if self.on_fetch:
            self.on_fetch(url)
        entry = self.pages.get(url)
        if entry is None:
            raise FetchError(f"{url}: HTTP 404")
        if isinstance(entry, Exception):
            raise entry
        return Response(url, 200, "text/html; charset=utf-8", entry)


def city_pages(n=4, address="{n}00 Oak Street San Diego, CA"):
    items = [(f"e{i}", f"Event {i}", i + 1) for i in range(n)]
    pages = {CITY_URL: city_listing(items)}
    for i in range(n):
        pages[f"https://www.sandiego.gov/event/e{i}"] = city_detail(f"Oak Street block {i}", address.format(n=i + 1))
    return pages


class Fixture(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = os.path.join(self.folder.name, "discovery.db")
        self.clock = Clock()

    def build(self, pages=None, client=None, config=DiscoveryConfig(), worker="worker-1", store_options=None):
        built = build_plain(self.db, clock=self.clock, fetch_client=client or FakeClient(pages if pages is not None else city_pages()), config=config,
                                 worker_id=worker, store_options=store_options)
        self.addCleanup(built.close)
        return built

    def count(self, table, where="1=1"):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
        finally:
            connection.close()


class Schema(Fixture):
    def test_a_fresh_database_is_created_at_the_latest_version_in_wal_mode(self):
        built = self.build()
        self.assertEqual(built.store.version(), store_module.SCHEMA_VERSION)
        with built.store.read() as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"sources", "pages", "source_pages", "contents", "fetches", "candidates", "evidence", "jobs", "changes", "consumers",
                             "schema_migrations", "store_meta"} <= tables)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        with built.store.write() as tx:
            self.assertEqual(tx.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_migrations_apply_once_in_order_and_a_newer_database_is_refused(self):
        self.build().close()
        extra = store_module.MIGRATIONS + ((LATEST + 1, "test column", "ALTER TABLE sources ADD COLUMN note TEXT;"),)
        store = DiscoveryStore(self.db, migrations=extra).open()
        self.assertEqual(store.version(), LATEST + 1)
        store.close()
        store = DiscoveryStore(self.db, migrations=extra).open()  # reopening applies nothing again
        with store.read() as connection:
            self.assertEqual([r[0] for r in connection.execute("SELECT version FROM schema_migrations ORDER BY version")], list(range(1, LATEST + 2)))
        store.close()
        with self.assertRaises(RuntimeError):
            DiscoveryStore(self.db).open()  # this program knows only the latest version
        with self.assertRaises(ValueError):
            DiscoveryStore(os.path.join(self.folder.name, "x.db"), migrations=((2, "a", "SELECT 1;"), (1, "b", "SELECT 1;"))).open()

    def test_constraints_hold(self):
        built = self.build()
        with self.assertRaises(sqlite3.IntegrityError):
            with built.store.write() as tx:
                tx.execute("INSERT INTO candidates (candidate_key, source_id, method, extractor, payload_json, payload_hash, first_seen_at, last_seen_at, updated_at) "
                           "VALUES ('x:1', 999, 'm', 'e', '{}', ?, 1, 1, 1)", ("0" * 64,))
        built.service.register_source(SPEC)
        with built.store.write() as tx:
            source_id = SourceRepository().upsert(tx, SPEC, 1)
            PageRepository().touch(tx, "https://Example.org/a", source_id, "detail", 1)
        for bad in ("INSERT INTO pages (url_key, url, first_seen_at) VALUES ((SELECT url_key FROM pages LIMIT 1), 'u', 1)",
                    "UPDATE pages SET status='ok' WHERE content_hash IS NULL",
                    "UPDATE jobs SET state='leased'",
                    "INSERT INTO sources (source_key, adapter, name, base_url, interval_s, created_at, updated_at) VALUES ('s', 'a', 'n', 'u', 5, 1, 1)"):
            with self.assertRaises(sqlite3.IntegrityError, msg=bad):
                with built.store.write() as tx:
                    tx.execute(bad)


class Identity(Fixture):
    def test_canonical_urls(self):
        same = ["https://WWW.Example.org/events?b=2&a=1#top", "https://www.example.org:443/events?a=1&b=2", "https://www.example.org/events?a=1&b=2&utm_source=x",
                "https://www.example.org/%65vents?a=1&b=2"]
        self.assertEqual(len({url_key(u) for u in same}), 1)
        self.assertEqual(canonical_url("http://example.org"), "http://example.org/")
        self.assertNotEqual(url_key("https://example.org/a"), url_key("http://example.org/a"))
        for bad in ("ftp://example.org/x", "mailto:a@b.c", "https://user:pw@example.org/", "", "javascript:alert(1)"):
            with self.assertRaises(ValueError):
                canonical_url(bad)

    def test_one_page_row_however_many_sources_discover_it(self):
        built = self.build()
        other = SourceSpec("other_source", "sandiego_gov", "Other", "https://other.example/", interval_s=3600)
        with built.store.write() as tx:
            a, b = SourceRepository().upsert(tx, SPEC, 1), SourceRepository().upsert(tx, other, 1)
            first, new1 = PageRepository().touch(tx, "https://www.sandiego.gov/event/e1#x", a, "detail", 1)
            second, new2 = PageRepository().touch(tx, "https://WWW.sandiego.gov/event/e1", b, "event", 2)
            again, new3 = PageRepository().touch(tx, "https://www.sandiego.gov/event/e1", a, "detail", 3)
        self.assertEqual((first == second == again, new1, new2, new3), (True, True, False, False))
        self.assertEqual(self.count("pages"), 1)
        self.assertEqual(self.count("source_pages"), 2)
        page = built.reader.page(url_key("https://www.sandiego.gov/event/e1"))
        self.assertEqual(page.sources, (("other_source", "event"), ("sandiego_gov", "detail")))


class Running(Fixture):
    def test_a_run_stores_pages_contents_candidates_evidence_and_changes_and_completes_its_job(self):
        built = self.build()
        built.service.register_source(SPEC)
        report = built.service.run_once()
        self.assertEqual(report.outcome, "completed")
        self.assertEqual((report.requests, report.pages_seen, report.content_new, report.candidates_new), (5, 5, 5, 4))
        self.assertEqual(self.count("pages"), 5)
        self.assertEqual(self.count("candidates"), 4)
        self.assertEqual(self.count("changes"), 9)  # 5 pages and 4 candidates
        job = built.reader.status().jobs
        self.assertEqual((job.done, job.ready, job.leased), (1, 0, 0))
        record = built.reader.candidate("sandiego_gov:e1@" + built.reader.candidates(CandidateQuery(title_contains="Event 1"))[0].candidate_key.split("@")[1])
        self.assertEqual(record.payload["address"], "200 Oak Street, San Diego, CA")
        page = built.reader.page(record.url_key, include_body=True)
        text = page.body.decode("utf-8")
        located = [e for e in record.evidence if e.quote]
        self.assertTrue(located)
        for ref in located:  # every quote is text that is really in a stored page
            stored = built.reader.page(ref.url_key, include_body=True).body.decode("utf-8")
            self.assertIn(ref.quote.lower().replace("&amp;", "&"), json.dumps(stored).lower() + stored.lower())
        self.assertIn("Oak Street", text)

    def test_a_second_run_reuses_what_is_stored(self):
        built = self.build()
        built.service.register_source(SPEC)
        built.service.run_once()
        changes = self.count("changes")
        self.assertEqual(built.service.run_once().outcome, "nothing_due", "the job is not due again for an hour")
        self.clock.t += 3600
        report = built.service.run_once()
        self.assertEqual(report.outcome, "completed")
        self.assertEqual((report.requests, report.details_reused, report.candidates_unchanged, report.content_unchanged, report.changes_appended), (1, 4, 4, 1, 0))
        self.assertEqual(self.count("changes"), changes, "nothing changed, so nothing was appended")
        self.assertEqual(self.count("contents"), 5)

    def test_random_markup_does_not_count_as_a_changed_page(self):
        pages = city_pages(1)
        built = self.build(pages)
        built.service.register_source(SPEC)
        built.service.run_once()
        pages[CITY_URL] = pages[CITY_URL].replace("<html>", '<html><div class="view-dom-id-a1b2c3"></div>')
        self.clock.t += 3600
        report = built.service.run_once()
        self.assertEqual((report.content_new, report.content_unchanged), (0, 1))
        pages[CITY_URL] = pages[CITY_URL].replace("About Event 0.", "About Event 0, now with a new line.")
        self.clock.t += 3600
        report = built.service.run_once()
        self.assertEqual((report.content_new, report.changes_appended >= 1), (1, True))


class Jobs(Fixture):
    def seeded(self):
        built = self.build()
        built.service.register_source(SPEC)
        return built

    def claim(self, built, worker, token, lease=600):
        with built.store.write() as tx:
            return JobRepository().claim(tx, worker, token, self.clock(), lease)

    def test_first_run_completed_not_yet_due_and_due_again(self):
        built = self.seeded()
        job = self.claim(built, "w1", "t1")
        self.assertIsNotNone(job)
        self.assertIsNone(self.claim(built, "w2", "t2"), "a leased job is never claimed twice")
        with built.store.write() as tx:
            self.assertTrue(JobRepository().complete(tx, job["job_id"], "t1", self.clock(), self.clock() + 3600))
        self.assertIsNone(self.claim(built, "w2", "t2"), "completed and not yet due")
        self.clock.t += 3599
        self.assertIsNone(self.claim(built, "w2", "t2"))
        self.clock.t += 1
        again = self.claim(built, "w2", "t3")
        self.assertEqual((again["job_id"], again["attempts"]), (job["job_id"], 1))

    def test_an_expired_lease_is_reclaimed_and_the_stale_worker_cannot_complete(self):
        built = self.seeded()
        first = self.claim(built, "w1", "old", lease=10)
        self.clock.t += 11
        second = self.claim(built, "w2", "new", lease=600)
        self.assertEqual((second["job_id"], second["attempts"]), (first["job_id"], 2))
        with built.store.write() as tx:
            jobs = JobRepository()
            self.assertFalse(jobs.lease_valid(tx, first["job_id"], "old", self.clock()))
            self.assertFalse(jobs.complete(tx, first["job_id"], "old", self.clock(), self.clock() + 3600))
            self.assertIsNone(jobs.fail(tx, first["job_id"], "old", self.clock(), "late", 900, 43200, 3600))
            self.assertTrue(jobs.complete(tx, second["job_id"], "new", self.clock(), self.clock() + 3600))

    def test_a_stale_worker_writes_nothing_even_after_crawling(self):
        pages = city_pages(2)
        clock = self.clock

        def slow(url):  # the lease expires while worker 1 is crawling, and worker 2 takes the job over
            clock.t += 400

        config = DiscoveryConfig(lease_s=600)
        slow_worker = self.build(client=FakeClient(pages, on_fetch=slow), config=config, worker="slow")
        slow_worker.service.register_source(SPEC)
        token_holder = {}

        def take_over(url):
            if "takeover" not in token_holder:
                token_holder["takeover"] = True
                clock.t += 700  # worker 1's lease has expired
                fast = build_plain(self.db, clock=clock, fetch_client=FakeClient(pages), config=config, worker_id="fast")
                self.addCleanup(fast.close)
                token_holder["report"] = fast.service.run_once()

        slow_worker.service.fetch_client.on_fetch = take_over
        report = slow_worker.service.run_once()
        self.assertEqual(token_holder["report"].outcome, "completed", "the second worker reclaimed the expired lease")
        self.assertEqual(report.outcome, "stale_lease")
        self.assertEqual(self.count("candidates"), 2, "only the valid worker's results exist")
        self.assertEqual(self.count("changes"), 5)
        self.assertEqual(self.count("fetches"), 3)

    def test_writes_are_atomic(self):
        built = self.seeded()
        original = built.service.candidates.upsert
        calls = {"n": 0}

        def failing(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise sqlite3.OperationalError("disk I/O error")
            return original(*args, **kwargs)

        with mock.patch.object(built.service.candidates, "upsert", side_effect=failing):
            with self.assertRaises(sqlite3.OperationalError):
                built.service.run_once()
        for table in ("candidates", "changes", "contents", "fetches", "evidence"):
            self.assertEqual(self.count(table), 0, table)
        self.assertEqual(self.count("jobs", "state='leased'"), 1, "the job stays leased; it is reclaimed when the lease expires")
        self.clock.t += DiscoveryConfig().lease_s + 1
        self.assertEqual(built.service.run_once().outcome, "completed")
        self.assertEqual(self.count("candidates"), 4)

    def test_fetch_failures_back_off_then_wait_for_the_next_regular_run(self):
        pages = {CITY_URL: FetchError("www.sandiego.gov: HTTP 503")}
        built = self.build(pages)
        built.service.register_source(SPEC)
        waits = []
        for attempt in range(1, 5):
            report = built.service.run_once()
            self.assertEqual(report.outcome, "fetch_failed")
            with built.store.read() as connection:
                job = JobRepository().get(connection, job_key_for("sandiego_gov"))
            waits.append((job["state"], job["next_run_at"] - self.clock()))
            self.clock.t = job["next_run_at"]
        self.assertEqual(waits, [("ready", 900), ("ready", 1800), ("ready", 3600), ("failed", 3600)])
        self.assertEqual(self.count("fetches", "outcome='http_error'"), 4)
        pages[CITY_URL] = city_listing([("e0", "Event 0", 1)])
        self.assertEqual(built.service.run_once().outcome, "completed", "after a failure the job is tried again at its regular time")


class Recovery(Fixture):
    def test_pending_work_survives_a_restart_and_a_new_process_reads_the_database(self):
        first = self.build()
        first.service.register_source(SPEC)
        with first.store.write() as tx:
            JobRepository().claim(tx, "crashed-worker", "lost", self.clock(), 600)  # the process dies holding the lease
        first.close()
        self.clock.t += 601
        second = self.build(worker="after-restart")
        self.assertEqual(second.service.recover(), 1)
        self.assertEqual(second.service.run_once().outcome, "completed")
        second.close()
        code = ("import sys, json; sys.path.insert(0, sys.argv[1]); from discovery.service.app import build; app = build(sys.argv[2]); "
                "s = app.reader.status(); print(json.dumps([s.candidates, s.pages, s.jobs.done, s.schema_version])); app.close()")
        out = subprocess.run([sys.executable, "-c", code, os.path.join(ROOT, "area_events"), self.db], capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(out.stdout), [4, 5, 1, LATEST], out.stderr)

    def test_a_locked_or_failing_database_pauses_discovery_for_a_bounded_time_and_then_it_resumes(self):
        built = self.build(store_options={"busy_timeout_ms": 50, "write_retries": 2, "retry_sleep": 0.001})
        built.service.register_source(SPEC)
        blocker = sqlite3.connect(self.db, isolation_level=None, timeout=0.1)
        blocker.execute("BEGIN IMMEDIATE")
        report = built.service.run_once()
        self.assertEqual(report.outcome, "storage_unavailable")
        self.assertEqual(built.service.run_once().outcome, "paused")
        blocker.execute("ROLLBACK")
        blocker.close()
        self.assertGreater(built.store.stats.summary()["retries"], 0)
        self.clock.t += 61
        self.assertEqual(built.service.run_once().outcome, "completed", "once the database can be written again")
        with mock.patch.object(built.store, "write", side_effect=StorageUnavailable("database or disk is full")):
            pauses = []
            for _ in range(8):
                self.clock.t = max(self.clock.t, built.service.paused_until) + 0
                built.service.run_once()
                pauses.append(built.service.pause_seconds)
        self.assertEqual(pauses[:4], [60, 120, 240, 480])
        self.assertEqual(max(pauses), DiscoveryConfig().storage_pause_cap_s)


class Contention(Fixture):
    def test_two_writers_on_one_file_wait_for_each_other_within_the_busy_timeout(self):
        self.build().close()
        stores = [DiscoveryStore(self.db).open() for _ in range(2)]  # as two processes would
        errors = []

        def write(store, offset):
            for n in range(100):
                try:
                    with store.write() as tx:
                        source_id = SourceRepository().upsert(tx, SourceSpec(f"s{offset}", "sandiego_gov", "n", "https://x.example/"), n)
                        PageRepository().touch(tx, f"https://x.example/{offset}/{n}", source_id, "detail", n)
                except Exception as error:
                    errors.append(repr(error))

        threads = [threading.Thread(target=write, args=(s, i)) for i, s in enumerate(stores)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for s in stores:
            s.close()
        self.assertEqual(errors, [])
        self.assertEqual(self.count("pages"), 200)


class ChangeFeed(Fixture):
    def test_changes_are_ordered_acknowledged_and_replay_is_idempotent(self):
        built = self.build()
        built.service.register_source(SPEC)
        built.service.run_once()
        batch = built.reader.changes(0, limit=4)
        self.assertEqual([c.seq for c in batch.changes], [1, 2, 3, 4])
        rest = built.reader.changes(batch.next_seq, limit=100)
        self.assertEqual(rest.changes[-1].seq, rest.latest_seq)
        consumer = {}
        for change in batch.changes + rest.changes + batch.changes:  # one batch delivered twice
            consumer[(change.entity, change.entity_key)] = change.payload_hash
        self.assertEqual(len(consumer), 9)
        built.reader.acknowledge("processing", 4)
        built.reader.acknowledge("processing", 2)  # an older acknowledgement never moves the checkpoint back
        built.reader.acknowledge("processing", 10_000)  # nor beyond what exists
        with built.store.read() as connection:
            self.assertEqual(connection.execute("SELECT acked_seq FROM consumers").fetchone()[0], 9)

    def test_a_consumer_behind_the_retained_history_must_resync_and_misses_nothing(self):
        built = self.build()
        built.service.register_source(SPEC)
        built.service.run_once()
        built.reader.acknowledge("fast", 9)
        built.reader.acknowledge("slow", 0)
        self.clock.t += 2 * 86400
        policy = RetentionPolicy(change_min_age_s=86400, change_max_rows=3)
        with built.store.write() as tx:
            removed = built.service.retention.prune(tx, self.clock(), policy)
        self.assertEqual(removed["changes"], 6, "the cap applies even though one consumer is behind")
        stale = built.reader.changes(0)
        self.assertTrue(stale.resync_required)
        self.assertEqual(stale.changes, ())
        self.assertFalse(built.reader.changes(6).resync_required)
        seen, after, as_of = set(), None, None
        while True:
            page = built.reader.snapshot(after_key=after, limit=3, as_of_seq=as_of)
            as_of = page.as_of_seq
            seen |= {c.candidate_key for c in page.candidates}
            if page.next_after_key is None:
                break
            after = page.next_after_key
        self.assertEqual(len(seen), 4)
        self.assertFalse(built.reader.changes(as_of).resync_required)

    def test_retention_keeps_what_retained_candidates_need(self):
        built = self.build()
        built.service.register_source(SPEC)
        built.service.run_once()
        with built.store.read() as connection:
            evidence_hashes = {r[0] for r in connection.execute("SELECT DISTINCT content_hash FROM evidence WHERE content_hash IS NOT NULL")}
        self.clock.t += 2 * 86400
        with built.store.write() as tx:
            built.service.retention.prune(tx, self.clock(), RetentionPolicy(content_budget_bytes=1, candidate_days_after_start=30))
        with built.store.read() as connection:
            remaining = {r[0] for r in connection.execute("SELECT content_hash FROM contents")}
        self.assertTrue(evidence_hashes <= remaining, "content referenced by evidence or by a page's current state is never pruned")
        self.clock.t += 40 * 86400
        with built.store.write() as tx:
            removed = built.service.retention.prune(tx, self.clock(), RetentionPolicy())
        self.assertEqual(removed["candidates"], 4)
        self.assertEqual(removed["pages"], 5, "pages nothing refers to and no source has seen for 30 days go too")
        self.assertEqual(self.count("evidence"), 0)
        self.assertEqual(self.count("source_pages"), 0)
        self.assertEqual(self.count("changes", "op='delete'"), 4, "consumers are told about removed candidates")


class HostPolicyShared(Fixture):
    def test_every_fetcher_of_the_service_shares_one_injected_policy_and_the_legacy_global_is_untouched(self):
        sent = []

        def send(scheme, host, port, ip, target, headers, timeout, limit):
            sent.append((host, target))
            if target == "/robots.txt":
                return 404, {}, b""
            if target.startswith("/blocked"):
                return 429, {"Retry-After": "120"}, b""
            return 200, {"Content-Type": "text/html"}, city_listing([("e0", "Event 0", 1)]).encode() if target.startswith("/events") else city_detail("x", "1 Oak Street San Diego, CA").encode()

        policy = HostPolicy()
        client = SafeFetcher(0, resolver=lambda host, port, type=None: [(None, None, None, None, ("93.184.216.34", port))], send=send, sleep=lambda s: None, state=policy)
        built = build_plain(self.db, clock=self.clock, fetch_client=client, policy=policy)
        self.addCleanup(built.close)
        self.assertIs(built.policy, policy)
        self.assertIs(built.service.fetch_client.state, policy)
        legacy_hosts = set(safe._STATE.last)
        built.service.register_source(SPEC)
        self.assertEqual(built.service.run_once().outcome, "completed")
        self.assertIn("www.sandiego.gov", policy.last)
        self.assertEqual(set(safe._STATE.last), legacy_hosts, "nothing in the discovery path used the legacy global")
        with self.assertRaises(BlockedError):
            client.fetch("https://www.sandiego.gov/blocked")
        count = len(sent)
        with self.assertRaises(BlockedError):
            client.fetch("https://www.sandiego.gov/events")  # backing off: no request is made at all
        self.assertEqual(len(sent), count)
        self.assertTrue(policy.backing_off("www.sandiego.gov", client.clock()))
        other = SafeFetcher(0, resolver=client.resolver, send=send, sleep=lambda s: None, state=policy)
        with self.assertRaises(BlockedError):
            other.fetch("https://www.sandiego.gov/events")  # a second fetcher with the same policy obeys it too

    def test_ssrf_protection_still_applies_in_the_discovery_path(self):
        client = SafeFetcher(0, resolver=lambda host, port, type=None: [(None, None, None, None, ("10.0.0.5", port))], send=lambda *a: (200, {}, b""),
                             sleep=lambda s: None, state=HostPolicy())
        built = build_plain(self.db, clock=self.clock, fetch_client=client)
        self.addCleanup(built.close)
        built.service.register_source(SPEC)
        report = built.service.run_once()
        self.assertEqual(report.outcome, "fetch_failed")
        self.assertEqual(self.count("fetches", "outcome='blocked'"), 1)


class NoGlobals(unittest.TestCase):
    """New modules hold no module-level mutable state: only constants (literals, tuples, frozensets, compiled patterns, read-only mappings),
    functions and classes; and no `global` statement."""

    ALLOWED_CALLS = {"frozenset", "tuple", "re.compile", "types.MappingProxyType", "SourceSpec", "os.path.join", "RetentionPolicy", "DiscoveryConfig", "logging.getLogger", "SchedulerConfig", "HostConfig", "re.compile"}

    def test_the_service_package(self):
        for name in sorted(os.listdir(SERVICE_DIR)):
            if not name.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(SERVICE_DIR, name), encoding="utf-8").read())
            for node in ast.walk(tree):
                self.assertNotIsInstance(node, (ast.Global, ast.Nonlocal), f"{name}: global statement")
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = node.value
                    self.assertFalse(isinstance(value, (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)), f"{name}: mutable module-level value")
                    if isinstance(value, ast.Call):
                        self.assertIn(ast.unparse(value.func), self.ALLOWED_CALLS, f"{name}: module-level object {ast.unparse(value)[:60]}")


class Readers(Fixture):
    def test_twenty_concurrent_readers_with_different_queries_while_discovery_writes(self):
        built = self.build(city_pages(20))
        built.service.register_source(SPEC)
        built.service.run_once()
        base = built.reader.candidates(CandidateQuery(limit=500))
        queries = [CandidateQuery(title_contains=f"Event {i}", limit=50) for i in range(10)] + \
                  [CandidateQuery(starts_from=base[i].starts_at, starts_to=base[i].starts_at + 86400 * 3, limit=50) for i in range(10)]
        expected = [tuple(c.candidate_key for c in built.reader.candidates(q)) for q in queries]
        other = SourceSpec("other_source", "sandiego_gov", "Other", "https://other.example/events", interval_s=60)
        stop, write_latency, errors, read_latency = threading.Event(), [], [], []

        def writer():  # discovery keeps writing another source's pages and candidates while the readers read
            n = 0
            while not stop.is_set():
                n += 1
                started = time.perf_counter()
                try:
                    with built.store.write() as tx:
                        source_id = SourceRepository().upsert(tx, other, n)
                        page_id, _ = PageRepository().touch(tx, f"https://other.example/event/{n}", source_id, "detail", n)
                        built.service.candidates.upsert(tx, f"other_source:{n}", source_id, page_id, "adapter", "test", f"Zeta {n}", None, {"id": n},
                                                        repositories.payload_hash({"id": n}), n)
                        built.service.changes.append(tx, "candidate", f"other_source:{n}", "0" * 64, n)
                except Exception as error:  # pragma: no cover - reported below
                    errors.append(repr(error))
                write_latency.append(time.perf_counter() - started)

        results = [None] * len(queries)

        def reader(i):
            local = []
            for _ in range(15):
                started = time.perf_counter()
                found = tuple(c.candidate_key for c in built.reader.candidates(queries[i]))
                local.append(time.perf_counter() - started)
                if found != expected[i]:
                    errors.append(f"reader {i} saw {found}")
            results[i] = found
            read_latency.extend(local)

        write_thread = threading.Thread(target=writer)
        write_thread.start()
        threads = [threading.Thread(target=reader, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop.set()
        write_thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(results, list(expected))
        self.assertGreater(len(write_latency), 5, "writes proceeded while the readers read")

        def pct(values, q):
            values = sorted(values)
            return round(values[min(len(values) - 1, int(q * len(values)))] * 1000, 1)
        summary = {"readers": 20, "reads": len(read_latency), "read_p50_ms": pct(read_latency, 0.5), "read_p95_ms": pct(read_latency, 0.95),
                   "writes": len(write_latency), "write_p50_ms": pct(write_latency, 0.5), "write_p95_ms": pct(write_latency, 0.95),
                   "lock_retries": built.store.stats.summary()["retries"], "lock_failures": built.store.stats.summary()["failures"]}
        print("\nconcurrency (development machine):", json.dumps(summary))
        self.assertEqual(summary["lock_failures"], 0)

    def test_a_stalled_consumer_does_not_hold_up_discovery(self):
        built = self.build()
        built.service.register_source(SPEC)
        stalled = sqlite3.connect(self.db, isolation_level=None)
        stalled.execute("BEGIN")
        stalled.execute("SELECT * FROM changes").fetchall()  # a consumer sitting in an open read transaction, never finishing
        try:
            self.assertEqual(built.service.run_once().outcome, "completed")
            self.clock.t += 3600
            self.assertEqual(built.service.run_once().outcome, "completed")
        finally:
            stalled.execute("ROLLBACK")
            stalled.close()
        self.assertEqual(self.count("candidates"), 4)

    def test_the_reader_contract_returns_records_not_rows(self):
        built = self.build()
        built.service.register_source(SPEC)
        built.service.run_once()
        record = built.reader.candidates(CandidateQuery(limit=1))[0]
        self.assertTrue(record.candidate_key.startswith("sandiego_gov:"))
        self.assertEqual(len(record.payload_hash), 64)
        self.assertIsNone(built.reader.candidate("missing:key"))
        self.assertIsNone(built.reader.page("0" * 32))
        status = built.reader.status()
        self.assertEqual((status.schema, status.candidates, status.sources[0].source_key), ("discovery.v1", 4, "sandiego_gov"))
        self.assertGreater(status.database_bytes + status.wal_bytes, 0)


if __name__ == "__main__":
    unittest.main()
