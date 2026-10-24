"""Producer for craft fairs found by following public listings to the organizers' own pages (the source-acquisition pilot).

    acquisition.py   SourceAcquirer (the bounded crawl, leads, merging) and CraftFairProducer (the standard-record answer)
    reading.py       what a listing card, an organizer page and a craft fair look like
    pages.py         the producer's own page reader: element tree, schema.org Event data, written dates

Everything about this source lives in this package; it imports only the contract, the fetch client, URL identity and the neutral text and time utilities."""
from .acquisition import AcquisitionConfig, CraftFairProducer, SourceAcquirer

__all__ = ["AcquisitionConfig", "CraftFairProducer", "SourceAcquirer"]
