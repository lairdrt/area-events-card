"""Dates and time zones for the generic website adapter.

Times that carry a UTC offset are used exactly. A time without one is a wall-clock time at the event, so it needs a
time zone: the zone the page names (an ICS TZID) or else the search area's zone. When no zone can be determined the
event is skipped rather than given a guessed one. IANA zones come from `zoneinfo` when the system has tzdata; when it
does not (a slim container, Windows), the common United States zones are computed from the US daylight-saving rules.
"""
import re
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone, tzinfo

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

US_ZONES = {  # name: (standard offset hours, observes daylight saving)
    "America/New_York": (-5, True), "America/Detroit": (-5, True), "America/Chicago": (-6, True), "America/Denver": (-7, True),
    "America/Boise": (-7, True), "America/Phoenix": (-7, False), "America/Los_Angeles": (-8, True), "America/Anchorage": (-9, True),
    "Pacific/Honolulu": (-10, False), "US/Eastern": (-5, True), "US/Central": (-6, True), "US/Mountain": (-7, True),
    "US/Pacific": (-8, True), "US/Arizona": (-7, False),
}
WHEN = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})(?:[T ](\d{2}):?(\d{2})(?::?(\d{2})(?:[.,]\d+)?)?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$", re.I)


def _nth_sunday(year, month, n):
    first = datetime(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7, weeks=n - 1)


class _UsZone(tzinfo):
    """A United States zone from its standard offset and the DST rules in force since 2007."""

    def __init__(self, name, standard, dst):
        self.name, self.standard, self.observes = name, timedelta(hours=standard), dst

    def _dst(self, wall):
        if not self.observes or wall is None:
            return False
        wall = wall.replace(tzinfo=None)
        return _nth_sunday(wall.year, 3, 2) + timedelta(hours=2) <= wall < _nth_sunday(wall.year, 11, 1) + timedelta(hours=2)

    def utcoffset(self, dt):
        return self.standard + (timedelta(hours=1) if self._dst(dt) else timedelta(0))

    def dst(self, dt):
        return timedelta(hours=1) if self._dst(dt) else timedelta(0)

    def tzname(self, dt):
        return self.name


def zone(name):
    """A tzinfo for an IANA zone name, or None when it cannot be provided."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,2}", name):
        return None
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except (KeyError, ValueError, OSError):
            pass
    if name in US_ZONES:
        return _UsZone(name, *US_ZONES[name])
    return None


def parse_when(value, zone_name=None, fallback_zone=None):
    """(aware datetime, None) or (None, reason) for an ISO 8601 / iCalendar style date-time string."""
    text = str(value or "").strip()
    match = WHEN.match(text)
    if not match and re.match(r"^[A-Za-z]{3},? \d{1,2} [A-Za-z]{3} \d{4} \d{2}:\d{2}", text):  # RFC 822, as RSS writes dates
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None, "invalid_date"
        if moment.tzinfo is not None:
            return moment, None
        tz = zone(zone_name) or zone(fallback_zone)
        return (moment.replace(tzinfo=tz), None) if tz else (None, "no_timezone")
    if not match:
        return None, "invalid_date"
    year, month, day, hour, minute, second, offset = match.groups()
    if hour is None:
        return None, "date_only"
    try:
        naive = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
    except ValueError:
        return None, "invalid_date"
    if offset:
        if offset.upper() == "Z":
            return naive.replace(tzinfo=timezone.utc), None
        sign = -1 if offset[0] == "-" else 1
        digits = offset[1:].replace(":", "")
        delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
        return naive.replace(tzinfo=timezone(sign * delta)), None
    tz = zone(zone_name) or zone(fallback_zone)
    if tz is None:
        return None, "no_timezone"
    return naive.replace(tzinfo=tz), None
