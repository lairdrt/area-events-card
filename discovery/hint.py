"""Record a location hint for the discovery backend.

    python -m discovery.hint --source home_assistant --latitude 30.27 --longitude -97.74 --timezone America/Chicago

The discovery service cannot read Home Assistant's or a browser's location itself, and it exposes no endpoint
that accepts one (an unauthenticated write endpoint would let anyone redirect discovery). This local command is
the supported way to hand it a location: it writes `location-hint.json` in the data directory, which the next
refresh uses only when the configuration has no explicit `location`.
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

from .location import HINT_FILE, HINT_SOURCES, read_hints
from .publish import write_atomic
from .refresh import DATA_DIR


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m discovery.hint", description="Record a Home Assistant or browser location hint.")
    parser.add_argument("--data-dir", default=DATA_DIR, help="the discovery data directory (default: data/)")
    parser.add_argument("--source", choices=HINT_SOURCES, required=True)
    parser.add_argument("--latitude", type=float)
    parser.add_argument("--longitude", type=float)
    parser.add_argument("--timezone", help="IANA timezone, e.g. Home Assistant's time_zone setting")
    parser.add_argument("--name", help="display name")
    parser.add_argument("--accuracy-m", type=float, help="reported accuracy in meters (browser locations)")
    parser.add_argument("--clear", action="store_true", help="remove this source's hint")
    args = parser.parse_args(argv)

    path = os.path.join(args.data_dir, HINT_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        document = {}
    if args.clear:
        document.pop(args.source, None)
    else:
        if args.latitude is None or args.longitude is None:
            parser.error("--latitude and --longitude are required")
        entry = {"latitude": args.latitude, "longitude": args.longitude, "timezone": args.timezone, "display_name": args.name,
                 "accuracy_m": args.accuracy_m, "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        document[args.source] = {k: v for k, v in entry.items() if v is not None}
        # Validate exactly as the backend will, so a typo cannot silently break the next refresh.
        with tempfile.TemporaryDirectory() as folder:
            probe = os.path.join(folder, HINT_FILE)
            with open(probe, "w", encoding="utf-8") as handle:
                json.dump({args.source: document[args.source]}, handle)
            hints, problems = read_hints(probe)
        if problems or args.source not in hints:
            print("; ".join(problems) or "invalid location", file=sys.stderr)
            return 1
    os.makedirs(args.data_dir, exist_ok=True)
    write_atomic(path, json.dumps(document, indent=2) + "\n")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
