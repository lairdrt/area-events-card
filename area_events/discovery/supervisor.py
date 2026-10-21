"""Hand the integration its connection details through Home Assistant's Supervisor discovery.

When the app runs under the Supervisor it tells Home Assistant where it is (its internal hostname and port) and the
token that protects its API. The Supervisor passes that message to Home Assistant Core, which offers the Area
Events integration to the user for confirmation: nothing to type or copy. The message travels only on the
Supervisor's internal network (the same way the MQTT broker app shares its credentials). The app must list the
service in its config.yaml (`discovery: [area_events]`) or the Supervisor refuses the message.

Outside the Supervisor (no SUPERVISOR_TOKEN) nothing is announced and the token is configured by hand.
"""
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("area-events")

SERVICE = "area_events"
SUPERVISOR_URL = "http://supervisor"
# The current API first, then the one it replaces; either may be the one a given Supervisor offers.
INFO_PATHS = ("/addons/self/info", "/v2/apps/self/info")
DISCOVERY_PATHS = ("/discovery", "/v2/discovery")


class AnnounceError(Exception):
    pass


def _call(base, path, supervisor_token, body=None):
    request = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None)
    request.add_header("Authorization", f"Bearer {supervisor_token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raise AnnounceError(f"{path} answered {error.code}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise AnnounceError(f"{path}: {error}") from error
    if isinstance(payload, dict) and payload.get("result") not in (None, "ok"):
        raise AnnounceError(f"{path}: {payload.get('message') or payload.get('result')}")
    return payload.get("data", payload) if isinstance(payload, dict) else {}


def _first(paths, action):
    last = None
    for path in paths:
        try:
            return action(path)
        except AnnounceError as error:
            last = error
    raise last


def announce(port, token, supervisor_token, base=SUPERVISOR_URL):
    """Tell Home Assistant (through the Supervisor) where this app is. Raises AnnounceError."""
    info = _first(INFO_PATHS, lambda path: _call(base, path, supervisor_token))
    hostname = info.get("hostname")
    if not hostname:
        raise AnnounceError("the Supervisor did not report this app's hostname")
    config = {"host": hostname, "port": port, "token": token}
    _first(DISCOVERY_PATHS, lambda path: _call(base, path, supervisor_token, {"service": SERVICE, "config": config}))
    return hostname


def announce_with_retries(port, token, supervisor_token, stop, base=SUPERVISOR_URL, attempts=8, delay=15):
    """Announce, retrying while the Supervisor is busy starting. Returns True once announced."""
    for attempt in range(1, attempts + 1):
        try:
            hostname = announce(port, token, supervisor_token, base)
            log.info("announced %s:%s to Home Assistant for the Area Events integration", hostname, port)
            return True
        except AnnounceError as error:
            log.warning("could not announce to Home Assistant (attempt %d of %d): %s", attempt, attempts, error)
            if stop.wait(delay * attempt) or attempt == attempts:
                break
    log.error("Home Assistant was not told about this app; add the Area Events integration manually with the app's hostname and token")
    return False
