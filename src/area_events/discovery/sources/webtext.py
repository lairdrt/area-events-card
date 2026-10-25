"""Events written in ordinary page or document text, and text from PDF documents.

The least reliable stage of the website adapter, so it is deliberately conservative. A line (or a line with the one after it) becomes an
event only when it states a calendar date (a month and day, or a numeric date) **and** a start time, has a title of its own, and is not
a posting date, a deadline or similar. A date without a year is read as this year; one that has already passed is not rolled into next
year (it is history), except in the last weeks of a year for a date early in the next. Nothing about the place is inherited from the page:
a venue is only an explicit "Location:"/"Where:" label next to the event, an address only one written in that same text.

`pdf_text` reads the text of simple, text-based PDFs with the standard library alone (Flate-compressed content streams, literal and
hexadecimal strings). A PDF made of scanned images, or fonts with their own encodings, yields no text and is reported as such, never guessed.
"""
import re
import zlib
from datetime import date, datetime, timedelta

from ...geolocation import textloc
from ..core import clean_text, clock_range

MAX_BLOCK = 400
MAX_TITLE = 120
MAX_STREAMS = 300
MAX_INFLATED = 2_000_000
MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
_MONTH = r"(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_WEEKDAY = r"(?:(?:Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)[a-z]*\.?,?\s+)?"
DATE = re.compile(r"\b" + _WEEKDAY + _MONTH + r"\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b(?:\s*,?\s*(?P<year>20\d{2})\b)?", re.I)
NUMERIC_DATE = re.compile(r"\b" + _WEEKDAY + r"(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>20\d{2}|\d{2}))?\b", re.I)
TIME = re.compile(r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?\s?m\b\.?|\bnoon\b", re.I)
TIME_RANGE = re.compile(r"(\d{1,2}(?::\d{2})?\s*(?:[ap]\.?\s?m\.?)?\s*(?:-|–|—|to)\s*\d{1,2}(?::\d{2})?\s*[ap]\.?\s?m\.?|\d{1,2}(?::\d{2})?\s*[ap]\.?\s?m\.?|noon)", re.I)
NOT_AN_EVENT = re.compile(r"\b(posted|published|updated|copyright|deadline|due by|due on|closes|closed|expires|registration closes|last updated|archive)\b|©", re.I)
NAVIGATION = {"calendar", "events", "event", "home", "upcoming events", "read more", "more", "learn more", "view all", "see all", "menu", "search", "news",
              "contact", "about", "donate", "give", "login", "sign in", "register", "subscribe", "today", "next", "previous", "back"}
LABEL_START = re.compile(r"^(?:Location|Where|Venue|Place|Time|Date|Cost|Contact|Admission|Price|Details?)\s*:", re.I)
LABEL = re.compile(r"\b(?:Location|Where|Venue|Place)\s*:\s*([^|•\n]{3,90}?)(?=\s{2,}|\s*[|•]|$)", re.I)


def _month_number(name):
    name = name.lower().rstrip(".")
    for index, full in enumerate(MONTH_NAMES, 1):
        if full.startswith(name[:3]):
            return index
    return None


def _day_of(match_month, match_day, match_year, today):
    """The calendar date a month/day (and perhaps year) names, as a date; None when it is not a real one or is history."""
    month, day = match_month, int(match_day)
    year = int(match_year) if match_year else None
    if len(str(match_year or "")) == 2:
        year = 2000 + int(match_year)
    try:
        if year is None:
            candidate = date(today.year, month, day)
            if candidate < today - timedelta(days=1):
                following = date(today.year + 1, month, day)
                # a date that has passed this year is history, unless the year is about to turn and the date is early in the next one
                return following if (following - today).days <= 120 else None
            return candidate
        return date(year, month, day)
    except ValueError:
        return None


def _title(block, spans):
    text = block
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " + text[end:]
    text = re.sub(r"\b(?:at|from|on|@|until|starts?|doors?|begins?)\b\s*$", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" \t-–—|:@,;•()[]")
    text = re.sub(r"^(?:and|&)\s+", "", text)
    return text


def valid_title(title):
    if not title or len(title) < 4 or len(title) > MAX_TITLE:
        return False
    if title.lower().strip(".:!") in NAVIGATION:
        return False
    if not re.search(r"[A-Za-z]{3,}", title) or sum(ch.isdigit() for ch in title) > len(title) / 2:
        return False
    return not NOT_AN_EVENT.search(title)


def events_in(lines, today, source_url=None):
    """Candidates (the website adapter's format, kind "text") from an ordered list of text lines."""
    found, seen = [], set()
    lines = [re.sub(r"\s+", " ", clean_text(line or "")).strip() for line in lines]
    for index, line in enumerate(lines):
        if not line or len(line) > MAX_BLOCK or NOT_AN_EVENT.search(line):
            continue
        spans, day = [], None
        match = DATE.search(line)
        if match and _month_number(match.group("mon")):
            day = _day_of(_month_number(match.group("mon")), match.group("day"), match.group("year"), today)
            spans.append(match.span())
        else:
            numeric = NUMERIC_DATE.search(line)
            if numeric and 1 <= int(numeric.group("m")) <= 12:
                day = _day_of(int(numeric.group("m")), numeric.group("d"), numeric.group("y"), today)
                spans.append(numeric.span())
        if day is None or day < today:
            continue
        block = line
        times = TIME.search(line)
        if not times and index + 1 < len(lines) and lines[index + 1] and len(lines[index + 1]) <= 160 and not DATE.search(lines[index + 1]):
            nxt = lines[index + 1]
            if TIME.search(nxt) and len(line) <= 120:
                block = line + " " + nxt   # "Saturday, Nov 8" on one line and "7:00 pm Fall concert" on the next
                times = TIME.search(block)
        if not times:
            continue
        after = spans[0][1] if spans and spans[0][1] <= times.start() else 0   # the clock is read after the date, so a year is not taken for an hour
        span = TIME_RANGE.search(block, after)
        clock_text = span.group(1) if span else times.group(0)
        start_hm, end_hm = clock_range(clock_text.replace(".", "").replace("–", "-").replace("—", "-"))
        if not start_hm:
            continue
        spans_all = list(spans)
        for m in TIME_RANGE.finditer(block):
            spans_all.append(m.span())
        # drop the date's own span if the block was extended
        addresses = textloc.extract_addresses(block)
        title = _title(block, [s for s in spans_all if s[1] <= len(block)])
        for item in addresses:
            title = re.sub(r"\s*(?:at|@)?\s*" + re.escape(item["address"]), " ", title).strip(" -,;")
        if not valid_title(title) or LABEL_START.match(title):
            neighbours = [lines[index + 1] if index + 1 < len(lines) else "", lines[index - 1] if index else ""]
            title = next((n for n in neighbours if n and block != line + " " + n and not DATE.search(n) and not TIME.search(n) and not LABEL_START.match(n)
                          and valid_title(n) and len(n) <= 100), None)
            if title is None:
                continue
        start = datetime(day.year, day.month, day.day, start_hm[0], start_hm[1])
        end = datetime(day.year, day.month, day.day, end_hm[0], end_hm[1]) if end_hm else None
        if end is not None and end <= start:
            end = None
        key = (title.casefold(), start)
        if key in seen:
            continue
        seen.add(key)
        label = LABEL.search(block) or (LABEL.search(lines[index + 1]) if index + 1 < len(lines) else None)
        found.append({
            "kind": "text", "evidence_text": block, "title": title, "start": start.strftime("%Y-%m-%dT%H:%M"), "end": end.strftime("%Y-%m-%dT%H:%M") if end else None, "tz": None,
            "description": None, "venue": clean_text(label.group(1)).strip(" .") if label else None,
            "address": addresses[0]["address"] if len(addresses) == 1 else None, "url": source_url, "image": None, "image_source": None,
            "organizer": None, "geo": None, "categories": [], "price": None, "online": False, "cancelled": False, "uid": None,
        })
    return found


# --- PDF text ---------------------------------------------------------------------------------------------

_STRING = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>")
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}
_NUM = rb"[-+]?\d*\.?\d+"
_OPERATION = re.compile(
    rb"/([A-Za-z0-9_.+-]+)\s+" + _NUM + rb"\s+Tf"                                    # 1: the font in use
    rb"|\[(.*?)\]\s*TJ"                                                               # 2: strings and spacing
    rb"|(\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>)\s*(?:Tj|'|\")"                        # 3: one string
    rb"|(T\*)"                                                                        # 4: next line
    rb"|(" + _NUM + rb")\s+(" + _NUM + rb")\s+(?:Td|TD)\b"                            # 5, 6: move by dx, dy
    rb"|(?:" + _NUM + rb"\s+){5}(" + _NUM + rb")\s+Tm\b",                             # 7: text matrix: the vertical position
    re.S)


def _bytes_of(token):
    """The raw bytes a PDF string token stands for."""
    body = token[1:-1]
    if token.startswith(b"<"):
        digits = re.sub(rb"\s", b"", body)
        if len(digits) % 2:
            digits += b"0"
        return bytes.fromhex(digits.decode("ascii")) if digits else b""
    out, i = bytearray(), 0
    while i < len(body):
        ch = body[i:i + 1]
        if ch == b"\\" and i + 1 < len(body):
            nxt = body[i + 1:i + 2]
            if nxt in _ESCAPES:
                out += _ESCAPES[nxt]
                i += 2
            elif nxt.isdigit():
                digits = re.match(rb"[0-7]{1,3}", body[i + 1:])
                out.append(int(digits.group(0), 8) & 255)
                i += 1 + len(digits.group(0))
            else:
                i += 2
        else:
            out += ch
            i += 1
    return bytes(out)


def _cmap(text):
    """{code: text} from a ToUnicode CMap (bfchar and bfrange entries)."""
    table = {}

    def unicode_of(hexstring):
        raw = bytes.fromhex(hexstring)
        return raw.decode("utf-16-be", "ignore") if len(raw) % 2 == 0 else raw.decode("latin-1")

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for code, target in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            table[int(code, 16)] = unicode_of(target)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for low, high, rest in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", block):
            first, last = int(low, 16), int(high, 16)
            if last < first or last - first > 4096:
                continue
            if rest.startswith("["):
                for offset, target in enumerate(re.findall(r"<([0-9A-Fa-f]+)>", rest)[:last - first + 1]):
                    table[first + offset] = unicode_of(target)
            else:
                base = int(rest[1:-1], 16)
                width = len(rest) - 2
                for offset in range(last - first + 1):
                    table[first + offset] = unicode_of(f"{base + offset:0{width}x}")
    return table


def _codes(raw):
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw) - 1, 2)]


def _best_cmap(samples, cmaps):
    """The ToUnicode table that explains most of a font's two-byte codes (all of them nearly), else None."""
    best, best_share = None, 0.0
    codes = [c for raw in samples for c in _codes(raw)]
    if not codes:
        return None
    for table in cmaps:
        share = sum(1 for c in codes if c in table) / len(codes)
        if share > best_share:
            best, best_share = table, share
    return best if best_share >= 0.9 else None


def _content_lines(content, cmaps):
    """Text lines from one PDF content stream. A font whose strings are two-byte codes is decoded with the ToUnicode table that fits them."""
    runs, font = [], None
    for block in re.findall(rb"BT(.*?)ET", content, re.S):   # a text object starts a new line (its position is set outside it)
        y, last_y = 0.0, None
        for token in _OPERATION.finditer(block):
            if token.group(1):
                font = token.group(1)
            elif token.group(4):
                y -= 12.0
            elif token.group(6) is not None:
                y += float(token.group(6))
            elif token.group(7) is not None:
                y = float(token.group(7))
            else:
                pieces = _STRING.findall(token.group(2)) if token.group(2) is not None else [token.group(3)]
                runs.append((last_y is None or abs(y - last_y) > 0.5, font, [_bytes_of(p) for p in pieces], [p.startswith(b"<") for p in pieces]))
                last_y = y
    by_font = {}
    for _, name, raws, hexes in runs:
        for raw, is_hex in zip(raws, hexes):
            if is_hex and len(raw) % 2 == 0 and len(raw) >= 2:
                by_font.setdefault(name, []).append(raw)
    tables = {name: _best_cmap(samples, cmaps) for name, samples in by_font.items()} if cmaps else {}
    lines, current = [], []
    for newline, name, raws, hexes in runs:
        if newline and current:
            lines.append("".join(current))
            current = []
        for raw, is_hex in zip(raws, hexes):
            table = tables.get(name)
            if table is not None and is_hex:
                current.append("".join(table.get(code, "") for code in _codes(raw)))
            else:
                current.append(raw.decode("latin-1"))
    if current:
        lines.append("".join(current))
    return lines


def pdf_text(data):
    """(lines, reason): the text of a text-based PDF, or ([], why there is none)."""
    if not data.startswith(b"%PDF"):
        return [], "not a PDF"
    if b"/Encrypt" in data[-2000:] or b"/Encrypt" in data[:2000]:
        return [], "encrypted"
    cmaps, contents, count = [], [], 0
    for match in re.finditer(rb"<<(.*?)>>\s*stream\r?\n(.*?)\r?\nendstream", data, re.S):
        count += 1
        if count > MAX_STREAMS:
            break
        header, body = match.group(1), match.group(2)
        if b"/Image" in header or b"/FontFile" in header or b"/XRef" in header:
            continue
        if b"/FlateDecode" in header:
            try:
                body = zlib.decompressobj().decompress(body, MAX_INFLATED)
            except zlib.error:
                continue
        elif b"/Filter" in header:
            continue
        if b"beginbfchar" in body or b"beginbfrange" in body:
            cmaps.append(_cmap(body.decode("latin-1")))
        elif b"/ObjStm" not in header and b"BT" in body and b" ET" in body or b"\nET" in body:
            contents.append(body)
    lines = []
    for body in contents:
        lines += _content_lines(body, cmaps)
    text = "\n".join(lines)
    letters = sum(ch.isalpha() for ch in text)
    printable = sum(ch.isprintable() or ch in "\n\t" for ch in text)
    if len(text.strip()) < 40:
        return [], "no text (a scanned or image-only document)"
    if letters < 0.4 * len(text) or printable < 0.95 * len(text):
        return [], "text uses an encoding that is not readable without a font table"
    return [ln for ln in (re.sub(r"\s+", " ", x).strip() for x in lines) if ln], None
