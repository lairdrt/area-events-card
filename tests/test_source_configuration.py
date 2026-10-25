"""Database-backed producer instance configuration through Control, the API and scheduler."""
import json
import os
import sqlite3
import tempfile
import unittest

from area_events import app as app_module
from area_events.api.http_api import ApiTokens, DiscoveryApi
from area_events.control.service import CreateAreaRequest, CreateSourceRequest, UpdateSourceRequest
from area_events.contracts.models import NotFound, RevisionConflict, SourceExists, SourceSpec, ValidationFailed
from tests.city_fixtures import CITY_URL
from tests.test_discovery_service import Clock, FakeClient, city_pages

CONTROL_TOKEN = "c" * 40
READ_TOKEN = "r" * 40
LOCALIST_OPTIONS = {"url": "https://calendar.one.example", "days": 30, "max_pages": 4, "timezone": "America/Chicago"}


class SourceConfiguration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = os.path.join(self.temp.name, "discovery.db")
        self.clock = Clock()
        self.app = self._open()

    def tearDown(self):
        if self.app is not None:
            self.app.close()

    def _open(self, fetch_client=None):
        app = app_module.build(self.db, clock=self.clock, fetch_client=fetch_client or FakeClient({}))
        app.register_known_sources()
        return app

    def _source(self, key="calendar-one", **overrides):
        values = dict(source_key=key, adapter="localist", name="Campus Calendar", enabled=True, interval_minutes=60, options=dict(LOCALIST_OPTIONS))
        values.update(overrides)
        return self.app.control.create_source(CreateSourceRequest(**values))

    def test_create_list_update_enable_disable_and_restart(self):
        first = self._source()
        self.assertEqual((first.revision, first.source.source_key, first.source.adapter, first.source.interval_minutes), (1, "calendar-one", "localist", 60))
        self.assertEqual(first.source.options_dict(), LOCALIST_OPTIONS)
        self._source("calendar-two", name="Second Calendar", options={**LOCALIST_OPTIONS, "url": "https://calendar.two.example"})
        self.assertEqual({s.source_key for s in self.app.control.list_sources()}, {"sandiego_gov", "ut_austin", "calendar-one", "calendar-two"})

        changed = self.app.control.update_source(UpdateSourceRequest("calendar-one", 1, name="Updated Calendar", interval_minutes=90))
        self.assertEqual((changed.revision, changed.source.name, changed.source.interval_minutes), (2, "Updated Calendar", 90))
        replay = self.app.control.update_source(UpdateSourceRequest("calendar-one", 2, name="Updated Calendar"))
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.revision, 2)
        disabled = self.app.control.set_source_enabled("calendar-one", False, 2)
        self.assertEqual((disabled.source.enabled, disabled.revision), (False, 3))
        enabled = self.app.control.set_source_enabled("calendar-one", True, 3)
        self.assertEqual((enabled.source.enabled, enabled.revision), (True, 4))

        self.app.close()
        self.app = self._open()
        persisted = self.app.control.get_source("calendar-one")
        self.assertEqual((persisted.name, persisted.enabled, persisted.interval_minutes, persisted.revision, persisted.options_dict()),
                         ("Updated Calendar", True, 90, 4, LOCALIST_OPTIONS))

    def test_validation_and_builtin_identity_protection(self):
        self._source()
        with self.assertRaises(SourceExists):
            self._source()
        for key, adapter, name, enabled, interval, options in (
            ("bad key", "localist", "Name", True, 60, LOCALIST_OPTIONS),
            ("not-installed", "unknown", "Name", True, 60, LOCALIST_OPTIONS),
            ("missing-url", "localist", "Name", True, 60, {"days": 14}),
            ("unknown-option", "localist", "Name", True, 60, {**LOCALIST_OPTIONS, "api_key": "private"}),
            ("bad-interval", "localist", "Name", True, True, LOCALIST_OPTIONS),
            ("bad-url", "localist", "Name", True, 60, {**LOCALIST_OPTIONS, "url": "https://user:password@calendar.example"}),
            ("query-url", "localist", "Name", True, 60, {**LOCALIST_OPTIONS, "url": "https://calendar.example?api_key=private"}),
            ("bad-zone", "localist", "Name", True, 60, {**LOCALIST_OPTIONS, "timezone": "not_a_zone"}),
            ("bad-enabled", "localist", "Name", "yes", 60, LOCALIST_OPTIONS),
        ):
            with self.subTest(key=key), self.assertRaises(ValidationFailed):
                self.app.control.create_source(CreateSourceRequest(key, adapter, name, enabled, interval, options))
        with self.assertRaises(ValidationFailed):
            self._source("sandiego_gov", adapter="sandiego_gov", options={"url": CITY_URL})
        with self.assertRaises(ValidationFailed):
            self.app.control.update_source(UpdateSourceRequest("sandiego_gov", 1, name="changed"))
        with self.assertRaises(NotFound):
            self.app.control.get_source("not-present")
        self.assertEqual({source.source_key for source in self.app.control.list_sources()}, {"sandiego_gov", "ut_austin", "calendar-one"})

    def test_revision_conflicts_do_not_modify_configuration(self):
        self._source()
        first = self.app.control.update_source(UpdateSourceRequest("calendar-one", 1, interval_minutes=90))
        with self.assertRaises(RevisionConflict) as error:
            self.app.control.update_source(UpdateSourceRequest("calendar-one", 1, interval_minutes=120))
        self.assertEqual(error.exception.current_revision, 2)
        self.assertEqual(self.app.control.get_source("calendar-one").interval_minutes, 90)
        with self.assertRaises(ValidationFailed):
            self.app.control.update_source(UpdateSourceRequest("calendar-one", first.revision, options={**LOCALIST_OPTIONS, "token": "secret"}))
        self.assertEqual(self.app.control.get_source("calendar-one").revision, 2)

    def test_api_reads_use_read_token_and_mutations_require_control_token(self):
        api = DiscoveryApi(self.app.control, self.app.reader, ApiTokens(CONTROL_TOKEN, READ_TOKEN), self.clock)
        read_headers = {"Authorization": f"Bearer {READ_TOKEN}"}
        control_headers = {"Authorization": f"Bearer {CONTROL_TOKEN}", "Content-Type": "application/json"}
        status, page = api.handle("GET", "/v1/sources", read_headers, b"", "read-client")
        self.assertEqual((status, len(page["sources"])), (200, 2))
        status, _ = api.handle("POST", "/v1/sources", {**read_headers, "Content-Type": "application/json"},
                               json.dumps({"id": "api-calendar", "type": "localist", "name": "API Calendar", "enabled": True,
                                           "interval_minutes": 60, "options": LOCALIST_OPTIONS}).encode(), "read-client")
        self.assertEqual(status, 403)
        body = json.dumps({"id": "api-calendar", "type": "localist", "name": "API Calendar", "enabled": True,
                           "interval_minutes": 60, "options": LOCALIST_OPTIONS}).encode()
        status, created = api.handle("POST", "/v1/sources", control_headers, body, "control-client")
        self.assertEqual((status, created["source"]["id"], created["source"]["options"]), (201, "api-calendar", LOCALIST_OPTIONS))
        status, detail = api.handle("GET", "/v1/sources/api-calendar", read_headers, b"", "read-client")
        self.assertEqual((status, detail["source"]["revision"]), (200, 1))
        status, patched = api.handle("PATCH", "/v1/sources/api-calendar", control_headers,
                                     b'{"expected_revision":1,"interval_minutes":120}', "control-client")
        self.assertEqual((status, patched["revision"], patched["source"]["interval_minutes"]), (200, 2, 120))
        status, disabled = api.handle("POST", "/v1/sources/api-calendar/disable", control_headers,
                                      b'{"expected_revision":2}', "control-client")
        self.assertEqual((status, disabled["source"]["enabled"], disabled["revision"]), (200, False, 3))

    def test_scheduler_tracks_new_updated_and_disabled_instances_without_losing_candidates(self):
        options = {"url": CITY_URL, "pages": 1, "max_details_per_run": 10}
        created = self.app.control.create_source(CreateSourceRequest("city-instance", "sandiego_gov", "City Instance", True, 60, options))
        area = self.app.control.create_area(CreateAreaRequest("City", 32.7, -117.1, 25, sources=("city-instance",))).area
        self.app.close()
        self.app = self._open(FakeClient(city_pages(2)))
        # The new instance is already persisted; re-opening the composition root does not replace its settings.
        self.app.scheduler.plan()
        self.assertEqual(self.app.scheduler.next_due(), self.clock())
        self.assertEqual(self.app.service.run_once().outcome, "completed")
        connection = sqlite3.connect(self.db)
        try:
            before = connection.execute("SELECT COUNT(*) FROM candidates c JOIN sources s USING (source_id) WHERE s.source_key='city-instance'").fetchone()[0]
            self.assertGreater(before, 0)
        finally:
            connection.close()

        self.app.control.update_source(UpdateSourceRequest("city-instance", created.revision, interval_minutes=120))
        self.app.scheduler.plan()
        self.assertEqual(self.app.scheduler.next_due(), self.clock() + 120 * 60)
        disabled = self.app.control.set_source_enabled("city-instance", False, 2)
        self.app.scheduler.plan()
        self.assertIsNone(self.app.scheduler.next_due())
        self.app.control.set_source_enabled("city-instance", True, disabled.revision)
        self.app.scheduler.plan()
        self.assertEqual(self.app.scheduler.next_due(), self.clock() + 120 * 60)
        connection = sqlite3.connect(self.db)
        try:
            after = connection.execute("SELECT COUNT(*) FROM candidates c JOIN sources s USING (source_id) WHERE s.source_key='city-instance'").fetchone()[0]
            self.assertEqual(after, before)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM area_sources x JOIN areas a USING (area_id) JOIN sources s USING (source_id) WHERE a.area_key=? AND s.source_key='city-instance'",
                                                (area.area_key,)).fetchone()[0], 1)
        finally:
            connection.close()

    def test_updating_one_source_does_not_reschedule_an_unrelated_source(self):
        first = self._source("calendar-one")
        second = self._source("calendar-two", options={**LOCALIST_OPTIONS, "url": "https://calendar.two.example"})
        self.app.control.create_area(CreateAreaRequest("First area", 32.7, -117.1, 25, sources=(first.source.source_key,)))
        self.app.control.create_area(CreateAreaRequest("Second area", 30.2, -97.7, 25, sources=(second.source.source_key,)))
        self.app.scheduler.plan()

        connection = sqlite3.connect(self.db)
        try:
            now = self.clock()
            connection.execute("UPDATE sources SET last_success_at=? WHERE source_key IN ('calendar-one', 'calendar-two')", (now,))
            connection.execute("UPDATE jobs SET next_run_at=? WHERE source_id IN (SELECT source_id FROM sources WHERE source_key IN ('calendar-one', 'calendar-two'))",
                               (now + 60 * 60,))
            connection.commit()
        finally:
            connection.close()

        self.app.control.update_source(UpdateSourceRequest("calendar-one", 1, interval_minutes=120))
        self.app.scheduler.plan()
        connection = sqlite3.connect(self.db)
        try:
            due = dict(connection.execute("SELECT s.source_key, j.next_run_at FROM jobs j JOIN sources s USING (source_id) WHERE s.source_key IN ('calendar-one', 'calendar-two')"))
        finally:
            connection.close()
        now = self.clock()
        self.assertEqual(due, {"calendar-one": now + 120 * 60, "calendar-two": now + 60 * 60})

    def test_builtin_registration_does_not_overwrite_existing_database_configuration(self):
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("UPDATE sources SET name='Persisted Name', interval_s=7200 WHERE source_key='sandiego_gov'")
            connection.commit()
        finally:
            connection.close()
        self.app.register_known_sources()
        city = self.app.control.get_source("sandiego_gov")
        self.assertEqual((city.name, city.interval_minutes), ("Persisted Name", 120))


if __name__ == "__main__":
    unittest.main()
