"""Deployment self-check: can the installed image engine really find an image? `python -m area_events.image_extractor.selfcheck`

Importing Goose3 proves little (a broken lxml or Pillow only shows when a page is parsed and an image measured), so this runs the shipped engine
end to end on an offline page and a generated PNG, through the same `image_extractor/v1` contract the producers use. It touches no network and no
database. Exit status 0 means the engine selected the expected image; anything else prints the reason and exits 1, so a deployment can refuse it.
"""
import io
import json
import random
import sys

from .goose3_adapter import GOOSE3_VERSION, Goose3ImageExtractor
from .v1 import ImageRequest

EXPECTED_VERSION = "3.1.22"
EXPECTED_RAPIDFUZZ = "3.14.6"   # the deduplication scorer rides on the same deployment gate: it must be installed and working
PAGE = "https://selfcheck.invalid/events/fair"
IMAGE = "https://selfcheck.invalid/uploads/fair.png"
HTML = ("<html><head><title>Fair</title></head><body><article>"
        f'<img src="/uploads/fair.png" width="640" height="214">'
        "<p>The Autumn Fair is the best day of the year in the village, and all of the families who live in the area are invited to "
        "come along and enjoy the food, the music and the games that we have planned for them on the green.</p>"
        "<p>There will be something for everyone at the fair, and you can find out more about it on the day.</p>"
        "</article></body></html>")


def png():
    from PIL import Image
    buffer = io.BytesIO()
    Image.frombytes("RGB", (640, 214), random.Random(1).randbytes(640 * 214 * 3)).save(buffer, "PNG")
    return buffer.getvalue()


def check_scorer():
    """RapidFuzz (the deduplicator's text similarity) is installed at its pin and computes a sensible score."""
    import rapidfuzz
    from rapidfuzz import fuzz
    assert rapidfuzz.__version__ == EXPECTED_RAPIDFUZZ, f"rapidfuzz {rapidfuzz.__version__} is installed, {EXPECTED_RAPIDFUZZ} is required"
    assert fuzz.ratio("holiday marketplace", "holiday marketplaces") > 90 and fuzz.ratio("jazz night", "rock night") < 70, "rapidfuzz scores unexpectedly"


def check():
    """Raises AssertionError or any import/runtime error with the reason the image engine cannot be relied on."""
    check_scorer()
    engine = Goose3ImageExtractor()
    assert engine.available(), "goose3 is not installed"
    assert GOOSE3_VERSION == EXPECTED_VERSION, f"goose3 {GOOSE3_VERSION} is installed, {EXPECTED_VERSION} is required"
    body, fetched = png(), []

    def fetch_image(url):
        fetched.append(url)
        return body

    result = engine.extract(ImageRequest(HTML, PAGE, fetch_image=fetch_image))
    assert not result.error, f"the engine failed: {result.error}"
    assert result.image_url == IMAGE, f"the engine selected {result.image_url!r}, not the page's image"
    assert (result.width, result.height) == (640, 214), "the engine could not measure the image"
    assert fetched and set(fetched) == {IMAGE}, "the engine fetched something other than through the provided fetcher"
    return {"engine": result.engine, "method": result.method, "image": result.image_url}


def main():
    try:
        print(json.dumps({"ok": True, **check()}))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "reason": f"{type(error).__name__}: {error}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
