"""The card-facing WebSocket API. It rides on the signed-in user's Home Assistant session; the card holds no secret.

Reads (list areas, status, events, one event, the live subscription) are open to every signed-in user. Changing shared Discovery settings
(`area_create`, `area_update`, `settings`, `pause`, `resume`) needs a Home Assistant administrator AND a control token in the config entry;
the tokens themselves never leave the backend. A command that cannot reach the Discovery Service fails: nothing is reported as saved that
the service did not save.
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .const import (DOMAIN, PHOTOS_KEY, WS_AREA_CREATE, WS_AREA_UPDATE, WS_AREAS, WS_EVENT, WS_EVENTS, WS_PAUSE, WS_RESUME, WS_SETTINGS, WS_STATUS,
                    WS_SUBSCRIBE, WS_MANAGEMENT, WS_SOURCE_COMMAND)
from .discovery_client import DiscoveryAuthError, DiscoveryConflictError, DiscoveryConnectionError, DiscoveryRequestError
from .hub import ControlNotConfigured, UnknownArea

AREA = vol.All(str, vol.Length(min=1, max=100))
KEY = vol.All(str, vol.Length(min=8, max=60))
INTENSITY = vol.In(("low", "normal", "high"))
REVISION = vol.All(int, vol.Range(min=1))


@callback
def async_register(hass: HomeAssistant) -> None:
    for handler in (ws_areas, ws_status, ws_events, ws_event, ws_subscribe, ws_area_create, ws_area_update, ws_settings, ws_pause, ws_resume, ws_management, ws_source_command):
        websocket_api.async_register_command(hass, handler)


def _loaded_entry(hass):
    return next((e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED), None)


async def _serve(hass, connection, msg, action):
    """Run `action(hub)` and answer with its result, or with the error the card or administrator should see."""
    entry = _loaded_entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "not_configured", "The Area Events integration is not set up or is not connected to the Discovery Service.")
        return
    try:
        result = await action(entry.runtime_data.hub)
    except UnknownArea as error:
        connection.send_error(msg["id"], "unknown_area", str(error))
    except ControlNotConfigured:
        connection.send_error(msg["id"], "control_not_configured", "No control token is configured, so shared Discovery settings cannot be changed from Home Assistant.")
    except DiscoveryConflictError as error:
        connection.send_error(msg["id"], "revision_conflict", f"{error} (current revision {error.current_revision}); read it again and retry.")
    except DiscoveryAuthError as error:
        if error.status == 403:
            connection.send_error(msg["id"], "forbidden", "The Discovery Service does not allow this token to make changes.")
        else:
            entry.async_start_reauth(hass)
            connection.send_error(msg["id"], "unauthorized", "The Discovery Service rejected the integration's token; reauthenticate it in Settings.")
    except DiscoveryRequestError as error:
        connection.send_error(msg["id"], "not_found" if error.status == 404 else "rejected", error.message)
    except DiscoveryConnectionError as error:
        connection.send_error(msg["id"], "unavailable", str(error))
    else:
        connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): WS_AREAS})
@websocket_api.async_response
async def ws_areas(hass, connection, msg):
    """The discovery areas a card can show, with the shared service's state."""
    async def action(hub):
        areas = await hub.areas(reload=True)
        return {"areas": areas, "service": await hub.status()}
    await _serve(hass, connection, msg, action)


@websocket_api.websocket_command({vol.Required("type"): WS_STATUS})
@websocket_api.async_response
async def ws_status(hass, connection, msg):
    async def action(hub):
        return await hub.status()
    await _serve(hass, connection, msg, action)


@websocket_api.websocket_command({
    vol.Required("type"): WS_EVENTS,
    vol.Required("area"): AREA,
    vol.Optional("from"): vol.All(int, vol.Range(min=0)),
    vol.Optional("to"): vol.All(int, vol.Range(min=0)),
    vol.Optional("after"): vol.All(str, vol.Length(max=40)),
    vol.Optional("limit"): vol.All(int, vol.Range(min=1, max=200)),
})
@websocket_api.async_response
async def ws_events(hass, connection, msg):
    """One page of an area's events straight from the authoritative store (no cache, no crawl)."""
    async def action(hub):
        return await hub.page(msg["area"], msg.get("from"), msg.get("to"), msg.get("after"), msg.get("limit", 200))
    await _serve(hass, connection, msg, action)


@websocket_api.websocket_command({vol.Required("type"): WS_EVENT, vol.Required("event_key"): KEY})
@websocket_api.async_response
async def ws_event(hass, connection, msg):
    """One event with its revision history and the evidence behind each field."""
    async def action(hub):
        return await hub.event(msg["event_key"])
    await _serve(hass, connection, msg, action)


@websocket_api.websocket_command({vol.Required("type"): WS_SUBSCRIBE, vol.Required("area"): AREA})
@websocket_api.async_response
async def ws_subscribe(hass, connection, msg):
    """Follow one area for one card: the current view now, and a new one whenever it changes. Ends with the card's connection."""
    entry = _loaded_entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "not_configured", "The Area Events integration is not set up or is not connected to the Discovery Service.")
        return
    photos = hass.data.get(PHOTOS_KEY)

    ready, early = False, []

    def emit(view):
        connection.send_message(websocket_api.event_message(msg["id"], {**view, "photos": photos}))

    def send(view):
        if ready:
            emit(view)
        else:
            early.append(view)   # the first view waits until the subscription has been confirmed to the card

    async def action(hub):
        unsubscribe = await hub.subscribe(msg["area"], send)
        connection.subscriptions[msg["id"]] = unsubscribe
        return None

    await _serve(hass, connection, msg, action)
    ready = True
    for view in early:
        emit(view)


def _optional(msg, *names):
    return {name: msg[name] for name in names if name in msg}


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): WS_AREA_CREATE,
    vol.Required("name"): vol.All(str, vol.Length(min=1, max=80)),
    vol.Required("latitude"): vol.All(vol.Coerce(float), vol.Range(min=-90, max=90)),
    vol.Required("longitude"): vol.All(vol.Coerce(float), vol.Range(min=-180, max=180)),
    vol.Required("radius"): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=400)),
    vol.Optional("unit"): vol.In(("mi", "km")),
    vol.Optional("intensity"): INTENSITY,
    vol.Optional("enabled"): bool,
    vol.Optional("sources"): vol.All([vol.All(str, vol.Length(min=1, max=80))], vol.Length(max=20)),
    vol.Optional("request_id"): vol.All(str, vol.Length(min=1, max=100)),
})
@websocket_api.async_response
async def ws_area_create(hass, connection, msg):
    async def action(hub):
        return await hub.create_area(**_optional(msg, "name", "latitude", "longitude", "radius", "unit", "intensity", "enabled", "sources", "request_id"))
    await _serve(hass, connection, msg, action)


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): WS_AREA_UPDATE,
    vol.Required("area_key"): KEY,
    vol.Required("expected_revision"): REVISION,
    vol.Optional("name"): vol.All(str, vol.Length(min=1, max=80)),
    vol.Optional("latitude"): vol.All(vol.Coerce(float), vol.Range(min=-90, max=90)),
    vol.Optional("longitude"): vol.All(vol.Coerce(float), vol.Range(min=-180, max=180)),
    vol.Optional("radius"): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=400)),
    vol.Optional("unit"): vol.In(("mi", "km")),
    vol.Optional("intensity"): INTENSITY,
    vol.Optional("enabled"): bool,
    vol.Optional("sources"): vol.All([vol.All(str, vol.Length(min=1, max=80))], vol.Length(max=20)),
})
@websocket_api.async_response
async def ws_area_update(hass, connection, msg):
    """Change a shared area (including switching it on or off). The expected revision makes a stale edit fail instead of overwriting."""
    async def action(hub):
        return await hub.update_area(msg["area_key"], msg["expected_revision"], **_optional(msg, "name", "latitude", "longitude", "radius", "unit", "intensity", "enabled", "sources"))
    await _serve(hass, connection, msg, action)


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): WS_SETTINGS,
    vol.Required("expected_revision"): REVISION,
    vol.Optional("mode"): vol.In(("automatic", "manual")),
    vol.Optional("intensity"): INTENSITY,
})
@websocket_api.async_response
async def ws_settings(hass, connection, msg):
    """Discovery mode and intensity. A success means the service saved the setting, not that any crawl has finished."""
    async def action(hub):
        return await hub.set_settings(msg["expected_revision"], msg.get("mode"), msg.get("intensity"))
    await _serve(hass, connection, msg, action)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_PAUSE})
@websocket_api.async_response
async def ws_pause(hass, connection, msg):
    async def action(hub):
        return await hub.pause()
    await _serve(hass, connection, msg, action)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_RESUME})
@websocket_api.async_response
async def ws_resume(hass, connection, msg):
    async def action(hub):
        return await hub.resume()
    await _serve(hass, connection, msg, action)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_MANAGEMENT})
@websocket_api.async_response
async def ws_management(hass, connection, msg):
    """One admin-only snapshot, with no tokens sent to the browser."""
    await _serve(hass, connection, msg, lambda hub: hub.management_snapshot())


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): WS_SOURCE_COMMAND,
    vol.Required("action"): vol.In(("create", "update", "toggle")),
    vol.Optional("source_id"): vol.All(str, vol.Length(min=1, max=80)),
    vol.Optional("producer_type"): vol.All(str, vol.Length(min=1, max=80)),
    vol.Optional("name"): vol.All(str, vol.Length(min=1, max=80)),
    vol.Optional("enabled"): bool,
    vol.Optional("interval_minutes"): vol.All(int, vol.Range(min=1, max=525600)),
    vol.Optional("options"): dict,
    vol.Optional("expected_revision"): REVISION,
})
@websocket_api.async_response
async def ws_source_command(hass, connection, msg):
    """Validated, revisioned mutations through the Cubi control service and persistence."""
    action = msg["action"]
    expected = {
        "create": ("source_id", "producer_type", "name", "enabled", "interval_minutes", "options"),
        "update": ("source_id", "expected_revision"),
        "toggle": ("source_id", "expected_revision", "enabled"),
    }[action]
    if any(name not in msg for name in expected):
        connection.send_error(msg["id"], "invalid_request", "Required producer fields are missing.")
        return
    if action == "update" and not any(name in msg for name in ("name", "interval_minutes", "options")):
        connection.send_error(msg["id"], "invalid_request", "Choose at least one setting to change.")
        return

    async def change(hub):
        if action == "create":
            return await hub.create_source(msg["source_id"], msg["producer_type"], msg["name"], msg["enabled"],
                                           msg["interval_minutes"], msg["options"])
        if action == "update":
            changes = _optional(msg, "name", "interval_minutes", "options")
            return await hub.update_source(msg["source_id"], msg["expected_revision"], **changes)
        return await hub.set_source_enabled(msg["source_id"], msg["expected_revision"], msg["enabled"])

    await _serve(hass, connection, msg, change)
