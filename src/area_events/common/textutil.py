"""Text cleanup for what sources publish: HTML to readable text, mojibake repair and strict entity decoding. Neutral: no dependency on any source, engine or pipeline."""
import html
import html.entities
import re
from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "br", "li", "div", "h1", "h2", "h3", "h4", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def repair_mojibake(text):
    """Undo UTF-8 text that the source double-encoded (e.g. 'â€™' -> apostrophe)."""
    if "â€" not in text and "Ã" not in text:
        return text
    try:
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def html_to_text(fragment):
    extractor = _TextExtractor()
    extractor.feed(fragment)
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in "".join(extractor.parts).splitlines())
    return repair_mojibake("\n".join(line for line in lines if line))


# --- text cleanup -----------------------------------------------------------

_ENTITY = r"&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});"
_ANY_ENTITY = re.compile(_ENTITY)
_DOUBLE_ENCODED = re.compile(r"&amp;" + _ENTITY[1:])  # "&amp;amp;", "&amp;#39;": an entity that was escaped a second time
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
TEXT_FIELDS = ("title", "venue", "address", "description", "organizer", "price", "neighborhood")


def _decode_entity(match):
    """One entity written with its semicolon, decoded strictly: unknown names and invalid numbers stay as written, and the
    legacy "&not"-style prefixes html.unescape would also decode inside longer words are never touched."""
    token = match.group(0)
    body = token[1:-1]
    if body[0] == "#":
        try:
            code = int(body[2:], 16) if body[1] in "xX" else int(body[1:])
        except ValueError:
            return token
        return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else token
    return html.entities.html5.get(body + ";", token)


def clean_text(value):
    """Visible text from a source that sometimes leaves HTML entities in it ("Hotel &amp; Suites" -> "Hotel & Suites").

    Applied once, where events enter the engine, to the fields people read. Entities are decoded only when written in full
    (with their semicolon). A second pass happens only when the original holds a *double-encoded* entity ("&amp;amp;"), so
    valid text is not damaged by repeated decoding, and the result is stable when run again. Markup is not interpreted or
    removed (the card shows text as text), and control characters that no listing means are dropped. Web addresses never
    pass through here."""
    if not isinstance(value, str):
        return value
    text = value
    if "&" in text:
        passes = 2 if _DOUBLE_ENCODED.search(text) else 1
        for _ in range(passes):
            text = _ANY_ENTITY.sub(_decode_entity, text)
    return _CONTROLS.sub("", text)
