"""The card-facing WebSocket API. It rides on the signed-in user's Home Assistant session; the card holds no secret."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .client import AreaEventsAuthError, AreaEventsRequestError
from .const import DOMAIN, WS_EVENTS
from .profile import ProfileInputError, build_profile


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_events)


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_EVENTS,
        vol.Optional("area"): vol.All(str, vol.Length(max=100)),
        vol.Optional("radius_miles"): vol.All(vol.Coerce(float), vol.Range(min=1, max=250)),
        vol.Optional("sources"): vol.All([vol.All(str, vol.Length(max=300))], vol.Length(max=10)),
        vol.Optional("since"): str,
    }
)
@websocket_api.async_response
async def ws_events(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Return the events and status for the profile a card describes. Never waits for discovery to finish."""
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
    if not entries:
        connection.send_error(msg["id"], "not_configured", "The Area Events integration is not set up or is not connected to its app.")
        return
    home = {
        "latitude": hass.config.latitude,
        "longitude": hass.config.longitude,
        "name": hass.config.location_name,
        "time_zone": hass.config.time_zone,
    }
    try:
        profile = build_profile(msg, home, hass.config.country)
        result = await entries[0].runtime_data.hub.request(profile, msg.get("since"))
    except ProfileInputError as error:
        connection.send_error(msg["id"], "invalid_profile", str(error))
        return
    except AreaEventsRequestError as error:
        connection.send_error(msg["id"], "rejected", error.message)
        return
    except AreaEventsAuthError:
        entries[0].async_start_reauth(hass)
        connection.send_error(msg["id"], "unauthorized", "The Area Events app rejected the integration's token; reauthenticate it in Settings.")
        return
    connection.send_result(msg["id"], result)
