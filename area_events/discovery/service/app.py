"""The composition root: the one place that creates the Discovery Service's objects and connects them.

    app = build("data/discovery/discovery.db")
    app.service.register_source(KNOWN_SOURCES["san-diego-city"])
    app.service.run_once()
    app.reader.changes(after_seq=0)
    app.close()

Every object is created here and handed its dependencies; nothing is looked up globally. Tests build the same graph with a fake
fetch client and a fixed clock.
"""
import time
import types
from dataclasses import dataclass

from ..safe import HostPolicy, SafeFetcher
from ..sources import sandiego_gov
from .adapters import CityOfSanDiegoAdapter
from .reader import LocalDiscoveryReader
from .repositories import CandidateRepository, ChangeLog, JobRepository, PageRepository, Retention, SourceRepository, SourceSpec
from .service import DiscoveryConfig, DiscoveryService
from .store import DiscoveryStore

KNOWN_SOURCES = types.MappingProxyType({  # read-only
    "san-diego-city": SourceSpec("sandiego_gov", "sandiego_gov", sandiego_gov.NAME, sandiego_gov.LISTING_URL,
                                 options=(("pages", 2), ("max_details_per_run", 10)), interval_s=3600),
})


@dataclass
class DiscoveryApp:
    store: DiscoveryStore
    policy: HostPolicy
    service: DiscoveryService
    reader: LocalDiscoveryReader

    def close(self):
        self.store.close()


def build(db_path, clock=time.time, fetch_client=None, policy=None, config=DiscoveryConfig(), worker_id="worker-1", request_interval=2.0, store_options=None):
    """The connected Discovery Service objects for one database. `fetch_client` replaces the network client (tests); otherwise a
    SafeFetcher is made with the given (or a new) HostPolicy, which every fetcher of this service shares."""
    store = DiscoveryStore(db_path, **(store_options or {})).open()
    policy = policy or HostPolicy()
    client = fetch_client or SafeFetcher(request_interval, state=policy)
    sources, pages, candidates, jobs, changes = SourceRepository(), PageRepository(), CandidateRepository(), JobRepository(), ChangeLog()
    adapters = {"sandiego_gov": CityOfSanDiegoAdapter()}
    service = DiscoveryService(store, sources, pages, candidates, jobs, changes, Retention(changes), client, adapters, clock, config, worker_id)
    reader = LocalDiscoveryReader(store, sources, pages, candidates, jobs, changes, clock)
    return DiscoveryApp(store, policy, service, reader)
