"""Pacific time handling without tzdata (not installed on Windows), and clock-time text. Neutral: no dependency on any source, engine or pipeline."""
import re
from datetime import datetime, timedelta, timezone

TIMEZONE_NAME = "America/Los_Angeles"


def _nth_sunday(year, month, n):
    day = datetime(year, month, 1)
    first_sunday = day + timedelta(days=(6 - day.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


def pacific_offset(utc):
    """UTC offset of America/Los_Angeles at a UTC instant (US rules since 2007)."""
    naive = utc.replace(tzinfo=None)
    dst_start = _nth_sunday(naive.year, 3, 2) + timedelta(hours=10)  # 02:00 PST
    dst_end = _nth_sunday(naive.year, 11, 1) + timedelta(hours=9)  # 02:00 PDT
    return timedelta(hours=-7 if dst_start <= naive < dst_end else -8)


def to_pacific(utc):
    return utc.astimezone(timezone(pacific_offset(utc)))


def parse_compact_utc(value):
    """'20261020T233000Z' -> aware UTC datetime."""
    return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def localize_pacific(naive):
    """Naive wall-clock time in America/Los_Angeles -> aware datetime."""
    for hours in (-7, -8):
        candidate = naive.replace(tzinfo=timezone(timedelta(hours=hours)))
        if pacific_offset(candidate.astimezone(timezone.utc)) == timedelta(hours=hours):
            return candidate
    return naive.replace(tzinfo=timezone(timedelta(hours=-8)))  # nonexistent hour


def parse_clock(text, default_meridiem=None):
    """'5pm', '4:30 PM', '6:30' (with default_meridiem) -> (hour, minute) or None."""
    match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?m?\.?\s*", text.lower().replace("noon", "12pm"))
    if not match:
        return None
    meridiem = match.group(3) or default_meridiem
    if not meridiem:
        return None
    return int(match.group(1)) % 12 + (12 if meridiem == "p" else 0), int(match.group(2) or 0)


def clock_range(text):
    """'2 - 7pm', '9:30am - 1:30pm', '8 pm', '7:30 pm - 11:30 pm' -> (start, end|None) as (h, m)."""
    parts = [p.strip() for p in re.split(r"\s*(?:-|–|—|&ndash;|to)\s*", text.strip()) if p.strip()]
    if not parts or len(parts) > 2:
        return None, None
    end_meridiem = re.search(r"([ap])\.?m", parts[-1].lower())
    end = parse_clock(parts[-1])
    start = parse_clock(parts[0], end_meridiem.group(1) if end_meridiem else None)
    if start and len(parts) == 2 and end and not re.search(r"[ap]\.?m", parts[0].lower()) and start > end:
        start = (start[0] - 12, start[1])  # '11 - 1pm' means 11am
    return start, (end if len(parts) == 2 else None)


def now_pacific():
    return to_pacific(datetime.now(tz=timezone.utc))
