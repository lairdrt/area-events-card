"""Command line for the Discovery Service (works without Home Assistant).

    python scripts/discovery.py serve   [--db PATH] [--port 8780] [--bind 127.0.0.1 --allow-remote]    run the service until stopped
    python scripts/discovery.py stop    [--db PATH] [--port 8780]                                      graceful stop through the API
    python scripts/discovery.py status  [--db PATH]
    python scripts/discovery.py areas   list | show ID | create NAME LAT LON RADIUS [--unit mi|km] [--intensity ...] [--source KEY ...]
                                        | update ID --revision N [--name ...] [--radius ...] [--source KEY ...] | enable ID | disable ID
    python scripts/discovery.py settings [--mode automatic|manual] [--intensity low|normal|high]
    python scripts/discovery.py pause | resume
    python scripts/discovery.py changes [--after N] [--limit N]
    python scripts/discovery.py run     [--source san-diego-city] [--now]         one job, outside the scheduler
    python scripts/discovery.py inspect [--limit N] [--source KEY] [--title TEXT]
    python scripts/discovery.py extract [--db PATH | --url http://host:8780 --token-file PATH] [--once]   an extraction worker (local or remote)
    python scripts/discovery.py events  [--area ID] [--source KEY] [--limit N] | --event KEY           normalized events

The commands other than `serve`, `stop` and `run` open the same database as the service would (this tool belongs to the Discovery host)
and change configuration through the same DiscoveryControl contract the API uses. No tokens are printed.
"""
import argparse
import json
import os
import signal
import ssl
import sys
import threading
import time
import tracemalloc
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone

from .app import DEDUPLICATORS, DEFAULT_DEDUPLICATION, KNOWN_SOURCES, build, build_api, load_tokens
from .contracts import CandidateQuery
from .control import ControlError, CreateAreaRequest, RevisionConflict, SettingsRequest, UpdateAreaRequest
from .events import EventQuery
from .extraction import ExtractionService, HttpProcessingClient, StoredCandidateExtractor
from .service import job_key_for

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data", "discovery", "discovery.db")


def _when(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ") if epoch else "-"


def _size(n):
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def _area_line(area):
    state = "enabled" if area.enabled else "DISABLED"
    return (f"{area.area_key}  rev {area.revision}  {state:<8} {area.intensity:<6} {area.name}  ({area.latitude:.4f}, {area.longitude:.4f}) "
            f"{area.radius:g} {area.unit}  sources: {', '.join(area.sources) or '-'}")


# --- one job ---------------------------------------------------------------------------------------------------------

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
    app = build(args.db, service_gate=False, deduplication=args.deduplication)
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


# --- the service -----------------------------------------------------------------------------------------------------

def cmd_serve(args):
    app = build(args.db, deduplication=args.deduplication)
    directory = os.path.dirname(os.path.abspath(args.db))
    tokens, token_path = load_tokens(directory, read=True, process=True)
    app.register_known_sources()
    if bool(args.tls_cert) != bool(args.tls_key):
        print("--tls-cert and --tls-key go together", file=sys.stderr)
        return 2
    try:
        server = build_api(app, tokens, host=args.bind, port=args.port, allow_remote=args.allow_remote, tls=(args.tls_cert, args.tls_key) if args.tls_cert else None,
                           allow_plain=args.allow_plain_remote)
    except ValueError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(f"read token (for Home Assistant): {os.path.normpath(os.path.join(directory, 'api-read-token'))}; processing token (remote extraction worker): "
          f"{os.path.normpath(os.path.join(directory, 'api-process-token'))}", flush=True)
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: app.host.request_stop())
    try:
        recovered = app.service.recover()
        server.start()
        app.host.start()
        if not args.no_extract:
            app.extraction.start()
        if not args.no_media:
            app.enrichment.start()
        host, port = server.address[:2]
        print(f"discovery service running: database {os.path.normpath(args.db)}", flush=True)
        print(f"control API {server.scheme}://{host}:{port}/v1/  (bearer token in {os.path.normpath(token_path)})" + (f"; recovered {recovered} expired lease(s)" if recovered else ""), flush=True)
        while not app.host.wait(1.0):
            pass
        print("stopping: finishing the current job...", flush=True)
    finally:
        server.stop()
        stopped = app.host.stop(timeout=120)
        app.close()
    print("stopped cleanly" if stopped else "stopped; a job was still running and will be recovered by lease expiry", flush=True)
    return 0


def _wait_for_stop(stop):
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
    while not stop.wait(1.0):
        pass


def cmd_extract(args):
    """An extraction worker: with --db it reads and writes through this host's database-owning service code; with --url it uses only the API."""
    extractor = StoredCandidateExtractor()
    app = None
    if args.url:
        try:
            with open(args.token_file, encoding="utf-8") as handle:
                token = handle.read().strip()
        except (OSError, TypeError):
            print("--token-file must name the file holding the processing token", file=sys.stderr)
            return 1
        service, where = ExtractionService(HttpProcessingClient(args.url, token, cafile=args.ca_file), extractor, time.time, worker_id=args.worker), args.url
    else:
        app = build(args.db, service_gate=False, deduplication=args.deduplication)
        service, where = app.extraction, os.path.normpath(args.db)
    try:
        if args.once:
            claimed = submitted = failed = 0
            while True:
                report = service.run_once()
                claimed, submitted, failed = claimed + report.claimed, submitted + report.submitted, failed + report.failed
                if not report.claimed:
                    break
            print(f"{where}: {claimed} item(s) claimed, {submitted} result(s) submitted, {failed} failed")
            return 0 if not failed else 1
        stop = threading.Event()
        service.start()
        print(f"extraction worker {args.worker} running against {where}; Ctrl+C to stop", flush=True)
        _wait_for_stop(stop)
        print("stopping...", flush=True)
        return 0 if service.stop(30) else 1
    finally:
        if app is not None:
            app.close()


def _event_line(e):
    return (f"{e.event_key}  {_when(e.starts_at)}  rev {e.revision}  {e.status:<8} {e.location_status:<16} {e.title[:48]}"
            f"  [{e.address or e.venue_name or (e.location_text or '-')[:40]}]  ({', '.join(e.sources)})")


def cmd_events(args):
    app = build(args.db, deduplication=args.deduplication)
    try:
        if args.event:
            detail = app.events.get(args.event)
            e = detail.event
            print(_event_line(e))
            print(f"  description  {(e.description or '-')[:200]}")
            print(f"  location     status {e.location_status}; venue {e.venue_name or '-'}; address {e.address or '-'}; text {(e.location_text or '-')[:120]}")
            print(f"  missing      {', '.join(e.missing) or '-'}; source {e.source_url or '-'}")
            for h in detail.history:
                changed = json.dumps(h["changed"], ensure_ascii=False)[:300] if h["changed"] else "-"
                print(f"  revision {h['revision']}  {_when(h['created_at'])}  {h['extractor']}@{h['version']}  changed: {changed}")
            for name, entry in detail.evidence.items():
                quote = next((r["quote"] for r in entry["evidence"] if r.get("quote")), None)
                print(f"  evidence     {name:<12} from {entry['origin']}" + (f': "{quote[:80]}"' if quote else ""))
            for link in detail.candidate_links:
                print(f"  candidate    {link['candidate_key']} ({link['source_key']}, {link['role']})")
            return 0
        events, cursor = [], None
        while len(events) < args.limit:
            page = app.events.query(EventQuery(area_key=args.area, source_key=args.source, after=cursor, limit=min(args.limit - len(events), 200)))
            events.extend(page.events)
            cursor = page.next_cursor
            if not cursor:
                break
        print(f"{len(events)} event(s)")
        for e in events:
            print(_event_line(e))
    finally:
        app.close()
    return 0


def cmd_stop(args):
    token_path = os.path.join(os.path.dirname(os.path.abspath(args.db)), "api-token")
    try:
        with open(token_path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError:
        print("no token file next to the database: is this the service's database?", file=sys.stderr)
        return 1
    secure = bool(args.tls_cert)
    request = urllib.request.Request(f"{'https' if secure else 'http'}://{args.bind}:{args.port}/v1/service/stop", method="POST", data=b"",
                                     headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=10, **({"context": ssl.create_default_context(cafile=args.tls_cert)} if secure else {})) as response:
            print(f"stop requested (HTTP {response.status}); the service finishes its current job and exits")
    except (urllib.error.URLError, OSError) as error:
        print(f"could not reach the service: {getattr(error, 'reason', error)}", file=sys.stderr)
        return 1
    return 0


# --- configuration and inspection ------------------------------------------------------------------------------------

def _guard(function):
    def wrapped(args):
        try:
            return function(args)
        except RevisionConflict as error:
            print(f"conflict: {error} (read the current revision with 'areas show')", file=sys.stderr)
            return 3
        except ControlError as error:
            print(f"refused ({error.code}): {error}", file=sys.stderr)
            return 2
    return wrapped


@_guard
def cmd_areas(args):
    app = build(args.db, deduplication=args.deduplication)
    try:
        app.register_known_sources()
        control = app.control
        if args.action == "list":
            areas = control.list_areas()
            print(f"{len(areas)} area(s)")
            for area in areas:
                print(_area_line(area))
        elif args.action == "show":
            area = control.get_area(args.id)
            print(_area_line(area))
            print(f"created {_when(area.created_at)}, updated {_when(area.updated_at)}")
        elif args.action == "create":
            result = control.create_area(CreateAreaRequest(args.name, args.latitude, args.longitude, args.radius, args.unit, args.intensity, True, tuple(args.source or ()), args.request_id))
            print(("already exists (idempotent): " if result.replayed else "created: ") + _area_line(result.area))
            print(f"saved; change record {result.change_seq}; the scheduler picks it up on its next planning step")
        elif args.action == "update":
            result = control.update_area(UpdateAreaRequest(args.id, args.revision, args.name, args.latitude, args.longitude, args.radius, args.unit, args.intensity, None,
                                                           None if args.source is None else tuple(args.source)))
            print(("no change (idempotent): " if result.replayed else "updated: ") + _area_line(result.area))
        else:
            result = control.set_area_enabled(args.id, args.action == "enable")
            print(("no change (idempotent): " if result.replayed else "saved: ") + _area_line(result.area))
    finally:
        app.close()
    return 0


@_guard
def cmd_settings(args):
    app = build(args.db, deduplication=args.deduplication)
    try:
        control = app.control
        if args.mode or args.intensity:
            current = control.get_status().settings
            result = control.set_discovery_mode(SettingsRequest(args.revision or current.revision, args.mode, args.intensity))
            print(("no change (idempotent): " if result.replayed else "saved: ") + f"mode {result.settings.mode}, intensity {result.settings.intensity}, revision {result.settings.revision}")
        else:
            s = control.get_status().settings
            print(f"mode {s.mode}, intensity {s.intensity}, {'PAUSED' if s.paused else 'running'}, revision {s.revision}")
    finally:
        app.close()
    return 0


@_guard
def cmd_pause_resume(args):
    app = build(args.db, deduplication=args.deduplication)
    try:
        result = app.control.pause() if args.command == "pause" else app.control.resume()
        print(("already " + ("paused" if args.command == "pause" else "running") + " (idempotent)") if result.replayed
              else ("paused" if args.command == "pause" else "resumed") + f"; change record {result.change_seq}")
    finally:
        app.close()
    return 0


def cmd_status(args):
    app = build(args.db, deduplication=args.deduplication)
    try:
        status = app.reader.status()
        control = app.control.get_status()
        work = app.processing.status(StoredCandidateExtractor.name, StoredCandidateExtractor.version)
        app_media = app.media_work.status()
    finally:
        app.close()
    s = control.settings
    print(f"schema        {status.schema} version {status.schema_version}")
    print(f"database      {os.path.normpath(args.db)}: {_size(status.database_bytes)}, WAL {_size(status.wal_bytes)}")
    print(f"settings      mode {s.mode}, intensity {s.intensity}, {'PAUSED' if s.paused else 'running'} (revision {s.revision})")
    print(f"areas         {control.areas} ({control.areas_enabled} enabled)")
    for source in control.sources:
        print(f"source        {source.source_key}: {'used by an enabled area' if source.eligible else 'idle (no enabled area)'}; every {source.interval_s} s; "
              f"last success {_when(source.last_success_at)}; job {source.job_state or '-'}, next {_when(source.next_run_at)}"
              + (f", attempts {source.attempts}, last error {source.last_error}" if source.last_error else ""))
    print(f"pages         {status.pages} known, {status.pages_fetched} fetched; contents {status.contents} ({_size(status.content_bytes)})")
    print(f"candidates    {status.candidates}; evidence {status.evidence}; latest change {status.changes_latest_seq}")
    print(f"jobs          ready {status.jobs.ready}, running {status.jobs.leased}, completed {status.jobs.done}, retrying {status.jobs.retrying}, failed {status.jobs.failed}")
    print(f"last success  {_when(status.last_success_at)}")
    media = app_media
    print(f"media         {media['candidates_with_media']} candidates enriched ({media['complete']} finished); {media['verified_places']} verified places with maps; "
          f"{media['files']} cached files ({_size(media['file_bytes'])})")
    print(f"events        {work.events} normalized; extraction pending {work.pending}, leased {work.leased}, retrying {work.retrying}, given up {work.failed}; "
          f"results {dict(work.extractions) or '-'}")
    return 0


def cmd_changes(args):
    app = build(args.db, deduplication=args.deduplication)
    try:
        batch = app.reader.changes(args.after, args.limit)
    finally:
        app.close()
    if batch.resync_required:
        print(f"resync required: the oldest retained change is {batch.oldest_retained_seq}")
        return 0
    print(f"{len(batch.changes)} change(s) after {args.after}; latest {batch.latest_seq}")
    for c in batch.changes:
        print(f"  {c.seq:>6}  {_when(c.created_at)}  {c.kind:<30} {c.op:<7} {c.entity_key[:70]}")
    return 0


def cmd_inspect(args):
    app = build(args.db, deduplication=args.deduplication)
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
    common = []

    def add(name, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--db", default=os.path.normpath(DEFAULT_DB))
        p.add_argument("--deduplication", choices=sorted(DEDUPLICATORS), default=DEFAULT_DEDUPLICATION, help="the event-matching strategy (default: the conservative exact match)")
        common.append(p)
        return p

    serve = add("serve", "run the service (scheduler, workers, control API) until stopped")
    serve.add_argument("--bind", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8780)
    serve.add_argument("--allow-remote", action="store_true", help="allow a bind address other than loopback (the API always needs its token)")
    serve.add_argument("--tls-cert", help="certificate file: serve https (required beyond loopback)")
    serve.add_argument("--tls-key", help="private key file for --tls-cert")
    serve.add_argument("--allow-plain-remote", action="store_true", help="allow a non-loopback bind WITHOUT TLS (tokens cross the network in plain text)")
    serve.add_argument("--no-media", action="store_true", help="do not run the media enrichment worker (no event photographs, venue photographs or maps)")
    serve.add_argument("--no-extract", action="store_true", help="do not run the local extraction worker (run `extract` separately or remotely)")
    stop = add("stop", "ask a running service to stop gracefully")
    stop.add_argument("--bind", default="127.0.0.1")
    stop.add_argument("--port", type=int, default=8780)
    stop.add_argument("--tls-cert", help="the service's certificate (trusted as the CA) when it serves https")
    run = add("run", "run the source's due discovery job once, outside the scheduler")
    run.add_argument("--source", choices=sorted(KNOWN_SOURCES), default="san-diego-city")
    run.add_argument("--pages", type=int)
    run.add_argument("--details", type=int)
    run.add_argument("--now", action="store_true")
    add("status", "configuration, jobs and what the database holds")
    areas = add("areas", "list, create, update, enable or disable discovery areas")
    areas.add_argument("action", choices=("list", "show", "create", "update", "enable", "disable"))
    areas.add_argument("id", nargs="?", help="area id (show, update, enable, disable) or name (create)")
    areas.add_argument("latitude", nargs="?", type=float)
    areas.add_argument("longitude", nargs="?", type=float)
    areas.add_argument("radius", nargs="?", type=float)
    areas.add_argument("--name")
    areas.add_argument("--unit", choices=("mi", "km"))
    areas.add_argument("--intensity", choices=("low", "normal", "high"))
    areas.add_argument("--source", action="append", help="a source key (repeat for several; on update this replaces the list)")
    areas.add_argument("--revision", type=int, help="the revision the change is based on (update)")
    areas.add_argument("--request-id", help="makes a create idempotent")
    settings = add("settings", "show or change the discovery mode and intensity")
    settings.add_argument("--mode", choices=("automatic", "manual"))
    settings.add_argument("--intensity", choices=("low", "normal", "high"))
    settings.add_argument("--revision", type=int)
    add("pause", "pause discovery (persists across restarts)")
    add("resume", "resume discovery")
    changes = add("changes", "the durable change feed")
    changes.add_argument("--after", type=int, default=0)
    changes.add_argument("--limit", type=int, default=20)
    inspect = add("inspect", "stored candidates with their pages and evidence")
    inspect.add_argument("--limit", type=int, default=20)
    inspect.add_argument("--source")
    inspect.add_argument("--title")
    extract = add("extract", "run an extraction worker against this database or a remote service")
    extract.add_argument("--url", help="the Discovery Service address; the worker then uses only the API")
    extract.add_argument("--token-file", help="the file holding the processing token (with --url)")
    extract.add_argument("--worker", default="extractor-cli")
    extract.add_argument("--ca-file", help="certificate to trust for an https --url with a private certificate")
    extract.add_argument("--once", action="store_true", help="process what is pending, then exit")
    events = add("events", "normalized events, or one event's detail with --event")
    events.add_argument("--area")
    events.add_argument("--source")
    events.add_argument("--limit", type=int, default=20)
    events.add_argument("--event")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if args.command == "areas":
        if args.action == "create":  # `areas create NAME LAT LON RADIUS`
            if None in (args.id, args.latitude, args.longitude, args.radius):
                parser.error("areas create needs NAME LATITUDE LONGITUDE RADIUS")
            args.name, args.unit, args.intensity = args.id, args.unit or "mi", args.intensity or "normal"
        elif args.action == "update" and args.revision is None:
            parser.error("areas update needs --revision")
    handlers = {"run": cmd_run, "serve": cmd_serve, "stop": cmd_stop, "status": cmd_status, "areas": cmd_areas, "settings": cmd_settings,
                "pause": cmd_pause_resume, "resume": cmd_pause_resume, "changes": cmd_changes, "inspect": cmd_inspect, "extract": cmd_extract,
                "events": cmd_events}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
