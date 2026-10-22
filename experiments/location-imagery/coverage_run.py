"""Measure what the production media service makes of real feeds (collect_feeds.py output): how many places get a trusted map pin,
how many get a verified venue photograph, and why the rest are refused. Slow by design (polite request spacing); several minutes.

    python experiments/location-imagery/coverage_run.py [folder]     # default: review/location-imagery/coverage (git-ignored)
"""
import collections
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "area_events"))

from discovery import media  # noqa: E402
from discovery.safe import SafeFetcher  # noqa: E402

BASE = os.path.join(REPO, "review", "location-imagery")
folder = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "coverage")
feeds = []
for name in ("sandiego", "austin"):
    document = json.load(open(os.path.join(BASE, name, "events.json"), encoding="utf-8"))
    feeds.append((document["location"], document["events"]))
total = sum(len(events) for _, events in feeds)
service = media.MediaService(folder, SafeFetcher(1.1))
started = time.time()
while True:
    done = service.cycle(feeds, limit=25)
    pending = service.pending(feeds)
    print(f"{time.time() - started:6.0f}s  handled {done}, pending {pending}, requests {service.stats['requests']}", flush=True)
    if not pending or not done and service.stats["failed"] > 40:
        break
records = service.store.index["records"]
annotated = service.annotate([e for _, events in feeds for e in events], feeds[0][1] and feeds[0][0])
print("\nplaces:", len(records), " resolved:", sum(1 for r in records.values() if r["ok"]), " with venue photo:", sum(1 for r in records.values() if r.get("photo")))
print("confidence:", dict(collections.Counter(r["location"]["confidence"] for r in records.values() if r["ok"])))
print("refusal reasons:", dict(collections.Counter(r.get("reason") for r in records.values() if not r["ok"]).most_common(6)))
covered = 0
for area, events in feeds:
    covered += sum(1 for e in service.annotate(events, area) if "media" in e)
print(f"events with a map: {covered} of {total} ({100 * covered / total:.1f}%)")
photo_events = sum(1 for area, events in feeds for e in service.annotate(events, area) if "photo" in e.get("media", {}))
print(f"events with a verified venue photo: {photo_events} of {total} ({100 * photo_events / total:.1f}%)")
for key, r in records.items():
    if r.get("photo"):
        print("  photo:", r["venue"][:40], "|", r["photo"]["license"], "|", r["photo"]["source_url"])
print("stats:", service.stats, "| storage:", sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(folder) for f in fs) // 1024, "KB")
