"""The composition root: the one place that creates the Discovery Service's objects and connects them.

    app = build("data/discovery/discovery.db")
    app.register_known_sources()
    app.control.create_area(CreateAreaRequest("San Diego", 32.7157, -117.1611, 25, sources=("sandiego_gov",)))
    app.host.start()                      # scheduler and workers run on their own
    ...
    app.host.stop(); app.close()

Every object is created here and handed its dependencies; nothing is looked up globally. Tests build the same graph with a fake fetch
client and a fixed clock.
"""
import os
import secrets
import time
import types
from dataclasses import dataclass

from ..safe import HostPolicy, SafeFetcher
from ..sources import sandiego_gov
from .adapters import CityOfSanDiegoAdapter, CraftFairAcquisitionAdapter, LocalistAdapter
from .api import ApiTokens, DiscoveryApi, DiscoveryApiServer
from .areas import AreaRepository, SettingsRepository
from .control import LocalDiscoveryControl
from ..media import MediaService
from .enrichment import EnrichmentConfig, MediaEnrichmentService, SourcePhotos
from .events import EventQueryService, EventRepository
from .extraction import ExtractionConfig, ExtractionService, LocalProcessingClient, StoredCandidateExtractor
from .host import DiscoveryHost, HostConfig
from .media_store import LocalMediaFiles, LocalMediaWork, MediaRepository, SqliteMediaStore
from .processing import LocalProcessing, ProcessingConfig, ProcessingRepository
from .reader import LocalDiscoveryReader
from .repositories import CandidateRepository, ChangeLog, JobRepository, PageRepository, Retention, SourceRepository, SourceSpec
from .scheduler import AreaPacing, DiscoveryScheduler, SchedulerConfig
from .service import DiscoveryConfig, DiscoveryService
from .store import DiscoveryStore

KNOWN_SOURCES = types.MappingProxyType({  # read-only
    "san-diego-city": SourceSpec("sandiego_gov", "sandiego_gov", sandiego_gov.NAME, sandiego_gov.LISTING_URL,
                                 options=(("pages", 2), ("max_details_per_run", 10)), interval_s=3600),
    # Austin: the University of Texas at Austin's public events calendar (Localist, key-less JSON API; exact times with UTC offsets, venues, many with addresses)
    "ut-austin": SourceSpec("ut_austin", "localist", "UT Austin Events", "https://calendar.utexas.edu",
                            options=(("days", 14), ("max_pages", 5), ("timezone", "America/Chicago")), interval_s=3600),
    # San Diego craft fairs (source acquisition pilot): two public vendor listings are the seeds; the organizers' pages are found by following their links
    "sd-craft-fairs": SourceSpec("sd_craft_fairs", "craft_fairs", "San Diego craft fairs", "https://www.sweetandquirky.com/pages/upcoming-events",
                                 options=(("seeds", ("https://www.sweetandquirky.com/pages/upcoming-events", "https://my2dragonflies.com/pages/local-markets",
                                                     "https://www.holyfamilysd.org/ia-events/2026-holiday-craft-fair/")),
                                          ("max_organizer_pages", 10), ("horizon_days", 150), ("seed_region", "San Diego County")), interval_s=21600),
})


@dataclass
class DiscoveryApp:
    store: DiscoveryStore
    policy: HostPolicy
    service: DiscoveryService
    reader: LocalDiscoveryReader
    control: LocalDiscoveryControl
    scheduler: DiscoveryScheduler
    host: DiscoveryHost
    processing: LocalProcessing
    events: EventQueryService
    extraction: ExtractionService
    enrichment: MediaEnrichmentService
    media_files: LocalMediaFiles
    media_work: LocalMediaWork

    def register_known_sources(self):
        """Register every built-in source (idempotent). A source that no enabled area uses is registered but idle."""
        return [self.service.register_source(spec) for spec in KNOWN_SOURCES.values()]

    def close(self):
        self.enrichment.stop()
        self.extraction.stop()
        self.host.stop()
        self.store.close()


def build(db_path, clock=time.time, fetch_client=None, policy=None, config=None, worker_id="worker-1", request_interval=2.0, store_options=None,
          host_config=HostConfig(), scheduler_config=SchedulerConfig(), service_gate=True,
          extraction_config=ExtractionConfig(), processing_config=ProcessingConfig(), enrichment_config=EnrichmentConfig(), media_dir=None, nominatim_interval=15.0):
    """The connected Discovery Service objects for one database. `fetch_client` replaces the network client (tests); otherwise a
    SafeFetcher is made with the given (or a new) HostPolicy, which every fetcher of this service shares. With the default `config` the
    service crawls only sources that an enabled area uses and honours the operator's pause; pass a DiscoveryConfig (require_area False)
    and service_gate=False for the earlier, area-less behaviour."""
    store = DiscoveryStore(db_path, **(store_options or {})).open()
    policy = policy or HostPolicy()
    client = fetch_client or SafeFetcher(request_interval, state=policy)
    sources, pages, candidates, jobs, changes = SourceRepository(), PageRepository(), CandidateRepository(), JobRepository(), ChangeLog()
    media_repository = MediaRepository()
    areas, settings, event_repository = AreaRepository(), SettingsRepository(), EventRepository(media_repository)
    pacing = AreaPacing(areas, settings, scheduler_config)
    scheduler = DiscoveryScheduler(store, areas, settings, jobs, changes, clock, pacing)
    config = config or DiscoveryConfig(require_area=service_gate)
    service = DiscoveryService(store, sources, pages, candidates, jobs, changes, Retention(changes, event_repository), client, {"sandiego_gov": CityOfSanDiegoAdapter(), "localist": LocalistAdapter(), "craft_fairs": CraftFairAcquisitionAdapter()}, clock,
                               config, worker_id, pacing=pacing if service_gate else None, gate=scheduler if service_gate else None)
    reader = LocalDiscoveryReader(store, sources, pages, candidates, jobs, changes, clock)
    host = DiscoveryHost(service, scheduler, clock, host_config)
    control = LocalDiscoveryControl(store, sources, areas, settings, jobs, changes, clock, runtime=host.status)
    processing = LocalProcessing(store, ProcessingRepository(), event_repository, candidates, pages, changes, clock, processing_config)
    events = EventQueryService(store, event_repository, areas, sources, changes)
    extraction = ExtractionService(LocalProcessingClient(processing), StoredCandidateExtractor(), clock, extraction_config, worker_id="extractor-local")
    media_folder = media_dir or os.path.join(os.path.dirname(os.path.abspath(db_path)), "media")
    media_store = SqliteMediaStore(store, media_repository, media_folder, clock)
    media_work = LocalMediaWork(store, media_repository, clock)
    resolver = MediaService(os.path.dirname(media_folder), client, clock, nominatim_interval=nominatim_interval, store=media_store)
    enrichment = MediaEnrichmentService(media_work, resolver, SourcePhotos(client, media_store, media_work, clock, enrichment_config), clock, enrichment_config)
    return DiscoveryApp(store, policy, service, reader, control, scheduler, host, processing, events, extraction, enrichment, LocalMediaFiles(media_store), media_work)


def build_api(app, tokens, clock=time.time, host="127.0.0.1", port=8780, allow_remote=False, tls=None, allow_plain=False):
    """The authenticated HTTP API over an app's control and reader contracts (not started)."""
    return DiscoveryApiServer(DiscoveryApi(app.control, app.reader, tokens, clock, on_stop=app.host.request_stop, processing=app.processing, events=app.events, media=app.media_files),
                              host, port, allow_remote, tls, allow_plain)


def load_tokens(directory, read=False, process=False):
    """The control token kept in `<directory>/api-token` (mode 600, created on first use) and, when asked for, a separate read-only token in
    `api-read-token` (for Home Assistant) and the processing token in `api-process-token` (for a remote extraction worker). Each token has its
    own scope and file. Returns (ApiTokens, control token path)."""
    path = _token_file(directory, "api-token")
    return ApiTokens(_stored_token(path), _stored_token(_token_file(directory, "api-read-token")) if read else None,
                     _stored_token(_token_file(directory, "api-process-token")) if process else None), path


def _token_file(directory, name):
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, name)


def _stored_token(path):
    token = None
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError:
        pass
    if not token or len(token) < 32:
        token = secrets.token_urlsafe(32)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(token + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return token
