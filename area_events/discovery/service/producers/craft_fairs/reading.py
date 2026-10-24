"""Craft fairs on ordinary websites: reading vendor-calendar cards and organizer pages. Narrow and replaceable; pure functions on HTML and text.

The source-acquisition pilot (`service/acquisition.py`) starts from public listings that vendors keep of the fairs they sell at and follows the
links in those listings to the organizers' own pages. This module is the reading part:

* `listing_entries`: the dated cards of a listing page (a title, a date and clock, and the place the card names, with the card's own links).
* `page_event`: one event an organizer page states about itself (schema.org data when there is any, else a date, a clock and a place written in the text).
* `craft_fair_text` / `region_signal`: is it a craft fair (not a farmers market, a generic fair or a festival), and is it in San Diego County?
* `same_event`: do two descriptions, from different sites, describe one event?

Nothing is invented: an entry needs a calendar date, a start time and a title of its own; a place is only what the card or page writes.
"""
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .... import textloc
from ....textutil import clean_text
from ....timeutil import clock_range
from . import pages
from .pages import DATE, LABEL, NOT_AN_EVENT, TIME, TIME_RANGE

SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "template", "form", "button", "select"})
BLOCK_TAGS = frozenset({"p", "div", "li", "ul", "ol", "tr", "td", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "dd", "dt", "header", "footer", "nav", "aside"})
HEADINGS = ("h1", "h2", "h3", "h4")
OUT_OF_PAGE = frozenset({"nav", "footer", "header", "aside"})
MAX_CARD_TEXT = 1500

# What counts as a craft fair: a fair, show, sale or market of handmade or artisan goods. A farmers market, a flea market, a generic fair or a festival is not.
CRAFT = re.compile(r"\b(?:crafts?\s*(?:fairs?|faires?|shows?|sales?|markets?|bazaars?|festivals?|expos?|boutiques?)|craft\s+and\s+(?:gift|vendor)|crafters?|"
                   r"handmade\s*(?:arts?\s*(?:and|&)\s*crafts?|crafts?|goods|gifts?|items|products|fairs?|markets?|shows?|marketplace)|hand-?crafted\s+(?:fair|market)|"
                   r"arts?\s*(?:and|&)\s*crafts?\s*(?:fairs?|shows?|sales?|markets?)|artisans?\s*(?:alley\s*)?(?:fairs?|faires?|shows?|markets?|marketplace|crafts?|goods|bazaars?)|"
                   r"makers?'?s?\s*(?:markets?|fairs?|faires?|marketplace)|holiday\s+boutique|christmas\s+boutique|craft\s+boutique)\b", re.I)
NOT_CRAFT = re.compile(r"\b(?:farmers?'?\s*markets?|flea\s*markets?|swap\s*meets?|job\s*fairs?|science\s*fairs?|health\s*fairs?|county\s*fairs?|state\s*fairs?|book\s*fairs?|"
                       r"career\s*fairs?|college\s*fairs?)\b", re.I)

# San Diego County, by the names people write: a place counts only when the card or page itself names one (or a 919xx-921xx ZIP code).
COUNTY_PLACES = ("San Diego", "Coronado", "La Mesa", "El Cajon", "Santee", "Chula Vista", "National City", "Lemon Grove", "Poway", "Escondido", "Carlsbad", "Oceanside",
                 "Encinitas", "Vista", "San Marcos", "Del Mar", "Solana Beach", "Imperial Beach", "Rancho Bernardo", "Rancho Santa Fe", "Mira Mesa", "La Jolla", "Clairemont",
                 "Spring Valley", "Lakeside", "Ramona", "Fallbrook", "Alpine", "Bonita", "Cardiff", "Leucadia", "Pacific Beach", "Point Loma", "Scripps Ranch", "Tierrasanta",
                 "Rancho Penasquitos", "Carmel Valley", "Bonsall", "Valley Center", "Julian", "Jamul", "Lakeside", "Mission Valley", "North Park", "Normal Heights", "Ocean Beach")
_COUNTY = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in sorted(set(COUNTY_PLACES), key=len, reverse=True)) + r")\b", re.I)
_ZIP = re.compile(r"\bCA\.?\s+(9(?:19|20|21)\d{2})\b")
GENERIC_TITLE_WORDS = frozenset({"annual", "craft", "crafts", "fair", "faire", "fairs", "show", "sale", "holiday", "market", "markets", "marketplace", "the", "a", "an", "of", "at", "and",
                       "in", "for", "winter", "fall", "spring", "summer", "handmade", "arts", "art", "boutique", "bazaar", "community", "center", "info", "ca", "st", "nd", "rd", "th"})


def craft_fair_text(*texts):
    """True when the text names a craft fair, show, sale or market of handmade or artisan goods and is not a farmers or flea market, a job or science fair, etc."""
    joined = " . ".join(t for t in texts if t)
    return bool(CRAFT.search(joined)) and not NOT_CRAFT.search(joined.split(" . ")[0] if joined else "")


def region_signal(*texts):
    """How the text places the event in San Diego County: a county place name, or a 919xx-921xx ZIP code. Returns the matched text or None."""
    for text in texts:
        if not text:
            continue
        match = _ZIP.search(text) or _COUNTY.search(text)
        if match:
            return match.group(0)
    return None


_FOREIGN = re.compile(r",\s*(?!CA\b)(?:[A-Z]{2}|Texas|Arizona|Nevada|Oregon|Washington|Florida|New York)\.?\s+\d{5}\b")


def foreign_signal(*texts):
    """The text of an address in another state ('Austin, TX 78701'), or None: an event the listing says is in San Diego but whose own card or page puts it elsewhere."""
    for text in texts:
        match = _FOREIGN.search(text or "")
        if match:
            return match.group(0)
    return None


def distinctive_words(title):
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return {w for w in words if w not in GENERIC_TITLE_WORDS and not re.fullmatch(r"20\d{2}|\d+(?:st|nd|rd|th)?", w)}


def street_key(address):
    """'4133 Mt. Albertine Ave' -> '4133 mt albertine': the house number and the first words of the street, enough to tell two addresses apart."""
    match = re.match(r"\s*(\d{1,6})\s+([A-Za-z0-9.'’ ]+)", address or "")
    if not match:
        return None
    street = re.sub(r"\b(street|st|avenue|ave|boulevard|blvd|drive|dr|road|rd|way|lane|ln|court|ct|place|pl)\b\.?", " ", match.group(2).lower())
    return match.group(1) + " " + " ".join(re.findall(r"[a-z0-9]+", street)[:3])


def same_event(a, b):
    """Two descriptions (dicts with 'day', 'title', 'address') describe one event when they fall on the same day and share a street address or most of
    their distinctive title words."""
    if a["day"] != b["day"]:
        return False
    ka, kb = street_key(a.get("address")), street_key(b.get("address"))
    if ka and kb:
        return ka == kb
    wa, wb = distinctive_words(a.get("title")), distinctive_words(b.get("title"))
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.6


# --- text of a block of a page ------------------------------------------------------------------------------

def card_lines(node):
    """The readable lines of an element: a line break at every <br> and block boundary; no scripts, styles or form controls. A decorative ✦ starts a new line."""
    out = [""]

    def walk(parent):
        for child in parent.children:
            if isinstance(child, str):
                out[-1] += child
            elif child.tag in SKIP_TAGS:
                continue
            elif child.tag == "br":
                out.append("")
            else:
                block = child.tag in BLOCK_TAGS
                if block:
                    out.append("")
                walk(child)
                if block:
                    out.append("")

    walk(node)
    lines = []
    for raw in out:
        for part in re.split(r"[✦•●▪◆★]", raw):
            line = re.sub(r"\s+", " ", clean_text(part)).strip("  |–-")
            if line:
                lines.append(line)
    return lines


def _absolute(base, href):
    url = pages.absolute(base, href)
    return url if url and urlsplit(url).scheme in ("http", "https") else None


def _day(line, today):
    match = DATE.search(line)
    if not match or not pages.month_number(match.group("mon")):
        return None, None
    return pages.day_of(pages.month_number(match.group("mon")), match.group("day"), match.group("year"), today), match


def _clock(lines):
    for line in lines[:4]:
        found = TIME_RANGE.search(line)
        if found and TIME.search(line):
            start, end = clock_range(found.group(1).replace(".", "").replace("–", "-").replace("—", "-"))
            if start:
                return start, end
    return None, None


STREETISH = re.compile(r"^\d{1,6}\s+[A-Za-z0-9.'’ ]+\b(?:" + "|".join(textloc.STREET_TYPES) + r")\b\.?(?:\s*,?\s*(?:#|Suite|Ste|Unit)\s*\w+)?", re.I)


LABELED = re.compile(r"^(?:Location|Where|Venue|Place)\s*:\s*", re.I)


def _place(lines, date_index):
    """(venue, address) from the lines after the date: an address is a line that is a street address (with or without its city); the venue is the name
    written before it on the same line ("Seven Oaks Community Center - 16789 Bernardo Oaks Drive") or the short plain line just above. Only what the page writes."""
    after = lines[date_index + 1:date_index + 8]
    for index, line in enumerate(after):
        found = textloc.extract_addresses(line)
        address = found[0]["address"] if found else None
        if not address:
            street = STREETISH.search(line)
            address = street.group(0).rstrip(".") if street and (street.start() == 0 or re.search(r"[-–—,:]\s*$", line[:street.start()])) else None
        if not address:
            continue
        rest = " ".join(after[index + 1:index + 3])
        city = re.match(r"^([A-Z][A-Za-z. ]+),?\s*(?:CA|California)\s*(\d{5})?\b", rest)
        if city and city.group(1).lower() not in address.lower():
            address = f"{address}, {city.group(1).strip()}, CA" + (f" {city.group(2)}" if city.group(2) else "")
        venue = None
        position = line.find(address.split(",")[0])
        before = LABELED.sub("", line[:position]).strip(" -–—,:") if position > 0 else ""
        if before and len(before) <= 80:
            venue = before
        elif index:
            prior = LABELED.sub("", after[index - 1])
            if len(prior) <= 80 and not TIME.search(prior) and not DATE.search(prior) and not re.search(r"[.!?]$", prior) and not STREETISH.match(prior):
                venue = prior
        return venue, address
    return None, None


def listing_entries(html, base_url, today):
    """The dated cards of a listing page. Each is a dict: title, day (date), start/end ((h, m) or None), venue, address, description, links [(url, text)],
    evidence_text. A card is the innermost list item or article that holds one calendar date; a card with no clock time or no title is not an entry."""
    page = pages.read_page(html, base_url)
    cards = []
    for n in page.root.walk():
        if n.tag in ("li", "article"):
            text = n.text()
            if len(text) < 4000 and DATE.search(text):
                cards.append(n)
    inner = [n for n in cards if not any(c is not n and c in cards and _inside(c, n) for c in cards)]
    entries, seen = [], set()
    for card in inner:
        lines = card_lines(card)
        index = next((i for i, line in enumerate(lines) if _day(line, today)[0]), None)
        if index is None:
            continue
        day, match = _day(lines[index], today)
        if day is None or day < today:
            continue
        start, end = _clock(lines[index:index + 4])
        heading = next((card_lines(n)[0] for n in card.walk() if n.tag in HEADINGS and card_lines(n) and not DATE.search(card_lines(n)[0])), None)
        title = heading or (lines[index - 1] if index else None)
        if not start or not title or not pages.valid_title(title):
            continue
        venue, address = _place(lines, index)
        used = {title, lines[index]}
        description = " ".join(line for line in lines if line not in used and len(line) >= 40 and line != venue and line != address)[:400] or None
        links = []
        for n in card.walk():
            if n.tag == "a" and n.attrs.get("href"):
                url = _absolute(base_url, n.attrs["href"].strip())
                if url and all(url != known for known, _ in links):
                    links.append((url, n.text()[:80]))
        key = (title.casefold(), day)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"title": re.sub(r"\s+", " ", clean_text(title)).strip(), "day": day, "start": start, "end": end, "venue": venue, "address": address,
                        "description": description, "links": links, "evidence_text": " ".join(lines)[:600]})
    return entries


def _inside(node, ancestor):
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


def _tidy_address(address):
    """A source sometimes repeats the city ('3812 29th Street, San Diego, CA 92104, San Diego, CA'): the repeat is dropped."""
    text = re.sub(r"\s+", " ", clean_text(address or "")).strip(" ,")
    parts = [p.strip() for p in text.split(",")]
    for size in (2, 1):
        if len(parts) > 2 * size and [p.lower() for p in parts[-size:]] == [p.lower() for p in parts[-2 * size:-size]]:
            parts = parts[:-size]
    match = re.match(r"^(.*?,\s*[A-Za-z .]+,\s*CA(?:\s*\d{5})?),\s*[A-Za-z .]+,\s*CA$", ", ".join(parts))
    return (match.group(1) if match else ", ".join(parts)) or None


def _clean_title(text, site=None):
    title = re.sub(r"\s+", " ", clean_text(text or "")).strip()
    for sep in (" | ", " – ", " — ", " - "):
        if sep in title:
            first = title.split(sep)[0].strip()
            if len(first) >= 8:
                title = first
                break
    return re.sub(r"^20\d{2}\s+", "", title).strip()


def page_event(html, url, today):
    """The event an organizer page states about itself, or None. Structured (schema.org) data first, else the first upcoming date with a clock time in the
    page's main text. A dict: title, start (aware datetime), end, venue, address, description, image, organizer, method, evidence_text."""
    page = pages.read_page(html, url)
    today = today.date() if isinstance(today, datetime) else today
    now = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    image = page.meta.get("og:image") or None
    image = _absolute(url, image) if image else None
    for candidate in page.events:
        start = pages.when(candidate.get("start"))
        if start is None or start < now:
            continue
        end = pages.when(candidate.get("end")) if candidate.get("end") else None
        return {"title": _clean_title(candidate.get("title")), "start": start, "end": end if end and end >= start else None, "venue": clean_text(candidate.get("venue") or "") or None,
                "address": _tidy_address(candidate.get("address")), "description": (clean_text(candidate.get("description") or "")[:400] or None),
                "image": candidate.get("image") or image, "organizer": candidate.get("organizer"), "method": "jsonld",
                "evidence_text": f"{candidate.get('title')} {candidate.get('start')}"}
    body = next((n for n in page.root.walk() if n.tag == "body"), page.root)
    lines = _main_lines(body)
    for index, line in enumerate(lines):
        if NOT_AN_EVENT.search(line) or len(line) > 300:
            continue
        day, match = _day(line, today)
        if day is None or day < today:
            continue
        start, end = _clock([line] + lines[index + 1:index + 3])
        if not start:
            continue
        title = _page_title(page, lines)
        if not title:
            continue
        venue, address = _place(lines, index)
        label = LABEL.search(" ".join(lines[index:index + 3]))
        if label and not venue:
            text = label.group(1)
            found = textloc.extract_addresses(text)
            venue = (text[:text.find(found[0]["address"])] if found and found[0]["address"] in text else text).strip(" ,.-") or None
            address = address or (found[0]["address"] if found else None)
        naive_start = datetime(day.year, day.month, day.day, start[0], start[1])
        naive_end = datetime(day.year, day.month, day.day, end[0], end[1]) if end else None
        description = page.meta.get("og:description") or page.meta.get("description")
        return {"title": title, "naive_start": naive_start, "naive_end": naive_end if naive_end and naive_end > naive_start else None, "venue": venue, "address": address,
                "description": (clean_text(description)[:400] if description else None), "image": image, "organizer": page.site_name, "method": "text",
                "evidence_text": " ".join(lines[max(0, index - 1):index + 4])[:400]}
    return None


def page_summary(html, url):
    """What a page is about, for deciding whether to keep it: its name, whether its headings or title name a craft fair, and the county place it states."""
    page = pages.read_page(html, url)
    body = next((n for n in page.root.walk() if n.tag == "body"), page.root)
    lines = _main_lines(body)
    names = [page.meta.get("og:title"), page.title, page.meta.get("og:description"), page.meta.get("description")] + [n.text() for n in page.root.walk() if n.tag in HEADINGS][:15]
    text = " ".join(lines)[:30000]
    everything = " ".join(card_lines(body))[:60000]   # a street address is often in the footer: it places the page, though it is not part of the event's text
    return {"title": _clean_title(page.meta.get("og:title") or page.title or ""), "site_name": page.site_name,
            "craft": any(craft_fair_text(name) for name in names if name), "region": region_signal(text, everything), "foreign": foreign_signal(text, everything),
            "words": frozenset(re.findall(r"[a-z0-9]+", " ".join([text] + [n for n in names if n]).lower()))}


def _page_title(page, lines):
    """The page's own name for the event: a heading that names a craft fair, else the Open Graph or document title (the site name after '|' is dropped)."""
    for n in page.root.walk():
        if n.tag in ("h1", "h2", "h3") and n.text() and craft_fair_text(n.text()) and len(n.text()) <= 100:
            return _clean_title(n.text())
    for candidate in (page.meta.get("og:title"), page.title):
        if candidate and craft_fair_text(candidate):
            return _clean_title(candidate)
    return None


def _main_lines(body):
    """Lines of the page's content, in order, without navigation, headers, footers and sidebars."""
    lines = []

    def visit(node):
        if node.tag in OUT_OF_PAGE or node.tag in SKIP_TAGS:
            return
        if not any(not isinstance(c, str) and c.tag in BLOCK_TAGS for c in node.children):
            lines.extend(card_lines(node))
            return
        buffered = ""
        for child in node.children:
            if isinstance(child, str):
                buffered += child
                continue
            if buffered.strip():
                lines.append(re.sub(r"\s+", " ", buffered).strip())
            buffered = ""
            visit(child)
        if buffered.strip():
            lines.append(re.sub(r"\s+", " ", buffered).strip())

    visit(body)
    seen, out = set(), []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out
