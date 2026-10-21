"""Area Events: local events for dashboard cards, discovered by the Area Events app."""
from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from . import websocket_api
from .client import AreaEventsAuthError, AreaEventsClient, AreaEventsConnectionError
from .const import CARD_URL, CONF_TOKEN, CONF_URL, DOMAIN
from .coordinator import AreaEventsConfigEntry, AreaEventsCoordinator
from .hub import Hub

CARD_FILE = Path(__file__).parent / "frontend" / "area-events-card.js"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the card and register its WebSocket command once; every config entry shares them."""
    websocket_api.async_register(hass)
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(CARD_FILE), cache_headers=False)])
    # The card loads on every dashboard without a Lovelace resource; the query changes with the file so browsers refetch it.
    version = await hass.async_add_executor_job(lambda: CARD_FILE.stat().st_mtime_ns)
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AreaEventsConfigEntry) -> bool:
    client = AreaEventsClient(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN])
    try:
        await client.status()
    except AreaEventsAuthError as error:
        raise ConfigEntryAuthFailed(str(error)) from error
    except AreaEventsConnectionError as error:
        raise ConfigEntryNotReady(str(error)) from error
    coordinator = AreaEventsCoordinator(hass, entry, Hub(client))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AreaEventsConfigEntry) -> bool:
    return True
