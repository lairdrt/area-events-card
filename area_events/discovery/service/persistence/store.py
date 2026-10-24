"""DiscoveryStore: the discovery database file, its connections and its transactions.

* One write connection, used by one writer at a time (an instance lock coordinates the threads of this process; SQLite's own
  lock and `busy_timeout` coordinate other processes). Every write is a short `BEGIN IMMEDIATE` transaction: callers do their
  network, parsing and compression before entering it.
* Readers get their own connection per `read()` (`query_only`), inside a read transaction, so a reader sees one consistent state and
  never blocks the writer (WAL).
* Pragmas on every connection: `foreign_keys=ON`, `busy_timeout`. Database: `journal_mode=WAL`, `synchronous=NORMAL`,
  `journal_size_limit`.
* A busy database is retried a bounded number of times; a database that stays locked, is full or fails is reported as
  `StorageUnavailable`, which callers turn into a bounded pause, never into silently dropped results.
"""
import collections
import contextlib
import os
import sqlite3
import threading
import time

from ..contracts import StaleLease, StorageUnavailable  # noqa: F401
from .schema import LATEST, MIGRATIONS

MIN_SQLITE = (3, 35, 0)  # RETURNING


def _statements(script):
    """The statements of a migration script, split where SQLite says a statement is complete."""
    out, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                out.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise ValueError("a migration ends with an incomplete statement")
    return out


class WriteStats:
    """Latency and contention of this store's writes (instance-owned)."""

    def __init__(self, keep=2000):
        self.lock = threading.Lock()
        self.latencies = collections.deque(maxlen=keep)
        self.writes = self.retries = self.failures = 0

    def record(self, seconds, retries, failed=False):
        with self.lock:
            self.retries += retries
            if failed:
                self.failures += 1
            else:
                self.writes += 1
                self.latencies.append(seconds)

    def summary(self):
        with self.lock:
            values = sorted(self.latencies)
            writes, retries, failures = self.writes, self.retries, self.failures

        def pick(q):
            return round(values[min(len(values) - 1, int(q * len(values)))] * 1000, 2) if values else None
        return {"writes": writes, "retries": retries, "failures": failures, "p50_ms": pick(0.5), "p95_ms": pick(0.95), "max_ms": pick(1.0)}


class DiscoveryStore:
    def __init__(self, path, busy_timeout_ms=5000, write_retries=3, retry_sleep=0.05, migrations=MIGRATIONS, sleep=time.sleep, monotonic=time.perf_counter):
        self.path = os.path.abspath(path)
        self.busy_timeout_ms, self.write_retries, self.retry_sleep = busy_timeout_ms, write_retries, retry_sleep
        self.migrations, self.sleep, self.monotonic = migrations, sleep, monotonic
        self.stats = WriteStats()
        self._write_lock = threading.Lock()
        self._writer = None

    # --- lifecycle -------------------------------------------------------------------------------------------------

    def open(self):
        if self._writer is not None:
            return self
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise RuntimeError(f"SQLite {sqlite3.sqlite_version} is too old (3.35 or newer is needed)")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        connection = self._connect()
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode.lower() != "wal":
            connection.close()
            raise RuntimeError(f"the database could not be put in WAL mode ({mode})")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA journal_size_limit=67108864")
        self._writer = connection
        try:
            self.migrate()
        except BaseException:
            self.close()
            raise
        return self

    def close(self):
        with self._write_lock:
            if self._writer is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._writer.execute("PRAGMA optimize")
                self._writer.close()
                self._writer = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _connect(self, readonly=False):
        connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
        return connection

    # --- schema ----------------------------------------------------------------------------------------------------

    def migrate(self):
        """Apply every migration not yet applied, each in its own transaction. Returns the schema version."""
        self._writer.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at INTEGER NOT NULL)")
        applied = {row[0] for row in self._writer.execute("SELECT version FROM schema_migrations")}
        versions = [version for version, _, _ in self.migrations]
        if versions != sorted(versions) or len(set(versions)) != len(versions):
            raise ValueError("migrations must have increasing, unique versions")
        if applied and max(applied) > max(versions):
            raise RuntimeError(f"the database has schema version {max(applied)}, newer than this program ({max(versions)})")
        for version, description, script in self.migrations:
            if version in applied:
                continue
            with self.write() as tx:
                if callable(script):   # a data migration: it is given the transaction and does its work with SQL statements
                    script(tx)
                else:
                    for statement in _statements(script):
                        tx.execute(statement)
                tx.execute("INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)", (version, description, int(time.time())))
                tx.execute(f"PRAGMA user_version={int(version)}")
        return self.version()

    def version(self):
        with self.read() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    # --- transactions ----------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def write(self):
        """A short write transaction on the one write connection. Raises StorageUnavailable when the database cannot be written."""
        if self._writer is None:
            raise StorageUnavailable("the discovery database is not open")
        with self._write_lock:
            started, retries = self.monotonic(), 0
            while True:
                try:
                    self._writer.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as error:
                    if "locked" in str(error) or "busy" in str(error):
                        if retries < self.write_retries:
                            retries += 1
                            self.sleep(self.retry_sleep * retries)
                            continue
                    self.stats.record(self.monotonic() - started, retries, failed=True)
                    raise StorageUnavailable(str(error)) from error
            try:
                yield self._writer
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    self._writer.execute("ROLLBACK")
                self.stats.record(self.monotonic() - started, retries, failed=True)
                raise
            try:
                self._writer.execute("COMMIT")
            except sqlite3.OperationalError as error:
                with contextlib.suppress(sqlite3.Error):
                    self._writer.execute("ROLLBACK")
                self.stats.record(self.monotonic() - started, retries, failed=True)
                raise StorageUnavailable(str(error)) from error
            self.stats.record(self.monotonic() - started, retries)

    @contextlib.contextmanager
    def read(self):
        """A read-only connection with a consistent view for the duration of the block; closed afterwards."""
        connection = self._connect(readonly=True)
        try:
            connection.execute("BEGIN")
            yield connection
        finally:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            connection.close()

    # --- inspection ------------------------------------------------------------------------------------------------

    def sizes(self):
        def size(path):
            try:
                return os.path.getsize(path)
            except OSError:
                return 0
        return {"database_bytes": size(self.path), "wal_bytes": size(self.path + "-wal")}

    def integrity(self):
        with self.read() as connection:
            return connection.execute("PRAGMA quick_check").fetchone()[0]


SCHEMA_VERSION = LATEST
