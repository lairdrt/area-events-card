"""Source adapters for the Discovery Service: the EXISTING adapters behind the small `SourceAdapter` protocol.

An adapter reads a source only through the fetcher it is given (a RecordingFetcher over the shared-policy SafeFetcher) and returns
candidates: the event dicts the existing adapter already produces. It never stores anything itself.
"""
from ..sources import sandiego_gov
from .contracts import AdapterResult


class CityOfSanDiegoAdapter:
    """The City of San Diego events listing (EXISTING `sources/sandiego_gov.py`, unchanged): listing pages, then a bounded number of
    event pages per run, with already-read event pages reused from the previous candidates."""

    adapter = "sandiego_gov"
    extractor = f"sources/sandiego_gov@v{sandiego_gov.DETAIL_VERSION}"
    method = "adapter"

    def crawl(self, fetcher, previous, options):
        events, notes = sandiego_gov.discover(fetcher, previous, details=True, pages=int(options.get("pages", 2)),
                                              max_details_per_run=int(options.get("max_details_per_run", 10)),
                                              source_id=options["source_key"], source_name=options.get("name", sandiego_gov.NAME),
                                              url=options.get("url", sandiego_gov.LISTING_URL))
        return AdapterResult(events, notes)

    @staticmethod
    def page_role(url, base_url):
        return "listing" if url.split("?")[0].rstrip("/") == base_url.split("?")[0].rstrip("/") else "detail"
