"""Responsive variants of an image, taken only from what the page itself declares.

An engine often returns the original upload (2172 px wide) while the page's own `srcset` or `<picture>` offers the same picture at a sensible
size. Only addresses written in the markup around that image count; file names are never rewritten to guess a size. The caller's later
validation of the bytes (type, 2 MB limit) still applies to whichever address is chosen.
"""
import math
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

MAX_VARIANTS = 1000
PREFERRED_WIDTH = 1024
PHOTO_TYPES = ("", "image/jpeg", "image/png", "image/webp")
URL_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-full-url", "src")
SRCSET_ATTRS = ("data-srcset", "data-lazy-srcset", "srcset")


def parse_srcset(value):
    """(url, width, density) triples; commas inside a URL are kept. Malformed entries are skipped."""
    variants, position, value = [], 0, value or ""
    url_pattern = re.compile(r"[\s,]*(\S+)")
    while position < len(value):
        if len(variants) > MAX_VARIANTS:
            raise ValueError("too many responsive image variants")
        match = url_pattern.match(value, position)
        if not match:
            break
        url, position = match[1], match.end()
        if url.endswith(","):
            url, descriptor = url.rstrip(","), ""
        else:
            end = value.find(",", position)
            end = len(value) if end == -1 else end
            descriptor, position = value[position:end].strip(), end + 1
        if not descriptor:
            variants.append((url, 0, 1.0))
        elif re.fullmatch(r"[1-9][0-9]*w", descriptor):
            variants.append((url, int(descriptor[:-1]), 0))
        elif re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)x", descriptor):
            density = float(descriptor[:-1])
            if math.isfinite(density) and density > 0:
                variants.append((url, 0, density))
    return variants


class _Images(HTMLParser):
    """Every <img> with the variants of its own element and its enclosing <picture>, as raw (url, width, density)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.groups, self._picture = [], None

    def handle_starttag(self, tag, attrs):
        attrs = {key: value or "" for key, value in attrs}
        if tag == "picture":
            self._picture = []
        elif tag == "source" and self._picture is not None and attrs.get("type", "").lower() in PHOTO_TYPES:
            for key in SRCSET_ATTRS:
                self._picture.extend(parse_srcset(attrs.get(key)))
        elif tag == "img" and len(self.groups) < MAX_VARIANTS:
            group = list(self._picture or ())
            for key in SRCSET_ATTRS:
                group.extend(parse_srcset(attrs.get(key)))
            group.extend((attrs[key], 0, 0) for key in URL_ATTRS if attrs.get(key))
            self.groups.append(group)

    def handle_endtag(self, tag):
        if tag == "picture":
            self._picture = None


def _absolute(raw, page_url):
    raw = (raw or "").strip()
    if not raw or raw.startswith("#"):
        return None
    url = urljoin(page_url, raw)
    return url if url.lower().startswith(("http://", "https://")) else None


def variants_of(html, page_url, primary):
    """Absolute (url, width, density) variants of the image element(s) that reference `primary`; empty when the page shows it bare."""
    parser = _Images()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, TypeError):
        return []
    found = []
    for group in parser.groups:
        resolved = [(_absolute(raw, page_url), width, density) for raw, width, density in group]
        resolved = [item for item in resolved if item[0]]
        if any(url == primary for url, _, _ in resolved):
            found.extend(resolved)
    return found


def prefer_variant(primary, variants):
    """The declared variant to use instead of `primary`: the widest of at most 1024 px, else the smallest wider one.
    Width describes layout, not bytes, so this is a preference; `primary` stays the answer when the page declares no width."""
    widths = [item for item in variants if item[1] > 0]
    within = [item for item in widths if item[1] <= PREFERRED_WIDTH]
    if within:
        return max(within, key=lambda item: item[1])[0]
    if widths:
        return min(widths, key=lambda item: item[1])[0]
    return primary


def measurable(fetch_image, html, page_url):
    """Wrap the caller's bounded `fetch_image` for an engine that must look at an image's bytes to rank it.

    Each address is fetched at most once. When the original cannot be fetched within the caller's limits (typically a 2000 px upload), the
    engine is given the bytes of the page-declared variant of that same image instead, so it can still rank the page's picture; the
    engine's answer stays the original address and `prefer_variant` later picks the address to use. No limit is raised.
    """
    if fetch_image is None:
        return None
    cache = {}

    def fetch(url):
        if url not in cache:
            try:
                cache[url] = fetch_image(url)
            except Exception:
                cache[url] = None
                declared = [item for item in variants_of(html, page_url, url) if item[0] != url and item[1] > 0]
                for alternative in sorted(declared, key=lambda item: (item[1] > PREFERRED_WIDTH, abs(item[1] - PREFERRED_WIDTH)))[:2]:
                    try:
                        cache[url] = fetch_image(alternative[0])
                        break
                    except Exception:
                        continue
        return cache[url]
    return fetch
