"""The event grid in a REAL browser (headless Edge or Chrome, when one is installed): the card's own stylesheet is measured for 1, 2, 3, 4, 5 and 20 events at desktop,
tablet and phone widths. A tile must have the same width and picture height whatever the number of events, the first tiles must fill the first columns, and the
unused columns must stay empty. (The first version of the grid used auto-fit, which stretched a lone tile across the whole row.) Every browser run has a hard timeout."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROWSERS = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe", "microsoft-edge", "google-chrome", "chromium", "chromium-browser")
WIDTHS, COUNTS = (1200, 900, 700, 500, 390), (1, 2, 3, 4, 5, 20)
PAGE = """<!doctype html><meta charset="utf-8"><style>html,body{margin:0;padding:0} CSS</style><body><pre id="out"></pre><script>
const widths = WIDTHS, counts = COUNTS, out = [];
for (const w of widths) {
  const host = document.createElement("div"); host.style.width = w + "px"; document.body.append(host);
  for (const n of counts) {
    const grid = document.createElement("div"); grid.className = "grid";
    for (let i = 0; i < n; i++) {
      const e = document.createElement("div"); e.className = "event";
      e.innerHTML = '<div class="image"><div class="art"><svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg></div></div><div class="body"><div class="title">Event ' + i + '</div><div class="meta">A location</div></div>';
      grid.append(e);
    }
    host.append(grid);
    const boxes = [...grid.children].map((e) => ({ r: e.getBoundingClientRect(), i: e.querySelector(".image").getBoundingClientRect() }));
    const lefts = [...new Set(boxes.map((b) => Math.round(b.r.left)))].sort((a, b) => a - b);
    const top = Math.round(boxes[0].r.top);
    out.push({ w, n, tile: [...new Set(boxes.map((b) => Math.round(b.r.width)))], image: [...new Set(boxes.map((b) => Math.round(b.i.height)))],
               firstRow: boxes.filter((b) => Math.round(b.r.top) === top).map((b) => lefts.indexOf(Math.round(b.r.left)) + 1) });
    grid.remove();
  }
}
document.getElementById("out").textContent = JSON.stringify(out);
</script>"""


def find_browser():
    return next((path for path in BROWSERS if os.path.isfile(path) or shutil.which(path)), None)


def browser_flags():
    return ["--no-sandbox"] if os.name != "nt" and os.environ.get("CI") == "true" else []


def measure(css, browser):
    page = PAGE.replace("CSS", css).replace("WIDTHS", json.dumps(WIDTHS)).replace("COUNTS", json.dumps(COUNTS))
    with tempfile.TemporaryDirectory() as folder:
        html = os.path.join(folder, "grid.html")
        with open(html, "w", encoding="utf-8") as handle:
            handle.write(page)
        done = subprocess.run([browser, *browser_flags(), "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={os.path.join(folder, 'profile')}", "--window-size=1400,1000",
                               "--virtual-time-budget=3000", "--dump-dom", "file:///" + html.replace("\\", "/")], capture_output=True, text=True, timeout=90, encoding="utf-8")
    match = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
    if not match:
        raise AssertionError("the browser produced no measurement: " + done.stderr[-300:])
    return json.loads(match.group(1).replace("&quot;", '"'))


@unittest.skipUnless(find_browser(), "no headless Edge or Chrome on this machine")
class GridInABrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "src", "area-events-card.js"), encoding="utf-8") as handle:
            css = re.search(r"const STYLES = `(.*?)`;", handle.read(), re.S).group(1)
        cls.rows = measure(css, find_browser())

    def test_a_tile_has_the_same_width_and_picture_height_whatever_the_number_of_events(self):
        for width in WIDTHS:
            rows = [r for r in self.rows if r["w"] == width]
            self.assertEqual(len(rows), len(COUNTS))
            self.assertTrue(all(len(r["tile"]) == 1 and len(r["image"]) == 1 for r in rows), f"{width}px: tiles of one size per result")
            self.assertEqual(len({r["tile"][0] for r in rows}), 1, f"{width}px: the tile width changes with the number of events: {[(r['n'], r['tile']) for r in rows]}")
            self.assertEqual(len({r["image"][0] for r in rows}), 1, f"{width}px: the picture height changes with the number of events")

    def test_few_events_fill_the_first_columns_and_leave_the_rest_empty(self):
        desktop = {r["n"]: r for r in self.rows if r["w"] == 1200}
        self.assertEqual(desktop[1]["firstRow"], [1])
        self.assertEqual(desktop[2]["firstRow"], [1, 2])
        self.assertEqual(desktop[3]["firstRow"], [1, 2, 3])
        self.assertEqual(desktop[4]["firstRow"], [1, 2, 3, 4], "four equal columns on a desktop")
        self.assertEqual(desktop[20]["firstRow"], [1, 2, 3, 4])
        self.assertAlmostEqual(desktop[1]["tile"][0] * 4 + 3 * 8, 1200, delta=4)

    def test_the_layout_still_follows_the_width_of_the_card(self):
        columns = {w: max(r["firstRow"]) for w in WIDTHS for r in self.rows if r["w"] == w and r["n"] == 20}
        self.assertEqual(columns, {1200: 4, 900: 3, 700: 3, 500: 2, 390: 1})
        phone = [r for r in self.rows if r["w"] == 390]
        self.assertTrue(all(r["firstRow"] == [1] and r["tile"] == [390] for r in phone), "on a phone every tile is the full width, one per row")


if __name__ == "__main__":
    unittest.main()
