"""The management console's Sources and Areas layout in a REAL browser (headless Edge or Chrome, when installed): one shared split, half and half on a wide screen,
stacked on a narrow one, with nothing wider than the page. Every browser run has a hard timeout."""
import functools
import http.server
import json
import os
import re
import subprocess
import tempfile
import threading
import unittest

from tests.test_grid_layout import browser_flags, find_browser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(ROOT, "custom_components", "area_events", "frontend")
PAGE = """<!doctype html><meta charset="utf-8"><style>body{margin:0}</style><body><pre id="out"></pre>
<script type="module">
import "/manager.js";
const out = document.getElementById("out");
const results = [];
const snapshot = {
  schema: "area_events.management.v1", can_control: true, status: { settings: { revision: 3 }, sources: [{ source_key: "serpapi_sd", eligible: true }] },
  sources: [{ id: "serpapi_sd", type: "serpapi_google_events", name: "SerpApi San Diego with a rather long display name for wrapping", enabled: true,
              interval_minutes: 720, revision: 2, options: { query: "events in San Diego", location: "San Diego, California, United States", timezone: "America/Los_Angeles" } }],
  areas: [{ area_key: "abcdefghi1", name: "San Diego", enabled: true, sources: ["serpapi_sd"], revision: 3, latitude: 32.7, longitude: -117.1, radius: 25, unit: "mi" }],
  producer_types: { schema: "area_events.producer_forms.v1", types: [{ id: "serpapi_google_events", name: "SerpApi Google Events", fields: [
    { name: "query", label: "Event search query", kind: "text", required: true }, { name: "location", label: "Search location", kind: "text", required: true },
    { name: "timezone", label: "Event time zone", kind: "text", required: true }, { name: "max_pages", label: "Result pages per scan", kind: "integer", default: 1, min: 1, max: 3 }] }] },
  usage: { schema: "discovery.usage.v1", serpapi: { configured: true, stale: false, error: null, state: "active", reason: null, resumes: null,
    account: { plan_name: "Free Plan", monthly_price: 0, searches_per_month: 250, used: 124, remaining: 126, renewal: "2026-11-20", checked_at: 1793462400 },
    policy: { auto_pause: true, pause_at: 200, free_tier_reserve: 5 }, sources: { serpapi_sd: { state: "active", reason: null } } } },
};
const width = window.innerWidth;
{
  for (const tab of ["sources", "areas"]) {
    const frame = document.createElement("div");
    const manager = document.createElement("area-events-manager");
    frame.append(manager);
    document.body.append(frame);
    manager._snapshot = snapshot; manager._selectedSource = "serpapi_sd"; manager._selectedArea = "abcdefghi1"; manager._tab = tab;
    manager.render();
    const root = manager.shadowRoot;
    const split = root.querySelector(".split");
    const [list, detail] = [root.querySelector(".split > .list"), root.querySelector(".split > .detail")];
    const box = (el) => el.getBoundingClientRect();
    const inputs = [...root.querySelectorAll(".detail input:not([type=hidden]), .detail select, .detail button")].map((el) => box(el));
    results.push({ width, tab, list: Math.round(box(list).width), detail: Math.round(box(detail).width), sameRow: Math.round(box(list).top) === Math.round(box(detail).top),
                   overflow: manager.scrollWidth - frame.clientWidth, splitOverflow: split.scrollWidth - split.clientWidth,
                   detailOverflow: detail.scrollWidth - detail.clientWidth, controlsInside: inputs.every((b) => b.right <= box(detail).right + 1 && b.width > 0) });
    frame.remove();
  }
}
out.textContent = JSON.stringify(results);
</script>"""


@unittest.skipUnless(find_browser(), "no headless Edge or Chrome on this machine")
class ManagerLayoutInABrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {}
        handler = functools.partial(Quiet, directory=FRONTEND)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.page = PAGE.encode()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for width in (1400, 1000, 760, 360):   # one browser window per width: the console's media queries follow the window
                with tempfile.TemporaryDirectory() as folder:
                    done = subprocess.run([find_browser(), *browser_flags(), "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={os.path.join(folder, 'profile')}",
                                           f"--window-size={width},1000", "--virtual-time-budget=5000", "--dump-dom", f"http://127.0.0.1:{server.server_address[1]}/layout.html"],
                                          capture_output=True, text=True, timeout=90, encoding="utf-8")
                match = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
                if not match or not match.group(1).startswith("["):
                    raise AssertionError("the console produced no result in the browser: " + (match.group(1)[:300] if match else done.stderr[-300:]))
                for r in json.loads(match.group(1).replace("&quot;", '"').replace("&amp;", "&")):
                    cls.results[(width, r["tab"])] = r
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_wide_screens_split_sources_and_areas_half_and_half(self):
        for width in (1400, 1000):
            for tab in ("sources", "areas"):
                result = self.results[(width, tab)]
                self.assertTrue(result["sameRow"], result)
                self.assertLessEqual(abs(result["list"] - result["detail"]), 2, f"{width}px {tab}: {result}")

    def test_narrow_screens_stack_and_nothing_overflows(self):
        for (width, tab), result in self.results.items():
            with self.subTest(width=width, tab=tab):
                self.assertLessEqual(result["overflow"], 0, result)
                self.assertLessEqual(result["splitOverflow"], 0, result)
                self.assertLessEqual(result["detailOverflow"], 0, result)
                self.assertTrue(result["controlsInside"], result)
                if width <= 760:
                    self.assertFalse(result["sameRow"], f"stacked at {width}px")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/layout.html":
            body = self.server.page
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/manager.js":
            self.send_error(404)
            return
        super().do_GET()

    def guess_type(self, path):
        return "text/javascript" if str(path).endswith(".js") else super().guess_type(path)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    unittest.main()
