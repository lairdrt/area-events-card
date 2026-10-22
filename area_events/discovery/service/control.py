"""DiscoveryControl: the control plane's typed contract and its local implementation.

Control commands change what Discovery is asked to cover (areas, their sources, intensity, pause); they never carry discoveries. A
command is validated, checked against the expected configuration revision, committed in ONE write transaction together with a durable
change record, and acknowledged immediately. An acknowledgement therefore means "saved", not "discovery has caught up": the scheduler
notices the new configuration on its next planning step.

Idempotence: creating an area with a `request_id` that was already used returns the existing area; setting an area to the state it
already has, an update that changes nothing, pausing a paused service and resuming a running one all succeed without a new revision or
change record (`replayed=True`). Stale or conflicting revisions are refused with `RevisionConflict`, never merged.
"""
import hashlib
import json
import math
import sqlite3
import types
import uuid
from dataclasses import dataclass, field
from typing import Optional, Protocol

from .areas import INTENSITIES, MODES, UNITS, AreaRecord, AreaRepository, SettingsRecord, SettingsRepository
from .contracts import JobCounts
from .store import DiscoveryStore

CONTROL_SCHEMA = "discovery.control.v1"
MAX_SOURCES_PER_AREA = 20
MAX_RADIUS = types.MappingProxyType({"mi": 250.0, "km": 400.0})


class ControlError(Exception):
    code = "error"
    status = 400


class ValidationFailed(ControlError):
    code, status = "invalid_request", 400


class NotFound(ControlError):
    code, status = "not_found", 404


class RevisionConflict(ControlError):
    code, status = "revision_conflict", 409

    def __init__(self, message, current_revision):
        super().__init__(message)
        self.current_revision = current_revision


@dataclass(frozen=True)
class CreateAreaRequest:
    name: str
    latitude: float
    longitude: float
    radius: float
    unit: str = "mi"
    intensity: str = "normal"
    enabled: bool = True
    sources: tuple = ()
    request_id: Optional[str] = None


@dataclass(frozen=True)
class UpdateAreaRequest:
    area_key: str
    expected_revision: int
    name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    radius: Optional[float] = None
    unit: Optional[str] = None
    intensity: Optional[str] = None
    enabled: Optional[bool] = None
    sources: Optional[tuple] = None


@dataclass(frozen=True)
class SettingsRequest:
    expected_revision: int
    mode: Optional[str] = None
    intensity: Optional[str] = None


@dataclass(frozen=True)
class ControlResult:
    schema: str
    revision: int                   # the revision of the thing that changed (area or settings)
    change_seq: Optional[int]       # the durable change record; None when nothing changed
    replayed: bool = False
    area: Optional[AreaRecord] = None
    settings: Optional[SettingsRecord] = None


@dataclass(frozen=True)
class SourceStatus:
    source_key: str
    enabled: bool
    eligible: bool                  # an enabled area uses it
    interval_s: int
    last_success_at: Optional[int]
    last_attempt_at: Optional[int]
    job_state: Optional[str]
    next_run_at: Optional[int]
    attempts: int
    last_error: Optional[str]


@dataclass(frozen=True)
class ControlStatus:
    schema: str
    settings: SettingsRecord
    areas: int
    areas_enabled: int
    sources: tuple
    jobs: JobCounts
    latest_change_seq: int
    database_bytes: int
    wal_bytes: int
    runtime: dict = field(default_factory=dict)


class DiscoveryControl(Protocol):
    def get_status(self) -> ControlStatus: ...
    def list_areas(self) -> tuple: ...
    def get_area(self, area_key: str) -> AreaRecord: ...
    def create_area(self, request: CreateAreaRequest) -> ControlResult: ...
    def update_area(self, request: UpdateAreaRequest) -> ControlResult: ...
    def set_area_enabled(self, area_key: str, enabled: bool, expected_revision: Optional[int] = None) -> ControlResult: ...
    def set_discovery_mode(self, request: SettingsRequest) -> ControlResult: ...
    def pause(self) -> ControlResult: ...
    def resume(self) -> ControlResult: ...


def _number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value) or math.isinf(value):
        raise ValidationFailed(f"{name} must be a number")
    if not low <= value <= high:
        raise ValidationFailed(f"{name} must be between {low} and {high}")
    return float(value)


def _name(value):
    if not isinstance(value, str):
        raise ValidationFailed("name must be text")
    text = " ".join(value.split())
    if not 1 <= len(text) <= 80 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValidationFailed("name must be 1 to 80 printable characters")
    return text


def _choice(value, name, allowed):
    if value not in allowed:
        raise ValidationFailed(f"{name} must be one of {', '.join(allowed)}")
    return value


def _revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationFailed("expected_revision is required")
    return value


class LocalDiscoveryControl:
    """The control contract over the local database. `runtime` is a callable returning the host's operating state (loop, uptime) for status."""

    def __init__(self, store: DiscoveryStore, sources, areas: AreaRepository, settings: SettingsRepository, jobs, changes, clock, runtime=lambda: {},
                 new_key=lambda: uuid.uuid4().hex[:16]):
        self._store, self._sources, self._areas, self._settings = store, sources, areas, settings
        self._jobs, self._changes, self._clock, self._runtime, self._new_key = jobs, changes, clock, runtime, new_key

    def _now(self):
        return int(self._clock())

    # --- reads -----------------------------------------------------------------------------------------------------

    def list_areas(self):
        with self._store.read() as connection:
            return self._areas.all(connection)

    def get_area(self, area_key):
        with self._store.read() as connection:
            area = self._areas.get(connection, area_key)
        if area is None:
            raise NotFound(f"no area '{area_key}'")
        return area

    def get_status(self):
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
                             **self._store.sizes(), runtime=dict(self._runtime()))

    # --- commands --------------------------------------------------------------------------------------------------

    def create_area(self, request: CreateAreaRequest):
        unit = _choice(request.unit, "unit", UNITS)
        fields = dict(name=_name(request.name), latitude=_number(request.latitude, "latitude", -90, 90), longitude=_number(request.longitude, "longitude", -180, 180),
                      radius=_number(request.radius, "radius", 0.1, MAX_RADIUS[unit]), unit=unit, intensity=_choice(request.intensity, "intensity", INTENSITIES))
        if not isinstance(request.enabled, bool):
            raise ValidationFailed("enabled must be true or false")
        keys = self._source_keys(request.sources)
        request_id = request.request_id
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 100):
            raise ValidationFailed("request_id must be 1 to 100 characters")
        now = self._now()
        try:
            with self._store.write() as tx:
                if request_id is not None:
                    existing = self._areas.row_by_request(tx, request_id)
                    if existing is not None:
                        return ControlResult(CONTROL_SCHEMA, existing["revision"], None, True, area=self._areas.record(tx, existing))
                source_ids = self._source_ids(tx, keys)
                area_key = self._new_key()
                area_id = self._areas.create(tx, area_key, request_id, fields["name"], fields["latitude"], fields["longitude"], fields["radius"], fields["unit"],
                                             request.enabled, fields["intensity"], now)
                self._areas.set_sources(tx, area_id, source_ids, now)
                area = self._areas.record(tx, self._areas.row_by_key(tx, area_key))
                seq = self._record_area_change(tx, area, now)
        except sqlite3.IntegrityError as error:  # two creates with the same request_id raced: the second one replays the first
            if request_id is not None and "request_id" in str(error):
                return self.create_area(request)
            raise
        return ControlResult(CONTROL_SCHEMA, area.revision, seq, False, area=area)

    def update_area(self, request: UpdateAreaRequest):
        expected = _revision(request.expected_revision)
        wanted = {}
        if request.name is not None:
            wanted["name"] = _name(request.name)
        if request.unit is not None:
            wanted["unit"] = _choice(request.unit, "unit", UNITS)
        if request.latitude is not None:
            wanted["latitude"] = _number(request.latitude, "latitude", -90, 90)
        if request.longitude is not None:
            wanted["longitude"] = _number(request.longitude, "longitude", -180, 180)
        if request.intensity is not None:
            wanted["intensity"] = _choice(request.intensity, "intensity", INTENSITIES)
        if request.enabled is not None:
            if not isinstance(request.enabled, bool):
                raise ValidationFailed("enabled must be true or false")
            wanted["enabled"] = 1 if request.enabled else 0
        keys = None if request.sources is None else self._source_keys(request.sources)
        now = self._now()
        with self._store.write() as tx:
            row = self._areas.row_by_key(tx, request.area_key)
            if row is None:
                raise NotFound(f"no area '{request.area_key}'")
            unit = wanted.get("unit", row["unit"])
            if request.radius is not None:
                wanted["radius"] = _number(request.radius, "radius", 0.1, MAX_RADIUS[unit])
            elif "unit" in wanted and row["radius"] > MAX_RADIUS[unit]:
                raise ValidationFailed(f"radius must be at most {MAX_RADIUS[unit]} {unit}")
            if row["revision"] != expected:
                raise RevisionConflict(f"the area is at revision {row['revision']}, not {expected}", row["revision"])
            changed = {k: v for k, v in wanted.items() if row[k] != v}
            source_ids = None if keys is None else self._source_ids(tx, keys)
            sources_changed = source_ids is not None and set(source_ids) != set(self._areas.source_ids(tx, row["area_id"]))
            if not changed and not sources_changed:
                return ControlResult(CONTROL_SCHEMA, row["revision"], None, True, area=self._areas.record(tx, row))
            self._areas.update(tx, row["area_id"], changed or {"name": row["name"]}, now)  # a source-list change alone is still a new revision
            if sources_changed:
                self._areas.set_sources(tx, row["area_id"], source_ids, now)
            area = self._areas.record(tx, self._areas.row_by_key(tx, request.area_key))
            seq = self._record_area_change(tx, area, now)
        return ControlResult(CONTROL_SCHEMA, area.revision, seq, False, area=area)

    def set_area_enabled(self, area_key, enabled, expected_revision=None):
        if not isinstance(enabled, bool):
            raise ValidationFailed("enabled must be true or false")
        revision = expected_revision if expected_revision is not None else self.get_area(area_key).revision
        return self.update_area(UpdateAreaRequest(area_key, revision, enabled=enabled))

    def set_discovery_mode(self, request: SettingsRequest):
        expected = _revision(request.expected_revision)
        mode = None if request.mode is None else _choice(request.mode, "mode", MODES)
        intensity = None if request.intensity is None else _choice(request.intensity, "intensity", INTENSITIES)
        now = self._now()
        with self._store.write() as tx:
            current = self._settings.all(tx)
            if current.revision != expected:
                raise RevisionConflict(f"the settings are at revision {current.revision}, not {expected}", current.revision)
            if mode in (None, current.mode) and intensity in (None, current.intensity):
                return ControlResult(CONTROL_SCHEMA, current.revision, None, True, settings=current)
            if mode is not None:
                self._settings.set(tx, "mode", mode, now)
            if intensity is not None:
                self._settings.set(tx, "intensity", intensity, now)
            self._settings.bump(tx, now)
            settings = self._settings.all(tx)
            seq = self._changes.append(tx, "settings", "discovery", self._digest(settings), now, kind="discovery.settings_changed")
        return ControlResult(CONTROL_SCHEMA, settings.revision, seq, False, settings=settings)

    def pause(self):
        return self._set_paused(True)

    def resume(self):
        return self._set_paused(False)

    # --- internals -------------------------------------------------------------------------------------------------

    def _set_paused(self, paused):
        now = self._now()
        with self._store.write() as tx:
            current = self._settings.all(tx)
            if current.paused == paused:
                return ControlResult(CONTROL_SCHEMA, current.revision, None, True, settings=current)
            self._settings.set(tx, "paused", "1" if paused else "0", now)
            self._settings.bump(tx, now)
            settings = self._settings.all(tx)
            seq = self._changes.append(tx, "discovery", "service", self._digest(settings), now, kind="discovery.paused" if paused else "discovery.resumed")
        return ControlResult(CONTROL_SCHEMA, settings.revision, seq, False, settings=settings)

    def _record_area_change(self, tx, area, now):
        return self._changes.append(tx, "area", area.area_key, self._digest(area), now, kind="area.configuration_changed")

    @staticmethod
    def _digest(record):
        data = {k: v for k, v in record.__dict__.items() if k not in ("created_at", "updated_at")}
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _source_keys(sources):
        if isinstance(sources, (str, bytes)) or not isinstance(sources, (list, tuple)):
            raise ValidationFailed("sources must be a list of source keys")
        if len(sources) > MAX_SOURCES_PER_AREA or any(not isinstance(k, str) or not 1 <= len(k) <= 80 for k in sources):
            raise ValidationFailed(f"sources must be at most {MAX_SOURCES_PER_AREA} source keys")
        return tuple(dict.fromkeys(sources))

    def _source_ids(self, tx, keys):
        ids = []
        for key in keys:
            row = self._sources.by_key(tx, key)
            if row is None:
                raise NotFound(f"no source '{key}'")
            ids.append(row["source_id"])
        return ids
