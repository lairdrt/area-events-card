"""The private category-photograph preview: subject choice in the engine, the integration's validated index and static serving,
the preparation script, and the guarantee that no photograph can enter Git or the build output."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from discovery import classify, photo_subject

REPO = Path(__file__).resolve().parent.parent
INTEGRATION = REPO / "custom_components" / "area_events"
sys.path.insert(0, str(REPO))


def food(title, description="", categories=()):
    return {"title": title, "description": description, "categories": list(categories), "venue": ""}


class Subjects(unittest.TestCase):
    def annotated(self, *events):
        classify.annotate(list(events))
        photo_subject.annotate(list(events))
        return events

    def test_food_events_get_the_subject_their_listing_states(self):
        farmers, truck, dinner, plain = self.annotated(food("Hillcrest Farmers Market"), food("Taco Tuesday Food Truck Rally"), food("Wine Tasting Dinner"), food("Restaurant Week Brunch"))
        self.assertEqual([e["illustration"]["category"] for e in (farmers, truck, dinner, plain)], ["farmers_market", "food", "food", "food"])
        self.assertEqual(farmers["illustration"]["subject"], "farmers-market")
        self.assertEqual(truck["illustration"]["subject"], "food-truck")
        self.assertEqual(dinner["illustration"]["subject"], "restaurant")
        self.assertEqual(plain["illustration"]["subject"], "restaurant")

    def test_a_market_that_also_has_food_trucks_is_a_market(self):
        self.assertEqual(photo_subject.subject_of(food("Farmers Market and Food Trucks")), "farmers-market")

    def test_nothing_is_guessed_and_other_categories_are_untouched(self):
        music, = self.annotated(food("Live Jazz Night", "Dinner and drinks are available and we are near the farmers market."))
        self.assertNotEqual(music["illustration"]["category"], "food")
        self.assertNotIn("subject", music["illustration"])
        self.assertIsNone(photo_subject.subject_of(food("Cooking Class")))
        event = {"illustration": {"category": "food", "strength": "weak", "reasons": [], "fallback": False}, "title": "Cooking Class"}
        photo_subject.annotate([event])
        self.assertNotIn("subject", event["illustration"])
        self.assertEqual(event["illustration"]["category"], "food")  # the classifier's decision is never changed

    def test_a_passing_mention_late_in_a_description_does_not_decide(self):
        self.assertIsNone(photo_subject.subject_of(food("Cooking Class", "x" * 300 + " farmers market")))
        self.assertEqual(photo_subject.subject_of(food("Cooking Class", "Visit the farmers market first.")), "farmers-market")

    def test_events_without_a_decision_are_skipped(self):
        events = [{"title": "Farmers Market"}, {"title": "Farmers Market", "illustration": None}]
        photo_subject.annotate(events)
        self.assertNotIn("illustration", events[0])


class Pipeline(unittest.TestCase):
    def test_the_feed_carries_the_subject_of_food_events(self):
        from datetime import datetime, timedelta, timezone
        from discovery import refresh, registry
        from discovery.core import make_event, finish_event
        start = datetime.now(timezone.utc) + timedelta(days=2)
        event = finish_event(make_event("web_t", "T", "https://t.example", "https://t.example/e", "Hillcrest Farmers Market", start, local_id="1"), "2030-01-01T00:00:00+00:00", ["x"], False)
        config = {"sources": [{"id": "web_t", "name": "T", "adapter": "web", "url": "https://t.example", "coverage": {"type": "global"}}]}
        selected = registry.load_sources(config)
        document = refresh.build_document(config, {"web_t": {"events": [event], "ok": True, "last_success_at": datetime.now(timezone.utc).isoformat()}}, datetime.now(timezone.utc), selected)
        decision = document["events"][0]["illustration"]
        self.assertEqual((decision["category"], decision["subject"]), ("farmers_market", "farmers-market"))


class Index(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def load(self, index):
        for item in (index.get("sets") or {}).values() if isinstance(index, dict) else []:
            for entry in item:
                if isinstance(entry, dict) and isinstance(entry.get("file"), str) and entry["file"].endswith(".webp") and "/" not in entry["file"] and entry.get("make", True):
                    (self.folder / entry["file"]).write_bytes(b"RIFF")
        (self.folder / "index.json").write_text(json.dumps(index), encoding="utf-8")
        return photos.load_index(self.folder)

    def test_a_valid_index_is_passed_on_without_private_details(self):
        index = self.load({"version": 1, "sets": {"music": [{"file": "music-1.webp", "alt": " A band ", "v": "abcd1234", "pexels": 167605}]}})
        self.assertEqual(index, {"base": "/area_events/photos/", "sets": {"music": [{"file": "music-1.webp", "alt": "A band", "v": "abcd1234"}]}})
        self.assertNotIn("pexels", json.dumps(index))

    def test_no_folder_or_no_index_means_no_photographs(self):
        self.assertIsNone(photos.load_index(self.folder / "missing"))
        self.assertIsNone(photos.load_index(self.folder))
        (self.folder / "index.json").write_text("not json", encoding="utf-8")
        self.assertIsNone(photos.load_index(self.folder))
        (self.folder / "index.json").write_text("[]", encoding="utf-8")
        self.assertIsNone(photos.load_index(self.folder))

    def test_only_known_sets_and_plain_existing_webp_files_survive(self):
        index = self.load({"sets": {
            "music": [{"file": "../secret.webp", "alt": "x", "v": "abcd1234", "make": False}, {"file": "a.jpg", "alt": "x", "v": "abcd1234"},
                      {"file": "gone.webp", "alt": "x", "v": "abcd1234", "make": False}, {"file": "ok-1.webp", "alt": "ok", "v": "abcd1234"},
                      {"file": "bad-version.webp", "alt": "x", "v": "xyz"}, {"file": "no-alt.webp", "v": "abcd1234"}, "junk"],
            "unknown-set": [{"file": "u.webp", "alt": "x", "v": "abcd1234"}],
        }})
        self.assertEqual(list(index["sets"]), ["music"])
        self.assertEqual([e["file"] for e in index["sets"]["music"]], ["ok-1.webp"])
        self.assertIsNone(self.load({"sets": {"unknown-set": [{"file": "u.webp", "alt": "x", "v": "abcd1234"}]}}))

    def test_sets_are_bounded(self):
        entries = [{"file": f"music-{n}.webp", "alt": "x", "v": "abcd1234"} for n in range(40)]
        self.assertEqual(len(self.load({"sets": {"music": entries}})["sets"]["music"]), photos.MAX_PER_SET)


class Serving(unittest.TestCase):
    """Home Assistant serves the folder only when it holds a valid index, from a path of its own, with caching."""

    def setup(self, folder):
        paths = []

        async def register(configs):
            paths.append(configs)

        hass = types.SimpleNamespace(http=types.SimpleNamespace(async_register_static_paths=register, register_view=lambda view: None), data={},
                                     async_add_executor_job=lambda func, *args: asyncio.sleep(0, func(*args)))
        with mock.patch.object(init, "PHOTOS_FOLDER", folder):
            self.assertTrue(asyncio.run(init.async_setup(hass, {})))
        return hass, paths

    def test_photographs_are_served_locally_and_cacheably_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "music-1.webp").write_bytes(b"RIFF")
            (folder / "index.json").write_text(json.dumps({"sets": {"music": [{"file": "music-1.webp", "alt": "A band", "v": "abcd1234"}]}}), encoding="utf-8")
            hass, paths = self.setup(folder)
        self.assertEqual(len(paths), 2)
        (card_path, _), card_options = paths[0][0]
        (photo_path, served_folder), photo_options = paths[1][0]
        self.assertEqual(card_path, "/area_events/area-events-card.js")
        self.assertEqual((photo_path, served_folder, photo_options), ("/area_events/photos", str(folder), {"cache_headers": True}))
        self.assertEqual(hass.data["area_events_photos"]["base"], "/area_events/photos/")
        self.assertTrue(all(not url.startswith("http") for url in [photo_path, hass.data["area_events_photos"]["base"]]), "no remote address")

    def test_without_photographs_nothing_extra_is_served_and_the_card_is_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            hass, paths = self.setup(Path(tmp) / "absent")
        self.assertEqual(len(paths), 1)  # only the card
        self.assertIsNone(hass.data["area_events_photos"])


class Hygiene(unittest.TestCase):
    """Photograph binaries must never reach Git, the build output or a release by accident."""

    def test_the_photograph_folder_and_review_assets_are_git_ignored(self):
        ignore = (REPO / ".gitignore").read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
        self.assertIn("/custom_components/area_events/frontend/photos/", ignore)
        self.assertIn("/review/", ignore)
        try:
            for sample in ("custom_components/area_events/frontend/photos/music-1.webp", "custom_components/area_events/frontend/photos/index.json", "review/category-photos/images/music-1.jpg"):
                result = subprocess.run(["git", "check-ignore", "-q", sample], cwd=REPO, capture_output=True)
                self.assertEqual(result.returncode, 0, f"{sample} is not ignored")
            tracked = subprocess.run(["git", "ls-files", "custom_components/area_events/frontend/photos", "review"], cwd=REPO, capture_output=True, text=True).stdout
            self.assertEqual(tracked.strip(), "", "a photograph or review asset is tracked by Git")
        except FileNotFoundError:
            self.skipTest("git is not available")

    def test_the_build_copies_only_the_card_script(self):
        build = (REPO / "scripts" / "build.mjs").read_text(encoding="utf-8")
        self.assertNotIn("photos", build)
        self.assertNotIn("webp", build)
        dist = REPO / "dist"
        if dist.exists():
            self.assertEqual([p.name for p in dist.iterdir()], ["area-events-card.js"])
        card = (INTEGRATION / "frontend" / "area-events-card.js").read_text(encoding="utf-8")
        self.assertNotIn("data:image", card, "no photograph is embedded in the tracked script")
        self.assertLess(len(card), 400_000)

    def test_no_tracked_file_is_an_image_of_a_photograph(self):
        try:
            names = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split("\n")
        except (FileNotFoundError, subprocess.CalledProcessError):
            self.skipTest("git is not available")
        photographs = [n for n in names if n.lower().endswith((".webp", ".jpg", ".jpeg"))]
        self.assertEqual(photographs, [])


class Preparation(unittest.TestCase):
    def script(self, *args):
        return subprocess.run([sys.executable, str(REPO / "scripts" / "prepare-category-photos.py"), *args], capture_output=True, text=True)

    def load_table(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("prepare_category_photos", REPO / "scripts" / "prepare-category-photos.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.PHOTOS

    def test_the_approved_selection_uses_the_replacements_and_not_the_superseded_photographs(self):
        table = self.load_table()
        ids = [pexels for _, _, pexels, _ in table]
        self.assertEqual(len(table), 39)
        self.assertEqual(len(set(ids)), 39, "no photograph is used twice")
        for replacement in (20898292, 27036324, 27052301, 16782204, 39967166):
            self.assertIn(replacement, ids)
        for superseded in (13632046, 30693440, 30211544, 30391784, 19469682):  # the photographs the five replacements superseded
            self.assertNotIn(superseded, ids)
        by_set = {}
        for name, *_ in table:
            by_set[name] = by_set.get(name, 0) + 1
        self.assertEqual(by_set, {"music": 3, "theater": 3, "festival": 3, "food-restaurant": 3, "food-farmers-market": 3, "food-truck": 2, "sports": 3,
                                  "arts": 3, "science": 3, "family": 3, "outdoors": 3, "community": 4, "general": 3})
        self.assertEqual(set(by_set), set(photos.SETS), "the script and the integration agree on the sets")
        self.assertTrue(all(alt.strip() for *_, alt in table), "every photograph has a description")

    def test_a_missing_review_photograph_stops_the_script_and_nothing_is_invented_or_written(self):
        with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as out:
            result = self.script("--source", empty, "--out", out)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing review photographs", result.stderr)
            self.assertEqual(os.listdir(out), [])

    def test_check_mode_writes_nothing(self):
        if not (REPO / "review" / "category-photos" / "images" / "music-1.jpg").exists():
            self.skipTest("the private review photographs are not on this machine")
        with tempfile.TemporaryDirectory() as out:
            result = self.script("--check", "--out", out)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("39 photographs in 13 sets", result.stdout)
            self.assertEqual(os.listdir(out), [])


# the integration imports Home Assistant; the glue tests already load it against stand-ins
from tests import test_ha_glue as glue  # noqa: E402

init = glue.init
photos = glue.importlib.import_module("area_events_glue.photos")

if __name__ == "__main__":
    unittest.main()
