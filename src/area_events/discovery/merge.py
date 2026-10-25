"""Merge per-source events into one feed, with evidence-based deduplication.

Candidate selection is bounded: records are sorted by start time and only
records starting within NEAR_START_MINUTES of each other (from different
sources) are ever compared. Each candidate pair is judged by explicit rules
(see evaluate_pair) that return the reasons for a match, or None. Matching
needs the same event title (ignoring punctuation, years and "annual"-style
suffixes) plus evidence beyond the title: a venue or address that agrees, or a
description that largely repeats, or an identical end time together with the
area being named in the other listing. Separate showtimes, other dates, other
acts at one venue and generic titles ("Live Music") are not merged. Venues may
differ for a multi-location event when the description or end time and area
evidence agree.

Records of one source never merge, a group never holds two records of one
source, and "verified" needs at least `min_publishers` distinct publishers:
several pages from one publisher count once.
"""
import re
import unicodedata
from datetime import datetime

from ..enrichment import images
from ..geolocation.place import combine_places

NEAR_START_MINUTES = 30
STOPWORDS = {"the", "a", "an", "and", "at", "of", "in", "live", "tickets", "presents", "present", "with", "featuring", "annual"}
VENUE_NOISE = {"the", "san", "diego", "center", "centre", "theatre", "theater", "hall", "of", "at", "and", "hotel", "park", "bar",
               "grill", "cafe", "club", "room", "museum", "library", "college", "university", "arena", "performing", "arts"}
GENERIC_WORDS = {"music", "jazz", "night", "show", "concert", "market", "markets", "farmers", "farmer", "happy", "hour", "karaoke",
                 "trivia", "yoga", "class", "open", "mic", "party", "festival", "free", "day", "weekly", "sale", "meeting", "tour"}
DESCRIPTION_STOPWORDS = STOPWORDS | {"is", "are", "to", "for", "on", "this", "that", "will", "be", "you", "your", "our", "we", "it", "as",
                                     "by", "from", "or", "all", "their", "its", "can", "has", "have", "more", "join", "us"}
MIN_DESCRIPTION_WORDS = 12
DESCRIPTION_OVERLAP = 0.6


def _words(text, drop=frozenset()):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().replace("'", "")
    return [w for w in re.findall(r"[a-z0-9]+", text) if w not in drop]


def title_key(title):
    """Title tokens with punctuation, stopwords, years and ordinals ("62nd") removed."""
    return [w for w in _words(title, STOPWORDS) if not re.fullmatch(r"(19|20)\d\d|\d+(st|nd|rd|th)", w)]


def is_generic(key):
    return not key or all(w in GENERIC_WORDS for w in key)


def title_relation(a, b):
    """'identical', 'contained' (one title is the other plus extra words) or None."""
    ka, kb = title_key(a), title_key(b)
    if not ka or not kb:
        return None
    if ka == kb:
        return "identical"
    smaller, larger = (set(ka), set(kb)) if len(ka) <= len(kb) else (set(kb), set(ka))
    short_text = " ".join(ka if len(ka) <= len(kb) else kb)
    if smaller <= larger and (len(smaller) >= 2 or len(short_text) >= 8):
        return "contained"
    return None


def _street_key(address):
    match = re.match(r"\s*(\d+)\s+([a-z0-9]+)", (address or "").lower())
    return match.groups() if match else None


def venues_match(a, b):
    """True only on positive evidence; a missing venue never matches."""
    ka, kb = _street_key(a.get("address")), _street_key(b.get("address"))
    if ka and kb and ka == kb:
        return True
    va, vb = set(_words(a.get("venue"), VENUE_NOISE)), set(_words(b.get("venue"), VENUE_NOISE))
    if not va or not vb:
        return False
    shared = len(va & vb)
    return shared >= 2 or shared / min(len(va), len(vb)) >= 0.75


def description_overlap(a, b):
    """Share of the shorter description's content words found in the other (0 if either is short)."""
    wa, wb = set(_words(a.get("description"), DESCRIPTION_STOPWORDS)), set(_words(b.get("description"), DESCRIPTION_STOPWORDS))
    if len(wa) < MIN_DESCRIPTION_WORDS or len(wb) < MIN_DESCRIPTION_WORDS:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def _venue_named_in(record, other):
    """The record's venue name appears as a phrase in the other record's title or description."""
    venue = " ".join(_words(record.get("venue"), VENUE_NOISE))
    if len(venue) < 4:
        return None
    haystack = " " + " ".join(_words((other.get("title") or "") + " " + (other.get("description") or ""))) + " "
    return record.get("venue") if f" {venue} " in haystack else None


def _minutes_apart(a, b):
    return abs((datetime.fromisoformat(a["start"]) - datetime.fromisoformat(b["start"])).total_seconds()) / 60


def _showtime_key(record):
    place = _street_key(record.get("address")) or tuple(_words(record.get("venue"), VENUE_NOISE))
    return (record["_key"], tuple(title_key(record["title"])), place, record["start"][:10])


def evaluate_pair(a, b):
    """Return the list of reasons these two records are the same event, or None.

    The rules are deterministic and every rule needs evidence beyond the title.
    """
    apart = _minutes_apart(a, b)
    if apart > NEAR_START_MINUTES:
        return None
    relation = title_relation(a["title"], b["title"])
    if not relation:
        return None
    generic = is_generic(title_key(a["title"])) or is_generic(title_key(b["title"]))
    venue_ok = venues_match(a, b)
    overlap = description_overlap(a, b)
    description_ok = overlap >= DESCRIPTION_OVERLAP
    same_end = bool(a.get("end") and a.get("end") == b.get("end"))
    named = _venue_named_in(a, b) or _venue_named_in(b, a)

    reasons = ["titles agree" + (" after ignoring punctuation, years and suffixes" if relation == "identical" else " (one adds a lineup or tour name)")]
    if apart:
        # Slightly different start times (doors vs show time): needs venue or description evidence plus
        # an identical end time, or the same venue with no listing of separate showtimes.
        showtimes = a.get("_showtimes") or b.get("_showtimes")
        ok = (same_end and relation == "identical" and (venue_ok or description_ok)) or (venue_ok and not generic and not showtimes)
        if not ok:
            return None
        reasons.append(f"start times {int(apart)} min apart (doors vs show time); no separate showtimes listed" if not same_end
                       else f"start times {int(apart)} min apart but the end times are identical")
    else:
        reasons.append("same start time")

    if generic:  # "Live Music", "Farmers Market": only the venue can distinguish them
        if not (venue_ok and relation == "identical"):
            return None
        reasons.append("venue or address agrees")
        return reasons

    supported = False
    if venue_ok:
        supported = True
        reasons.append("venue or address agrees")
    if description_ok:
        supported = True
        reasons.append(f"descriptions share {int(overlap * 100)}% of their wording")
        if not venue_ok and (a.get("venue") or a.get("address")) and (b.get("venue") or b.get("address")):
            reasons.append("venues differ: treated as a multi-location event")
    if not supported and relation == "identical" and same_end and named:
        supported = True
        reasons.append("end times are identical")
        reasons.append(f"'{named}' is named in the other listing")
        reasons.append("venues differ: treated as a multi-location event")
    return reasons if supported else None


def _strength(reasons):
    return sum(2 if ("venue" in r and "differ" not in r) or "descriptions" in r else 1 for r in reasons)


def _combine(group, publishers, min_publishers, reasons):
    """group: records in priority order, each from a different source."""
    primary = group[0]
    with_address = next((r for r in group if r.get("address")), None)
    descriptions = [r["description"] for r in group if r.get("description")]
    chosen_description = max(descriptions, key=len) if descriptions else None
    image = images.choose(group)
    names = [r["source"]["name"] for r in group]
    pubs = sorted({publishers[r["_key"]] for r in group})

    evidence = []
    for record in group:
        evidence += [f"[{record['source']['name']}] {line}" for line in record["verification"]["evidence"]]
    conflicts = any("CONFLICT" in line for line in evidence)
    statuses = {r["verification"]["status"] for r in group}

    locations = []
    for r in group:
        place = {"venue": r.get("venue"), "address": r.get("address"), "source": r["source"]["name"]}
        if (place["venue"] or place["address"]) and not any(p["venue"] == place["venue"] and p["address"] == place["address"] for p in locations):
            locations.append(place)
    multi_location = len({(p["venue"] or "").lower() for p in locations}) > 1 and not any(venues_match(group[0], r) for r in group[1:])

    if len(pubs) >= min_publishers and not conflicts:
        status, verified = "verified", True
        same_start = len({r["start"] for r in group}) == 1
        agreed = ("title and start time" if multi_location else "title, start time and venue") if same_start else             "title and venue with start times within 30 minutes of each other"
        evidence.append(f"{len(pubs)} independent publishers ({', '.join(pubs)}) list the same {agreed} ({primary['start']})")
    elif "corroborated" in statuses and not conflicts:
        status, verified = "corroborated", False
    else:
        status, verified = "unverified", False
    if len(group) > len(pubs):
        evidence.append("multiple pages from one publisher count as one source")

    sources = []
    for r in group:
        entry = {
            "name": r["source"]["name"], "publisher": publishers[r["_key"]], "url": r["url"],
            "listing_url": r["source"].get("listing_url"), "retrieved_at": r["retrieved_at"],
            "status": r["verification"]["status"], "evidence": r["verification"]["evidence"],
            "missing": r["verification"]["missing"],
        }
        if r["start"] != primary["start"]:
            entry["start"] = r["start"]  # this source's own start time (e.g. door time vs show time)
        # Keep anything that lost the field-by-field choice so no image or description is discarded.
        if r.get("description") and r["description"] != chosen_description:
            entry["description"] = r["description"]
        if r.get("image_url") and image["url"] and r["image_url"] != image["url"]:
            entry["image_url"] = r["image_url"]
        sources.append(entry)

    merged = {k: v for k, v in primary.items() if not k.startswith("_") and k not in ("extraction",)}
    merged.update({
        "end": next((r["end"] for r in group if r.get("end")), None),
        "venue": (with_address or primary).get("venue") or next((r["venue"] for r in group if r.get("venue")), None),
        "address": with_address["address"] if with_address else None,
        "description": chosen_description,
        "image_url": image["url"],
        "image_source": (f"{image['source']}: {image['detail']}" if image["url"] else None),
        "images": ([image["url"]] + image["alternates"]) if image["url"] else [],
        "image_provenance": {"tier": image["tier"], "kind": image["kind"], "source": image["source"], "skipped": image["skipped"]},
        "categories": list(dict.fromkeys(c for r in group for c in r.get("categories", []))),
        "price": next((r["price"] for r in group if r.get("price")), None),
        "retrieved_at": max(r["retrieved_at"] for r in group),
        "sources": sources,
        "verified": verified,
    })
    if len(locations) > 1:
        merged["locations"] = locations
    place = combine_places([(r["source"]["name"], r.get("place")) for r in group])
    if place:
        merged["place"] = place
        if place["classification"] == "confirmed" and primary.get("geo") is None:
            merged["geo"] = next((r["geo"] for r in group if r.get("geo")), None)
    merged["verification"] = {
        "status": status,
        "evidence": evidence,
        "missing": [f for f in ("end", "venue", "address", "description", "image_url") if not merged.get(f)],
        "independent_publishers": pubs,
        "match_reasons": reasons,
        "note": ("Confirmed by independent publishers." if verified else
                 "Single publisher; verified stays false until independently confirmed."),
    }
    merged["source"] = {"name": names[0], "listing_url": primary["source"].get("listing_url")}
    return merged


def candidate_pairs(records):
    """Pairs of records from different sources that start within NEAR_START_MINUTES (sliding window over start time)."""
    order = sorted(range(len(records)), key=lambda i: (datetime.fromisoformat(records[i]["start"]), i))
    times = [datetime.fromisoformat(records[i]["start"]) for i in order]
    for pos, i in enumerate(order):
        for later in range(pos + 1, len(order)):
            if (times[later] - times[pos]).total_seconds() / 60 > NEAR_START_MINUTES:
                break
            j = order[later]
            if records[i]["_key"] != records[j]["_key"]:
                yield min(i, j), max(i, j)


def merge_events(records_by_source, publishers, min_publishers=2, now=None):
    """records_by_source: {key: [event dicts]} in priority order. Returns merged events sorted by start."""
    records = [dict(record, _key=key) for key, items in records_by_source.items() for record in items]
    # A source that lists one title at one venue several times on a day has separate showtimes.
    counts = {}
    for record in records:
        counts[_showtime_key(record)] = counts.get(_showtime_key(record), 0) + 1
    for record in records:
        record["_showtimes"] = counts[_showtime_key(record)] > 1

    matches = []
    for i, j in candidate_pairs(records):
        reasons = evaluate_pair(records[i], records[j])
        if reasons:
            matches.append((-_strength(reasons), i, j, reasons))
    matches.sort(key=lambda m: m[:3])

    group_of = list(range(len(records)))
    members = {i: [i] for i in range(len(records))}
    used_reasons = {}
    for _, i, j, reasons in matches:
        gi, gj = group_of[i], group_of[j]
        if gi == gj:
            continue
        sources_i = {records[m]["_key"] for m in members[gi]}
        sources_j = {records[m]["_key"] for m in members[gj]}
        if sources_i & sources_j:
            continue  # a group never holds two records of one source
        if any(not title_relation(records[a]["title"], records[b]["title"]) for a in members[gi] for b in members[gj]):
            continue  # every pair joined through a chain must still have related titles
        members[gi] += members.pop(gj)
        for m in members[gi]:
            group_of[m] = gi
        used_reasons.setdefault(gi, []).extend(used_reasons.pop(gj, []))
        used_reasons[gi].append(f"{records[i]['source']['name']} + {records[j]['source']['name']}: " + "; ".join(reasons))

    merged = []
    for gid, indexes in members.items():
        group = [records[m] for m in sorted(indexes)]  # input order = source priority
        merged.append(_combine(group, publishers, min_publishers, used_reasons.get(gid, [])))
    merged.sort(key=lambda e: (e["start"], e["title"]))
    return merged
