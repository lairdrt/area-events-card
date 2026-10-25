"""The classifier as the first replaceable module: its contract, interchangeable implementations and unchanged production behaviour."""
import copy
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from area_events.classification import classifiers, classify
from area_events.enrichment import media
from area_events.discovery import refresh, registry
from area_events.discovery.core import finish_event, make_event
from tests.test_media import SD, FakeFetcher, nominatim

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConstantClassifier:
    """A minimal alternate implementation: every event is the same category. It exists to show that implementations are interchangeable."""

    def __init__(self, category="carnival"):
        self.name = f"constant-{category}"
        self.category = category

    def classify(self, event):
        return {"category": self.category, "strength": "weak", "reasons": ["a constant classifier"], "fallback": False, "scores": {self.category: 1.0}}


class BrokenClassifier:
    name = "broken"

    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def classify(self, event):
        if self.error:
            raise self.error
        return self.result


def sample_events():
    start = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0)
    out = []
    for i, (title, description) in enumerate([("Hillcrest Farmers Market", "Fresh produce."), ("Jazz Night", "Live trio."), ("Quarterly meeting", ""), ("Winter Carnival", "Rides and games.")]):
        event = make_event("web_t", "T", "https://t.example", f"https://t.example/{i}", title, start, local_id=str(i), venue="Hall", description=description or None)
        out.append(finish_event(event, start.isoformat(), ["fixture"], False))
    return out


class Contract(unittest.TestCase):
    implementations = (classifiers.KeywordClassifier(), ConstantClassifier("music"), ConstantClassifier("farmers_market"))

    def test_every_implementation_honours_the_contract_for_every_event(self):
        for implementation in self.implementations:
            for event in sample_events():
                before = copy.deepcopy(event)
                decision = implementation.classify(event)
                self.assertEqual(classifiers.check_decision(decision), [], implementation.name)
                self.assertEqual(event, before, "a classifier must not change the event")

    def test_a_decision_that_breaks_the_contract_is_described(self):
        good = {"category": "music", "strength": "weak", "reasons": [], "fallback": False}
        self.assertEqual(classifiers.check_decision(good), [])
        for bad in (None, "music", {}, {**good, "category": "nonsense"}, {**good, "strength": "huge"}, {**good, "reasons": "because"}, {**good, "reasons": [1]},
                    {**good, "fallback": "no"}, {**good, "scores": {"music": "high"}}, {**good, "scores": {"nonsense": 1}}, {**good, "scores": {"music": True}}):
            self.assertTrue(classifiers.check_decision(bad), bad)

    def test_all_seventeen_categories_are_accepted(self):
        for category in classify.CATEGORIES:
            self.assertEqual(classifiers.check_decision({"category": category, "strength": "none", "reasons": [], "fallback": True}), [])
        self.assertEqual(len(classify.CATEGORIES), 17)


class Interchange(unittest.TestCase):
    def test_a_different_implementation_is_installed_without_changing_its_consumers(self):
        events = sample_events()
        classifiers.annotate(events)
        self.assertEqual([e["illustration"]["category"] for e in events], ["farmers_market", "music", "general", "carnival"])
        with classifiers.use(ConstantClassifier("halloween")):
            self.assertEqual(classifiers.current().name, "constant-halloween")
            classifiers.annotate(events)
            self.assertEqual({e["illustration"]["category"] for e in events}, {"halloween"})
        self.assertEqual(classifiers.current().name, "keyword-lexicon", "the production classifier is back")

    def test_the_feed_builder_uses_whatever_classifier_is_installed(self):
        config = {"sources": [{"id": "web_t", "name": "T", "adapter": "web", "url": "https://t.example", "coverage": {"type": "global"}}]}
        selected = registry.load_sources(config)
        now = datetime.now(timezone.utc)
        cache = {"web_t": {"events": sample_events(), "ok": True, "last_success_at": now.isoformat()}}
        with classifiers.use(ConstantClassifier("christmas")):
            document = refresh.build_document(config, cache, now, selected)
        self.assertEqual({e["illustration"]["category"] for e in document["events"]}, {"christmas"})
        document = refresh.build_document(config, {"web_t": {"events": sample_events(), "ok": True, "last_success_at": now.isoformat()}}, now, selected)
        self.assertIn("carnival", {e["illustration"]["category"] for e in document["events"]})

    def test_a_failing_or_invalid_classifier_never_costs_the_feed_an_event(self):
        for broken, reason in ((BrokenClassifier(error=RuntimeError("boom")), "the classifier failed: RuntimeError"), (BrokenClassifier(result={"category": "x"}), "invalid decision"),
                               (BrokenClassifier(result=None), "invalid decision")):
            events = classifiers.annotate(sample_events(), broken)
            self.assertEqual({e["illustration"]["category"] for e in events}, {"general"})
            self.assertTrue(all(reason in e["illustration"]["reasons"][0] for e in events), reason)
            self.assertTrue(all(e["illustration"]["fallback"] for e in events))

    def test_only_objects_with_a_classify_method_can_be_installed(self):
        with self.assertRaises(TypeError):
            classifiers.set_classifier(object())
        self.assertEqual(classifiers.current().name, "keyword-lexicon")


class ProductionUnchanged(unittest.TestCase):
    def test_the_contract_wrapper_gives_exactly_what_classify_annotate_gave(self):
        with open(os.path.join(ROOT, "tests", "data", "classification_eval.json"), encoding="utf-8") as handle:
            rows = json.load(handle)["events"]
        events = [{"title": r["title"], "description": r["description"], "categories": r["tags"], "venue": r["venue"]} for r in rows] + sample_events()
        old, new = copy.deepcopy(events), copy.deepcopy(events)
        classify.annotate(old)
        classifiers.annotate(new)
        self.assertEqual(old, new)
        self.assertGreaterEqual(len(events), 30)

    def test_the_production_classifier_returns_classify_s_decision_untouched(self):
        for event in sample_events():
            self.assertEqual(classifiers.KeywordClassifier().classify(event), classify.classify(event))


class DiagnosticResolve(unittest.TestCase):
    """media.resolve(draw=False, photos=False) is for diagnostics; the default (production) path is unchanged."""

    def test_only_the_location_decision_is_made_when_drawing_and_photos_are_switched_off(self):
        with tempfile.TemporaryDirectory() as folder:
            hit = nominatim("Viejas Arena", 32.7738, -117.0745, house="5500", road="Canyon Crest Drive", category="leisure", kind="sports_centre", wikidata="Q1")
            fetcher = FakeFetcher({"Viejas Arena": [hit]})
            service = media.MediaService(folder, fetcher, clock=time.time, sleep=lambda s: None, nominatim_interval=0)
            candidate = media.candidate_for({"venue": "Viejas Arena", "address": "5500 Canyon Crest Dr., San Diego"}, SD)
            record = service.resolve(candidate, draw=False, photos=False)
            self.assertTrue(record["ok"])
            self.assertEqual(record["map"]["files"], {})
            self.assertEqual(record["photo_reason"], "not looked up")
            self.assertFalse([u for u in fetcher.urls if "openfreemap" in u or "wikipedia" in u or "wikidata" in u], "no tile or photo request was made")


if __name__ == "__main__":
    unittest.main()
