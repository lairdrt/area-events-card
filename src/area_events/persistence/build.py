"""Opening Persistence: the one SQLite database and the port implementations over it, built once by the composition root.

Everything that touches the database is created here and handed out as ports (see ports.py); the deduplication strategy is injected as an object that implements the
`Deduplicator` interface and is only ever called through that interface, inside the atomic operations of the processing port.
"""
import os
import time
from dataclasses import dataclass

from .areas import AreaRepository, SettingsRepository
from .control import SqliteControlStorage
from .crawl import SqliteCrawlStorage
from .events import DuplicateMaintenance, EventRepository, SqliteEventQueries
from .reviewed import ReviewedReconciliation
from .naming import DisplayNames
from .media import LocalMediaFiles, LocalMediaWork, MediaRepository, SqliteMediaStore
from .ports import MediaCache
from .processing import ProcessingRepository, SqliteProcessingStorage
from .reading import LocalDiscoveryReader
from .repositories import CandidateRepository, ChangeLog, JobRepository, PageRepository, Retention, SourceCleanup, SourceRepository
from .scheduling import SqliteSchedulingStorage
from .store import DiscoveryStore, SCHEMA_VERSION
from .trashcan import SqliteTrashcan


def open_duplicate_maintenance(db_path, matcher, clock=time.time):
    """Open the narrow maintenance port without initializing the service or applying migrations."""
    return DuplicateMaintenance(db_path, matcher, SCHEMA_VERSION, clock)


def open_reviewed_reconciliation(db_path, clock=time.time):
    """Open the operator-reviewed reconciliation port (no matcher, no migrations)."""
    return ReviewedReconciliation(os.path.abspath(db_path), SCHEMA_VERSION, clock)


@dataclass
class Persistence:
    """The database and what it offers. `close` closes the database; nothing else is exposed."""
    crawl: object
    scheduling: object
    control: object
    processing: object
    events: object
    reader: object
    media_cache: MediaCache
    media_work: object
    media_files: object
    source_cleanup: object
    display_names: object
    trashcan: object
    _store: object

    @property
    def path(self):
        return self._store.path

    def close(self):
        self._store.close()


def open_persistence(db_path, deduplicator, clock=time.time, store_options=None, media_dir=None, reserved_source_keys=()):
    store = DiscoveryStore(db_path, **(store_options or {})).open()
    sources, pages, candidates, jobs, changes = SourceRepository(), PageRepository(), CandidateRepository(), JobRepository(), ChangeLog()
    media_repository = MediaRepository()
    areas, settings, events = AreaRepository(), SettingsRepository(), EventRepository(media_repository)
    media_folder = media_dir or os.path.join(os.path.dirname(os.path.abspath(db_path)), "media")
    media_store = SqliteMediaStore(store, media_repository, media_folder, clock)
    trashcan = SqliteTrashcan(store, changes, clock)
    return Persistence(
        crawl=SqliteCrawlStorage(store, sources, pages, candidates, jobs, changes, Retention(changes, events), areas, settings, clock, trashcan, events),
        scheduling=SqliteSchedulingStorage(store, areas, settings, jobs, clock),
        control=SqliteControlStorage(store, sources, areas, settings, jobs, changes, clock),
        processing=SqliteProcessingStorage(store, ProcessingRepository(), events, candidates, pages, changes, clock, deduplicator),
        events=SqliteEventQueries(store, events, areas, sources, changes),
        reader=LocalDiscoveryReader(store, sources, pages, candidates, jobs, changes, clock),
        media_cache=media_store,
        media_work=LocalMediaWork(store, media_repository, clock),
        media_files=LocalMediaFiles(media_store),
        source_cleanup=SourceCleanup(store, clock, reserved_source_keys),
        display_names=DisplayNames(store, changes, clock),
        trashcan=trashcan,
        _store=store)
