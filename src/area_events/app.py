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
import logging
import os
import secrets
import time
import types
from dataclasses import dataclass

from .enrichment.media import MediaService
from .discovery.safe import HostPolicy, SafeFetcher
from .api.http_api import ApiTokens, DiscoveryApi, DiscoveryApiServer
from .contracts.models import ProcessingConfig, SourceSpec
from .control.service import LocalDiscoveryControl
from .deduplication.exact import ExactMatch
from .deduplication.tolerant import TolerantMatch
from .enrichment.service import EnrichmentConfig, MediaEnrichmentService, SourcePhotos
from .api.events import EventQueryService
from .extraction.service import ExtractionConfig, ExtractionService, LocalProcessingClient, StoredCandidateExtractor
from .discovery.host import DiscoveryHost, HostConfig
from .persistence.build import open_duplicate_maintenance, open_persistence, open_reviewed_reconciliation
from .persistence.store import create_verified_database_backup
from .validation.processing import ProcessingService
from .discovery.producers import city_of_san_diego
from .discovery.producers.city_of_san_diego import CityOfSanDiegoProducer
from .discovery.producers.localist import LocalistProducer
from .discovery.producers.sources_producer import SourcesProducer
from .image_extractor.goose3_adapter import Goose3ImageExtractor
from .discovery.scheduler import AreaPacing, DiscoveryScheduler, SchedulerConfig
from .discovery.service import DiscoveryConfig, DiscoveryService

# The deduplication strategies, by the name an operator or a test selects. Replacing the matching algorithm means choosing another name (or passing another object that
# implements `deduplication.Deduplicator`) here; nothing else in the application changes.
DEDUPLICATORS = types.MappingProxyType({"exact": ExactMatch, "tolerant": TolerantMatch})
DEFAULT_DEDUPLICATION = "exact"


def duplicate_maintenance(db_path, strategy=DEFAULT_DEDUPLICATION, clock=time.time):
    """Build duplicate maintenance with the same replaceable matcher used by event processing."""
    deduplicator = DEDUPLICATORS[strategy]() if isinstance(strategy, str) else strategy
    return open_duplicate_maintenance(db_path, deduplicator, clock)


def reviewed_reconciliation(db_path, clock=time.time):
    """The operator-reviewed reconciliation of explicitly listed event pairs (see persistence/reviewed.py)."""
    return open_reviewed_reconciliation(db_path, clock)


KNOWN_SOURCES = types.MappingProxyType({  # read-only
    "san-diego-city": SourceSpec("sandiego_gov", "sandiego_gov", city_of_san_diego.NAME, city_of_san_diego.LISTING_URL,
                                 options=(("pages", 2), ("max_details_per_run", 10)), interval_s=3600),
    # Austin: the University of Texas at Austin's public events calendar (Localist, key-less JSON API; exact times with UTC offsets, venues, many with addresses)
    "ut-austin": SourceSpec("ut_austin", "localist", "UT Austin Events", "https://calendar.utexas.edu",
                            options=(("days", 14), ("max_pages", 5), ("timezone", "America/Chicago")), interval_s=3600),
})

def default_image_extractor():
    """The image_extractor/v1 engine the producers share: Goose3 when installed. Swapping engines means changing this line only."""
    engine = Goose3ImageExtractor()
    if not engine.available():
        logging.getLogger("area-events").warning("goose3 is not installed: pages will not be searched for event images")
        return None
    return engine


@dataclass
class DiscoveryApp:
    persistence: object
    policy: HostPolicy
    service: DiscoveryService
    reader: object
    control: LocalDiscoveryControl
    scheduler: DiscoveryScheduler
    host: DiscoveryHost
    processing: ProcessingService
    events: EventQueryService
    extraction: ExtractionService
    enrichment: MediaEnrichmentService
    media_files: object
    media_work: object

    def register_known_sources(self):
        """Register every built-in source (idempotent). A source that no enabled area uses is registered but idle."""
        return [self.service.register_source(spec) for spec in KNOWN_SOURCES.values()]

    def close(self):
        self.enrichment.stop()
        self.extraction.stop()
        self.host.stop()
        self.persistence.close()


def build(db_path, clock=time.time, fetch_client=None, policy=None, config=None, worker_id="worker-1", request_interval=2.0, store_options=None,
          host_config=HostConfig(), scheduler_config=SchedulerConfig(), service_gate=True,
          extraction_config=ExtractionConfig(), processing_config=ProcessingConfig(), enrichment_config=EnrichmentConfig(), media_dir=None, nominatim_interval=15.0,
          deduplication=DEFAULT_DEDUPLICATION):
    """The connected Discovery Service objects for one database. `fetch_client` replaces the network client (tests); otherwise a
    SafeFetcher is made with the given (or a new) HostPolicy, which every fetcher of this service shares. With the default `config` the
    service crawls only sources that an enabled area uses and honours the operator's pause; pass a DiscoveryConfig (require_area False)
    and service_gate=False for the earlier, area-less behaviour. `deduplication` is the name of a strategy in DEDUPLICATORS, or an object
    that implements the Deduplicator interface."""
    policy = policy or HostPolicy()
    client = fetch_client or SafeFetcher(request_interval, state=policy)
    deduplicator = DEDUPLICATORS[deduplication]() if isinstance(deduplication, str) else deduplication
    persistence = open_persistence(db_path, deduplicator, clock, store_options, media_dir,
                                   reserved_source_keys=(spec.source_key for spec in KNOWN_SOURCES.values()))
    pacing = AreaPacing(scheduler_config)
    scheduler = DiscoveryScheduler(persistence.scheduling, pacing)
    config = config or DiscoveryConfig(require_area=service_gate)
    adapters = {"sandiego_gov": CityOfSanDiegoProducer(), "localist": LocalistProducer(),
                "sources_producer": SourcesProducer(image_extractor=default_image_extractor())}
    service = DiscoveryService(persistence.crawl, client, adapters, clock, config, worker_id,
                               pacing=pacing if service_gate else None, gate=scheduler if service_gate else None)
    host = DiscoveryHost(service, scheduler, clock, host_config)
    control = LocalDiscoveryControl(persistence.control, runtime=host.status, adapters=service.adapters,
                                    source_validators={key: producer.validate_options for key, producer in service.adapters.items()},
                                    reserved_source_keys=(spec.source_key for spec in KNOWN_SOURCES.values()),
                                    producer_definitions=tuple({
                                        "id": key, "name": getattr(producer, "display_name", key),
                                        "fields": list(getattr(producer, "configuration_fields", ()))
                                    } for key, producer in service.adapters.items()))
    processing = ProcessingService(persistence.processing, clock, processing_config)
    events = EventQueryService(persistence.events)
    extraction = ExtractionService(LocalProcessingClient(processing), StoredCandidateExtractor(), clock, extraction_config, worker_id="extractor-local")
    media_folder = media_dir or os.path.join(os.path.dirname(os.path.abspath(db_path)), "media")
    resolver = MediaService(os.path.dirname(media_folder), client, clock, nominatim_interval=nominatim_interval, store=persistence.media_cache)
    enrichment = MediaEnrichmentService(persistence.media_work, resolver, SourcePhotos(client, persistence.media_cache, persistence.media_work, clock, enrichment_config), clock,
                                        enrichment_config)
    return DiscoveryApp(persistence, policy, service, persistence.reader, control, scheduler, host, processing, events, extraction, enrichment, persistence.media_files,
                        persistence.media_work)


def build_api(app, tokens, clock=time.time, host="127.0.0.1", port=8780, allow_remote=False, tls=None, allow_plain=False):
    """The authenticated HTTP API over an app's control and reader contracts (not started)."""
    return DiscoveryApiServer(DiscoveryApi(app.control, app.reader, tokens, clock, on_stop=app.host.request_stop, processing=app.processing, events=app.events, media=app.media_files,
                                           trashcan=app.persistence.trashcan),
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
