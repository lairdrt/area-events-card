"""Async client for the Discovery Service's control API (see area_events/discovery/service/api.py). No Home Assistant imports.

Same conventions as `client.py`: the aiohttp session is injected (Home Assistant's shared session in production), every request has a
timeout, nothing blocks the event loop, and failures become typed exceptions the integration can turn into availability and
config-entry errors. The token is only ever sent in the Authorization header and is never part of an error message.

Not wired into the integration yet (Milestone 2B): the Discovery Service has no config entry or WebSocket command of its own; the
remaining Home Assistant wiring is described in docs/discovery-service-architecture.md.
"""
import asyncio
from urllib.parse import quote, urlsplit

try:
    import aiohttp

    _NETWORK_ERRORS = (OSError, asyncio.TimeoutError, aiohttp.ClientError)
except ImportError:  # pragma: no cover
    _NETWORK_ERRORS = (OSError, asyncio.TimeoutError)

TIMEOUT_SECONDS = 20


class DiscoveryError(Exception):
    """Base class."""


class DiscoveryConnectionError(DiscoveryError):
    """The Discovery Service could not be reached."""


class DiscoveryAuthError(DiscoveryError):
    """The service rejected the token (401) or the token may not make this change (403)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class DiscoveryConflictError(DiscoveryError):
    """The configuration changed since it was read (revision conflict); read it again and retry."""

    def __init__(self, message, current_revision):
        super().__init__(message)
        self.current_revision = current_revision


class DiscoveryRequestError(DiscoveryError):
    """The service understood the request and refused it (invalid, unknown, too many attempts)."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def normalize_base_url(value):
    parts = urlsplit(str(value).strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.query or parts.fragment:
        raise ValueError("enter the Discovery Service address as http://hostname:8780")
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


class DiscoveryControlClient:
    def __init__(self, session, base_url, token):
        self._session, self._base, self._token = session, normalize_base_url(base_url), token

    async def _request(self, method, path, body=None):
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                async with self._session.request(method, self._base + path, json=body, headers={"Authorization": f"Bearer {self._token}"}) as response:
                    status = response.status
                    try:
                        payload = await response.json(content_type=None)
                    except ValueError:
                        payload = None
        except _NETWORK_ERRORS as error:
            raise DiscoveryConnectionError(f"could not reach the Discovery Service at {self._base}: {error or type(error).__name__}") from error
        if status < 400:
            if not isinstance(payload, dict):
                raise DiscoveryConnectionError(f"the Discovery Service answered {status} without JSON")
            return payload
        error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
        message = str(error.get("message") or f"the Discovery Service answered {status}")[:200]
        if status in (401, 403):
            raise DiscoveryAuthError(status, message)
        if status == 409:
            raise DiscoveryConflictError(message, error.get("current_revision"))
        if status >= 500:
            raise DiscoveryConnectionError(f"the Discovery Service answered {status}")
        raise DiscoveryRequestError(status, str(error.get("code") or "error"), message)

    async def status(self):
        return (await self._request("GET", "/v1/discovery/status"))["status"]

    async def areas(self):
        return (await self._request("GET", "/v1/areas"))["areas"]

    async def area(self, area_key):
        return (await self._request("GET", f"/v1/areas/{quote(area_key, safe='')}"))["area"]

    async def create_area(self, name, latitude, longitude, radius, unit="mi", intensity="normal", sources=(), enabled=True, request_id=None):
        body = {"name": name, "latitude": latitude, "longitude": longitude, "radius": radius, "unit": unit, "intensity": intensity,
                "enabled": enabled, "sources": list(sources)}
        if request_id is not None:
            body["request_id"] = request_id
        return await self._request("POST", "/v1/areas", body)

    async def update_area(self, area_key, expected_revision, **changes):
        return await self._request("PATCH", f"/v1/areas/{quote(area_key, safe='')}", {"expected_revision": expected_revision, **changes})

    async def set_settings(self, expected_revision, mode=None, intensity=None):
        body = {"expected_revision": expected_revision}
        if mode is not None:
            body["mode"] = mode
        if intensity is not None:
            body["intensity"] = intensity
        return await self._request("PUT", "/v1/discovery/settings", body)

    async def pause(self):
        return await self._request("POST", "/v1/discovery/pause", {})

    async def resume(self):
        return await self._request("POST", "/v1/discovery/resume", {})

    async def changes(self, after=0, limit=200):
        return await self._request("GET", f"/v1/changes?after={int(after)}&limit={int(limit)}")
