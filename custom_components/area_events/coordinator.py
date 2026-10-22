"""The one DataUpdateCoordinator: a bounded-interval check of the Discovery change feed, shared by every card."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .discovery_client import DiscoveryAuthError, DiscoveryConnectionError, DiscoveryRequestError
from .hub import DiscoveryHub

_LOGGER = logging.getLogger(__name__)


class DiscoveryCoordinator(DataUpdateCoordinator[dict]):
    """Calls `hub.refresh()` every `interval` seconds. The hub tells the cards; this class only schedules and maps failures to Home Assistant's."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, hub: DiscoveryHub, interval: int) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=timedelta(seconds=interval))
        self.hub = hub

    async def _async_update_data(self) -> dict:
        try:
            await self.hub.refresh()
        except DiscoveryAuthError as error:
            raise ConfigEntryAuthFailed(str(error)) from error
        except (DiscoveryConnectionError, DiscoveryRequestError) as error:
            raise UpdateFailed(str(error)) from error
        areas, cards = self.hub.followed
        return {"followed_areas": areas, "card_subscriptions": cards}


@dataclass
class RuntimeData:
    hub: DiscoveryHub
    coordinator: DiscoveryCoordinator


type AreaEventsConfigEntry = ConfigEntry[RuntimeData]
