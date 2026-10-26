"""DiscoveryControl: the control plane's typed contract and its implementation.

Control commands change what Discovery is asked to cover (areas, their sources, intensity, pause); they never carry discoveries. A command is validated here, then Persistence (the
ControlStorage port) checks it against the expected configuration revision and commits it in ONE write transaction together with a durable change record; it is acknowledged
immediately. An acknowledgement therefore means "saved", not "discovery has caught up": the scheduler notices the new configuration on its next planning step.

Idempotence: creating an area with a `request_id` that was already used returns the existing area; setting an area to the state it already has, an update that changes nothing,
pausing a paused service and resuming a running one all succeed without a new revision or change record (`replayed=True`). Stale or conflicting revisions are refused with
`RevisionConflict`, never merged. This module holds no SQL: it validates, and asks Persistence.
"""
import math
import re
import types
import uuid
from dataclasses import dataclass
from typing import Optional, Protocol

from ..contracts.models import (CONTROL_SCHEMA, INTENSITIES, MODES, UNITS, AreaRecord, ControlError, ControlResult, ControlStatus, NotFound, RevisionConflict,
                                SourceControlResult, SourceSpec, SourceStatus, ValidationFailed)
from ..persistence.ports import ControlStorage

MAX_SOURCES_PER_AREA = 20
MAX_RADIUS = types.MappingProxyType({"mi": 250.0, "km": 400.0})
SOURCE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")


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
class CreateSourceRequest:
    source_key: str
    adapter: str
    name: str
    enabled: bool
    interval_minutes: int
    options: dict


@dataclass(frozen=True)
class UpdateSourceRequest:
    source_key: str
    expected_revision: int
    name: Optional[str] = None
    enabled: Optional[bool] = None
    interval_minutes: Optional[int] = None
    options: Optional[dict] = None


class DiscoveryControl(Protocol):
    def get_status(self) -> ControlStatus: ...
    def list_areas(self) -> tuple: ...
    def list_sources(self) -> tuple: ...
    def get_source(self, source_key: str): ...
    def get_area(self, area_key: str) -> AreaRecord: ...
    def create_area(self, request: CreateAreaRequest) -> ControlResult: ...
    def update_area(self, request: UpdateAreaRequest) -> ControlResult: ...
    def create_source(self, request: CreateSourceRequest) -> SourceControlResult: ...
    def update_source(self, request: UpdateSourceRequest) -> SourceControlResult: ...
    def set_source_enabled(self, source_key: str, enabled: bool, expected_revision: int) -> SourceControlResult: ...
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
    """The control contract: validates a command and asks Persistence to carry it out. `runtime` is a callable returning the host's operating state (loop, uptime) for status."""

    def __init__(self, storage: ControlStorage, runtime=lambda: {}, new_key=lambda: uuid.uuid4().hex[:16], adapters=(), source_validators=None, reserved_source_keys=(), producer_definitions=None):
        self._storage, self._runtime, self._new_key = storage, runtime, new_key
        self._adapters, self._source_validators = frozenset(adapters), dict(source_validators or {})
        self._reserved_source_keys = frozenset(reserved_source_keys)
        self._producer_definitions = tuple(producer_definitions or ())

    # --- reads -----------------------------------------------------------------------------------------------------

    def list_areas(self):
        return self._storage.list_areas()

    def list_producer_types(self):
        """Versioned form metadata defined by replaceable producer adapters."""
        return {"schema": "area_events.producer_forms.v1", "types": list(self._producer_definitions)}

    def list_sources(self):
        return self._storage.list_sources()

    def get_source(self, source_key):
        source = self._storage.get_source(source_key)
        if source is None:
            raise NotFound(f"no source '{source_key}'")
        return source

    def get_area(self, area_key):
        area = self._storage.get_area(area_key)
        if area is None:
            raise NotFound(f"no area '{area_key}'")
        return area

    def get_status(self):
        return self._storage.status(self._runtime())

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
        return self._storage.create_area(fields, request.enabled, request_id, keys, self._new_key)

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

        def settle(current, wanted):
            """The checks that depend on the stored area: a radius against the unit it will have."""
            unit = wanted.get("unit", current.unit)
            if request.radius is not None:
                wanted["radius"] = _number(request.radius, "radius", 0.1, MAX_RADIUS[unit])
            elif "unit" in wanted and current.radius > MAX_RADIUS[unit]:
                raise ValidationFailed(f"radius must be at most {MAX_RADIUS[unit]} {unit}")
            return wanted

        return self._storage.update_area(request.area_key, expected, wanted, keys, settle)

    def create_source(self, request: CreateSourceRequest):
        if not isinstance(request.source_key, str) or not SOURCE_ID.fullmatch(request.source_key):
            raise ValidationFailed("id must be a lowercase source key using letters, numbers, '_' or '-'")
        if request.source_key in self._reserved_source_keys:
            raise ValidationFailed("id is reserved for a built-in source")
        if not isinstance(request.enabled, bool):
            raise ValidationFailed("enabled must be true or false")
        spec = self._source_spec(request.source_key, request.adapter, request.name, request.interval_minutes, request.options)
        return self._storage.create_source(spec, request.enabled)

    def update_source(self, request: UpdateSourceRequest):
        expected = _revision(request.expected_revision)
        # Built-in producer identity/type remains reserved and immutable. Operators can
        # still tune the persisted instance; registration never overwrites those settings.
        current = self.get_source(request.source_key)
        name = current.name if request.name is None else _name(request.name)
        enabled = current.enabled if request.enabled is None else request.enabled
        if not isinstance(enabled, bool):
            raise ValidationFailed("enabled must be true or false")
        interval = current.interval_minutes if request.interval_minutes is None else request.interval_minutes
        options = current.options_dict() if request.options is None else request.options
        spec = self._source_spec(current.source_key, current.adapter, name, interval, options)
        return self._storage.update_source(current.source_key, expected, spec, enabled)

    def set_source_enabled(self, source_key, enabled, expected_revision):
        if not isinstance(enabled, bool):
            raise ValidationFailed("enabled must be true or false")
        expected = _revision(expected_revision)
        # Enabling an unavailable producer is unsafe, but disabling must still work.
        if enabled:
            current = self.get_source(source_key)
            if current.revision != expected:
                raise RevisionConflict(f"the source is at revision {current.revision}, not {expected}", current.revision)
            if current.adapter not in self._adapters:
                raise ValidationFailed("cannot enable a producer whose adapter is not installed")
            self._source_spec(current.source_key, current.adapter, current.name, current.interval_minutes, current.options_dict())
        return self._storage.set_source_enabled(source_key, expected, enabled)

    def set_area_enabled(self, area_key, enabled, expected_revision=None):
        if not isinstance(enabled, bool):
            raise ValidationFailed("enabled must be true or false")
        revision = expected_revision if expected_revision is not None else self.get_area(area_key).revision
        return self.update_area(UpdateAreaRequest(area_key, revision, enabled=enabled))

    def set_discovery_mode(self, request: SettingsRequest):
        expected = _revision(request.expected_revision)
        mode = None if request.mode is None else _choice(request.mode, "mode", MODES)
        intensity = None if request.intensity is None else _choice(request.intensity, "intensity", INTENSITIES)
        return self._storage.set_settings(expected, mode, intensity)

    def _source_spec(self, source_key, adapter, name, interval_minutes, options):
        if not isinstance(adapter, str) or adapter not in self._adapters:
            raise ValidationFailed("type must name an installed producer")
        validate_options = self._source_validators.get(adapter)
        if validate_options is None:
            raise ValidationFailed("the installed producer does not support source configuration")
        clean_name = _name(name)
        if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, int) or interval_minutes < 1 or interval_minutes > (2**63 - 1) // 60:
            raise ValidationFailed("interval_minutes must be a positive integer")
        if not isinstance(options, dict):
            raise ValidationFailed("options must be an object")
        try:
            url, clean_options = validate_options(options)
        except (TypeError, ValueError) as error:
            raise ValidationFailed(str(error)) from error
        if not isinstance(url, str) or not isinstance(clean_options, dict):
            raise ValidationFailed("producer configuration validator returned an invalid result")
        return SourceSpec(source_key, adapter, clean_name, url, tuple(sorted(clean_options.items())), interval_minutes * 60)

    def pause(self):
        return self._storage.set_paused(True)

    def resume(self):
        return self._storage.set_paused(False)

    @staticmethod
    def _source_keys(sources):
        if isinstance(sources, (str, bytes)) or not isinstance(sources, (list, tuple)):
            raise ValidationFailed("sources must be a list of source keys")
        if len(sources) > MAX_SOURCES_PER_AREA or any(not isinstance(k, str) or not 1 <= len(k) <= 80 for k in sources):
            raise ValidationFailed(f"sources must be at most {MAX_SOURCES_PER_AREA} source keys")
        return tuple(dict.fromkeys(sources))
