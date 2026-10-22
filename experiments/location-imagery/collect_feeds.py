import os as _os

REPO = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
LOC_DIR = _os.environ.get("AE_LOC_DIR") or _os.path.join(REPO, "review", "location-imagery")  # git-ignored working folder
import json, os, sys
sys.path.insert(0, _os.path.join(REPO, "area_events"))
from discovery import refresh
from discovery.refresh import load_config

OUT = LOC_DIR
os.makedirs(OUT, exist_ok=True)
for name, area in (("sandiego", "San Diego, CA"), ("austin", "Austin, TX")):
    folder = os.path.join(OUT, name)
    os.makedirs(folder, exist_ok=True)
    config = load_config()
    config["location"] = {"query": area, "radius_miles": 25, "country": "US"}
    code = refresh.refresh(config, {"interval": 2.0}, os.path.join(folder, "cache.json"), os.path.join(folder, "events.json"),
                           os.path.join(folder, "lock"), location_cache_path=os.path.join(folder, "loc.json"))
    print(name, "exit", code, flush=True)
