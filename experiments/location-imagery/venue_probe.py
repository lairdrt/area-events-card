import os as _os

REPO = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
LOC_DIR = _os.environ.get("AE_LOC_DIR") or _os.path.join(REPO, "review", "location-imagery")  # git-ignored working folder
import json, os, re, sys, time, random, urllib.parse, urllib.request, collections

B = LOC_DIR
UA = "area-events-card-research/0.1 (investigation; contact robin.laird@proton.me)"
data = json.load(open(os.path.join(B, "classified.json"), encoding="utf-8"))
random.seed(7)


def get(url):
    time.sleep(1.1)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def tokens(text):
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in {"the", "of", "and", "at", "in", "a", "san", "diego", "austin", "center", "park"} and len(t) > 2}


pairs = {}
for city, events in data.items():
    for e in events:
        if e["_tier"][0] in "ABC" and e.get("venue"):
            key = (city, e["venue"].strip()[:90], (e.get("address") or "").strip())
            pairs.setdefault(key, e)
keys = list(pairs)
random.shuffle(keys)
sample = []
per = collections.Counter()
for k in keys:
    if per[k[0]] < 30:
        sample.append(k)
        per[k[0]] += 1
print("unique venue/address pairs:", len(pairs), "sampled:", len(sample), dict(per))

results = []
for city, venue, address in sample:
    region = "San Diego, CA" if city == "sandiego" else "Austin, TX"
    q = address if address else f"{venue}, {region}"
    if address and not re.search(r"\b(CA|TX|California|Texas)\b", address):
        q = f"{address}, {region}"
    row = {"city": city, "venue": venue, "address": address, "query": q}
    try:
        hits = get("https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({"q": q, "format": "jsonv2", "limit": 1, "addressdetails": 1, "countrycodes": "us"}))
    except Exception as ex:
        row["error"] = str(ex)[:80]
        results.append(row)
        continue
    if hits:
        h = hits[0]
        a = h.get("address", {})
        number = re.match(r"\s*(\d+)", address or "")
        row.update(lat=float(h["lat"]), lon=float(h["lon"]), type=h.get("type"), cls=h.get("category"), place_rank=h.get("place_rank"),
                   house_number=a.get("house_number"), road=a.get("road"), display=h.get("display_name")[:110],
                   exact_number=bool(number and a.get("house_number") and number.group(1) == a["house_number"].split("-")[0]))
        # same venue on Wikipedia within 120 m?
        try:
            near = get("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({"action": "query", "list": "geosearch", "gscoord": f"{row['lat']}|{row['lon']}", "gsradius": 120, "gslimit": 6, "format": "json"}))["query"]["geosearch"]
        except Exception:
            near = []
        vt = tokens(venue)
        match = None
        for n in near:
            overlap = vt & tokens(n["title"])
            if vt and len(overlap) >= max(1, min(2, len(vt))) and len(overlap) / len(vt) >= 0.5:
                match = n
                break
        if match:
            try:
                page = get("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({"action": "query", "titles": match["title"], "prop": "pageimages|pageprops", "piprop": "original|name", "format": "json"}))["query"]["pages"]
                p = next(iter(page.values()))
                fname = p.get("pageimage")
                lic = None
                if fname:
                    info = get("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({"action": "query", "titles": "File:" + fname, "prop": "imageinfo", "iiprop": "extmetadata", "format": "json"}))["query"]["pages"]
                    meta = next(iter(info.values())).get("imageinfo", [{}])[0].get("extmetadata", {})
                    lic = meta.get("LicenseShortName", {}).get("value")
                row["wiki"] = {"title": match["title"], "dist_m": match["dist"], "image": fname, "license": lic}
            except Exception as ex:
                row["wiki"] = {"title": match["title"], "error": str(ex)[:60]}
    results.append(row)
    print(".", end="", flush=True)
json.dump(results, open(os.path.join(B, "venue_probe.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print()
n = len(results)
ok = [r for r in results if "lat" in r]
print("geocoded:", len(ok), "of", n)
print("by type:", dict(collections.Counter((r.get("cls"), r.get("type")) for r in ok).most_common(8)))
print("house-level (place_rank>=30):", sum(1 for r in ok if (r.get("place_rank") or 0) >= 30), " exact house number matched:", sum(1 for r in ok if r.get("exact_number")), " with a street-address query:", sum(1 for r in ok if re.match(r"\s*\d", r["address"] or "")))
w = [r for r in ok if r.get("wiki")]
print("Wikipedia venue matches within 120 m:", len(w), "with an image:", sum(1 for r in w if r["wiki"].get("image")), "licenses:", dict(collections.Counter(r["wiki"].get("license") for r in w if r["wiki"].get("image"))))
for r in w[:12]:
    print("  ", r["venue"][:40], "|", r["wiki"])
