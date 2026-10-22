"""Client for the Area Events app's HTTP API (see area_events/discovery/server.py). No Home Assistant imports."""
import asyncio
from urllib.parse import quote, urlsplit

try:  # Home Assistant always has aiohttp; the tests do not need it
    import aiohttp

    _NETWORK_ERRORS = (OSError, asyncio.TimeoutError, aiohttp.ClientError)
except ImportError:  # pragma: no cover
    _NETWORK_ERRORS = (OSError, asyncio.TimeoutError)

TIMEOUT_SECONDS = 20


class AreaEventsError(Exception):
    """Base class."""


class AreaEventsConnectionError(AreaEventsError):
    """The app could not be reached."""


class AreaEventsAuthError(AreaEventsError):
    """The app rejected the token."""


class AreaEventsRequestError(AreaEventsError):
    """The app understood the request and refused it (invalid profile, too many areas, unknown profile)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def normalize_base_url(value):
    """'http://host:8765/' -> 'http://host:8765'; raises ValueError for anything that is not a plain http(s) address."""
    parts = urlsplit(str(value).strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.query or parts.fragment:
        raise ValueError("enter the app address as http://hostname:8765")
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


class AreaEventsClient:
    def __init__(self, session, base_url, token):
        self._session, self._base, self._token = session, normalize_base_url(base_url), token

    async def _request(self, method, path, body=None):
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                async with self._session.request(method, self._base + path, json=body,
                                                 headers={"Authorization": f"Bearer {self._token}"}) as response:
                    status = response.status
                    try:
                        payload = await response.json(content_type=None)
                    except ValueError:
                        payload = None
        except _NETWORK_ERRORS as error:
            raise AreaEventsConnectionError(f"could not reach the Area Events app at {self._base}: {error or type(error).__name__}") from error
        if status in (401, 403):
            raise AreaEventsAuthError("the Area Events app rejected the token")
        if status >= 400 or not isinstance(payload, dict):
            message = payload.get("error") if isinstance(payload, dict) else None
            if status >= 500 or not message:
                raise AreaEventsConnectionError(f"the Area Events app answered {status}")
            raise AreaEventsRequestError(status, str(message))
        return payload

    async def get_media(self, name):
        """(bytes, content type) of a stored map or venue photograph. Raises AreaEventsRequestError(404) when there is none."""
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                async with self._session.get(f"{self._base}/api/v1/media/{quote(name, safe='')}", headers={"Authorization": f"Bearer {self._token}"}) as response:
                    status, kind, data = response.status, response.headers.get("Content-Type", ""), await response.read()
        except _NETWORK_ERRORS as error:
            raise AreaEventsConnectionError(f"could not reach the Area Events app at {self._base}: {error or type(error).__name__}") from error
        if status in (401, 403):
            raise AreaEventsAuthError("the Area Events app rejected the token")
        if status == 404:
            raise AreaEventsRequestError(404, "no such image")
        if status >= 400:
            raise AreaEventsConnectionError(f"the Area Events app answered {status}")
        return data, kind

    async def status(self):
        """Engine counters; also the connection and token check."""
        return await self._request("GET", "/api/v1/status")

    async def register(self, profile):
        """Idempotently register a profile; returns its snapshot without events (including `id`)."""
        return await self._request("POST", "/api/v1/profiles", profile)

    async def get(self, profile_id, since=None):
        suffix = f"?since={quote(since)}" if since else ""
        return await self._request("GET", f"/api/v1/profiles/{quote(profile_id, safe='')}{suffix}")
