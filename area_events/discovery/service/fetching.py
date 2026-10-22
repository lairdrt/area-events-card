"""RecordingFetcher: the only fetch client an adapter gets in the Discovery Service.

It wraps the injected SafeFetcher (which carries the shared HostPolicy: public addresses only, robots.txt, per-host spacing and
backoff, size and time limits), enforces a request budget for one job, and records every response, so the service can store each
page the adapter read. One instance per job run; it holds that run's bodies in memory only until they are stored.
"""
from dataclasses import dataclass
from typing import Optional

from ..core import FetchError
from ..safe import BlockedError


@dataclass
class PageFetch:
    url: str
    outcome: str                    # 'ok', 'http_error', 'blocked', 'too_large', 'unsupported', 'network'
    final_url: Optional[str] = None
    http_status: Optional[int] = None
    media_type: Optional[str] = None
    body: Optional[bytes] = None
    error: Optional[str] = None


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
    def __init__(self, client, max_requests):
        self.client, self.max_requests = client, max_requests
        self.fetches = []

    @property
    def requests(self):
        return len(self.fetches)

    def fetch(self, url, check_robots=True, binary=None, timeout=None):
        if len(self.fetches) >= self.max_requests:
            raise BudgetExceeded(f"the request budget of this job ({self.max_requests}) is spent")
        try:
            response = self.client.fetch(url, binary=binary) if binary else self.client.fetch(url)
        except FetchError as error:
            self.fetches.append(PageFetch(url, classify_failure(error), error=str(error)[:300]))
            raise
        body = response.body if response.body else response.text.encode("utf-8")
        self.fetches.append(PageFetch(url, "ok", response.url, getattr(response, "status", 200), (response.content_type or "").split(";")[0].strip() or None, body))
        return response

    def get(self, url, check_robots=True):
        """The interface registry adapters use (core.Fetcher.get): the page's text."""
        return self.fetch(url, check_robots).text
