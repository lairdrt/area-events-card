"""Places an event's own text names, found without any lookup: a full street address inside the venue or description, a named park or
centre at the start of a venue sentence, a "corner of A and B" phrase, and room-number-free forms of a venue name. Pure functions on text.

Nothing here decides that a place is right: each result is only a *candidate* that the location verifier (media.py) must still confirm
against the geocoder (house number and street, exact name, shared road node) before a pin is drawn."""
import html
import re

STREET_TYPES = ("Street", "St", "Avenue", "Ave", "Boulevard", "Blvd", "Drive", "Dr", "Road", "Rd", "Way", "Lane", "Ln", "Court", "Ct", "Place", "Pl",
                "Highway", "Hwy", "Parkway", "Pkwy", "Circle", "Cir", "Terrace", "Ter", "Trail", "Plaza", "Square")
_TYPE = r"(?:" + "|".join(STREET_TYPES) + r")"
_WORD = r"(?:[A-Z][\w'’-]*\.?|\d{1,3}(?:st|nd|rd|th))"
_DIR = r"(?:N|S|E|W|NE|NW|SE|SW)"
_MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
           "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
_ADDRESS = re.compile(
    r"(?<![\w$#:/.,-])(?P<number>\d{1,5})\s+"
    r"(?P<street>(?:" + _DIR + r"\.?\s+)?(?:" + _WORD + r"\s+){0,3}?" + _WORD + r"\s+" + _TYPE + r"\b\.?(?:\s+" + _DIR + r"\b\.?)?)"
    r"(?:,?\s*(?:Suite|Ste\.?|Unit|Apt\.?|#)\s*[\w-]+)?"
    r"(?:,\s*(?P<city>[A-Z][A-Za-z.'’-]*(?:\s+[A-Z][A-Za-z.'’-]*){0,2}))?"
    r"(?:,?\s+(?P<state>[A-Z]{2})\b(?:\s+(?P<zip>\d{5}))?)?")
_AT_BARE = re.compile(r"\bat\s+(?P<number>\d{3,5})\s+(?P<street>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b(?!\s+(?:am|pm|a\.m|p\.m)\b)")
_PLACE_WORDS = (r"Park|Plaza|Beach|Pier|Recreation Center|Rec Center|Community Center|Library|Museum|Gardens?|Zoo|Stadium|Arena|Amphitheat(?:er|re)|"
                r"Theat(?:er|re)|Cove|Marina|Campus|Center|Hall|Pavilion|Fairgrounds|Preserve|Reserve|Playground|Skate Park")
_NAME_CORE = (r"(?:The\s+)?[A-Z][\w'’.&-]*(?:\s+(?:of|de|la|del|&|[A-Z][\w'’.&-]*)){0,5}\s+(?:" + _PLACE_WORDS + r")\b")
_NAME_FIRST = re.compile(r"^\s*(?P<name>" + _NAME_CORE + r")")
_NAME_BEFORE = re.compile(r"(?P<name>" + _NAME_CORE + r")[\s,;:-]*$")
_CORNER = re.compile(r"\b(?:corner|intersection|junction)\s+of\s+(?P<a>[^.;,()]{3,50}?)\s+(?:and|&)\s+(?P<b>[^.;,()]{3,50}?)(?=\s*(?:[.;,()]|$|\s(?:in|on|at|near|between|along|from|where|with|is)\b))", re.I)


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(str(text or ""))).strip()


def is_prose(venue):
    """Is a venue field a sentence about the place rather than the place's name? (Such text is not looked up as a name.)"""
    text = clean(venue)
    words = text.split()
    verbs = re.search(r"\b(is|are|will|located|takes? place|operates|between|along)\b", text)
    return len(words) > 12 or (bool(verbs) and len(words) > 5) or (text.endswith(".") and len(words) > 5)


def extract_addresses(text, known_cities=()):
    """Distinct full street addresses written in `text`, in order: [{"address", "number", "name_before"}]. A city counts only when a state code
    follows it or it is one of `known_cities` (so "Drive, October 22" is not read as a place); an address of the form "at 4325 Wightman"
    (a number and a street name with no street type) is accepted only after the word "at"."""
    text = clean(text)
    cities = {c.lower() for c in known_cities}
    found, seen = [], set()

    def add(number, street, city, state, zip_code, start):
        core = re.sub(r"[^a-z0-9 ]", "", street.lower()).split()
        key = (number, tuple(core))
        if key in seen:
            return
        seen.add(key)
        address = f"{number} {street.strip()}"
        if city:
            address += f", {city}"
        if state:
            address += f", {state}" + (f" {zip_code}" if zip_code else "")
        before = _NAME_BEFORE.search(text[:start])
        found.append({"address": address, "number": number, "name_before": clean(before.group("name")) if before else None, "start": start})

    for m in _ADDRESS.finditer(text):
        city, state, zip_code = m.group("city"), m.group("state"), m.group("zip")
        if city and (city.lower() in _MONTHS or not (state or city.lower() in cities)):
            city = state = zip_code = None  # the words after the comma were not a place
        elif city is None and state is None:
            zip_code = None
        add(m.group("number"), m.group("street"), city, state, zip_code, m.start())
    for m in _AT_BARE.finditer(text):
        if not any(m.group("number") == f["number"] for f in found):
            add(m.group("number"), m.group("street"), None, None, None, m.start())
    return [{k: v for k, v in f.items() if k != "start"} for f in found]


def named_place(text):
    """A park, centre or similar named at the very start of a venue sentence ("De Anza Cove Park is a scenic waterfront park…"), or None."""
    match = _NAME_FIRST.match(clean(text))
    return clean(match.group("name")) if match else None


def corner_phrases(text):
    """Street pairs from phrases like "the intersection of 29th Street and North Park Way": ["29th Street and North Park Way", ...]."""
    return [f"{clean(m.group('a'))} and {clean(m.group('b'))}" for m in _CORNER.finditer(clean(text))]


def without_rooms(venue):
    """Forms of a venue name with a room, suite or floor suffix removed ("SEAY Library - Room 4.244" -> "SEAY Library"); [] when nothing changes."""
    text = clean(venue)
    out = []
    for pattern in (r"\s+[-–—]\s+.*$", r"\s*,\s*(?:Room|Rm|Suite|Ste|Floor|Level)\b.*$", r"\s+(?:Room|Rm|Suite|Ste)\s+[\w.-]+.*$", r"\s+\d+(?:\.\d+)?[A-Za-z]?\s*$"):
        stripped = re.sub(pattern, "", text, flags=re.I).strip(" ,-")
        if (stripped and stripped != text and len(stripped) >= 4 and "," not in stripped and stripped not in out
                and not re.search(r"\b(room|rm|suite|ste|floor|level)$", stripped, re.I)):
            out.append(stripped)
    return out
