"""Event image selection: one place, one priority order, with provenance.

Priority (see docs/images.md for the whole picture, including the card's artwork):

  1. the event's own image      the image of the event's primary source record, if it is a usable photo
  2. a matched event image      otherwise the best usable photo among the other source records already matched to
                                the same event (no new matching happens here)
  3. an organizer/venue image   reserved: no adapter supplies one that may be reused, so none is ever invented
  4. category artwork           chosen by the card from the event's categories; local, never presented as a photo
  5. general artwork            when no category can be determined

Only tiers 1 and 2 are decided here. A candidate is a *usable photo* unless it is a placeholder, a tracking pixel,
a logo or other site graphic, or not an http(s) address; the reason is kept so the choice can be audited. Nothing is
downloaded: judging is done on the address alone.
"""
import re
from urllib.parse import parse_qs, unquote, urlparse

MAX_URL_LENGTH = 2000

PLACEHOLDER = re.compile(r"place-?holder|no[-_ ]?image|noimage|image[-_ ]?(coming|missing|unavailable|not[-_ ]?found)|coming[-_ ]?soon|"
                         r"(^|[-_ ])default[-_ ]?(image|event|thumb|photo|icon)|events?_default_icon|missing|fallback|dummy|stock[-_ ]?image", re.I)
PIXEL = re.compile(r"(^|[^0-9])1x1([^0-9]|$)|(^|[-_/.])(pixel|spacer|blank|transparent|tracking|beacon|clear)([-_/.]|$)", re.I)
SITE_GRAPHIC = re.compile(r"logo|favicon|sprite|apple-touch|site-?icon|avatar|wordmark|(^|[-_ ])brand([-_ ]|$)|(^|[-_ ])icon([-_ .]|$)", re.I)
SIZE_HINT = re.compile(r"(\d{3,4})x(\d{3,4})")
EXTENSIONLESS_OK = True  # CDN resizers often serve images from extensionless addresses


def _filename(url):
    """The name of the image itself: for image resizers (…?url=<original>) that is the original's name."""
    parts = urlparse(url)
    inner = parse_qs(parts.query).get("url")
    path = urlparse(unquote(inner[0])).path if inner else parts.path
    return unquote(path.rstrip("/").rsplit("/", 1)[-1])


def judge(url):
    """(True, "") if `url` looks like a usable event photo, else (False, reason)."""
    if not isinstance(url, str) or not url.strip():
        return False, "empty"
    url = url.strip()
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False, "not an http(s) address"
    if len(url) > MAX_URL_LENGTH or re.search(r"\s", url):
        return False, "malformed address"
    name = _filename(url)
    if PIXEL.search(name) or re.search(r"[?&](w|width|h|height)=1(&|$)", url):
        return False, "tracking pixel"
    if PLACEHOLDER.search(name):
        return False, "placeholder"
    if SITE_GRAPHIC.search(name):
        return False, "logo or site graphic"
    if name.lower().endswith((".svg", ".ico", ".gif")) and not SIZE_HINT.search(url):
        return False, "icon or animation, not a photo"
    return True, ""


def _size(url):
    sizes = [int(w) * int(h) for w, h in SIZE_HINT.findall(unquote(url))]
    return max(sizes) if sizes else 0


def choose(records):
    """Pick the image for one event from its matched source records (primary record first).

    Returns {"url", "tier", "kind", "source", "alternates", "skipped"}; `url` is None when no usable photo exists,
    and the card then shows its own artwork. `alternates` are the other usable photos, in order, for the browser to
    fall back to if the first fails to load.
    """
    usable, skipped = [], []
    for index, record in enumerate(records):
        for url in ([record.get("image_url")] + list(record.get("images") or [])):
            if not url:
                continue
            ok, reason = judge(url)
            if ok:
                usable.append((index, url.strip(), record))
            else:
                skipped.append({"url": url, "source": record["source"]["name"], "reason": reason})
    unique = list(dict.fromkeys(u for _, u, _ in usable))
    skipped = [s for s in {s["url"]: s for s in skipped}.values() if s["url"] not in unique][:4]
    if not usable:
        return {"url": None, "tier": None, "kind": None, "source": None, "detail": None, "alternates": [], "skipped": skipped}
    own = [item for item in usable if item[0] == 0]
    if own:
        index, url, record = own[0]
        tier, kind = 1, "event"
    else:  # the primary record has no usable photo: the largest among the matched ones, earliest source on a tie
        index, url, record = max(usable, key=lambda item: (_size(item[1]), -item[0]))
        tier, kind = 2, "matched"
    alternates = [u for u in unique if u != url]
    if kind == "event":
        alternates.sort(key=lambda u: -_size(u))
    return {"url": url, "tier": tier, "kind": kind, "source": record["source"]["name"], "detail": record.get("image_source"),
            "alternates": alternates, "skipped": skipped}


OVERUSED_TITLES = 4  # one picture on this many differently named events is a calendar or department graphic, not an event photo


def drop_overused(events, min_titles=OVERUSED_TITLES):
    """After merging: stop using a picture that many unrelated events share (a department banner on every listing).

    A recurring event repeats one title, so it never counts; four or more different titles on one address do. Such an
    address is removed from each event's choices and the next usable photo, if any, is promoted. Returns how many
    addresses were dropped.
    """
    titles = {}
    for event in events:
        for url in event.get("images") or ([event["image_url"]] if event.get("image_url") else []):
            titles.setdefault(url, set()).add(" ".join(str(event.get("title", "")).lower().split()))
    generic = {url for url, names in titles.items() if len(names) >= min_titles}
    if not generic:
        return 0
    for event in events:
        choices = event.get("images") or ([event["image_url"]] if event.get("image_url") else [])
        hit = [url for url in choices if url in generic]
        if not hit:
            continue
        keep = [url for url in choices if url not in generic]
        provenance = dict(event.get("image_provenance") or {})
        provenance["skipped"] = (provenance.get("skipped") or []) + [{"url": url, "source": provenance.get("source"), "reason": "shared by unrelated events"} for url in hit][:4]
        if event.get("image_url") in generic:
            event["image_url"] = keep[0] if keep else None
            event["image_source"] = None  # the promoted photo came from another matched record
            provenance.update(tier=(2 if keep else None), kind=("matched" if keep else None))
        event["images"] = keep
        event["image_provenance"] = provenance
        if not event.get("image_url") and "image_url" not in event["verification"].get("missing", []):
            event["verification"]["missing"] = event["verification"].get("missing", []) + ["image_url"]
    return len(generic)
