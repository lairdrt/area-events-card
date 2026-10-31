"""Exercise renderer substitution and CSS isolation against built modules in a real browser."""

import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from tests.test_grid_layout import browser_flags, find_browser


ROOT = Path(__file__).resolve().parents[1]
BROWSER = find_browser()
REPLACEMENT = """import { element, PRESENTATION_CONTRACT } from "./contract.js";
export const communityRenderer = {
  id: "community", contract: PRESENTATION_CONTRACT, eventTypes: ["community.event"],
  styles: ".community-event .replacement-layout { display: grid; grid-template-columns: 1fr 2fr; color: rgb(17, 34, 51); }",
  render(event) {
    const tile = element("article", "event community-event");
    const layout = element("div", "replacement-layout");
    layout.append(element("strong", "replacement-heading", "ALTERNATE PRESENTATION"), element("span", "replacement-title", event.title));
    tile.append(layout);
    return tile;
  },
  cleanup() {},
};
"""

PAGE = """<!doctype html><meta charset="utf-8"><pre id="out"></pre>
<script>customElements.define("home-assistant", class extends HTMLElement {});</script>
<script type="module">
import { PRESENTATIONS } from "/presentation/manifest.js";
import "/area-events-card.js";
const { element, PRESENTATION_CONTRACT } = await import("/presentation/contract.js");
if (@@OVERLAP@@) PRESENTATIONS.register({
  id: "overlap-test", contract: PRESENTATION_CONTRACT, eventTypes: ["overlap.test"],
  styles: ".overlap-event .image { aspect-ratio: 1 / 1; } .overlap-event .description { font-weight: 700; }",
  render(event) {
    const tile = element("article", "event overlap-event");
    tile.append(element("div", "image", "SECOND IMAGE"), element("div", "description", event.description));
    return tile;
  }, cleanup() {},
});
const events = [{ id: "community-1", type: "community.event", title: "Community Fixture", description: "COMMUNITY DESCRIPTION" }];
if (@@OVERLAP@@) events.push({ id: "overlap-1", type: "overlap.test", title: "Overlap Fixture", description: "OVERLAP DESCRIPTION" });
const card = document.createElement("area-events-card");
document.body.append(card);
card.setConfig({ area: "Test", events, presentation: { event_types: @@OVERLAP@@ ? { "overlap.test": "overlap-test" } : {} } });
const root = card.shadowRoot;
const community = root.querySelector(".community-event");
const overlap = root.querySelector(".overlap-event");
const style = (node) => node && ({ aspectRatio: getComputedStyle(node).aspectRatio, position: getComputedStyle(node).position,
  display: getComputedStyle(node).display, fontWeight: getComputedStyle(node).fontWeight });
document.getElementById("out").textContent = JSON.stringify({
  community: { text: community.textContent, original: !!community.querySelector(".image"),
    replacement: !!community.querySelector(".replacement-layout"), layout: style(community.querySelector(".replacement-layout")),
    image: style(community.querySelector(".image")), description: style(community.querySelector(".description")) },
  overlap: overlap && { text: overlap.textContent, image: style(overlap.querySelector(".image")),
    description: style(overlap.querySelector(".description")) },
  selectors: [...root.querySelector("style").sheet.cssRules].flatMap((rule) => rule.selectorText ? [rule.selectorText] :
    rule.cssRules ? [...rule.cssRules].map((child) => child.selectorText).filter(Boolean) : []),
});
</script>"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@unittest.skipUnless(BROWSER, "no headless Edge or Chrome on this machine")
class PresentationModularityInBrowser(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "src" / "frontend" / "presentation").mkdir(parents=True)
        (self.root / "scripts").mkdir()
        (self.root / "custom_components" / "area_events").mkdir(parents=True)
        shutil.copy2(ROOT / "src" / "frontend" / "area-events-card.js", self.root / "src" / "frontend" / "area-events-card.js")
        for source in (ROOT / "src" / "frontend" / "presentation").glob("*.js"):
            shutil.copy2(source, self.root / "src" / "frontend" / "presentation" / source.name)
        (self.root / "src" / "frontend" / "filters").mkdir()
        for source in (ROOT / "src" / "frontend" / "filters").glob("*.js"):
            shutil.copy2(source, self.root / "src" / "frontend" / "filters" / source.name)
        shutil.copy2(ROOT / "scripts" / "build.mjs", self.root / "scripts" / "build.mjs")
        shutil.copy2(ROOT / "custom_components" / "area_events" / "manifest.json",
                     self.root / "custom_components" / "area_events" / "manifest.json")
        # The isolated build script uses ESM imports; the copied package marks this temporary tree as ESM.
        (self.root / "package.json").write_text('{"type":"module"}', encoding="utf-8")

    def build(self):
        result = subprocess.run(["node", "scripts/build.mjs"], cwd=self.root, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def browser_result(self, overlap=False):
        frontend = self.root / "custom_components" / "area_events" / "frontend"
        (frontend / "fixture.html").write_text(PAGE.replace("@@OVERLAP@@", "true" if overlap else "false"), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(frontend)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.browser_runs = getattr(self, "browser_runs", 0) + 1
            profile = self.root / f"profile-{self.browser_runs}"
            url = f"http://127.0.0.1:{server.server_address[1]}/fixture.html"
            result = subprocess.run([BROWSER, *browser_flags(), "--headless=new", "--disable-gpu", "--no-first-run",
                                     f"--user-data-dir={profile}", "--virtual-time-budget=10000", "--dump-dom", url],
                                    capture_output=True, text=True, timeout=90, check=False)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        match = re.search(r'<pre id="out">(.*?)</pre>', result.stdout, re.S)
        self.assertIsNotNone(match, result.stderr[-500:])
        self.assertTrue(match.group(1).startswith("{"), f"output: {match.group(1)[:500]} stderr: {result.stderr[-500:]} stdout: {result.stdout[-700:]}")
        return json.loads(html.unescape(match.group(1)))

    def test_replacing_only_community_module_changes_the_rendered_layout_and_restoring_recovers_it(self):
        original = (self.root / "src" / "frontend" / "presentation" / "community.js").read_bytes()
        other_sources = [self.root / "src" / "frontend" / "area-events-card.js", *(
            path for path in (self.root / "src" / "frontend" / "presentation").glob("*.js") if path.name != "community.js")]
        unchanged = {path: path.read_bytes() for path in other_sources}
        self.build()
        before = self.browser_result()
        self.assertTrue(before["community"]["original"])
        self.assertFalse(before["community"]["replacement"])

        (self.root / "src" / "frontend" / "presentation" / "community.js").write_text(REPLACEMENT, encoding="utf-8")
        self.build()
        replaced = self.browser_result()
        self.assertFalse(replaced["community"]["original"])
        self.assertTrue(replaced["community"]["replacement"])
        self.assertIn("ALTERNATE PRESENTATION", replaced["community"]["text"])
        self.assertEqual(replaced["community"]["layout"]["display"], "grid")
        self.assertEqual({path: path.read_bytes() for path in other_sources}, unchanged)

        (self.root / "src" / "frontend" / "presentation" / "community.js").write_bytes(original)
        self.build()
        restored = self.browser_result()
        self.assertTrue(restored["community"]["original"])
        self.assertFalse(restored["community"]["replacement"])
        self.assertIn("COMMUNITY DESCRIPTION", restored["community"]["text"])
        self.assertEqual({path: path.read_bytes() for path in other_sources}, unchanged)

    def test_two_renderers_with_overlapping_classes_keep_their_own_styles(self):
        self.build()
        result = self.browser_result(overlap=True)
        self.assertEqual(result["community"]["image"]["aspectRatio"], "16 / 9")
        self.assertEqual(result["community"]["image"]["position"], "relative")
        self.assertEqual(result["overlap"]["image"]["aspectRatio"], "1 / 1")
        self.assertEqual(result["overlap"]["image"]["position"], "static")
        self.assertNotEqual(result["community"]["description"]["display"], "block")
        self.assertEqual(result["overlap"]["description"]["display"], "block")
        self.assertEqual(result["overlap"]["description"]["fontWeight"], "700")
        for selector in result["selectors"]:
            if ".image" in selector or ".description" in selector:
                self.assertNotRegex(selector, r"(?:^|,)\s*\.(?:image|description)\b")


if __name__ == "__main__":
    unittest.main()
