"""Calendar (iCalendar/ICS) and RSS/Atom feeds for the generic website adapter.

Both produce the same candidate dicts as webparse.py. An RSS/Atom entry is an event only when the feed states when
the event happens (an event-module start date, xCal dtstart or Google `when`); a publication date is never used as
an event date. Entries without one are returned as detail links so the adapter can read their pages instead.
"""
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from .webparse import absolute, clean

MAX_VEVENTS = 600
MAX_INSTANCES = 60
DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
IMAGE_EXT = re.compile(r"\.(jpe?g|png|webp|gif|avif)(\?|$)", re.I)


def looks_like_ics(text):
    return text.lstrip("﻿ \r\n\t")[:15].upper().startswith("BEGIN:VCALENDAR")


# --- iCalendar -----------------------------------------------------------------------------------------

def _unfold(text):
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _split(line):
    """(NAME, {PARAM: value}, value) of a content line; colons inside quoted parameters are respected."""
    quoted, cut = False, None
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            cut = index
            break
    if cut is None:
        return None
    head, value = line[:cut], line[cut + 1:]
    name, *raw = head.split(";")
    params = {}
    for item in raw:
        key, _, val = item.partition("=")
        params[key.upper()] = val.strip('"')
    return name.upper(), params, value


def _text(value):
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _stamp(value, params):
    """('2026-10-24T19:00:00[Z]' | '2026-10-24', tz name) for an ICS DATE or DATE-TIME value."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}", None
    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)", value, re.I)
    if not match:
        return None, None
    y, mo, d, h, mi, s, z = match.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}{'Z' if z else ''}", params.get("TZID")


def _datetime(stamp):
    """Naive or UTC-aware datetime for a stamp (None for date-only or invalid)."""
    if not stamp or "T" not in stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _stamp_of(moment):
    return moment.isoformat().replace("+00:00", "Z") if moment.tzinfo else moment.isoformat()


def _weekday(token):
    match = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", token.strip().upper())
    return (int(match.group(1)) if match.group(1) else 0, DAYS.index(match.group(2))) if match else None


def _nth_weekday(year, month, weekday, n):
    first = datetime(year, month, 1)
    if n > 0:
        day = 1 + (weekday - first.weekday()) % 7 + 7 * (n - 1)
    else:
        last = (datetime(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
        day = last.day - (last.weekday() - weekday) % 7 + 7 * (n + 1)
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def occurrences(start, rule, horizon_end):
    """Start times of a simple recurrence (DAILY, WEEKLY, MONTHLY, YEARLY); None when the rule is beyond what is supported."""
    parts = dict(item.split("=", 1) for item in rule.split(";") if "=" in item)
    freq, interval = parts.get("FREQ"), int(parts.get("INTERVAL", "1") or 1)
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY") or interval < 1 or any(k in parts for k in ("BYSETPOS", "BYWEEKNO", "BYYEARDAY", "BYHOUR", "BYMINUTE", "BYMONTH")):
        return None
    count = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else None
    until = _datetime(_stamp(parts["UNTIL"], {})[0]) if parts.get("UNTIL") else None
    if parts.get("UNTIL") and until is None:
        date = _stamp(parts["UNTIL"], {"VALUE": "DATE"})[0]
        until = datetime.fromisoformat(date + "T23:59:59") if date else None
    days = [_weekday(t) for t in parts.get("BYDAY", "").split(",") if t]
    if any(d is None for d in days):
        return None
    monthdays = [int(t) for t in parts.get("BYMONTHDAY", "").split(",") if re.fullmatch(r"-?\d+", t)]
    tzinfo = start.tzinfo
    out, produced = [], 0

    def comparable(moment):
        return moment if (moment.tzinfo is None) == (start.tzinfo is None) else moment.replace(tzinfo=start.tzinfo)

    def candidates(index):
        if freq == "DAILY":
            return [start + timedelta(days=index * interval)]
        if freq == "WEEKLY":
            monday = start - timedelta(days=start.weekday()) + timedelta(weeks=index * interval)
            return [monday + timedelta(days=d[1]) for d in (days or [(0, start.weekday())])]
        if freq == "YEARLY":
            try:
                return [start.replace(year=start.year + index * interval)]
            except ValueError:
                return []
        month_index = start.month - 1 + index * interval
        year, month = start.year + month_index // 12, month_index % 12 + 1
        made = []
        if days:
            for n, weekday in days:
                day = _nth_weekday(year, month, weekday, n or 1)
                if day:
                    made.append(day.replace(hour=start.hour, minute=start.minute, second=start.second, tzinfo=tzinfo))
        else:
            for day in monthdays or [start.day]:
                try:
                    made.append(start.replace(year=year, month=month, day=day if day > 0 else 1))
                except ValueError:
                    continue
        return sorted(made)

    for index in range(2000):
        for moment in sorted(candidates(index)):
            if moment < start:
                continue
            if (until and comparable(moment) > comparable(until)) or (count is not None and produced >= count) or moment.replace(tzinfo=None) > horizon_end:
                return out
            produced += 1
            out.append(moment)
            if len(out) >= MAX_INSTANCES:
                return out
    return out


def parse_ics(text, horizon_end):
    """(candidates, notes) from an iCalendar document. `horizon_end` is a naive datetime bounding recurrence expansion."""
    events, current, depth, calendar_name = [], None, 0, None
    for line in _unfold(text):
        item = _split(line)
        if not item:
            continue
        name, params, value = item
        if name == "BEGIN":
            if value.upper() == "VEVENT" and current is None:
                current = {}
            elif current is not None:
                depth += 1
        elif name == "END":
            if value.upper() == "VEVENT" and current is not None and depth == 0:
                events.append(current)
                current = None
                if len(events) >= MAX_VEVENTS:
                    break
            elif current is not None and depth:
                depth -= 1
        elif current is not None and depth == 0:
            current.setdefault(name, (value, params))
            if name in ("EXDATE", "CATEGORIES"):
                current.setdefault("_" + name, []).append((value, params))
        elif current is None and name == "X-WR-CALNAME":
            calendar_name = _text(value).strip() or None
    overridden = {(e["UID"][0], _stamp(e["RECURRENCE-ID"][0], e["RECURRENCE-ID"][1])[0]) for e in events if "RECURRENCE-ID" in e and "UID" in e}
    candidates, notes = [], {"vevents": len(events), "recurrence_unsupported": 0, "malformed_events": 0, "calendar_name": calendar_name}
    for event in events:
        try:
            found = _event_candidates(event, overridden, horizon_end, notes)
        except (ValueError, TypeError, OverflowError, KeyError):
            notes["malformed_events"] += 1
            continue
        candidates.extend(found)
    return candidates, notes


def _event_candidates(event, overridden, horizon_end, notes):
    candidates = []
    summary = clean(_text(event["SUMMARY"][0])) if "SUMMARY" in event else None
    if "DTSTART" not in event or not summary:
        return candidates
    stamp, tz = _stamp(*event["DTSTART"])
    end_stamp = _stamp(*event["DTEND"])[0] if "DTEND" in event else None
    base = {"kind": "ics", "title": summary, "tz": tz, "end": end_stamp, "uid": event["UID"][0] if "UID" in event else None}
    base.update(_fields(event))
    if (event.get("STATUS", ("",))[0] or "").upper() == "CANCELLED":
        return candidates
    moment = _datetime(stamp)
    starts = [(stamp, end_stamp)]
    if "RRULE" in event and moment is not None and "RECURRENCE-ID" not in event:
        moments = occurrences(moment, event["RRULE"][0], horizon_end)
        if moments is None:
            notes["recurrence_unsupported"] += 1
        else:
            length = (_datetime(end_stamp) - moment) if _datetime(end_stamp) and (_datetime(end_stamp).tzinfo is None) == (moment.tzinfo is None) else None
            if length is not None and length > timedelta(days=2):
                length = None  # one instance of a recurring event does not last days: that end belongs to the whole series
            excluded = set()
            for value, params in event.get("_EXDATE", []):
                for part in value.split(","):
                    excluded.add(_stamp(part, params)[0])
            starts = [(_stamp_of(m), _stamp_of(m + length) if length else None) for m in moments
                      if _stamp_of(m) not in excluded and (base["uid"], _stamp_of(m)) not in overridden]
    for begin, finish in starts:
        candidates.append(dict(base, start=begin, end=finish))
    return candidates


def _fields(event):
    def value(name):
        return _text(event[name][0]).strip() if name in event and event[name][0].strip() else None

    location, venue, address = value("LOCATION"), None, None
    if location:
        parts = [p.strip() for p in location.split(",") if p.strip()]
        if len(parts) >= 3 and re.search(r"\d", parts[1]):
            venue, address = parts[0], ", ".join(parts[1:])
        elif re.search(r"\d", location) and len(parts) >= 2:
            address = location
        else:
            venue = location
    geo = None
    if "GEO" in event:
        try:
            lat, lon = (float(x) for x in event["GEO"][0].split(";"))
            geo = {"latitude": lat, "longitude": lon, "source": "ICS GEO property"}
        except ValueError:
            geo = None
    image = None
    for name in ("ATTACH", "IMAGE"):
        if name in event:
            params, target = event[name][1], event[name][0]
            if params.get("FMTTYPE", "").startswith("image/") or params.get("VALUE", "").upper() == "URI" and IMAGE_EXT.search(target) or IMAGE_EXT.search(target):
                image = absolute("", target)
                break
    organizer = event["ORGANIZER"][1].get("CN") if "ORGANIZER" in event else None
    categories = [c.strip() for value_, _ in event.get("_CATEGORIES", []) for c in _text(value_).split(",") if c.strip()][:6]
    description = clean(value("DESCRIPTION"))
    return {"description": description[:1500] if description else None, "venue": venue, "address": address, "geo": geo,
            "url": absolute("", value("URL")) if value("URL") else None, "image": image, "image_source": "ICS attachment" if image else None,
            "organizer": organizer, "categories": categories, "price": None, "online": False, "cancelled": False}


# --- RSS / Atom ----------------------------------------------------------------------------------------

def _local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def parse_feed(text, base):
    """(candidates, detail_links, title) from an RSS 2.0 / RSS 1.0 / Atom document; ValueError when it is not a feed."""
    head = text.lstrip("﻿ \r\n\t")
    if not head.startswith("<") or re.search(r"<!(DOCTYPE|ENTITY)", text[:5000], re.I):
        raise ValueError("not a feed")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError(f"invalid XML: {error}") from error
    if _local(root.tag) not in ("rss", "feed", "rdf"):
        raise ValueError("not an RSS or Atom feed")
    title = next((clean(e.text) for e in root.iter() if _local(e.tag) == "title" and e.text), None)
    candidates, links = [], []
    for item in root.iter():
        if _local(item.tag) not in ("item", "entry"):
            continue
        fields = {}
        categories, image = [], None
        for child in item:
            name = _local(child.tag)
            if "search.yahoo.com/mrss" in str(child.tag):
                if name in ("content", "thumbnail") and (child.attrib.get("medium") == "image" or child.attrib.get("type", "").startswith("image/") or name == "thumbnail"):
                    image = image or absolute(base, child.attrib.get("url"))
                continue
            if name == "link":
                href = child.attrib.get("href") or (child.text or "").strip()
                if href and (child.attrib.get("rel", "alternate") == "alternate"):
                    fields.setdefault("link", href)
            elif name == "location" and len(child):
                inner = next((g.text for g in child if _local(g.tag) == "name" and (g.text or "").strip()), "")
                fields.setdefault("location", inner)
            elif name in ("title", "description", "summary", "content", "guid", "id", "location"):
                fields.setdefault(name, child.text or "")
            elif name in ("startdate", "dtstart", "eventstart", "startdatetime"):
                fields.setdefault("start", child.text or "")
            elif name in ("enddate", "dtend", "eventend", "enddatetime"):
                fields.setdefault("end", child.text or "")
            elif name == "when" and child.attrib.get("startTime"):
                fields.setdefault("start", child.attrib["startTime"])
                fields.setdefault("end", child.attrib.get("endTime", ""))
            elif name == "where" and child.attrib.get("valueString"):
                fields.setdefault("location", child.attrib["valueString"])
            elif name == "category":
                categories.append(clean(child.attrib.get("term") or child.text))
            elif name == "enclosure" and child.attrib.get("type", "").startswith("image/"):
                image = image or absolute(base, child.attrib.get("url"))
        link = absolute(base, fields.get("link") or (fields.get("guid") if (fields.get("guid") or "").startswith("http") else None))
        heading = clean(fields.get("title"))
        if fields.get("start") and heading:
            summary = clean(fields.get("description") or fields.get("summary") or fields.get("content"))
            candidates.append({"kind": "feed", "title": heading, "start": fields["start"].strip(), "end": (fields.get("end") or "").strip() or None, "tz": None,
                               "description": summary[:1500] if summary else None, "venue": clean(fields.get("location")), "address": None,
                               "url": link, "image": image, "image_source": "feed enclosure" if image else None, "organizer": None, "geo": None,
                               "categories": [c for c in categories if c][:6], "price": None, "online": False, "cancelled": False,
                               "uid": fields.get("guid") or fields.get("id")})
        elif link:
            links.append(link)
    return candidates, list(dict.fromkeys(links)), title
