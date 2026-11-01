"""A deployment reaches a browser that already has the card cached (headless Edge or Chrome, when installed; one persistent profile for both visits).

Home Assistant serves the card at /area_events/area-events-card.js?v=<file time> and its modules beside it with an ETag and Last-Modified but no
Cache-Control, so a browser may reuse a cached module without asking again (a hard reload refetches only the card). The test builds the frontend
twice, changing one renderer, serves each build the way Home Assistant does (query ignored, changed files with a new time, unchanged ones keep
theirs) and visits both in the same browser profile: the second visit must run the new renderer, and modules that did not change stay cached.
Every browser run has a hard timeout."""
import email.utils
import http.server
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from tests.test_grid_layout import browser_flags, find_browser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAIN_LABEL = "`Source: ${event.sourceName}`"
PAGE = """<!doctype html><meta charset="utf-8"><body><pre id="out"></pre>
<script>customElements.define("home-assistant", class extends HTMLElement {});</script>
<script type="module" src="/area_events/area-events-card.js?v=@@VERSION@@"></script>
<script type="module">
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
window.addEventListener("error", (e) => { document.getElementById("out").textContent = "ERROR " + e.message; });
await Promise.race([customElements.whenDefined("area-events-card"), wait(4000)]);
const view = { state: "ready", connected: true, message: null, refreshing: false, generated_at: "2030-10-22T12:00:00+00:00", location: { display_name: "Test", coverage: "sources" },
  sources: {}, events: [{ id: "e1", title: "Trunk or Treat", start: "2030-10-23T17:00:00-05:00", timezone: "America/Chicago", url: null,
  source: { name: "Google Crossville", key: "serp_crossville" }, verified: false, verification: { status: "unverified", evidence: [] }, categories: [] }] };
const card = document.createElement("area-events-card");
document.body.append(card);
card.setConfig({ area: "Test" });
card.hass = { locale: { language: "en-US" }, themes: { darkMode: false }, callWS: async () => ({ value: null }),
  connection: { subscribeMessage: async (callback) => { setTimeout(() => callback(view), 0); return () => {}; } } };
for (let i = 0; i < 40 && !card.shadowRoot.querySelector(".event"); i++) await wait(100);
const label = card.shadowRoot.querySelector(".event .source");
document.getElementById("out").textContent = JSON.stringify({ label: label ? label.textContent : null, build: (card.shadowRoot.querySelector(".footer") || {}).textContent || null });
</script>"""


def build(work):
    subprocess.run(["node", os.path.join(work, "scripts", "build.mjs")], cwd=work, check=True, capture_output=True, timeout=60)
    return os.path.join(work, "custom_components", "area_events", "frontend")


class Served(http.server.BaseHTTPRequestHandler):
    """Home Assistant's static files, as aiohttp serves them with cache_headers=False: the query is ignored; ETag and Last-Modified, no Cache-Control."""
    folder = None
    requests = []
    page = ""

    def do_GET(self):
        path, _, _ = self.path.partition("?")
        if path == "/page.html":
            body = self.page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if not path.startswith("/area_events/") or ".." in path:
            self.send_error(404)
            return
        file = os.path.join(self.folder, *path[len("/area_events/"):].split("/"))
        if not os.path.isfile(file):
            self.send_error(404)
            return
        Served.requests.append(path)
        stat = os.stat(file)
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        with open(file, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", email.utils.formatdate(stat.st_mtime, usegmt=True))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@unittest.skipUnless(find_browser() and shutil.which("node"), "no headless Edge or Chrome, or no Node, on this machine")
class ANewDeploymentReachesACachedBrowser(unittest.TestCase):
    def test_the_second_visit_runs_the_new_renderer_and_unchanged_modules_stay_cached(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = os.path.join(temporary, "repo")
            shutil.copytree(os.path.join(ROOT, "src", "frontend"), os.path.join(work, "src", "frontend"))
            shutil.copytree(os.path.join(ROOT, "custom_components", "area_events", "frontend"), os.path.join(work, "custom_components", "area_events", "frontend"),
                            ignore=shutil.ignore_patterns("photos", "media"))
            shutil.copy(os.path.join(ROOT, "custom_components", "area_events", "manifest.json"), os.path.join(work, "custom_components", "area_events"))
            os.makedirs(os.path.join(work, "scripts"))
            shutil.copy(os.path.join(ROOT, "scripts", "build.mjs"), os.path.join(work, "scripts"))

            deployed = os.path.join(temporary, "ha", "frontend")      # what Home Assistant serves; a deployment copies changed files only
            shutil.copytree(build(work), deployed)
            month_ago = time.time() - 30 * 86400
            for folder, _, files in os.walk(deployed):
                for name in files:
                    os.utime(os.path.join(folder, name), (month_ago, month_ago))

            community = os.path.join(work, "src", "frontend", "presentation", "community.js")
            with open(community, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn(PLAIN_LABEL, text)
            with open(community, "w", encoding="utf-8", newline="") as handle:
                handle.write(text.replace(PLAIN_LABEL, "`Source: ${event.sourceName} (rebuilt)`"))
            rebuilt = build(work)

            Served.folder = deployed
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Served)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            profile = os.path.join(temporary, "profile")

            def visit():
                card = os.path.join(deployed, "area-events-card.js")
                Served.page = PAGE.replace("@@VERSION@@", str(os.stat(card).st_mtime_ns))   # the integration's add_extra_js_url(f"{CARD_URL}?v={mtime}")
                Served.requests = []
                done = subprocess.run([find_browser(), *browser_flags(), "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}",
                                       "--virtual-time-budget=8000", "--dump-dom",
                                       f"http://127.0.0.1:{server.server_address[1]}/page.html"], capture_output=True, text=True, timeout=90, encoding="utf-8")
                match = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
                self.assertTrue(match and match.group(1).startswith("{"), (done.stdout[-400:], done.stderr[-400:]))
                return json.loads(match.group(1).replace("&quot;", '"').replace("&amp;", "&")), list(Served.requests)

            try:
                first, _ = visit()
                self.assertEqual(first["label"], "Source: Google Crossville")
                changed = []
                for folder, _, files in os.walk(rebuilt):
                    for name in files:
                        source = os.path.join(folder, name)
                        target = os.path.join(deployed, os.path.relpath(source, rebuilt))
                        with open(source, "rb") as a, open(target, "rb") as b:
                            if a.read() != b.read():
                                shutil.copy(source, target)          # a new time, as a deployment gives the files it replaces
                                changed.append(os.path.relpath(source, rebuilt).replace(os.sep, "/"))
                self.assertIn("presentation/community.js", changed)
                second, requested = visit()
            finally:
                server.shutdown()
            self.assertEqual(second["label"], "Source: Google Crossville (rebuilt)", "the new renderer runs without clearing any cache")
            self.assertNotEqual(first["build"], second["build"])
            self.assertIn("/area_events/presentation/community.js", requested, "the changed renderer is fetched")
            for unchanged in ("presentation/contract.js", "presentation/generic.js", "presentation/nasa-mission.js", "filters/date-windows.js"):
                self.assertNotIn(unchanged, changed)
                self.assertNotIn("/area_events/" + unchanged, requested, f"{unchanged} did not change and is used from the browser's cache")


if __name__ == "__main__":
    unittest.main()
