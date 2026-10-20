"""Command line: python -m discovery [--publish PATH] [--only KEY ...] [--dry-run] ..."""
import argparse
import sys

from .refresh import CONFIG_FILE, load_config, refresh


def main():
    parser = argparse.ArgumentParser(prog="python -m discovery",
                                     description="Refresh data/events.json from the sources in discovery.config.json.")
    parser.add_argument("--config", default=CONFIG_FILE, help="source configuration (default: discovery.config.json)")
    parser.add_argument("--output", help="local feed file (default: data/events.json)")
    parser.add_argument("--publish", metavar="PATH", help="also publish the validated feed to PATH (the HA www events.json)")
    parser.add_argument("--only", nargs="+", metavar="KEY", help="refresh only these sources")
    parser.add_argument("--no-details", action="store_true", help="skip per-event page requests")
    parser.add_argument("--limit", type=int, help="max events per source (for testing)")
    parser.add_argument("--offline", action="store_true", help="rebuild the feed from cached source data without any requests")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report without writing")
    args = parser.parse_args()
    options = {"only": args.only, "no_details": args.no_details, "limit": args.limit, "dry_run": args.dry_run, "offline": args.offline}
    sys.exit(refresh(load_config(args.config), options, output=args.output, publish_to=args.publish))


if __name__ == "__main__":
    main()
