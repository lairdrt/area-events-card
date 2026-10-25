"""Text normalization for what people read, in one place.

`decode_entities` is the single boundary where character references written as text in a source (`Children&#039;s`) become the characters they stand for.
It runs once, when a stored candidate becomes an event (extraction) and when a label is made (enrichment); the stored candidate itself and its evidence keep the
source's exact text. Decoded text is still only text: nothing here (or in the card, which renders with text nodes) ever interprets it as markup.

`place_label` makes the short name shown on a map: a venue's name, the place a venue sentence starts by naming, or the street of the address, taken only from the
event's own fields. It returns None rather than a sentence or a guess.
"""
import html
import re

from ..geolocation import textloc

_REFERENCE = re.compile(r"&(#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
_LEADING_NAME = re.compile(r"^(?:The\s+)?(?P<name>[A-Z][\w'’.&-]*(?:\s+[A-Z][\w'’.&-]*){1,4})\s+(?:is|are|will|operates|takes|located|offers|hosts|features|runs)\b")
_HOUSE_NUMBER = re.compile(r"^\s*(?:#\s*)?\d+[A-Za-z]?(?:\s*[-/]\s*\d+)?\s+")
MAX_LABEL = 60


def decode_entities(text):
    """`Children&#039;s` -> `Children's`; `&amp;`, `&quot;`, `&#x27;`, `&eacute;` likewise. Only complete references (ending in `;`) are touched, once, so `AT&T`, `Q&A`,
    an unknown name or a number that is not a printable character stay exactly as written. A decoded no-break space becomes an ordinary space."""
    def one(match):
        decoded = html.unescape(match.group(0))
        if decoded == match.group(0) or decoded == "�":
            return match.group(0)
        if decoded.isspace():
            return " "
        return decoded if decoded.isprintable() else match.group(0)
    return _REFERENCE.sub(one, str(text or ""))


def _tidy(value):
    return re.sub(r"\s+", " ", decode_entities(value)).strip(" .,;:-–—")


def place_label(event):
    """A concise place name for an event, or None. Never the event's summary, never a whole address, never invented."""
    venue = re.sub(r"\s+", " ", decode_entities(event.get("venue"))).strip()
    candidates = []
    if venue and not textloc.is_prose(venue):
        candidates.append(venue.split(",")[0])
    else:
        candidates.append(textloc.named_place(venue) or "")
        match = _LEADING_NAME.match(venue)
        candidates.append(match.group("name") if match else "")
    street = decode_entities(event.get("address")).split(",")[0]
    candidates.append(_HOUSE_NUMBER.sub("", street, count=1) if _HOUSE_NUMBER.match(street) else "")
    for phrase in textloc.corner_phrases(venue)[:1]:
        candidates.append(phrase)
    for candidate in candidates:
        label = _tidy(candidate)
        if 2 <= len(label) <= MAX_LABEL and len(label.split()) <= 8 and not re.match(r"^\d+$", label):
            return label
    return None
