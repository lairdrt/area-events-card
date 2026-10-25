"""A plain evidence reference: what supports a field, where it came from, and how it was obtained.

Modules that read, classify, locate or verify an event can attach references like this to the same event, so that a person (or a later module)
can select a field and inspect its support. A reference is a dict:

    field         the event field the evidence is about ("title", "start", "venue", "address", "category", "location", ...)
    value         the value that was derived
    kind          how the value relates to the source:
                    "extracted"  the value, or the text it was read from, is in the source
                    "inferred"   derived by a rule from source fields (a category from keywords, a date normalised to ISO)
                    "verified"   confirmed by an external lookup (the geocoder), with the lookup's own evidence
                    "recorded"   a statement a module wrote about its own work (a verification line), kept as it was written
    recorded_by   the module and function that made the decision (e.g. "sources/webparse.candidate_from_ld")
    source_url    the page or document the supporting text was found in, when there is one
    quote         the exact supporting text as it appears in the source (after decoding HTML entities and collapsing white space), when found
    context       a window of the source around the quote, when found
    found_in      "visible text" or "page markup" (structured data such as JSON-LD) for a located quote
    note          anything a reader needs to know (for example that a value was not found verbatim)

Nothing here invents text: a quote exists only if the value was found in the source, and `locate` says nothing otherwise.
"""
import html
import re

WINDOW = 120
MAX_QUOTE = 300


def squeeze(text):
    return re.sub(r"\s+", " ", html.unescape(str(text or ""))).strip()


def reference(field, value, kind, recorded_by, source_url=None, quote=None, context=None, found_in=None, note=None):
    ref = {"field": field, "value": value, "kind": kind, "recorded_by": recorded_by, "source_url": source_url, "quote": quote, "context": context,
           "found_in": found_in, "note": note}
    return {k: v for k, v in ref.items() if v is not None}


def locate(needle, sources):
    """Find `needle` verbatim in the sources, the first match wins. `sources` is an ordered list of (url, found_in, text) with text already squeezed.
    Returns {"source_url", "found_in", "quote", "context"} or None; the match ignores case only, never wording."""
    target = squeeze(needle)
    if len(target) < 2:
        return None
    pattern = re.compile(re.escape(target), re.I)
    for url, found_in, text in sources:
        match = pattern.search(text)
        if match:
            start, end = match.span()
            return {"source_url": url, "found_in": found_in, "quote": text[start:end][:MAX_QUOTE],
                    "context": ("…" if start > WINDOW else "") + text[max(0, start - WINDOW):end + WINDOW] + ("…" if end + WINDOW < len(text) else "")}
    return None


def visible_text(markup):
    """The readable text of an HTML page, squeezed (scripts and styles removed, inline tags joined, blocks separated)."""
    markup = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", markup or "")
    markup = re.sub(r"(?i)</?(?:a|b|strong|em|i|u|span|font|small|sup|sub|mark|abbr|code)\b[^>]*>", "", markup)
    markup = re.sub(r"(?i)</?(p|div|li|ul|ol|tr|td|th|h[1-6]|br|section|article|header|footer|nav|table|dd|dt)\b[^>]*>", "\n", markup)
    return squeeze(re.sub(r"<[^>]+>", " ", markup))
