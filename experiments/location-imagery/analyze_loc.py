import os as _os

REPO = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
LOC_DIR = _os.environ.get("AE_LOC_DIR") or _os.path.join(REPO, "review", "location-imagery")  # git-ignored working folder
import json, re, sys, collections, os
B = LOC_DIR
STREET = re.compile(r"^\s*\d{1,6}[A-Za-z]?\s+(?:[NSEW]\.?\s+)?[A-Za-z0-9.' -]+?\s+(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Way|Ln|Lane|Pl|Place|Ct|Court|Pkwy|Parkway|Hwy|Highway|Cir|Circle|Trl|Trail|Plaza|Sq|Square|Loop|Ter|Terrace)\b", re.I)
STREET_ANYWHERE = re.compile(r"\b\d{1,6}[A-Za-z]?\s+(?:[NSEW]\.?\s+)?[A-Za-z0-9.' -]{2,40}?\s+(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Way|Ln|Lane|Pl|Place|Ct|Court|Pkwy|Parkway|Hwy|Highway|Cir|Circle|Trl|Trail|Plaza|Sq|Square|Loop|Ter|Terrace)\b", re.I)
VAGUE = re.compile(r"online|virtual|zoom|tba|tbd|various|multiple|see (?:website|description)|webinar|livestream|to be announced|everywhere|citywide|neighborhoods", re.I)


def classify(e):
    place = e.get("place") or {}
    address = (e.get("address") or "").strip()
    venue = (e.get("venue") or "").strip()
    geo = e.get("geo") or {}
    out = {}
    out["source_coords"] = place.get("classification") == "confirmed"
    out["street_address"] = bool(address and (STREET.match(address) or STREET_ANYWHERE.search(address)))
    named = bool(venue) and len(venue) <= 90 and not VAGUE.search(venue) and not STREET.match(venue) and not venue.lower().startswith(("http", "www."))
    out["named_venue"] = named
    out["city_only"] = (place.get("classification") == "estimated") or bool(address and not out["street_address"])
    out["vague"] = bool(VAGUE.search(venue + " " + address)) or (len(venue) > 120)
    out["none"] = not (venue or address or out["source_coords"])
    return out


def report(name, path):
    d = json.load(open(path, encoding="utf-8"))
    events = d["events"]
    n = len(events)
    flags = [classify(e) for e in events]
    tiers = collections.Counter()
    for e, f in zip(events, flags):
        if f["source_coords"]:
            t = "A source-supplied coordinates (venue-level)"
        elif f["street_address"] and not f["vague"]:
            t = "B full street address, no coordinates"
        elif f["named_venue"] and not f["vague"]:
            t = "C named venue only"
        elif f["city_only"] and not f["none"]:
            t = "D partial / city-only"
        else:
            t = "E missing or ambiguous"
        tiers[t] += 1
        e["_tier"] = t
    print(f"\n=== {name}: {n} events, sources {dict(collections.Counter((e.get('source') or {}).get('name') for e in events))}")
    for key, label in (("source_coords", "verified lat/lon from the source"), ("street_address", "full street address"), ("named_venue", "identifiable venue name"), ("city_only", "partial/city-only"), ("vague", "vague/online/long text"), ("none", "no location at all")):
        c = sum(1 for f in flags if f[key])
        print(f"  {label:38} {c:4}  {100*c/n:5.1f}%")
    print("  -- exclusive tiers --")
    for t in sorted(tiers):
        print(f"  {t:50} {tiers[t]:4}  {100*tiers[t]/n:5.1f}%")
    byplace = collections.Counter((e.get("place") or {}).get("classification") for e in events)
    print("  engine place classes:", dict(byplace), " precision:", dict(collections.Counter((e.get("place") or {}).get("precision") for e in events)))
    bysrc = collections.defaultdict(collections.Counter)
    for e in events:
        bysrc[(e.get("source") or {}).get("name")][e["_tier"][0]] += 1
    for s, c in bysrc.items():
        print("   ", s, dict(sorted(c.items())))
    return events


if __name__ == "__main__":
    allev = {}
    for name in ("sandiego", "austin"):
        p = os.path.join(B, name, "events.json")
        if os.path.exists(p):
            allev[name] = report(name, p)
    json.dump(allev, open(os.path.join(B, "classified.json"), "w", encoding="utf-8"), ensure_ascii=False, default=str)
