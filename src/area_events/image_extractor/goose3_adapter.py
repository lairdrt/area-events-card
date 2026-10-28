"""The first image_extractor/v1 engine: Goose3 (https://pypi.org/project/goose3/), kept behind the contract.

Goose3 is an optional dependency (`pip install "area-events[images]"`); without it `Goose3ImageExtractor.available()` is False and a producer
simply goes without page images. Nothing outside this module sees a Goose3 object.

Goose3 downloads candidate images to measure them (`enable_image_fetching=True` is what makes it pick the large content image rather than
only a meta tag). Its own HTTP client is never used: the engine's fetcher is replaced by `_RequestFetcher`, so every download is the
caller's `fetch_image`, which carries the shared safe-fetch rules (public addresses only, robots.txt, host spacing, size and time limits). On
top of that this adapter caps the number and size of downloads per page, keeps Goose3's scratch files in a throw-away directory, and
treats any failure as 'no image'.
"""
import logging
import tempfile
from urllib.parse import urljoin

from .v1 import ImageResult, no_image

try:
    from goose3 import Configuration, Goose
    from goose3.network import NetworkFetcher
    from goose3.version import __version__ as GOOSE3_VERSION
except ImportError:   # optional dependency
    Goose = Configuration = NetworkFetcher = None
    GOOSE3_VERSION = None

log = logging.getLogger("area-events.image-extractor")

MAX_IMAGE_FETCHES = 10          # downloads per page
MAX_IMAGE_BYTES = 2_000_000     # per image; the same limit enrichment applies before anything is stored
MAX_TOTAL_BYTES = 8_000_000     # per page
MAX_HTML_CHARS = 1_500_000


if NetworkFetcher is not None:
    class _RequestFetcher(NetworkFetcher):
        """Goose3's network client, replaced: image bytes come only from the caller's bounded `fetch_image`; pages are never fetched."""

        def __init__(self, fetch_image):   # no HTTP session is created
            self._fetch_image, self.count, self.total = fetch_image, 0, 0

        def close(self):
            pass

        def fetch_obj(self, url):
            raise RuntimeError("the image engine may not fetch pages")

        def fetch(self, url):
            if self._fetch_image is None or self.count >= MAX_IMAGE_FETCHES or self.total >= MAX_TOTAL_BYTES:
                return b""
            self.count += 1
            try:
                body = self._fetch_image(url)
            except Exception as error:   # a refused or failed download is just a candidate that cannot be measured
                log.debug("image candidate not fetched: %s", error)
                return b""
            if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_IMAGE_BYTES:
                return b""
            self.total += len(body)
            return bytes(body)


class Goose3ImageExtractor:
    name = "goose3"

    @staticmethod
    def available():
        return Goose is not None

    @property
    def engine(self):
        return f"goose3@{GOOSE3_VERSION}"

    def extract(self, request):
        if Goose is None:
            return no_image(self.engine, "goose3 is not installed")
        if not request.html or len(request.html) > MAX_HTML_CHARS:
            return no_image(self.engine)
        try:
            with tempfile.TemporaryDirectory(prefix="area-events-goose-") as scratch:
                config = Configuration()
                config.enable_image_fetching = True   # False disables image selection altogether
                config.strict = False
                config.local_storage_path = scratch
                with Goose(config) as goose:
                    goose.fetcher.close()
                    goose.fetcher = _RequestFetcher(request.fetch_image)
                    image = goose.extract(url=request.page_url, raw_html=request.html).top_image
                    src = image.src if image else None
                    if not src:
                        return no_image(self.engine)
                    url = urljoin(request.page_url, src)
                    if not url.lower().startswith(("http://", "https://")):
                        return no_image(self.engine)
                    return ImageResult(image_url=url, engine=self.engine, method=image.extraction_type or None,
                                       confidence=float(image.confidence_score) if image.confidence_score is not None else None,
                                       width=image.width or None, height=image.height or None)
        except Exception as error:   # the contract: an engine failure costs the page its image and nothing else
            log.warning("image engine failed on %s: %s", request.page_url, error)
            return no_image(self.engine, f"{type(error).__name__}: {str(error)[:120]}")
