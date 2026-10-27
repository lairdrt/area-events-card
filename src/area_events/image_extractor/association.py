"""Does a page's primary image belong to the events found on that page? One small policy shared by every producer.

Engines say which picture is the page's; this module says whether the page's events may carry it. It matches no filename, caption or
heading, and it knows no source.

* One distinct event on the page (recurrences share a title): the primary image is theirs, if it is a usable photo.
* Several distinct events: one page picture cannot be told apart between them, so none is assigned.
* An image an event already carries from its source (for example JSON-LD) is never replaced.
"""
from dataclasses import dataclass
from typing import Optional

from ..enrichment.images import judge
from . import responsive
from .v1 import ImageResult


@dataclass(frozen=True)
class Association:
    image_url: Optional[str]     # the address imageless events may take, None when ownership is not clear
    reason: str                  # why, in words an operator can read


def identity(title):
    return " ".join(str(title).casefold().split())


def wanted_for_page(events):
    """Is it worth asking an engine at all? Only when an event lacks an image and the page holds exactly one distinct event."""
    events = list(events)
    return any(not image for _, image in events) and len({identity(title) for title, _ in events}) == 1


def associate(result: ImageResult, events, html="", page_url=""):
    """`events` is an iterable of (title, image_url or None) for one page. Returns the `Association` for the events without an image."""
    events = list(events)
    if not result.image_url:
        return Association(None, f"image engine failed: {result.error}" if result.error else "page has no primary image")
    if all(image for _, image in events):
        return Association(None, "every event already has its own image")
    if len({identity(title) for title, _ in events}) != 1:
        return Association(None, "several distinct events on one page: ownership is ambiguous")
    usable, why = judge(result.image_url)
    if not usable:
        return Association(None, f"primary image rejected: {why}")
    declared = responsive.variants_of(html, page_url, result.image_url) if html else []
    declared += [(url, 0, 0) for url in result.alternatives]
    chosen = responsive.prefer_variant(result.image_url, declared)
    if chosen != result.image_url and not judge(chosen)[0]:
        chosen = result.image_url
    return Association(chosen, "single event page: the page's primary image")
