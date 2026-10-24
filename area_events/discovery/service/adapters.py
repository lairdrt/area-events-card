"""Source adapters for the Discovery Service: the EXISTING adapters behind the small `SourceAdapter` protocol.

An adapter reads a source only through the fetcher it is given (a RecordingFetcher over the shared-policy SafeFetcher) and returns
candidates: the event dicts the existing adapter already produces. It never stores anything itself.
"""
from .. import core
from ..sources import localist, sandiego_gov
from .acquisition import AcquisitionConfig, SourceAcquirer
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


class LocalistAdapter:
    """Any Localist calendar (EXISTING `sources/localist.py`, unchanged): the site's public, key-less JSON API, paged. One adapter serves every
    Localist site; each is its own source, with the site address as its base URL and its `days`, `max_pages` and `timezone` as options.
    The timezone is the calendar's own (the API gives each time with its UTC offset; the zone name is only recorded, never used to guess a time)."""

    adapter = "localist"
    extractor = "sources/localist@v1"
    method = "adapter"

    def crawl(self, fetcher, previous, options):
        zone = options.get("timezone")
        events, notes = localist.discover(fetcher, previous, details=False, days=int(options.get("days", 14)), max_pages=int(options.get("max_pages", 5)),
                                          source_id=options["source_key"], source_name=options.get("name", "Localist"), url=options["url"],
                                          location={"timezone": zone} if zone else None)
        return AdapterResult(events, notes)

    @staticmethod
    def page_role(url, base_url):
        return "feed"


class CraftFairAcquisitionAdapter:
    """Source acquisition pilot: craft fairs found by following public listings to the organizers' own pages (`acquisition.SourceAcquirer`).
    The source's `seeds` option lists the listing pages it starts from; everything else (organizer and event pages) is found by following their links.
    The pages it considered are returned as leads and stored with the reasons they were accepted or rejected."""

    adapter = "craft_fairs"
    extractor = "sources/craftfairs@v1"
    method = "adapter"

    def __init__(self, today=lambda: core.now_pacific().date()):
        self._today = today

    def crawl(self, fetcher, previous, options):
        seeds = tuple(options.get("seeds") or (options["url"],))
        config = AcquisitionConfig(max_organizer_pages=int(options.get("max_organizer_pages", 10)), horizon_days=int(options.get("horizon_days", 150)),
                                   seed_region=str(options.get("seed_region", "")))
        acquirer = SourceAcquirer(fetcher, options["source_key"], options.get("name", "Craft fairs"), self._today(), config)
        events, leads, notes = acquirer.run(seeds)
        return AdapterResult(events, notes, leads)

    @staticmethod
    def page_role(url, base_url):
        return "listing" if url.split("?")[0].rstrip("/") == base_url.split("?")[0].rstrip("/") else "detail"
