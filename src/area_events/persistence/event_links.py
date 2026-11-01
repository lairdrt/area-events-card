"""What the shared event-page finder (discovery/event_links.py) keeps in the one database: the answer of each lookup (the checked page and the street
address it states, or nothing), and the requests counted per UTC day and month, so that a restart neither repeats searches and page reads nor resets the
free-allowance limits. A cache: losing it costs searches, never events.
"""
import json
import re

_KEY = re.compile(r"[0-9a-f]{64}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_PERIOD = re.compile(r"\d{4}-\d{2}(?:-\d{2})?")   # a UTC day (YYYY-MM-DD) or month (YYYY-MM)


def _periods(periods):
    periods = tuple(periods)
    if not periods or not all(isinstance(p, str) and _PERIOD.fullmatch(p) for p in periods):
        raise ValueError("invalid usage period")
    return periods


class SqliteEventLinkCache:
    """The EventLinkCache port (persistence/ports.py) on the discovery database."""

    def __init__(self, store):
        self._store = store

    def recall(self, key):
        with self._store.read() as connection:
            row = connection.execute("SELECT url, address, address_basis, checked_at, pending, rereads FROM event_link_lookups WHERE lookup_key=?",
                                     (key,)).fetchone()
        if not row:
            return None
        try:
            pending = tuple(tuple(item) for item in json.loads(row[4])) if row[4] else ()
        except (ValueError, TypeError):
            pending = ()                                     # unreadable: as though nothing were waiting
        return row[0], row[1], row[2], int(row[3]), pending, int(row[5])

    def remember(self, key, url, address, address_basis, checked_at, event_day, pending=(), rereads=0):
        """`pending`: (url, kind) of proposed pages that could not be read for a passing reason, to be read again later; `rereads`: how often they were."""
        if not isinstance(key, str) or not _KEY.fullmatch(key) or not isinstance(event_day, str) or not _DAY.fullmatch(event_day):
            raise ValueError("invalid event link lookup")
        if url is not None and (not isinstance(url, str) or not 0 < len(url) <= 2000):
            raise ValueError("invalid event page address")
        if address is not None and (url is None or not isinstance(address, str) or not 0 < len(address) <= 300
                                    or not isinstance(address_basis, str) or not 0 < len(address_basis) <= 100):
            raise ValueError("invalid street address")
        pending = [[page, int(kind)] for page, kind in pending]
        if (pending and url is not None) or len(pending) > 5 or not all(isinstance(page, str) and 0 < len(page) <= 2000 for page, _ in pending):
            raise ValueError("invalid pages to read again")
        if isinstance(rereads, bool) or not isinstance(rereads, int) or rereads < 0:
            raise ValueError("invalid count of page reads")
        with self._store.write() as tx:
            tx.execute("""INSERT INTO event_link_lookups (lookup_key, url, address, address_basis, checked_at, event_day, pending, rereads)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                          ON CONFLICT(lookup_key) DO UPDATE SET url=excluded.url, address=excluded.address, address_basis=excluded.address_basis,
                                                                checked_at=excluded.checked_at, event_day=excluded.event_day,
                                                                pending=excluded.pending, rereads=excluded.rereads""",
                       (key, url, address, address_basis if address is not None else None, int(checked_at), event_day,
                        json.dumps(pending) if pending else None, rereads))

    def requests(self, periods):
        periods = _periods(periods)
        with self._store.read() as connection:
            rows = connection.execute(f"SELECT period, requests FROM event_link_usage WHERE period IN ({','.join('?' * len(periods))})", periods).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def reserve_request(self, limits):
        """Count one request in each period of `limits` ({period: limit}) and return True, or count nothing and return False when any period has
        reached its limit. One write transaction (BEGIN IMMEDIATE): concurrent callers, in this process or another, cannot together pass a limit."""
        periods = _periods(limits)
        if not all(isinstance(limits[p], int) and not isinstance(limits[p], bool) and limits[p] >= 0 for p in periods):
            raise ValueError("invalid usage limit")
        with self._store.write() as tx:
            used = dict(tx.execute(f"SELECT period, requests FROM event_link_usage WHERE period IN ({','.join('?' * len(periods))})", periods).fetchall())
            if any(used.get(p, 0) >= limits[p] for p in periods):
                return False
            tx.executemany("""INSERT INTO event_link_usage (period, requests) VALUES (?, 1)
                              ON CONFLICT(period) DO UPDATE SET requests=requests + 1""", [(p,) for p in periods])
            return True

    def use_up(self, period, requests):
        (period,) = _periods((period,))
        with self._store.write() as tx:
            tx.execute("""INSERT INTO event_link_usage (period, requests) VALUES (?, ?)
                          ON CONFLICT(period) DO UPDATE SET requests=MAX(requests, excluded.requests)""", (period, int(requests)))

    def forget_before(self, day):
        if not isinstance(day, str) or not _DAY.fullmatch(day):
            raise ValueError("invalid day")
        with self._store.write() as tx:
            tx.execute("DELETE FROM event_link_lookups WHERE event_day < ?", (day,))
            tx.execute("DELETE FROM event_link_usage WHERE period < ?", (day[:7],))
