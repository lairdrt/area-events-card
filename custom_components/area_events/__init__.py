"""Area Events: local events for dashboard cards, read from the Discovery Service."""
from __future__ import annotations

import logging
import ssl
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from . import websocket_api
from .const import CARD_URL, CONF_CA_FILE, CONF_CONTROL_TOKEN, CONF_READ_TOKEN, CONF_SCAN_INTERVAL, CONF_URL, DEFAULT_SCAN_INTERVAL, DOMAIN, PHOTOS_KEY
from .coordinator import AreaEventsConfigEntry, DiscoveryCoordinator, RuntimeData
from .discovery_client import DiscoveryAuthError, DiscoveryConnectionError, DiscoveryControlClient
from .hub import DiscoveryHub
from .photos import PHOTOS_URL, load_index

CARD_FILE = Path(__file__).parent / "frontend" / "area-events-card.js"
PHOTOS_FOLDER = Path(__file__).parent / "frontend" / "photos"  # optional, private, not in Git (see photos.py)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_LOGGER = logging.getLogger(__name__)


def load_trust(path: str) -> ssl.SSLContext:
    """The system's certificate authorities plus the one file given. Reads a file: call it off the event loop."""
    return ssl.create_default_context(cafile=path)


async def async_migrate_entry(hass: HomeAssistant, entry: AreaEventsConfigEntry) -> bool:
    """Entries made for the earlier Area Events app hold that app's address and one token: they cannot become Discovery Service entries."""
    if entry.version < 2:
        _LOGGER.error("This Area Events entry was made for the earlier Area Events app and cannot be converted. Remove it in Settings > Devices & services and add "
                      "Area Events again with the Discovery Service's address and tokens.")
        return False
    return True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the card and register its WebSocket commands once; every config entry shares them."""
    websocket_api.async_register(hass)
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(CARD_FILE), cache_headers=False)])
    # The card loads on every dashboard without a Lovelace resource; the query changes with the file so browsers refetch it.
    version = await hass.async_add_executor_job(lambda: CARD_FILE.stat().st_mtime_ns)
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")
    # Category photographs exist only on installations that were given them; without them the card keeps its illustrations.
    index = await hass.async_add_executor_job(load_index, PHOTOS_FOLDER)
    hass.data[PHOTOS_KEY] = index
    if index:
        await hass.http.async_register_static_paths([StaticPathConfig(PHOTOS_URL, str(PHOTOS_FOLDER), cache_headers=True)])
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AreaEventsConfigEntry) -> bool:
    session = async_get_clientsession(hass)   # Home Assistant's own session: asynchronous, with its own connection handling
    trust = None
    if entry.data.get(CONF_CA_FILE):
        try:
            trust = await hass.async_add_executor_job(load_trust, entry.data[CONF_CA_FILE])
        except (OSError, ssl.SSLError) as error:
            raise ConfigEntryNotReady(f"cannot read the certificate file {entry.data[CONF_CA_FILE]}: {error}") from error
    read = DiscoveryControlClient(session, entry.data[CONF_URL], entry.data[CONF_READ_TOKEN], trust)
    control = DiscoveryControlClient(session, entry.data[CONF_URL], entry.data[CONF_CONTROL_TOKEN], trust) if entry.data.get(CONF_CONTROL_TOKEN) else None
    hub = DiscoveryHub(read, control)
    try:
        await hub.status()
    except DiscoveryAuthError as error:
        raise ConfigEntryAuthFailed(str(error)) from error
    except DiscoveryConnectionError as error:
        raise ConfigEntryNotReady(str(error)) from error
    coordinator = DiscoveryCoordinator(hass, entry, hub, int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = RuntimeData(hub, coordinator)
    # A coordinator only polls on its own while something listens to it. Nothing here is an entity, so listen once.
    entry.async_on_unload(coordinator.async_add_listener(lambda: None))
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass: HomeAssistant, entry: AreaEventsConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AreaEventsConfigEntry) -> bool:
    """Tell every card the integration is going away and drop the subscriptions. Discovery itself is untouched."""
    await entry.runtime_data.hub.aclose()
    return True
