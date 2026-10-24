"""Control storage: the ControlStorage port. Areas, their sources and the shared settings: reads, and the commands that change them.

Every command is ONE write transaction that checks the expected revision, makes the change and appends its durable change record. Validation of a request is the caller's; what
depends on the stored state (the expected revision, a radius against the area's unit) is checked here, inside the transaction, with the caller's `settle` function.
"""
import sqlite3

from ..contracts import CONTROL_SCHEMA, ControlResult, ControlStatus, NotFound, RevisionConflict, SourceStatus, digest_of


class SqliteControlStorage:
    def __init__(self, store, sources, areas, settings, jobs, changes, clock):
        self._store, self._sources, self._areas, self._settings = store, sources, areas, settings
        self._jobs, self._changes, self._clock = jobs, changes, clock

    def _now(self):
        return int(self._clock())

    # --- reads -----------------------------------------------------------------------------------------------------

    def list_areas(self):
        with self._store.read() as connection:
            return self._areas.all(connection)

    def get_area(self, area_key):
        with self._store.read() as connection:
            return self._areas.get(connection, area_key)

    def status(self, runtime):
        with self._store.read() as connection:
            settings = self._settings.all(connection)
            areas = self._areas.all(connection)
            eligible = {row[1] for row in self._areas.eligible_sources(connection)}
            jobs = {r["source_id"]: r for r in connection.execute("SELECT * FROM jobs WHERE kind='crawl_source'")}
            sources = []
            for row in connection.execute("SELECT * FROM sources ORDER BY source_key"):
                job = jobs.get(row["source_id"])
                sources.append(SourceStatus(row["source_key"], bool(row["enabled"]), row["source_key"] in eligible, row["interval_s"], row["last_success_at"],
                                            row["last_attempt_at"], job["state"] if job else None, job["next_run_at"] if job else None,
                                            job["attempts"] if job else 0, job["last_error"] if job else None))
            latest = self._changes.latest(connection)
            counts = self._jobs.counts(connection)
        return ControlStatus(CONTROL_SCHEMA, settings, len(areas), sum(1 for a in areas if a.enabled), tuple(sources), counts, latest,
                             **self._store.sizes(), runtime=dict(runtime))

    # --- commands --------------------------------------------------------------------------------------------------

    def create_area(self, fields, enabled, request_id, source_keys, new_key):
        now = self._now()
        try:
            with self._store.write() as tx:
                if request_id is not None:
                    existing = self._areas.row_by_request(tx, request_id)
                    if existing is not None:
                        return ControlResult(CONTROL_SCHEMA, existing["revision"], None, True, area=self._areas.record(tx, existing))
                source_ids = self._source_ids(tx, source_keys)
                area_key = new_key()
                area_id = self._areas.create(tx, area_key, request_id, fields["name"], fields["latitude"], fields["longitude"], fields["radius"], fields["unit"],
                                             enabled, fields["intensity"], now)
                self._areas.set_sources(tx, area_id, source_ids, now)
                area = self._areas.record(tx, self._areas.row_by_key(tx, area_key))
                seq = self._record_area_change(tx, area, now)
        except sqlite3.IntegrityError as error:  # two creates with the same request_id raced: the second one replays the first
            if request_id is not None and "request_id" in str(error):
                return self.create_area(fields, enabled, request_id, source_keys, new_key)
            raise
        return ControlResult(CONTROL_SCHEMA, area.revision, seq, False, area=area)

    def update_area(self, area_key, expected_revision, wanted, source_keys, settle):
        now = self._now()
        with self._store.write() as tx:
            row = self._areas.row_by_key(tx, area_key)
            if row is None:
                raise NotFound(f"no area '{area_key}'")
            wanted = settle(self._areas.record(tx, row), dict(wanted))   # checks that depend on the stored area (a radius against its unit); may refuse
            if row["revision"] != expected_revision:
                raise RevisionConflict(f"the area is at revision {row['revision']}, not {expected_revision}", row["revision"])
            changed = {k: v for k, v in wanted.items() if row[k] != v}
            source_ids = None if source_keys is None else self._source_ids(tx, source_keys)
            sources_changed = source_ids is not None and set(source_ids) != set(self._areas.source_ids(tx, row["area_id"]))
            if not changed and not sources_changed:
                return ControlResult(CONTROL_SCHEMA, row["revision"], None, True, area=self._areas.record(tx, row))
            self._areas.update(tx, row["area_id"], changed or {"name": row["name"]}, now)  # a source-list change alone is still a new revision
            if sources_changed:
                self._areas.set_sources(tx, row["area_id"], source_ids, now)
            area = self._areas.record(tx, self._areas.row_by_key(tx, area_key))
            seq = self._record_area_change(tx, area, now)
        return ControlResult(CONTROL_SCHEMA, area.revision, seq, False, area=area)

    def set_settings(self, expected_revision, mode, intensity):
        now = self._now()
        with self._store.write() as tx:
            current = self._settings.all(tx)
            if current.revision != expected_revision:
                raise RevisionConflict(f"the settings are at revision {current.revision}, not {expected_revision}", current.revision)
            if mode in (None, current.mode) and intensity in (None, current.intensity):
                return ControlResult(CONTROL_SCHEMA, current.revision, None, True, settings=current)
            if mode is not None:
                self._settings.set(tx, "mode", mode, now)
            if intensity is not None:
                self._settings.set(tx, "intensity", intensity, now)
            self._settings.bump(tx, now)
            settings = self._settings.all(tx)
            seq = self._changes.append(tx, "settings", "discovery", digest_of(settings), now, kind="discovery.settings_changed")
        return ControlResult(CONTROL_SCHEMA, settings.revision, seq, False, settings=settings)

    def set_paused(self, paused):
        now = self._now()
        with self._store.write() as tx:
            current = self._settings.all(tx)
            if current.paused == paused:
                return ControlResult(CONTROL_SCHEMA, current.revision, None, True, settings=current)
            self._settings.set(tx, "paused", "1" if paused else "0", now)
            self._settings.bump(tx, now)
            settings = self._settings.all(tx)
            seq = self._changes.append(tx, "discovery", "service", digest_of(settings), now, kind="discovery.paused" if paused else "discovery.resumed")
        return ControlResult(CONTROL_SCHEMA, settings.revision, seq, False, settings=settings)

    # --- internals -------------------------------------------------------------------------------------------------

    def _record_area_change(self, tx, area, now):
        return self._changes.append(tx, "area", area.area_key, digest_of(area), now, kind="area.configuration_changed")

    def _source_ids(self, tx, keys):
        ids = []
        for key in keys:
            row = self._sources.by_key(tx, key)
            if row is None:
                raise NotFound(f"no source '{key}'")
            ids.append(row["source_id"])
        return ids
