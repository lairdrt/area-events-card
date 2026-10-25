"""LocalDiscoveryReader: the DiscoveryReader contract over the local database.

Each call opens its own read-only connection inside one read transaction, so readers see a consistent state, are isolated from
each other and never block the Discovery Service's writes. A remote reader (later) returns the same records through the Discovery
API; consumers depend only on the contract in contracts.py.

Delivery semantics. `changes(after)` returns the changes after a consumer's checkpoint in sequence order. A consumer upserts by
`entity_key` and `payload_hash` (idempotent), stores its checkpoint with its own results, and may `acknowledge` it. When its
checkpoint is older than the retained history (`after < pruned_through`), the batch says `resync_required` and holds nothing; the
consumer then pages through `snapshot()` and continues with `changes(after=snapshot.as_of_seq)`. Changes made during the snapshot
appear again after `as_of_seq`, so nothing is missed; some records may be delivered twice, which idempotent upserts absorb.
"""
from ..contracts.models import SCHEMA, ChangeBatch, DiscoveryStatus, SnapshotPage


class LocalDiscoveryReader:
    def __init__(self, store, sources, pages, candidates, jobs, changes, clock):
        self._store, self._sources, self._pages, self._candidates = store, sources, pages, candidates
        self._jobs, self._changes, self._clock = jobs, changes, clock

    def changes(self, after_seq, limit=200):
        limit = max(1, min(int(limit), 1000))
        with self._store.read() as connection:
            latest = self._changes.latest(connection)
            pruned = self._changes.pruned_through(connection)
            if int(after_seq) < pruned:
                return ChangeBatch(SCHEMA, (), int(after_seq), latest, resync_required=True, oldest_retained_seq=pruned + 1)
            batch = self._changes.after(connection, int(after_seq), limit)
        return ChangeBatch(SCHEMA, batch, batch[-1].seq if batch else int(after_seq), latest, oldest_retained_seq=pruned + 1)

    def snapshot(self, after_key=None, limit=200, as_of_seq=None):
        limit = max(1, min(int(limit), 1000))
        with self._store.read() as connection:
            as_of = self._changes.latest(connection) if as_of_seq is None else int(as_of_seq)
            records = self._candidates.page_after(connection, after_key, limit)
        return SnapshotPage(SCHEMA, records, as_of, records[-1].candidate_key if len(records) == limit else None)

    def candidate(self, candidate_key):
        with self._store.read() as connection:
            return self._candidates.get(connection, candidate_key)

    def candidates(self, query):
        with self._store.read() as connection:
            return self._candidates.query(connection, query)

    def page(self, url_key, include_body=False):
        with self._store.read() as connection:
            return self._pages.get(connection, url_key, include_body)

    def recent_pages(self, limit=20):
        with self._store.read() as connection:
            return tuple(self._pages.get(connection, key) for key in self._pages.recent(connection, limit))

    def acknowledge(self, consumer, seq):
        with self._store.write() as tx:
            self._changes.acknowledge(tx, consumer, seq, int(self._clock()))

    def status(self, state="idle"):
        with self._store.read() as connection:
            def one(sql):
                return int(connection.execute(sql).fetchone()[0] or 0)
            sources = tuple(self._sources.all(connection))
            status = DiscoveryStatus(
                SCHEMA, int(connection.execute("PRAGMA user_version").fetchone()[0]), state, sources,
                one("SELECT COUNT(*) FROM pages"), one("SELECT COUNT(*) FROM pages WHERE status='ok'"), one("SELECT COUNT(*) FROM candidates"),
                one("SELECT COUNT(*) FROM evidence"), one("SELECT COUNT(*) FROM contents"), one("SELECT COALESCE(SUM(bytes), 0) FROM contents"),
                self._changes.latest(connection), self._jobs.counts(connection),
                max((s.last_success_at for s in sources if s.last_success_at), default=None), **self._store.sizes(), write_stats=self._store.stats.summary())
        return status
