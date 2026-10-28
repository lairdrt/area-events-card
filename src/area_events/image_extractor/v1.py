"""image_extractor/v1: the contract between a producer and any engine that finds a page's primary image.

A producer hands an engine the HTML it has already fetched and receives an `ImageResult`: at most one URL, never an engine's own objects. Whether
that image belongs to a particular event is not decided here (see `association`), and an image's bytes are never judged here: the enrichment
service validates type and size before anything is stored. The one network path an engine may use is `ImageRequest.fetch_image`, which the
caller backs with its safe, bounded fetcher; an engine that cannot work without other network access does not implement this contract.

Any object with `name` and `extract(request) -> ImageResult` is an engine. `extract` must not raise for an ordinary failure: it returns
`no_image(...)` with the reason, so a failing engine costs a producer its images and nothing else.
"""
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

CONTRACT = "image_extractor/v1"


@dataclass(frozen=True)
class ImageRequest:
    html: str                                                  # the page, already fetched and size-limited by the caller
    page_url: str                                              # its final address; relative references resolve against it
    event_titles: tuple = ()                                   # source context, a hint an engine may ignore
    fetch_image: Optional[Callable[[str], Optional[bytes]]] = None   # bytes of an image within the caller's limits, or None


@dataclass(frozen=True)
class ImageResult:
    image_url: Optional[str] = None       # the page's primary image (absolute http/https), None when there is none
    alternatives: tuple = ()              # other absolute URLs the engine says show the same image (for example resized copies)
    engine: str = ""                      # which engine and version decided, for provenance
    method: Optional[str] = None          # how it was selected, in the engine's words (for example "bigimage", "opengraph")
    confidence: Optional[float] = None    # 0-100 where the engine reports one
    width: Optional[int] = None
    height: Optional[int] = None
    error: Optional[str] = None           # set when the engine failed, as opposed to finding nothing


class ImageExtractor(Protocol):
    name: str

    def extract(self, request: ImageRequest) -> ImageResult: ...


def no_image(engine, error=None):
    """The explicit result for 'no primary image' (or 'the engine failed' when `error` is given)."""
    return ImageResult(engine=engine, error=error)
