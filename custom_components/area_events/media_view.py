"""Serves the pictures the Discovery Service has cached (event photographs, venue photographs, maps) to the dashboard's <img> elements.

The browser cannot send a login with an <img> request, and the service's bearer token must never reach it. So the browser asks THIS view, by
a content-derived name; the view asks the service with the integration's own read token and relays the bytes. The names are an unguessable
hash or a hash of the map position, only public pictures are cached, and the view answers only for names of that exact shape, never for a
path. Pictures are immutable by name, so the browser keeps them for a year and a card refresh downloads nothing.
"""
from __future__ import annotations

import re
import types

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState

from .const import DOMAIN
from .discovery_client import DiscoveryAuthError, DiscoveryConnectionError, DiscoveryRequestError

NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{3,60}\.(svg|jpg|png|webp)$")
TYPES = types.MappingProxyType({"svg": "image/svg+xml", "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"})
HEADERS = types.MappingProxyType({"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})
SVG_POLICY = "default-src 'none'; style-src 'unsafe-inline'; sandbox"   # a map is only a picture: no scripts, no loads


class AreaEventsMediaView(HomeAssistantView):
    url = "/area_events/media/{name}"
    name = "api:area_events:media"
    requires_auth = False   # an <img> cannot authenticate; see the module text

    def __init__(self, hass) -> None:
        self._hass = hass

    async def get(self, request: web.Request, name: str) -> web.Response:
        if not NAME.match(name):
            return web.Response(status=404)
        entry = next((e for e in self._hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED), None)
        if entry is None:
            return web.Response(status=503)
        try:
            data, _ = await entry.runtime_data.hub.media(name)
        except DiscoveryRequestError:
            return web.Response(status=404)
        except (DiscoveryConnectionError, DiscoveryAuthError):
            return web.Response(status=503, headers={"Retry-After": "30"})
        extension = name.rsplit(".", 1)[1]
        headers = dict(HEADERS)
        if extension == "svg":
            headers["Content-Security-Policy"] = SVG_POLICY
        return web.Response(body=data, content_type=TYPES[extension], headers=headers)   # the type comes from the name, never from the upstream header
