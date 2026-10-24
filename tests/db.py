"""Test-only access to the database file: plain sqlite3 connections, for inspecting stored rows and for preparing rows the production code would not write.

Production code never does this: the application reaches the database only through Persistence's ports. A test is outside the application, so it may look at the file itself."""
import contextlib
import sqlite3


class Database:
    def __init__(self, path):
        self.path = path

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextlib.contextmanager
    def read(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.execute("ROLLBACK")
            connection.close()

    @contextlib.contextmanager
    def write(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

    def version(self):
        with self.read() as connection:
            return connection.execute("PRAGMA user_version").fetchone()[0]


def database(built):
    """The database file of a built application (or of a Persistence)."""
    persistence = getattr(built, "persistence", built)
    return Database(persistence.path)
