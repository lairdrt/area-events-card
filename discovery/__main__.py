"""Command line: python -m discovery [--no-details] [--limit N] [--dry-run]"""
import argparse
import sys
from datetime import datetime, timezone

from .core import DATA_FILE, FetchError, Fetcher, load_events, save_events
from .sources import kpbs

SOURCES = [kpbs]


def refresh(args):
    previous = (load_events(args.output) or {}).get("events", [])
    fetcher = Fetcher()
    names = {s.NAME for s in SOURCES}
    events = [e for e in previous if e.get("source", {}).get("name") not in names]
    report, failed = {}, False

    for source in SOURCES:
        try:
            found, notes = source.discover(fetcher, previous, details=not args.no_details, limit=args.limit)
            if not found:
                raise FetchError("extraction produced no events")
        except FetchError as error:
            # A failing source keeps its previous events rather than losing them.
            print(f"[{source.NAME}] not refreshed: {error}", file=sys.stderr)
            events.extend(e for e in previous if e.get("source", {}).get("name") == source.NAME)
            failed = True
            continue
        events.extend(found)
        report[source.NAME] = notes
        print(f"[{source.NAME}] {len(found)} future events; {notes}")

    if not report:
        print("Nothing refreshed; existing data left untouched.", file=sys.stderr)
        return 1

    events.sort(key=lambda e: e.get("start") or "")
    document = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": report,
        "events": events,
    }
    if args.dry_run:
        print("Dry run: not writing", args.output)
    else:
        save_events(document, args.output)
        print(f"Wrote {len(events)} events to {args.output}")
    return 2 if failed else 0


def main():
    parser = argparse.ArgumentParser(prog="python -m discovery", description="Refresh data/events.json from public sources.")
    parser.add_argument("--output", default=DATA_FILE, help="events file (default: data/events.json)")
    parser.add_argument("--no-details", action="store_true", help="skip per-event page requests")
    parser.add_argument("--limit", type=int, help="max events per source (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report without writing")
    sys.exit(refresh(parser.parse_args()))


if __name__ == "__main__":
    main()
