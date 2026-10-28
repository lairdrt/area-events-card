"""image_extractor/v1: the contract, the Goose3 engine behind it, the shared association policy, and producer reuse. Offline and deterministic."""
import io
import json
import logging
import os
import random
import unittest
from unittest.mock import patch

from area_events.discovery.fetching import BudgetExceeded, RecordingFetcher
from area_events.discovery.producers.sources_producer import SourcesProducer
from area_events.discovery.safe import FetchError, Response
from area_events.image_extractor import goose3_adapter, responsive
from area_events.image_extractor.association import associate, wanted_for_page
from area_events.image_extractor.goose3_adapter import Goose3ImageExtractor
from area_events.image_extractor.v1 import CONTRACT, ImageRequest, ImageResult, no_image

try:
    from PIL import Image
except ImportError:   # Goose3 depends on Pillow; without it the engine tests are skipped
    Image = None

logging.getLogger("goose3").setLevel(logging.CRITICAL)   # undecodable test images are expected
GOOSE = unittest.skipUnless(Goose3ImageExtractor.available() and Image, "goose3 is not installed")
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "mtrp_marketplace.html")
MTRP = "https://mtrp.org/marketplace/"
UPLOADS = "https://mtrp.org/wp-content/uploads/2026/10/"
ORIGINAL = UPLOADS + "Nature-Center-Holiday-Craft-Market.png"
RESIZED = UPLOADS + "Nature-Center-Holiday-Craft-Market-1024x341.png"
PAGE = "https://example.org/events/autumn-fair"
ARTICLE = ("<p>The Autumn Fair is the best day of the year in the village, and all of the families who live in the area are invited to "
           "come along and enjoy the food, the music and the games that we have planned for them on the green.</p>"
           "<p>There will be something for everyone at the fair, and you can find out more about it on the day.</p>")
DOC = {"@context": "https://schema.org", "@type": "Event", "name": "Autumn Fair", "startDate": "2026-11-05T11:30:00-08:00",
       "location": {"@type": "Place", "name": "Community Hall", "address": "1 Main St, San Diego, CA"}}


def photo(width=640, height=214, seed=1):
    """A real PNG of noise: large enough for an engine to measure, small enough to be quick."""
    image = Image.frombytes("RGB", (width, height), random.Random(seed).randbytes(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


class ImageClient:
    """The caller's safe client: pages as text, images as bytes, anything else is a refusal."""

    def __init__(self, pages, images=None):
        self.pages, self.images, self.requests = pages, images or {}, []

    def fetch(self, url, binary=None, **_):
        self.requests.append(url)
        if binary:
            if url not in self.images:
                raise FetchError(f"missing image {url}")
            if isinstance(self.images[url], Exception):
                raise self.images[url]
            return Response(url, 200, "image/png", "", self.images[url])
        return Response(url, 200, "text/html; charset=utf-8", self.pages[url])


def crawl(pages, images=None, engine=None, url=PAGE, **options):
    producer = SourcesProducer(image_extractor=engine)
    base, clean = producer.validate_options({"urls": [url], "max_depth": 0, **options})
    client = ImageClient(pages, images)
    fetcher = RecordingFetcher(client, 15, 60)
    result = producer.crawl(fetcher, (), {**clean, "url": base, "source_key": "site", "name": "Websites"})
    return result, client, fetcher


def jsonld_page(*documents, body=ARTICLE):
    scripts = "".join(f'<script type="application/ld+json">{json.dumps(doc)}</script>' for doc in documents)
    return f"<html><head><title>Events</title>{scripts}</head><body><article>{body}</article></body></html>"


class StubEngine:
    """Any object with `name` and `extract` is an engine: no Goose3 anywhere."""

    def __init__(self, image_url=None, alternatives=(), error=None, raises=None, name="stub"):
        self.name, self.calls = name, []
        self.image_url, self.alternatives, self.error, self.raises = image_url, alternatives, error, raises

    def extract(self, request):
        self.calls.append(request)
        if self.raises:
            raise self.raises
        return ImageResult(self.image_url, self.alternatives, engine=self.name, method="stub", error=self.error)


class Contract(unittest.TestCase):
    def test_contract_is_versioned_and_small(self):
        self.assertEqual(CONTRACT, "image_extractor/v1")
        self.assertEqual(no_image("x"), ImageResult(engine="x"))
        self.assertIsNone(no_image("x", "boom").image_url)
        self.assertEqual(no_image("x", "boom").error, "boom")

    def test_unavailable_engine_degrades_to_no_image(self):
        with patch.object(goose3_adapter, "Goose", None):
            self.assertFalse(Goose3ImageExtractor.available())
            result = Goose3ImageExtractor().extract(ImageRequest("<p>x</p>", PAGE))
        self.assertIsNone(result.image_url)
        self.assertIn("not installed", result.error)


class Association(unittest.TestCase):
    def test_single_event_and_recurrences_share_the_primary_image(self):
        result = ImageResult("https://example.org/uploads/fair.png", engine="stub")
        for events in ([("Autumn Fair", None)], [("Autumn Fair", None), ("autumn  FAIR", None)]):
            self.assertEqual(associate(result, events).image_url, "https://example.org/uploads/fair.png")

    def test_distinct_events_and_missing_images_stay_unset(self):
        result = ImageResult("https://example.org/uploads/fair.png", engine="stub")
        ambiguous = associate(result, [("Autumn Fair", None), ("Winter Concert", None)])
        self.assertIsNone(ambiguous.image_url)
        self.assertIn("ambiguous", ambiguous.reason)
        self.assertIn("no primary image", associate(no_image("stub"), [("Autumn Fair", None)]).reason)
        self.assertIn("failed", associate(no_image("stub", "boom"), [("Autumn Fair", None)]).reason)
        self.assertFalse(wanted_for_page([("Autumn Fair", None), ("Winter Concert", None)]))
        self.assertFalse(wanted_for_page([("Autumn Fair", "https://example.org/a.jpg")]))
        self.assertTrue(wanted_for_page([("Autumn Fair", None), ("Autumn Fair", "https://example.org/a.jpg")]))

    def test_site_graphics_are_not_event_images(self):
        for url in ("https://example.org/uploads/site-logo.png", "https://example.org/no-image.png", "https://example.org/x/pixel.png"):
            self.assertIsNone(associate(ImageResult(url, engine="stub"), [("Autumn Fair", None)]).image_url, url)

    def test_declared_responsive_variant_is_preferred_and_names_are_never_guessed(self):
        html = ('<img src="/u/fair.png" srcset="/u/fair.png 2172w, /u/fair-300x100.png 300w, /u/fair-1024x341.png 1024w, '
                '/u/fair-2048x683.png 2048w">')
        chosen = associate(ImageResult("https://example.org/u/fair.png", engine="stub"), [("Autumn Fair", None)], html, PAGE)
        self.assertEqual(chosen.image_url, "https://example.org/u/fair-1024x341.png")
        bare = associate(ImageResult("https://example.org/u/fair.png", engine="stub"), [("Autumn Fair", None)], '<img src="/u/fair.png">', PAGE)
        self.assertEqual(bare.image_url, "https://example.org/u/fair.png")   # no guessed -1024x341 file name
        wide = '<img src="/u/fair.png" srcset="/u/fair-2048.png 2048w, /u/fair-3000.png 3000w">'
        self.assertEqual(associate(ImageResult("https://example.org/u/fair.png", engine="stub"), [("Autumn Fair", None)], wide, PAGE).image_url,
                         "https://example.org/u/fair-2048.png")

    def test_picture_sources_and_invalid_variants(self):
        html = ('<picture><source type="image/avif" srcset="/u/a.avif 1024w"><source srcset="/u/fair-640.webp 640w, javascript:alert(1) 800w, '
                'data:image/png;base64,AAAA 900w"><img src="/u/fair.jpg"></picture>')
        self.assertEqual(responsive.prefer_variant("https://example.org/u/fair.jpg", responsive.variants_of(html, PAGE, "https://example.org/u/fair.jpg")),
                         "https://example.org/u/fair-640.webp")
        with self.assertRaises(ValueError):
            responsive.parse_srcset(", ".join(f"/{i}.png 1w" for i in range(2000)))


class ProducerReuse(unittest.TestCase):
    """A producer (here the generic website producer, and a synthetic second one) uses the engine through the contract only."""

    def test_single_event_page_takes_the_engines_image_with_provenance(self):
        engine = StubEngine("https://example.org/uploads/fair.png")
        result, _, _ = crawl({PAGE: jsonld_page(DOC)}, engine=engine)
        event = result.candidates[0]
        self.assertEqual(event.image_url, "https://example.org/uploads/fair.png")
        self.assertEqual(event.image_source, "source HTML event image")
        self.assertTrue(any("Page primary image (stub, stub)" in item for item in event.verification.evidence))
        self.assertEqual(engine.calls[0].page_url, PAGE)
        self.assertEqual(engine.calls[0].event_titles, ("Autumn Fair",))

    def test_engines_are_interchangeable_without_producer_changes(self):
        for engine, expected in ((StubEngine("https://example.org/a.png", name="one"), "https://example.org/a.png"),
                                 (StubEngine("https://example.org/b.png", name="two"), "https://example.org/b.png"),
                                 (StubEngine(), None)):
            result, _, _ = crawl({PAGE: jsonld_page(DOC)}, engine=engine)
            self.assertEqual(result.candidates[0].image_url, expected)

    def test_a_second_synthetic_producer_needs_only_the_contract(self):
        def other_producer(engine, html, page_url, titles):
            result = engine.extract(ImageRequest(html, page_url, tuple(titles)))
            return associate(result, [(title, None) for title in titles], html, page_url).image_url
        self.assertEqual(other_producer(StubEngine("https://example.org/p.png"), "<p>x</p>", PAGE, ["One Show"]), "https://example.org/p.png")
        self.assertIsNone(other_producer(StubEngine("https://example.org/p.png"), "<p>x</p>", PAGE, ["One Show", "Two Show"]))

    def test_multiple_distinct_events_never_ask_the_engine(self):
        engine = StubEngine("https://example.org/uploads/fair.png")
        result, _, _ = crawl({PAGE: jsonld_page(DOC, {**DOC, "name": "Winter Concert", "startDate": "2026-11-06T11:30:00-08:00"})}, engine=engine)
        self.assertEqual([event.image_url for event in result.candidates], [None, None])
        self.assertEqual(engine.calls, [])

    def test_jsonld_images_keep_precedence_and_the_engine_is_not_asked(self):
        engine = StubEngine("https://example.org/uploads/fair.png")
        result, _, _ = crawl({PAGE: jsonld_page({**DOC, "image": "/images/autumn-fair-jsonld.jpg"})}, engine=engine)
        self.assertEqual(result.candidates[0].image_url, "https://example.org/images/autumn-fair-jsonld.jpg")
        self.assertEqual(result.candidates[0].image_source, "source JSON-LD image")
        self.assertEqual(engine.calls, [])

    def test_missing_image_failure_and_rejected_image_leave_events_intact(self):
        for engine in (StubEngine(), StubEngine(error="boom"), StubEngine(raises=RuntimeError("broken")),
                       StubEngine("https://example.org/site-logo.png")):
            result, _, _ = crawl({PAGE: jsonld_page(DOC)}, engine=engine)
            self.assertEqual(len(result.candidates), 1)
            self.assertIsNone(result.candidates[0].image_url)

    def test_no_engine_configured_is_fine(self):
        result, _, _ = crawl({PAGE: jsonld_page(DOC)})
        self.assertIsNone(result.candidates[0].image_url)

    def test_engine_gets_only_the_budgeted_unrecorded_image_fetch(self):
        class Probing(StubEngine):
            def extract(self, request):
                request.fetch_image("https://example.org/uploads/fair.png")
                return super().extract(request)
        engine = Probing("https://example.org/uploads/fair.png")
        _, client, fetcher = crawl({PAGE: jsonld_page(DOC)}, {"https://example.org/uploads/fair.png": photo()}, engine)
        self.assertEqual(client.requests, [PAGE, "https://example.org/uploads/fair.png"])
        self.assertEqual([item.url for item in fetcher.fetches], [PAGE])   # image bodies are not stored as pages
        self.assertEqual(fetcher.image_requests, 1)

    def test_image_budget_is_enforced(self):
        client = ImageClient({}, {"https://example.org/a.png": b"x"})
        fetcher = RecordingFetcher(client, 5, max_image_requests=1)
        fetcher.fetch_image("https://example.org/a.png")
        with self.assertRaises(BudgetExceeded):
            fetcher.fetch_image("https://example.org/a.png")


@GOOSE
class Goose3Engine(unittest.TestCase):
    engine = Goose3ImageExtractor()

    def request(self, html, images, page=PAGE):
        calls = []

        def fetch(url):
            calls.append(url)
            if url not in images:
                raise FetchError("missing")
            if isinstance(images[url], Exception):
                raise images[url]
            return images[url]
        return ImageRequest(html, page, fetch_image=fetch), calls

    def test_selects_the_content_image_and_returns_only_contract_types(self):
        html = f'<html><body><article><img src="/uploads/fair.png">{ARTICLE}</article></body></html>'
        request, _ = self.request(html, {"https://example.org/uploads/fair.png": photo()})
        result = self.engine.extract(request)
        self.assertIsInstance(result, ImageResult)
        self.assertEqual(result.image_url, "https://example.org/uploads/fair.png")
        self.assertEqual((result.width, result.height), (640, 214))
        self.assertTrue(result.engine.startswith("goose3@"))
        self.assertIsNotNone(result.method)

    def test_no_image_and_unmeasurable_images_give_the_explicit_no_image_result(self):
        for html, images in ((f"<html><body><article>{ARTICLE}</article></body></html>", {}),
                             (f'<html><body><article><img src="/uploads/fair.png">{ARTICLE}</article></body></html>',
                              {"https://example.org/uploads/fair.png": FetchError("the response is too large")}),
                             (f'<html><body><article><img src="/uploads/fair.png">{ARTICLE}</article></body></html>',
                              {"https://example.org/uploads/fair.png": b"not an image" * 500}),
                             (f'<html><body><article><img src="/uploads/fair.png">{ARTICLE}</article></body></html>',
                              {"https://example.org/uploads/fair.png": b"\0" * 2_100_000})):
            request, _ = self.request(html, images)
            self.assertIsNone(self.engine.extract(request).image_url)

    def test_engine_never_uses_its_own_network_client(self):
        html = f'<html><body><article><img src="https://cdn.example.net/uploads/fair.png">{ARTICLE}</article></body></html>'
        with patch("requests.Session.request", side_effect=AssertionError("network")), patch("requests.Session.get", side_effect=AssertionError("network")):
            unreachable = self.engine.extract(ImageRequest(html, PAGE))   # no fetch_image: no downloads at all
            request, calls = self.request(html, {"https://cdn.example.net/uploads/fair.png": photo()})
            reachable = self.engine.extract(request)
        self.assertIsNone(unreachable.image_url)
        self.assertEqual(reachable.image_url, "https://cdn.example.net/uploads/fair.png")
        self.assertEqual(calls, ["https://cdn.example.net/uploads/fair.png"])

    def test_downloads_per_page_are_capped(self):
        images = "".join(f'<img src="/uploads/p{i}.png">' for i in range(40))
        request, calls = self.request(f"<html><body><article>{images}{ARTICLE}</article></body></html>",
                                      {f"https://example.org/uploads/p{i}.png": photo(seed=i) for i in range(40)})
        self.engine.extract(request)
        self.assertLessEqual(len(calls), goose3_adapter.MAX_IMAGE_FETCHES)

    def test_engine_failure_is_a_result_not_an_exception(self):
        with patch.object(goose3_adapter, "Goose", side_effect=RuntimeError("broken")):
            result = self.engine.extract(ImageRequest(f"<html><body>{ARTICLE}</body></html>", PAGE))
        self.assertIsNone(result.image_url)
        self.assertIn("RuntimeError", result.error)


@GOOSE
class MissionTrails(unittest.TestCase):
    """The production page (retrieved 2026-10-27), replayed offline: both occurrences get the Holiday Marketplace photograph."""

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, encoding="utf-8") as handle:
            cls.html = handle.read()

    def replay(self, images):
        return crawl({MTRP: self.html}, images, Goose3ImageExtractor(), url=MTRP, timezone="America/Los_Angeles")

    def test_both_occurrences_get_the_event_photograph_in_its_declared_responsive_size(self):
        # The 2172 px original exceeds the safe fetcher's limit, so only the declared 1024 px variant can be measured.
        result, client, fetcher = self.replay({ORIGINAL: FetchError("the response is too large"), RESIZED: photo(1024, 341)})
        self.assertEqual(sorted(event.starts_at[:10] for event in result.candidates), ["2026-11-14", "2026-11-15"])
        self.assertEqual({event.image_url for event in result.candidates}, {RESIZED})
        self.assertTrue(all(event.image_source == "source HTML event image" for event in result.candidates))
        self.assertTrue(all(any("goose3@" in item and MTRP in item for item in event.verification.evidence) for event in result.candidates))
        self.assertEqual([item.url for item in fetcher.fetches], [MTRP])
        self.assertTrue(set(client.requests) - {MTRP} <= {ORIGINAL, RESIZED})

    def test_an_original_within_the_limit_is_measured_and_the_smaller_variant_is_still_preferred(self):
        result, _, _ = self.replay({ORIGINAL: photo(1024, 341), RESIZED: photo(1024, 341)})
        self.assertEqual({event.image_url for event in result.candidates}, {RESIZED})

    def test_unfetchable_images_leave_the_events_without_a_photograph(self):
        result, _, _ = self.replay({})
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual({event.image_url for event in result.candidates}, {None})


@GOOSE
class Deployment(unittest.TestCase):
    """What the deployment relies on: exact pins for the whole Goose3 tree, and a self-check that really exercises the engine."""

    @staticmethod
    def pins():
        import tomllib
        with open(os.path.join(os.path.dirname(FIXTURE), "..", "..", "pyproject.toml"), "rb") as handle:
            extra = tomllib.load(handle)["project"]["optional-dependencies"]["images"]
        return {item.split("==")[0].lower().replace("_", "-"): item.split("==")[1] for item in extra if "==" in item}, extra

    def test_every_dependency_of_goose3_is_pinned_exactly_and_installed_at_that_version(self):
        import importlib.metadata as metadata
        import re
        pins, extra = self.pins()
        self.assertEqual(len(pins), len(extra), "every entry is name==version")
        seen, queue = set(), ["goose3"]
        while queue:
            name = queue.pop()
            key = re.sub(r"[-_.]+", "-", name).lower()
            if key in seen:
                continue
            seen.add(key)
            for requirement in metadata.requires(name) or ():
                marker = requirement.split(";", 1)[1] if ";" in requirement else ""
                if "extra ==" in marker or "platform" in marker:
                    continue   # optional language packs and platform-only extras
                queue.append(re.split(r"[ ;<>=!~(\[]", requirement, 1)[0])
        self.assertEqual(seen, set(pins), "pins cover Goose3 and its whole dependency tree, and nothing else")
        for name, version in pins.items():
            self.assertEqual(metadata.version(name), version, f"{name}: the tested environment must match the pin")

    def test_rapidfuzz_is_a_core_dependency_pinned_exactly(self):
        import importlib.metadata as metadata
        import tomllib
        with open(os.path.join(os.path.dirname(FIXTURE), "..", "..", "pyproject.toml"), "rb") as handle:
            core = tomllib.load(handle)["project"]["dependencies"]
        self.assertEqual(core, ["rapidfuzz==" + metadata.version("rapidfuzz")], "the tested environment matches the pin, and there are no unpinned dependencies")
        self.assertEqual([r for r in metadata.requires("rapidfuzz") or [] if "extra ==" not in r], [], "RapidFuzz has no unconditional dependencies of its own to pin")

    def test_selfcheck_exercises_the_engine_end_to_end(self):
        from area_events.image_extractor import selfcheck
        outcome = selfcheck.check()
        self.assertEqual(outcome["image"], selfcheck.IMAGE)
        self.assertEqual(outcome["engine"], "goose3@" + selfcheck.EXPECTED_VERSION)
        self.assertEqual(selfcheck.main(), 0)

    def test_selfcheck_fails_for_a_missing_wrong_or_broken_engine(self):
        from area_events.image_extractor import selfcheck
        with patch.object(goose3_adapter, "Goose", None):
            self.assertEqual(selfcheck.main(), 1)
        with patch.object(selfcheck, "GOOSE3_VERSION", "3.1.21"):
            self.assertEqual(selfcheck.main(), 1)
        with patch.object(selfcheck, "EXPECTED_RAPIDFUZZ", "0.0.1"):
            self.assertEqual(selfcheck.main(), 1, "a missing or wrong RapidFuzz fails the deployment gate")
        with patch.object(selfcheck, "png", return_value=b"not an image" * 1000):
            self.assertEqual(selfcheck.main(), 1)
        with patch.object(selfcheck, "HTML", "<html><body><article><p>No image at all.</p></article></body></html>"):
            self.assertEqual(selfcheck.main(), 1)


if __name__ == "__main__":
    unittest.main()
