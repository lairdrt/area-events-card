"""What the shared venue resolver (discovery/venues.py) keeps in the one database: each venue's Google Maps answer (matched, with the provider's business
name, street address, coordinates and place id; no_match; or ambiguous) and the Maps searches counted per UTC month, so that a restart neither repeats
searches nor resets the Maps budget. A cache: losing it costs searches, never events.
"""
import math
import re

_KEY = re.compile(r"[0-9a-f]{64}")
_MONTH = re.compile(r"\d{4}-\d{2}")
_STATUSES = ("matched", "no_match", "ambiguous")


def _months(periods):
    periods = tuple(periods)
    if not periods or not all(isinstance(p, str) and _MONTH.fullmatch(p) for p in periods):
        raise ValueError("invalid usage period")
    return periods


def _text(value, limit):
    return value is None or (isinstance(value, str) and 0 < len(value) <= limit)


def _coordinate(value, bound):
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and -bound <= value <= bound)


class SqliteVenueCache:
    """The VenueCache port (persistence/ports.py) on the discovery database."""

    def __init__(self, store):
        self._store = store

    def recall(self, key):
        with self._store.read() as connection:
            row = connection.execute("SELECT status, name, address, latitude, longitude, place_id, evidence, checked_at FROM venue_lookups WHERE lookup_key=?",
                                     (key,)).fetchone()
        return None if row is None else (row[0], row[1], row[2], row[3], row[4], row[5], row[6], int(row[7]))

    def remember(self, key, query, status, name, address, latitude, longitude, place_id, evidence, checked_at):
        if not isinstance(key, str) or not _KEY.fullmatch(key) or not _text(query, 400) or query is None or status not in _STATUSES:
            raise ValueError("invalid venue lookup")
        matched = status == "matched"
        if matched != (name is not None and address is not None) or not _text(name, 300) or not _text(address, 300) or not _text(place_id, 200):
            raise ValueError("invalid venue answer")
        if not _coordinate(latitude, 90) or not _coordinate(longitude, 180) or (latitude is None) != (longitude is None):
            raise ValueError("invalid venue coordinates")
        if not isinstance(evidence, str) or not 0 < len(evidence) <= 600:
            raise ValueError("invalid venue evidence")
        with self._store.write() as tx:
            tx.execute("""INSERT INTO venue_lookups (lookup_key, query, status, name, address, latitude, longitude, place_id, evidence, checked_at)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                          ON CONFLICT(lookup_key) DO UPDATE SET query=excluded.query, status=excluded.status, name=excluded.name, address=excluded.address,
                                                                latitude=excluded.latitude, longitude=excluded.longitude, place_id=excluded.place_id,
                                                                evidence=excluded.evidence, checked_at=excluded.checked_at""",
                       (key, query, status, name, address, latitude, longitude, place_id, evidence, int(checked_at)))

    def requests(self, periods):
        periods = _months(periods)
        with self._store.read() as connection:
            rows = connection.execute(f"SELECT period, requests FROM venue_usage WHERE period IN ({','.join('?' * len(periods))})", periods).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def reserve_request(self, limits):
        """Count one search in each month of `limits` ({month: limit}) and return True, or count nothing and return False when a month has reached its
        limit. One write transaction (BEGIN IMMEDIATE): concurrent callers, in this process or another, cannot together pass a limit."""
        periods = _months(limits)
        if not all(isinstance(limits[p], int) and not isinstance(limits[p], bool) and limits[p] >= 0 for p in periods):
            raise ValueError("invalid usage limit")
        with self._store.write() as tx:
            used = dict(tx.execute(f"SELECT period, requests FROM venue_usage WHERE period IN ({','.join('?' * len(periods))})", periods).fetchall())
            if any(used.get(p, 0) >= limits[p] for p in periods):
                return False
            tx.executemany("""INSERT INTO venue_usage (period, requests) VALUES (?, 1)
                              ON CONFLICT(period) DO UPDATE SET requests=requests + 1""", [(p,) for p in periods])
            return True

    def forget_before(self, checked_before, period_before):
        (period_before,) = _months((period_before,))
        with self._store.write() as tx:
            tx.execute("DELETE FROM venue_lookups WHERE checked_at < ?", (int(checked_before),))
            tx.execute("DELETE FROM venue_usage WHERE period < ?", (period_before,))
