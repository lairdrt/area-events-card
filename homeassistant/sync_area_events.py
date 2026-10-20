#!/usr/bin/env python3
"""Copy the Area Events feed from the discovery service into Home Assistant's www folder.

Standard library only, so it runs with the python3 inside the Home Assistant
container (called from a shell_command). The feed is downloaded, validated,
written to a temporary file in the destination folder and renamed over the
live file. On any failure the previous feed is left untouched.

    python3 sync_area_events.py [URL] [DESTINATION_FILE]

Exit code 0: published or unchanged. 1: failed (previous feed kept).
"""
import hashlib
import json
import os
import sys
import urllib.request

DEFAULT_URL = "http://192.168.1.88:8765/events.json"
DEFAULT_DESTINATION = "/config/www/area-events-card/events.json"
TIMEOUT_SECONDS = 20
MAX_BYTES = 8 * 1024 * 1024
REQUIRED_EVENT_FIELDS = ("id", "title", "start", "url")


class SyncError(Exception):
    pass


def validate(document):
    """Raise SyncError unless `document` is a usable, non-empty feed."""
    if not isinstance(document, dict) or not isinstance(document.get("events"), list):
        raise SyncError("feed has no 'events' list")
    if not document["events"]:
        raise SyncError("feed contains no events")
    if not document.get("generated_at"):
        raise SyncError("feed has no generated_at")
    for event in document["events"]:
        for field in REQUIRED_EVENT_FIELDS:
            if not isinstance(event, dict) or not event.get(field):
                raise SyncError(f"event missing '{field}'")


def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "area-events-ha-sync/1", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise SyncError(f"HTTP {response.status}")
            raw = response.read(MAX_BYTES + 1)
    except SyncError:
        raise
    except Exception as error:  # URLError, HTTPError, timeouts, connection resets
        raise SyncError(f"download failed: {error}") from error
    if len(raw) > MAX_BYTES:
        raise SyncError("feed is larger than the size limit")
    return raw


def publish(raw, destination):
    """Atomically replace `destination` with `raw`. Returns True if it changed."""
    directory = os.path.dirname(destination)
    if not os.path.isdir(directory):
        raise SyncError(f"destination folder does not exist: {directory}")
    try:
        with open(destination, "rb") as handle:
            if hashlib.sha256(handle.read()).digest() == hashlib.sha256(raw).digest():
                return False
    except OSError:
        pass  # no existing feed yet
    temporary = os.path.join(directory, f".{os.path.basename(destination)}.{os.getpid()}.tmp")
    try:
        with open(temporary, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        with open(temporary, "rb") as handle:
            validate(json.loads(handle.read()))  # re-read what actually hit the disk
        os.replace(temporary, destination)
    except (OSError, ValueError, SyncError) as error:
        raise SyncError(f"could not publish: {error}") from error
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return True


def sync(url, destination):
    raw = download(url)
    try:
        document = json.loads(raw)
    except ValueError as error:
        raise SyncError(f"feed is not valid JSON: {error}") from error
    validate(document)
    changed = publish(raw, destination)
    state = "published" if changed else "unchanged"
    return f"{state}: {len(document['events'])} events, generated_at {document['generated_at']}"


def main(argv):
    url = argv[1] if len(argv) > 1 else DEFAULT_URL
    destination = argv[2] if len(argv) > 2 else DEFAULT_DESTINATION
    try:
        print(sync(url, destination))
    except SyncError as error:
        print(f"area-events sync failed, previous feed kept: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
