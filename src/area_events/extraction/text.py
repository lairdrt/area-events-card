"""Text normalization for what people read, in one place.

`decode_entities` is the single boundary where character references written as text in a source (`Children&#039;s`) become the characters they stand for.
It runs when a stored candidate becomes an event (extraction, through `plain_text`) and when a label is made (enrichment); the stored candidate itself and its evidence keep the
source's exact text. Decoded text is still only text: nothing here (or in the card, which renders with text nodes) ever interprets it as markup.

`plain_text` is what an event's title, description, venue and address become: those references decoded until nothing decodable is left (a source that escaped its
text twice, `&amp;amp;`, reads the same as one that did not), HTML formatting tags removed with the words inside them kept, line and paragraph tags turned into line
breaks, and script and style contents left out. Only real element tags are removed, so `Kids < 12`, `<3` or `<Ages 5+>` stay as written. It is idempotent.

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
_HIDDEN = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>|<(?:script|style)\b[^>]*>.*\Z|<!--.*?-->", re.I | re.S)
_ATTRIBUTES = r"""(?:\s+[A-Za-z_:][\w:.-]*\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'<>=`]+))*\s*/?"""   # name="value" pairs only, so `a<b and c>d` is not a tag
_LINE = "br"
_BLOCK = "p|div|h[1-6]|li|ul|ol|dl|dt|dd|tr|table|thead|tbody|blockquote|pre|section|article|header|footer|aside|nav|main|figure|figcaption|address|hr"
_CELL = "td|th"
_INLINE = "a|abbr|b|bdi|bdo|big|center|cite|code|del|dfn|em|font|i|img|ins|kbd|label|mark|picture|q|s|samp|small|source|span|strike|strong|sub|sup|time|tt|u|wbr"
_TAG = re.compile(rf"(?P<lead>[ \t]*)</?(?:(?P<line>{_LINE})|(?P<block>{_BLOCK})|(?P<cell>{_CELL})|{_INLINE})\b{_ATTRIBUTES}>(?P<trail>[ \t]*)", re.I)
_DECODE_PASSES = 5


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


def _spacing(match):
    if match["line"]:
        return "\n"
    if match["block"]:
        return "\n\n"
    return " " if match["cell"] or match["lead"] or match["trail"] else ""


def _without_markup(text):
    stripped = _TAG.sub(_spacing, _HIDDEN.sub(" ", text))
    if stripped == text:
        return text
    return re.sub(r"\n[ \t]*(?:\n[ \t]*)+", "\n\n", re.sub(r"[ \t]{2,}", " ", stripped)).strip()


def plain_text(text):
    """Readable text for an event field: `<b>Fish &amp;amp; Chips</b><br>Pier 1` -> `Fish & Chips` + line break + `Pier 1`. Text without markup or references is
    returned exactly as given, and running it again changes nothing."""
    text = str(text or "")
    for _ in range(_DECODE_PASSES):
        cleaned = _without_markup(decode_entities(text))
        if cleaned == text:
            break
        text = cleaned
    return text


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
