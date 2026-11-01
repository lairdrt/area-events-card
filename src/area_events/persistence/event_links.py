"""What the shared event-page finder (discovery/event_links.py) keeps in the one database: the answer of each lookup, and the requests counted per
UTC day and month, so that a restart neither repeats searches nor resets the free-allowance limits. A cache: losing it costs searches, never events.
"""
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
            row = connection.execute("SELECT url, checked_at FROM event_link_lookups WHERE lookup_key=?", (key,)).fetchone()
        return (row[0], int(row[1])) if row else None

    def remember(self, key, url, checked_at, event_day):
        if not isinstance(key, str) or not _KEY.fullmatch(key) or not isinstance(event_day, str) or not _DAY.fullmatch(event_day):
            raise ValueError("invalid event link lookup")
        if url is not None and (not isinstance(url, str) or not 0 < len(url) <= 2000):
            raise ValueError("invalid event page address")
        with self._store.write() as tx:
            tx.execute("""INSERT INTO event_link_lookups (lookup_key, url, checked_at, event_day) VALUES (?, ?, ?, ?)
                          ON CONFLICT(lookup_key) DO UPDATE SET url=excluded.url, checked_at=excluded.checked_at, event_day=excluded.event_day""",
                       (key, url, int(checked_at), event_day))

    def requests(self, periods):
        periods = _periods(periods)
        with self._store.read() as connection:
            rows = connection.execute(f"SELECT period, requests FROM event_link_usage WHERE period IN ({','.join('?' * len(periods))})", periods).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def count_request(self, periods):
        periods = _periods(periods)
        with self._store.write() as tx:
            tx.executemany("""INSERT INTO event_link_usage (period, requests) VALUES (?, 1)
                              ON CONFLICT(period) DO UPDATE SET requests=requests + 1""", [(p,) for p in periods])

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
