"""RecordingFetcher: the only fetch client an adapter gets in the Discovery Service.

It wraps the injected SafeFetcher (which carries the shared HostPolicy: public addresses only, robots.txt, per-host spacing and
backoff, size and time limits), enforces a request budget for one job, and records every response, so the service can store each
page the adapter read. One instance per job run; it holds that run's bodies in memory only until they are stored.
"""
import time
from dataclasses import dataclass
from typing import Optional

from .safe import BlockedError, FetchError


@dataclass
class PageFetch:
    url: str
    outcome: str                    # 'ok', 'http_error', 'blocked', 'too_large', 'unsupported', 'network'
    final_url: Optional[str] = None
    http_status: Optional[int] = None
    media_type: Optional[str] = None
    body: Optional[bytes] = None
    error: Optional[str] = None


MAX_IMAGE_REQUESTS = 30   # image downloads an image_extractor engine may make in one job, on top of the page budget
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")


class BudgetExceeded(FetchError):
    """The job's request budget is spent."""


def classify_failure(error):
    text = str(error).lower()
    if isinstance(error, BlockedError):
        return "blocked"
    if "too large" in text or "over the limit" in text:
        return "too_large"
    if "not a text document" in text or "unexpected content type" in text:
        return "unsupported"
    if "http " in text:
        return "http_error"
    return "network"


class RecordingFetcher:
    def __init__(self, client, max_requests, max_seconds=None, monotonic=time.monotonic, max_image_requests=MAX_IMAGE_REQUESTS):
        self.client, self.max_requests, self.max_seconds, self.monotonic = client, max_requests, max_seconds, monotonic
        self.max_image_requests, self.image_requests = max_image_requests, 0
        self.started = monotonic()
        self.fetches = []

    @property
    def requests(self):
        return len(self.fetches)

    def fetch(self, url, check_robots=True, binary=None, timeout=None):
        if len(self.fetches) >= self.max_requests:
            raise BudgetExceeded(f"the request budget of this job ({self.max_requests}) is spent")
        if self.max_seconds is not None and self.monotonic() - self.started > self.max_seconds:
            raise BudgetExceeded(f"the time budget of this job ({self.max_seconds} s) is spent")
        try:
            response = self.client.fetch(url, binary=binary) if binary else self.client.fetch(url)
        except FetchError as error:
            self.fetches.append(PageFetch(url, classify_failure(error), error=str(error)[:300]))
            raise
        body = response.body if response.body else response.text.encode("utf-8")
        self.fetches.append(PageFetch(url, "ok", response.url, getattr(response, "status", 200), (response.content_type or "").split(";")[0].strip() or None, body))
        return response

    def fetch_image(self, url):
        """The bytes of one image for an image_extractor engine, through the same safe client. Counted against its own budget and not
        recorded: image bodies are measured by the engine and are not stored pages."""
        if self.image_requests >= self.max_image_requests:
            raise BudgetExceeded(f"the image budget of this job ({self.max_image_requests}) is spent")
        if self.max_seconds is not None and self.monotonic() - self.started > self.max_seconds:
            raise BudgetExceeded(f"the time budget of this job ({self.max_seconds} s) is spent")
        self.image_requests += 1
        return self.client.fetch(url, binary=IMAGE_TYPES).body

    def get(self, url, check_robots=True):
        """Return the fetched page text through the standard producer interface."""
        return self.fetch(url, check_robots).text
