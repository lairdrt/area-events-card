"""Merge per-source events into one feed, with deduplication and provenance.

Two records are the same event only if they come from different sources,
start at exactly the same time, have similar titles AND a matching venue (or
street address). Same-source records are never merged, so separate showtimes
and recurring runs stay separate. "Verified" needs at least
`min_publishers` distinct publishers; several pages from one publisher count
once.
"""
import re
import unicodedata
from datetime import datetime

STOPWORDS = {"the", "a", "an", "and", "at", "of", "in", "live", "tickets", "presents", "present", "with", "featuring"}
VENUE_NOISE = {"the", "san", "diego", "center", "centre", "theatre", "theater", "hall", "of", "at", "and", "hotel", "park", "bar",
               "grill", "cafe", "club", "room", "museum", "library", "college", "university", "arena", "performing", "arts"}


def _words(text, drop):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().replace("'", "")
    return [w for w in re.findall(r"[a-z0-9]+", text) if w not in drop]


def normalize_title(title):
    return " ".join(_words(title, STOPWORDS))


def titles_match(a, b):
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    sa, sb = set(na.split()), set(nb.split())
    smaller, larger = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    # One title is the other plus extra words (a lineup, a tour name, a year).
    return smaller <= larger and (len(smaller) >= 2 or len(na if sa is smaller else nb) >= 8)


def _street_key(address):
    match = re.match(r"\s*(\d+)\s+([a-z0-9]+)", (address or "").lower())
    return match.groups() if match else None


def venues_match(a, b):
    """True only on positive evidence; a missing venue never matches."""
    ka, kb = _street_key(a.get("address")), _street_key(b.get("address"))
    if ka and kb:
        return ka == kb
    va, vb = _words(a.get("venue"), VENUE_NOISE), _words(b.get("venue"), VENUE_NOISE)
    if not va or not vb:
        return False
    sa, sb = set(va), set(vb)
    return len(sa & sb) / min(len(sa), len(sb)) >= 0.75


def same_event(a, b):
    return (a["start"] == b["start"] and titles_match(a["title"], b["title"]) and venues_match(a, b))


def _first(records, field):
    return next((r[field] for r in records if r.get(field)), None)


def _combine(group, publishers, min_publishers, now_iso):
    """group: records in priority order, each from a different source."""
    primary = group[0]
    with_address = next((r for r in group if r.get("address")), None)
    descriptions = [r["description"] for r in group if r.get("description")]
    image_record = next((r for r in group if r.get("image_url")), None)
    names = [r["source"]["name"] for r in group]
    pubs = sorted({publishers[r["_key"]] for r in group})

    evidence = []
    for record in group:
        evidence += [f"[{record['source']['name']}] {line}" for line in record["verification"]["evidence"]]
    conflicts = any("CONFLICT" in line for line in evidence)
    statuses = {r["verification"]["status"] for r in group}

    if len(pubs) >= min_publishers and not conflicts:
        status, verified = "verified", True
        evidence.append(f"{len(pubs)} independent publishers ({', '.join(pubs)}) list the same title, "
                        f"start time ({primary['start']}) and venue")
    elif "corroborated" in statuses and not conflicts:
        status, verified = "corroborated", False
    else:
        status, verified = "unverified", False
    if len(group) > len(pubs):
        evidence.append("multiple pages from one publisher count as one source")

    merged = {k: v for k, v in primary.items() if not k.startswith("_") and k not in ("extraction",)}
    merged.update({
        "end": _first(group, "end"),
        "venue": (with_address or primary).get("venue") or _first(group, "venue"),
        "address": with_address["address"] if with_address else None,
        "description": max(descriptions, key=len) if descriptions else None,
        "image_url": image_record["image_url"] if image_record else None,
        "image_source": (f"{image_record['source']['name']}: {image_record['image_source']}" if image_record else None),
        "categories": list(dict.fromkeys(c for r in group for c in r.get("categories", []))),
        "price": _first(group, "price"),
        "retrieved_at": max(r["retrieved_at"] for r in group),
        "sources": [{
            "name": r["source"]["name"], "publisher": publishers[r["_key"]], "url": r["url"],
            "listing_url": r["source"].get("listing_url"), "retrieved_at": r["retrieved_at"],
            "status": r["verification"]["status"], "evidence": r["verification"]["evidence"],
            "missing": r["verification"]["missing"],
        } for r in group],
        "verified": verified,
        "verification": {
            "status": status,
            "evidence": evidence,
            "missing": [f for f in ("end", "venue", "address", "description", "image_url") if not merged_value(group, f)],
            "independent_publishers": pubs,
            "note": ("Confirmed by independent publishers." if verified else
                     "Single publisher; verified stays false until independently confirmed."),
        },
    })
    merged["source"] = {"name": names[0], "listing_url": primary["source"].get("listing_url")}
    return merged


def merged_value(group, field):
    return any(r.get(field) for r in group)


def merge_events(records_by_source, publishers, min_publishers=2, now=None):
    """records_by_source: {key: [event dicts]} in priority order. Returns merged events sorted by start."""
    groups = []
    for key, records in records_by_source.items():
        for record in records:
            record = dict(record, _key=key)
            for group in groups:
                if all(g["_key"] != key for g in group) and same_event(group[0], record):
                    group.append(record)
                    break
            else:
                groups.append([record])
    merged = [_combine(g, publishers, min_publishers, None) for g in groups]
    merged.sort(key=lambda e: (e["start"], e["title"]))
    return merged
