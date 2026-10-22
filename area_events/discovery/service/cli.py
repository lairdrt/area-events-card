"""Command line for the Discovery Service (works without Home Assistant).

    python scripts/discovery.py run     [--source san-diego-city] [--db PATH] [--pages N] [--details N] [--now]
    python scripts/discovery.py status  [--db PATH]
    python scripts/discovery.py inspect [--db PATH] [--limit N] [--source KEY] [--title TEXT]

`run` registers the source, recovers expired leases, runs the due job (with --now: makes it due first) and reports what was stored.
No credentials are read or printed.
"""
import argparse
import os
import sys
import time
import tracemalloc
from dataclasses import replace
from datetime import datetime, timezone

from .app import KNOWN_SOURCES, build
from .contracts import CandidateQuery
from .service import job_key_for

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data", "discovery", "discovery.db")


def _when(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ") if epoch else "-"


def _size(n):
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def cmd_run(args):
    spec = KNOWN_SOURCES[args.source]
    options = dict(spec.options)
    if args.pages is not None:
        options["pages"] = args.pages
    if args.details is not None:
        options["max_details_per_run"] = args.details
    spec = replace(spec, options=tuple(sorted(options.items())))
    tracemalloc.start()
    started = time.perf_counter()
    app = build(args.db)
    try:
        source = app.service.register_source(spec)
        recovered = app.service.recover()
        if args.now:
            app.service.make_due(spec.source_key)
        report = app.service.run_once()
        status = app.reader.status()
    finally:
        app.close()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"database      {os.path.normpath(args.db)}")
    print(f"source        {source.source_key} ({source.name}) {source.base_url}")
    print(f"job           {job_key_for(source.source_key)}: {report.outcome}" + (f" ({report.error})" if report.error else ""))
    if recovered:
        print(f"recovered     {recovered} expired lease(s)")
    print(f"requests      {report.requests} (pages fetched through SafeFetcher; robots.txt and per-host spacing applied)")
    print(f"pages         {report.pages_seen} seen, {report.pages_new} new URLs; content {report.content_new} new, {report.content_unchanged} unchanged (stored once)")
    print(f"candidates    {report.candidates_seen} seen: {report.candidates_new} new, {report.candidates_changed} changed, {report.candidates_unchanged} unchanged; "
          f"{report.details_reused} event page(s) reused")
    print(f"evidence      {report.evidence_written} reference(s) written; {report.changes_appended} change record(s) appended")
    print(f"totals        {status.pages} pages ({status.pages_fetched} fetched), {status.candidates} candidates, {status.contents} contents ({_size(status.content_bytes)}), "
          f"latest change {status.changes_latest_seq}")
    print(f"files         database {_size(status.database_bytes)}, WAL {_size(status.wal_bytes)}")
    print(f"timing        {time.perf_counter() - started:.1f} s total; writes {status.write_stats}; peak Python memory {_size(peak)} (development machine)")
    return 0 if report.outcome in ("completed", "nothing_due") else 1


def cmd_status(args):
    app = build(args.db)
    try:
        status = app.reader.status()
        with app.store.read() as connection:
            job_rows = connection.execute("SELECT j.job_key, j.state, j.attempts, j.runs, j.next_run_at, j.last_error FROM jobs j ORDER BY j.job_key").fetchall()
    finally:
        app.close()
    print(f"schema        {status.schema} version {status.schema_version}")
    print(f"database      {os.path.normpath(args.db)}: {_size(status.database_bytes)}, WAL {_size(status.wal_bytes)}")
    for source in status.sources:
        print(f"source        {source.source_key} ({source.adapter}) every {source.interval_s} s; last success {_when(source.last_success_at)}; last attempt {_when(source.last_attempt_at)}")
    print(f"pages         {status.pages} known, {status.pages_fetched} fetched; contents {status.contents} ({_size(status.content_bytes)})")
    print(f"candidates    {status.candidates}; evidence {status.evidence}; latest change {status.changes_latest_seq}")
    print(f"jobs          ready {status.jobs.ready}, running {status.jobs.leased}, completed {status.jobs.done}, retrying {status.jobs.retrying}, failed {status.jobs.failed}")
    for row in job_rows:
        print(f"  {row['job_key']}: {row['state']}, {row['runs']} run(s), attempts {row['attempts']}, next {_when(row['next_run_at'])}" + (f", last error {row['last_error']}" if row["last_error"] else ""))
    print(f"last success  {_when(status.last_success_at)}")
    return 0


def cmd_inspect(args):
    app = build(args.db)
    try:
        records = app.reader.candidates(CandidateQuery(source_key=args.source, title_contains=args.title, limit=args.limit))
        detailed = [app.reader.candidate(r.candidate_key) for r in records]
        pages = {}
        for record in detailed:
            if record.url_key and record.url_key not in pages:
                pages[record.url_key] = app.reader.page(record.url_key)
        recent = app.reader.recent_pages(min(args.limit, 10))
    finally:
        app.close()
    print(f"{len(detailed)} candidate(s)")
    for record in detailed:
        page = pages.get(record.url_key)
        print(f"\n- {record.title}")
        print(f"  key       {record.candidate_key}  (source {record.source_key}, {record.extractor})")
        print(f"  starts    {_when(record.starts_at)}   first seen {_when(record.first_seen_at)}, last seen {_when(record.last_seen_at)}")
        if page:
            print(f"  page      {page.url}  [{page.status}{', ' + _size(page.bytes) if page.bytes else ''}; fetched {_when(page.fetched_at)}; sources {', '.join(f'{k}/{r}' for k, r in page.sources)}]")
        for ref in record.evidence:
            quote = f'"{ref.quote[:90]}"' if ref.quote else "-"
            print(f"  evidence  {ref.field:<12} {ref.kind:<9} {quote}" + (f"  ({ref.note[:80]})" if ref.note and not ref.quote else ""))
    print("\nrecent pages")
    for page in recent:
        print(f"  {page.status:<11} {page.url}  sources {', '.join(f'{k}/{r}' for k, r in page.sources)}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="discovery", description="The Area Events Discovery Service.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the source's due discovery job once")
    run.add_argument("--source", choices=sorted(KNOWN_SOURCES), default="san-diego-city")
    run.add_argument("--pages", type=int, help="listing pages to read (default 2)")
    run.add_argument("--details", type=int, help="event pages to read in this run (default 10)")
    run.add_argument("--now", action="store_true", help="make the job due now even if it ran recently")
    status = sub.add_parser("status", help="what the database holds")
    inspect = sub.add_parser("inspect", help="stored candidates with their pages and evidence")
    inspect.add_argument("--limit", type=int, default=20)
    inspect.add_argument("--source")
    inspect.add_argument("--title")
    for p in (run, status, inspect):
        p.add_argument("--db", default=os.path.normpath(DEFAULT_DB))
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    return {"run": cmd_run, "status": cmd_status, "inspect": cmd_inspect}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
