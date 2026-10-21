"""The one DataUpdateCoordinator: keeps the hub's active profiles current, quickly while discovery runs."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import AreaEventsAuthError, AreaEventsConnectionError, AreaEventsRequestError
from .const import BUSY_INTERVAL_SECONDS, DOMAIN, IDLE_INTERVAL_SECONDS
from .hub import Hub

_LOGGER = logging.getLogger(__name__)

type AreaEventsConfigEntry = ConfigEntry[AreaEventsCoordinator]


class AreaEventsCoordinator(DataUpdateCoordinator[dict]):
    """Polls the Area Events app for every profile a card has asked for recently (one coordinator, not one per card)."""

    config_entry: AreaEventsConfigEntry

    def __init__(self, hass: HomeAssistant, entry: AreaEventsConfigEntry, hub: Hub) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=timedelta(seconds=IDLE_INTERVAL_SECONDS))
        self.hub = hub

    @callback
    def async_boost(self) -> None:
        """A profile is being discovered: poll quickly now instead of waiting for the relaxed interval."""
        if self.update_interval != timedelta(seconds=BUSY_INTERVAL_SECONDS):
            self.update_interval = timedelta(seconds=BUSY_INTERVAL_SECONDS)
            self.config_entry.async_create_background_task(self.hass, self.async_request_refresh(), f"{DOMAIN} boost")

    async def _async_update_data(self) -> dict:
        try:
            await self.hub.refresh()
        except AreaEventsAuthError as error:
            raise ConfigEntryAuthFailed(str(error)) from error
        except (AreaEventsConnectionError, AreaEventsRequestError) as error:
            raise UpdateFailed(str(error)) from error
        finally:
            self.update_interval = timedelta(seconds=BUSY_INTERVAL_SECONDS if self.hub.busy else IDLE_INTERVAL_SECONDS)
        return {"profiles": self.hub.profiles}
