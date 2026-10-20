#!/usr/bin/env python3
"""End-to-end smoke test of the multi-area service against real event sites (needs internet, takes minutes).

    python scripts/e2e_local.py                     # starts a throwaway local service and drives it
    python scripts/e2e_local.py --url http://192.168.1.88:8765 --key <key>   # checks a deployed service (read/write: creates two areas)

It plays the part of Home Assistant: authenticated API calls only. It checks that unauthenticated and malformed
requests are refused, that two areas (Austin by city name, San Diego by coordinates) are discovered independently,
that each area's feed holds only its own events, and that the default feed is still served.
"""
import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def call(base, method, path, key=None, body=None):
    request = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def check(label, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + label + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        raise SystemExit(1)


def wait_for(predicate, seconds, label):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(5)
    check(label, False, "timed out")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url")
    parser.add_argument("--key")
    args = parser.parse_args()
    process, folder = None, None
    if args.url:
        base, key = args.url.rstrip("/"), args.key
    else:
        key = secrets.token_urlsafe(32)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        folder = tempfile.mkdtemp(prefix="area-events-e2e-")
        for name in ("source-cache.json", "events.json", "location-cache.json"):  # a warm default area keeps the run short
            source = os.path.join(ROOT, "data", name)
            if os.path.exists(source):
                shutil.copy(source, folder)
        env = dict(os.environ, AREA_EVENTS_DATA_DIR=folder, AREA_EVENTS_PORT=str(port), AREA_EVENTS_HOST="127.0.0.1", AREA_EVENTS_API_KEY=key)
        process = subprocess.Popen([sys.executable, "-m", "discovery.service"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        base = f"http://127.0.0.1:{port}"
        wait_for(lambda: call(base, "GET", "/health")[0] in (200, 503), 30, "service starts")
    try:
        check("API refuses a request without a key", call(base, "GET", "/api/v1/areas")[0] == 401)
        check("API refuses a wrong key", call(base, "GET", "/api/v1/areas", "x" * 40)[0] == 401)
        check("API refuses a URL in an area request", call(base, "POST", "/api/v1/areas", key, {"url": "http://x.example", "latitude": 1, "longitude": 2})[0] == 422)
        check("API refuses path-like ids", call(base, "GET", "/api/v1/areas/..%2f..", key)[0] == 404)
        status, raw = call(base, "POST", "/api/v1/areas", key, {"query": "Austin, TX", "country": "US", "radius_miles": 25, "name": "Austin"})
        austin = json.loads(raw)
        check("Austin area created", status in (200, 201), raw[:200])
        status, raw = call(base, "POST", "/api/v1/areas", key, {"latitude": 32.7157, "longitude": -117.1611, "radius_miles": 25, "name": "San Diego"})
        san_diego = json.loads(raw)
        check("San Diego area created", status in (200, 201), raw[:200])
        check("different areas get different ids", austin["id"] != san_diego["id"])

        def feed(area):
            status, raw = call(base, "GET", f"/api/v1/areas/{area['id']}/events", key)
            return json.loads(raw) if status == 200 else None

        print("waiting for discovery (Austin ~1 minute, San Diego several minutes when cold)...")
        a = wait_for(lambda: feed(austin), 900, "Austin feed ready")
        s = wait_for(lambda: feed(san_diego), 1500, "San Diego feed ready")
        austin_sources = {e["source"]["name"] for e in a["events"]}
        san_diego_sources = {e["source"]["name"] for e in s["events"]}
        print(f"Austin: {len(a['events'])} events from {sorted(austin_sources)}; San Diego: {len(s['events'])} events from {sorted(san_diego_sources)}")
        check("Austin feed has events", len(a["events"]) > 0)
        check("San Diego feed has events", len(s["events"]) > 0)
        check("Austin events come only from Austin sources", austin_sources == {"UT Austin Events"}, str(austin_sources))
        check("San Diego events never include Austin sources", "UT Austin Events" not in san_diego_sources)
        check("feeds report their own area", a["location"]["display_name"] != s["location"]["display_name"], f"{a['location']['display_name']} / {s['location']['display_name']}")
        status, raw = call(base, "GET", f"/api/v1/areas/{austin['id']}", key)
        check("area status reports ok", json.loads(raw)["status"]["state"] == "ok", raw[:300])
        status, raw = call(base, "GET", "/events.json")
        check("the default public feed still works", status == 200 and len(json.loads(raw)["events"]) > 0)
        print("END-TO-END OK")
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            if folder:
                shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    main()
