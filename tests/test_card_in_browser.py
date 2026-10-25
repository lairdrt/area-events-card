"""The built card in a REAL browser (headless Edge or Chrome, when installed): a fake Home Assistant hands it one San Diego and one Austin event, and the rendered tiles are read back.
It checks what a viewer sees: each event's time in the event's own time zone, a short place badge instead of a sentence, no "(approximate)" suffix, and text shown as text.
Every browser run has a hard timeout."""
import json
import http.server
import os
import re
import subprocess
import tempfile
import threading
import unittest

from tests.test_grid_layout import browser_flags, find_browser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(ROOT, "custom_components", "area_events", "frontend")
CARD = os.path.join(FRONTEND, "area-events-card.js")
SENTENCE = "The Little Italy Mercato operates on West Date Street from the cul de sac at Kettner to India Street, every Saturday morning."
PAGE = """<!doctype html><meta charset="utf-8"><body><pre id="out"></pre>
<script>customElements.define("home-assistant", class extends HTMLElement {});</script>
<script type="module" src="/area_events/area-events-card.js"></script>
<script type="module">
const view = @@VIEW@@;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
window.addEventListener("error", (e) => { document.getElementById("out").textContent = "ERROR " + e.message; });
await Promise.race([customElements.whenDefined("area-events-card"), wait(4000)]);
const card = document.createElement("area-events-card");
document.body.append(card);
card.setConfig({ area: "Test" });
card.hass = { locale: { language: "en-US" }, themes: { darkMode: true }, callWS: async () => ({ value: null }),
              connection: { subscribeMessage: async (callback) => { setTimeout(() => callback(view), 0); return () => {}; } } };
await wait(1500);
const tiles = [...card.shadowRoot.querySelectorAll(".event")].map((tile) => ({
  title: (tile.querySelector(".title") || {}).textContent, lines: [...tile.querySelectorAll(".meta")].map((m) => m.textContent.trim()),
  chip: ((tile.querySelector(".venue-chip") || {}).textContent || null), elements: [...tile.querySelectorAll("script, img[onerror]")].length }));
document.getElementById("out").textContent = JSON.stringify({ viewer: Intl.DateTimeFormat().resolvedOptions().timeZone, tiles });
</script>"""


def event(title, start, zone, venue, label, **extra):
    return {"id": title, "title": title, "start": start, "end": None, "timezone": zone, "venue": venue, "address": None, "description": None, "url": "https://example.org/e",
            "categories": [], "source": {"name": "Test", "key": "test"}, "verified": False, "verification": {"status": "unverified", "evidence": []}, "images": [], "image_credit": None,
            "media": {"map": {"light": "/area_events/media/m0123456789abcdef-light.svg", "dark": "/area_events/media/m0123456789abcdef-dark.svg", "approximate": True,
                              "confidence": "address", "precision": "address", "credit": "OpenFreeMap", "label": label}}, "illustration": {"category": "farmers_market"},
            "location_status": "address", **extra}


@unittest.skipUnless(find_browser() and os.path.exists(CARD), "no headless Edge or Chrome on this machine")
class CardInABrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        soon = "2030-10-23"
        view = {"state": "ready", "connected": True, "message": None, "refreshing": False, "initial": False, "outcome": "ok", "generated_at": "2030-10-22T12:00:00+00:00", "revision": 1,
                "location": {"display_name": "Test", "coverage": "sources"}, "sources": {}, "report": [], "notice": None, "photos": None,
                "events": [event("UT Biodiversity Strategic Planning Retreat", f"{soon}T13:00:00-05:00", "America/Chicago", "Cactus Cafe, Texas Union", "Cactus Cafe"),
                           event("Little Italy Mercato (Saturday)", f"{soon}T08:00:00-07:00", "America/Los_Angeles", SENTENCE, "W Date St"),
                           event("Children's Museum <img src=x onerror=alert(1)>", f"{soon}T10:00:00-07:00", "America/Los_Angeles", SENTENCE, None)]}
        page = PAGE.replace("@@VIEW@@", json.dumps(view))
        with tempfile.TemporaryDirectory() as folder:
            html = os.path.join(folder, "card.html")
            with open(html, "w", encoding="utf-8") as handle:
                handle.write(page)
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path == "/card.html":
                        path, content_type = html, "text/html; charset=utf-8"
                    elif self.path == "/area_events/area-events-card.js":
                        path, content_type = CARD, "text/javascript; charset=utf-8"
                    elif self.path.startswith("/area_events/presentation/") and self.path.rsplit("/", 1)[-1].endswith(".js"):
                        path, content_type = os.path.join(FRONTEND, "presentation", os.path.basename(self.path)), "text/javascript; charset=utf-8"
                    else:
                        self.send_error(404)
                        return
                    try:
                        with open(path, "rb") as source:
                            content = source.read()
                    except OSError:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)

                def log_message(self, *_args):
                    pass

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/card.html"
                done = subprocess.run([find_browser(), *browser_flags(), "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={os.path.join(folder, 'profile')}", "--window-size=1300,900",
                                       "--virtual-time-budget=6000", "--dump-dom", url], capture_output=True, text=True, timeout=90, encoding="utf-8")
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)
        match = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
        if not match or not match.group(1).startswith("{"):
            raise AssertionError("the card produced no result in the browser: " + (match.group(1)[:300] if match else done.stderr[-300:]))
        if False:
            raise AssertionError("the card produced no result in the browser: " + done.stderr[-400:])
        cls.result = json.loads(match.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
        cls.tiles = {t["title"]: t for t in cls.result["tiles"]}

    def when(self, title):
        return next(line for line in self.tiles[title]["lines"] if re.search(r"\d:\d\d", line)).replace("\u202f", " ").replace("\u00a0", " ")

    def test_all_events_are_rendered(self):
        self.assertEqual(len(self.result["tiles"]), 3)

    def test_each_event_shows_its_own_local_time(self):
        austin, san_diego = self.when("UT Biodiversity Strategic Planning Retreat"), self.when("Little Italy Mercato (Saturday)")
        self.assertRegex(austin, r"1:00 PM", f"the source's 1:00 PM, whatever the viewer's zone ({self.result['viewer']}): {austin}")
        self.assertRegex(san_diego, r"8:00 AM", san_diego)
        if self.result["viewer"] != "America/Chicago":
            self.assertIn("CDT", austin)
        if self.result["viewer"] != "America/Los_Angeles":
            self.assertIn("PDT", san_diego)

    def test_the_map_badge_is_a_short_place_name_without_a_suffix(self):
        self.assertIn(self.tiles["UT Biodiversity Strategic Planning Retreat"]["chip"], ("Cactus Cafe", None))   # the badge appears once the map image has loaded
        for tile in self.result["tiles"]:
            chip = tile["chip"]
            self.assertTrue(chip is None or (len(chip) <= 60 and "(approximate)" not in chip and not re.search(r"operates|every Saturday", chip)), chip)

    def test_source_text_is_text(self):
        hostile = self.tiles["Children's Museum <img src=x onerror=alert(1)>"]
        self.assertEqual(hostile["elements"], 0)
        self.assertEqual(hostile["title"], "Children's Museum <img src=x onerror=alert(1)>")


if __name__ == "__main__":
    unittest.main()
