"""Serves the maps and venue photographs the Area Events app has stored, to the dashboard's <img> elements.

The files are public derivatives (OpenStreetMap-based maps, Wikimedia Commons thumbnails) named by a content hash, so the view
needs no login (an <img> cannot send one) and answers only for names of that exact shape; it forwards the request to the app with
the integration's token and adds long-lived caching. SVG maps are served with a policy that forbids scripts and any resource load.
"""
from __future__ import annotations

import re

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState

from .client import AreaEventsError, AreaEventsRequestError
from .const import DOMAIN

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{3,60}\.(svg|jpg)$")
TYPES = {"svg": "image/svg+xml", "jpg": "image/jpeg"}
HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class AreaEventsMediaView(HomeAssistantView):
    url = "/area_events/media/{name}"
    name = "area_events:media"
    requires_auth = False
    cors_allowed = False

    def __init__(self, hass) -> None:
        self.hass = hass

    async def get(self, request, name: str):
        if not NAME.match(name):
            return web.Response(status=404)
        entries = [e for e in self.hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
        if not entries:
            return web.Response(status=404)
        try:
            data, _ = await entries[0].runtime_data.hub.client.get_media(name)
        except AreaEventsRequestError:
            return web.Response(status=404)
        except AreaEventsError:
            return web.Response(status=502)
        return web.Response(body=data, content_type=TYPES[name.rsplit(".", 1)[1]], headers=HEADERS)
