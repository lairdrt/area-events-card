"""Event Analysis Workbench v1: run a website (or a saved page) through the production pipeline and show every stage's work product.

It calls the real functions, in the order production uses them, and reports what they returned; it has no analysis engine of its own.

    Event Discovery          sources/web.crawl (pages, feeds, embedded calendars, linked documents) with a recording client
    Information Extraction   sources/web.discover (candidates -> events), core.normalize_event_text
    Geographic Resolution    place.locate_events, media.candidate_for and, when asked for, media.MediaService.resolve (rate-limited)
    Validation, Dedup        refresh.build_document (merge, expiry, schema) and publish.validate_feed
    Event Classification     classifiers.current().classify / classifiers.annotate (through the replaceable classifier contract)
    Output                   the feed document's events, in the existing Area Events schema

Where a stage has no separate entry point yet, its results are shown as the existing code produced them; nothing is invented. A fixture
(`fixture` mode) is served from disk and makes no network request. A website (`url` mode) is read only through SafeFetcher, with its robots.txt,
public-address and budget rules. Reports are written to review/ (Git-ignored): a JSON report and a standalone HTML report.
"""
import argparse
import base64
import html
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import classifiers, classify, evidence, media, refresh, registry
from .core import FetchError, normalize_event_text
from .place import locate_events
from .profiles import ProfileError, normalize_url
from .publish import FeedError, validate_feed
from .safe import BlockedError, Response, SafeFetcher
from .sources import web, webdiscover, webparse

VERSION = 1
SOURCE_ID = "web_workbench"
AREAS = {
    "san-diego": {"display_name": "San Diego, CA", "latitude": 32.7157, "longitude": -117.1611, "radius_miles": 25, "timezone": "America/Los_Angeles",
                  "timezone_source": "workbench preset", "country": "US", "region": "CA", "source": "workbench preset", "query": "San Diego, CA"},
    "austin": {"display_name": "Austin, TX", "latitude": 30.2672, "longitude": -97.7431, "radius_miles": 25, "timezone": "America/Chicago",
               "timezone_source": "workbench preset", "country": "US", "region": "TX", "source": "workbench preset", "query": "Austin, TX"},
}
DEFAULT_BASE = "https://fixture.example/events"
MAX_SHOWN = 80
MAX_TEXT = 400
STAGES = [
    ("discovery", "Event Discovery", "sources/web.py (crawl), sources/webdiscover.py, sources/webfeeds.py, sources/webparse.py, safe.py"),
    ("extraction", "Information Extraction", "sources/web.py (discover), sources/webparse.py, sources/webtext.py, sources/webtime.py, core.py (normalize_event_text)"),
    ("classification", "Event Classification", "classifiers.py (contract), classify.py (production implementation), photo_subject.py"),
    ("geolocation", "Geographic Resolution", "place.py (locate_events), media.py (candidate_for, MediaService.resolve), textloc.py, location.py"),
    ("validation", "Validation and Deduplication", "refresh.py (build_document), merge.py, publish.py (validate_feed), images.py"),
    ("output", "Home Assistant Presentation (input: the feed)", "refresh.py (feed document), engine.py (snapshot), the card"),
]


# --- clients: a recording client in front of SafeFetcher, or a fixture served from memory ----------------------------------

class FixtureClient:
    """Serves saved pages by address and makes no network request; anything else is refused."""

    def __init__(self, pages):
        self.pages = pages

    def fetch(self, url, check_robots=True, binary=None, timeout=None):
        entry = self.pages.get(url.split("#")[0])
        if entry is None:
            raise FetchError("offline fixture analysis: this address is not in the fixture (no network request is made)")
        body, content_type = entry
        if isinstance(body, bytes):
            if not binary:
                raise FetchError("not a text document")
            return Response(url, 200, content_type, "", body)
        return Response(url, 200, content_type, body)


class RecordingClient:
    """Passes every request to the real client and keeps what was asked, what came back and the page text (for locating evidence)."""

    def __init__(self, inner):
        self.inner, self.log, self.bodies = inner, [], {}

    def fetch(self, url, check_robots=True, binary=None, timeout=None):
        entry = {"url": url, "binary": bool(binary)}
        self.log.append(entry)
        try:
            response = self.inner.fetch(url, binary=binary) if binary else self.inner.fetch(url)
        except FetchError as error:
            entry.update(ok=False, error=str(error)[:200], refused=isinstance(error, BlockedError))
            raise
        size = len(response.body) if binary else len(response.text.encode("utf-8", "ignore"))
        entry.update(ok=True, status=response.status, content_type=(response.content_type or "")[:80], bytes=size,
                     final_url=response.url if response.url != url else None)
        if not binary:
            self.bodies.setdefault(url, response.text)
        return response


def load_fixture(path, base_url=None):
    """({url: (body, content_type)}, origin url) from a saved .html page, or a .json file: a manifest {"base_url", "pages": {url: text | {"body",
    "content_type"} | {"body_base64", "content_type"}}} or a schema.org JSON-LD document (served as a page's structured data)."""
    with open(path, "rb") as handle:
        raw = handle.read()
    name = os.path.basename(path).lower()
    if name.endswith((".html", ".htm")):
        origin = base_url or DEFAULT_BASE
        return {origin: (raw.decode("utf-8", "replace"), "text/html")}, origin
    if not name.endswith(".json"):
        raise ValueError("a fixture is a saved .html page or a .json file")
    data = json.loads(raw.decode("utf-8"))
    if isinstance(data, dict) and isinstance(data.get("pages"), dict):
        pages = {}
        for url, entry in data["pages"].items():
            if isinstance(entry, str):
                pages[url] = (entry, "text/html")
            elif isinstance(entry, dict) and "body_base64" in entry:
                pages[url] = (base64.b64decode(entry["body_base64"]), entry.get("content_type") or "application/pdf")
            elif isinstance(entry, dict) and isinstance(entry.get("body"), str):
                pages[url] = (entry["body"], entry.get("content_type") or "text/html")
            else:
                raise ValueError(f"fixture page {url!r} needs a body")
        origin = base_url or data.get("base_url") or next(iter(pages), None)
        if origin not in pages:
            raise ValueError("the fixture's base_url is not one of its pages")
        return pages, origin
    if isinstance(data, (dict, list)) and "@" in json.dumps(data)[:4000]:
        origin = base_url or DEFAULT_BASE
        page = "<!doctype html><html><head><title>fixture</title></head><body><script type=\"application/ld+json\">" + json.dumps(data) + "</script></body></html>"
        return {origin: (page, "text/html")}, origin
    raise ValueError("this JSON is neither a fixture manifest nor schema.org JSON-LD")


# --- text for locating evidence ---------------------------------------------------------------------------------------------

def visible_text(markup):
    markup = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)</?(?:a|b|strong|em|i|u|span|font|small|sup|sub|mark|abbr|code)\b[^>]*>", "", markup)  # inline tags do not break the text
    markup = re.sub(r"(?i)</?(p|div|li|ul|ol|tr|td|th|h[1-6]|br|section|article|header|footer|nav|table|dd|dt)\b[^>]*>", "\n", markup)
    return evidence.squeeze(re.sub(r"<[^>]+>", " ", markup))


def evidence_sources(bodies, first):
    """[(url, found_in, text)] for every page read, the pages most likely to hold an event first."""
    order = sorted(bodies, key=lambda u: (u not in first, u))
    out = []
    for url in order:
        body = webdiscover.unwrap(bodies[url])
        out.append((url, "visible text", visible_text(body)))
        out.append((url, "page markup", evidence.squeeze(body)))
    return out


# --- the pipeline ----------------------------------------------------------------------------------------------------------

def _short(value, limit=MAX_TEXT):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _candidate_view(candidate, kept):
    view = {k: _short(candidate.get(k), 300) for k in ("kind", "title", "start", "end", "venue", "address", "url", "organizer", "price") if candidate.get(k)}
    if candidate.get("description"):
        view["description"] = _short(candidate["description"], 300)
    if candidate.get("evidence_text"):
        view["source_text"] = _short(candidate["evidence_text"], 300)
    view["reached_output"] = kept
    return view


def _find_candidate(event, candidates):
    for c in candidates:
        if (c.get("title") or "").casefold() == (event.get("title") or "").casefold() and (c.get("url") or event["url"]) == event["url"]:
            return c
    return next((c for c in candidates if (c.get("title") or "").casefold() == (event.get("title") or "").casefold()), None)


def _term_pattern(term):
    return re.compile(r"\b" + re.escape(term.split()[0][:max(3, len(term.split()[0]) - 1)]) + r"\w*", re.I)


def classification_evidence(event, decision, classifier_name):
    refs = []
    fields = {"title": event.get("title"), "description": (event.get("description") or "")[:classify.DESCRIPTION_CHARS], "venue": event.get("venue"),
              "source tag": " | ".join(event.get("categories") or [])}
    for reason in decision.get("reasons", []):
        match = re.match(r'^(title|description|venue|source tag) "(.+)"$', reason)
        if not match:
            refs.append(evidence.reference("category", decision["category"], "inferred", f"classifiers/{classifier_name}", note=f"{reason}; scores {json.dumps(decision.get('scores', {}))}"))
            continue
        where, term = match.groups()
        text = fields.get(where) or ""
        found = _term_pattern(term).search(text)
        ref = evidence.reference("category", decision["category"], "inferred", f"classifiers/{classifier_name}", quote=found.group(0) if found else None,
                                 context=text[max(0, found.start() - 60):found.end() + 60] if found else None,
                                 note=f'{reason}: a lexicon term, matched by word stem in the event\'s {where}' + ("" if found else "; the matching words were not found verbatim"))
        ref["source_field"] = where
        refs.append(ref)
    return refs


def event_evidence(event, adapter_event, candidate, decision, classifier_name, geo, sources):
    extraction = (adapter_event or event).get("extraction") or {}
    method = extraction.get("method") or "unknown"
    recorded_by = f"sources/web ({method})"
    refs = []

    def located(field, needle, kind="extracted", note=None, recorder=recorded_by):
        if not needle:
            return
        found = evidence.locate(needle, sources)
        if not found and "," in needle and field == "address":
            part = evidence.locate(needle.split(",")[0], sources)
            if part:
                refs.append(evidence.reference(field, event.get(field) if field in event else needle, kind, recorder, **part,
                                               note="only the first part is in the source; the full value is composed from the source's structured fields (street, locality, region, postal code)"))
                return
        if found:
            refs.append(evidence.reference(field, event.get(field) if field in event else needle, kind, recorder, note=note, **found))
        else:
            refs.append(evidence.reference(field, event.get(field) if field in event else needle, kind, recorder,
                                           note=(note + "; " if note else "") + "not found verbatim in any page read (the value was cleaned, normalised or read from a document)"))

    located("title", event.get("title"), note="what remained of the text line after its date, time and address were taken out" if method in ("text", "pdf") else None)
    if extraction.get("source_text"):
        located("start", extraction["source_text"], "inferred", f"the date and time were read from this text by sources/webtext.events_in and normalised to {event.get('start')}")
    elif candidate and candidate.get("start"):
        located("start", candidate["start"], "inferred", f"the source's own date value, normalised to {event.get('start')} by sources/webtime.parse_when")
    for field in ("venue", "address"):
        if event.get(field):
            located(field, (candidate or {}).get(field) or event[field])
    if event.get("description"):
        located("description", evidence.squeeze(event["description"])[:60], note="the first 60 characters of the stored description")
    refs.extend(classification_evidence(event, decision, classifier_name))
    for line in (event.get("verification") or {}).get("evidence", []):
        refs.append(evidence.reference("verification", line, "recorded", "the source adapter", note="written by the adapter when it read the event"))
    for item in geo.get("candidates", []):
        text = item.get("text")
        found = (evidence.locate(text, sources) or (evidence.locate(text.split(",")[0], sources) if "," in text else None)) if text else None
        ref = evidence.reference("location", text or item.get("kind"), "extracted" if found else "inferred", "media.candidate_for",
                                 note=f"{'an' if item['kind'][0] in 'aeiou' else 'a'} {item['kind']} candidate" + (f", read from the event's {item['found_in']}" if item.get("found_in") else ""), **(found or {}))
        refs.append(ref)
    lookup = geo.get("lookup")
    if lookup and lookup.get("ok"):
        refs.append(evidence.reference("location", f"{lookup['location']['lat']:.5f}, {lookup['location']['lon']:.5f}", "verified", "media.MediaService.resolve",
                                       note=lookup["location"]["provenance"]))
    return refs


def geolocation_view(event, location, service, budget, offline):
    candidate = media.candidate_for(event, location)
    options = []
    option = candidate
    while option:
        text = option.get("address") or option.get("venue") or (option.get("intersection") or {}).get("a_text")
        if option["kind"] == "intersection":
            text = f"{option['intersection']['a_text']} and {option['intersection']['b_text']}"
        options.append({"kind": option["kind"], "text": text, "found_in": option.get("found_in"), "names": option.get("names"), "key": option["key"]})
        option = option.get("then")
    view = {"place": {k: v for k, v in (event.get("place") or {}).items() if k in ("classification", "confidence", "precision", "latitude", "longitude", "city", "evidence")},
            "candidates": options, "lookup": None}
    if not candidate:
        view["status"] = "no location candidate: the event record names no venue, address, coordinates or corner the extractor recognises"
    elif candidate["kind"] == "source":
        view["status"] = "source coordinates (no lookup needed)"
        view["lookup"] = {"ok": True, "location": {"lat": candidate["lat"], "lon": candidate["lon"], "provenance": candidate["provenance"], "confidence": "source",
                                                   "precision": "venue", "approximate": False}}
    elif service is None:
        view["status"] = "not attempted: offline analysis" if offline else "not attempted: geocoding not requested (--geocode N)"
    elif budget["left"] <= 0:
        view["status"] = "not attempted: this run's geocoding limit was reached (pending)"
    else:
        budget["left"] -= 1
        try:
            record = service.resolve(candidate, draw=False, photos=False)
            view["lookup"] = {k: record.get(k) for k in ("ok", "kind", "reason", "location", "via", "earlier") if record.get(k) is not None}
            view["status"] = "verified" if record["ok"] else "refused"
        except (FetchError, ValueError, KeyError, TypeError, OSError) as error:
            view["status"] = f"unavailable: {type(error).__name__}: {str(error)[:150]} (a temporary failure, not a refusal)"
    return view


def analyze(target, fixture=False, base_url=None, area="san-diego", geocode=0, client=None, now=None):
    """The report (a JSON-ready dict) for a website address or a saved fixture. `client` replaces the network client (tests)."""
    now = now or datetime.now(timezone.utc)
    location = dict(AREAS[area]) if isinstance(area, str) else dict(area)
    offline = bool(fixture)
    if fixture:
        pages, url = load_fixture(target, base_url)
        inner = FixtureClient(pages)
        label = os.path.basename(target)
    else:
        try:
            url = normalize_url(target)
        except ProfileError as error:
            raise ValueError(str(error)) from error
        inner = client or SafeFetcher(2.0)
        label = url
    recorder = RecordingClient(inner)
    report = {"tool": {"name": "Event Analysis Workbench", "version": VERSION, "generated_at": now.isoformat(timespec="seconds"),
                       "mode": "fixture" if offline else "url", "target": label, "origin_url": url, "offline": offline,
                       "area": {k: location[k] for k in ("display_name", "latitude", "longitude", "radius_miles", "timezone")}},
              "limits": {"max_requests": web.MAX_REQUESTS, "time_budget_seconds": web.TIME_BUDGET, "max_hub_pages": web.MAX_HUBS, "max_pdfs": web.MAX_DOCS,
                         "max_detail_pages": web.MAX_DETAIL, "geocoding_lookups_allowed": 0 if offline else int(geocode)},
              "stages": {}, "evidence": {}, "notices": []}
    notices = report["notices"]

    # discovery: the production crawl
    web.clear_cache()
    candidates, crawl_notes, site_name, discovery_error = [], {}, None, None
    try:
        candidates, crawl_notes, site_name = web.crawl(recorder, url, True)
    except BlockedError as error:
        discovery_error = f"refused: {error}"
    except FetchError as error:
        discovery_error = f"could not be read: {error}"
    # extraction: the production adapter (it shares the crawl through the adapter's own cache; no request is repeated)
    events, discover_notes = [], {}
    if not discovery_error:
        events, discover_notes = web.discover(recorder, [], url=url, location=location, source_id=SOURCE_ID, source_name=site_name or urlsplit(url).hostname or "website")
    origin_body = recorder.bodies.get(url) or next(iter(recorder.bodies.values()), "")
    ranked = []
    if origin_body:
        page = webparse.scan(webdiscover.unwrap(origin_body), url)
        fetched = {entry["url"] for entry in recorder.log}
        ranked = [{"url": u, "score": round(s, 2), "text": t[:80], "followed": u in fetched} for s, u, t in webdiscover.rank_links(page.anchors, url, limit=12)]
    pages = [dict(entry) for entry in recorder.log][:MAX_SHOWN]
    not_fetched = [{"url": r["url"], "reason": "ranked as event-like but beyond the hub limit or the request budget"} for r in ranked if not r["followed"]]
    not_fetched += [{"url": d["url"], "reason": d.get("reason") or "not read"} for d in crawl_notes.get("documents", []) if not d.get("extracted")]
    report["stages"]["discovery"] = {
        "status": discovery_error or "read", "requests_made": len(recorder.log), "pages": pages, "format": crawl_notes.get("format"), "formats": crawl_notes.get("formats", {}),
        "feeds_read": crawl_notes.get("feeds", []), "embedded_calendars": crawl_notes.get("embedded_calendars", []), "hub_pages": crawl_notes.get("discovered", []),
        "documents": crawl_notes.get("documents", []), "links_ranked_by_the_event_link_scorer": ranked, "not_fetched": not_fetched,
        "limit_reached": bool(crawl_notes.get("limit_reached")), "failed_fetches": crawl_notes.get("failed_fetches", 0), "javascript_hint": crawl_notes.get("javascript_hint")}
    if discovery_error:
        notices.append({"stage": "discovery", "text": f"the website {discovery_error}"})
    elif not candidates:
        notices.append({"stage": "discovery", "text": "no event candidates were found on the pages read"})

    kept_titles = {(e["title"] or "").casefold() for e in events}
    report["stages"]["extraction"] = {
        "candidates_found": len(candidates), "events_produced": len(events), "skipped_with_reasons": discover_notes.get("skipped", {}),
        "candidates": [_candidate_view(c, (c.get("title") or "").casefold() in kept_titles) for c in candidates[:MAX_SHOWN]],
        "note": "a candidate with reached_output false was dropped by sources/web.discover; the reasons are counted in skipped_with_reasons (the adapter does not record them per candidate)"}

    # processing: exactly what refresh.refresh does after an adapter returns
    found = [normalize_event_text(e) for e in events]
    located, place_summary = locate_events(found, location, None)
    config = {"sources": [{"id": SOURCE_ID, "name": site_name or "Website", "adapter": "web", "url": url, "coverage": {"type": "global"}}], "verification": {}}
    selected = registry.load_sources(config)
    cache = {SOURCE_ID: {"events": located, "ok": True, "last_success_at": now.isoformat(timespec="seconds"), "notes": discover_notes}}
    document = refresh.build_document(config, cache, now, selected, location)
    final = document["events"]
    feed_check = "valid"
    try:
        validate_feed(document)
    except FeedError as error:
        feed_check = f"not publishable: {error}"

    # classification
    classifier = classifiers.current()
    class_rows, decisions = [], {}
    for event in final[:MAX_SHOWN]:
        decision = classifier.classify(event)
        decisions[event["id"]] = decision
        class_rows.append({"event": event["title"], "id": event["id"], "classifier": classifier.name, "decision": decision, "illustration": event.get("illustration"),
                           "contract_problems": classifiers.check_decision(decision),
                           "input_fields": {"title": event.get("title"), "description_read": (event.get("description") or "")[:classify.DESCRIPTION_CHARS],
                                            "description_chars_available": len(event.get("description") or ""), "source_tags": event.get("categories") or [], "venue": event.get("venue")}})
        if decision["fallback"]:
            notices.append({"stage": "classification", "event": event["title"], "text": "the general illustration was used: " + "; ".join(decision["reasons"])})
    report["stages"]["classification"] = {"classifier": classifier.name, "events": class_rows}

    # geolocation
    service, budget = None, {"left": 0}
    if not offline and geocode > 0:
        service = media.MediaService(tempfile.mkdtemp(prefix="workbench-media-"), inner if hasattr(inner, "fetch") else SafeFetcher(1.1))
        budget = {"left": int(geocode)}
    geo_rows, geo_by_id = [], {}
    for event in final[:MAX_SHOWN]:
        view = geolocation_view(event, location, service, budget, offline)
        geo_by_id[event["id"]] = view
        geo_rows.append({"event": event["title"], "id": event["id"], **view})
        if view["status"].startswith("no location candidate"):
            notices.append({"stage": "geolocation", "event": event["title"], "text": view["status"]})
        elif view["status"] == "refused":
            notices.append({"stage": "geolocation", "event": event["title"], "text": "the location was refused: " + str(view["lookup"].get("reason"))})
    report["stages"]["geolocation"] = {"geocoder": "OpenStreetMap Nominatim (rate limit 4 lookups a minute) and Overpass for street corners" if service else "none used in this run",
                                       "events": geo_rows, "place_summary": place_summary}

    # validation
    dropped_before_feed = [e["title"] for e in located if e["id"] not in {f["id"] for f in final}]
    report["stages"]["validation"] = {
        "events_accepted": len(final), "feed_check": feed_check, "rejected_by_the_adapter": discover_notes.get("skipped", {}),
        "outside_the_search_area": place_summary.get("outside_area", 0), "dropped_when_building_the_feed": dropped_before_feed,
        "duplicates": "merged within the page by title and start: " + str(discover_notes.get("skipped", {}).get("duplicate", 0)) + "; cross-source merging has nothing to compare in a single-website run",
        "warnings": [{"event": e["title"], "missing": e["verification"]["missing"], "verification": e["verification"]["status"],
                      "location_class": (e.get("place") or {}).get("classification")} for e in final[:MAX_SHOWN]]}
    report["stages"]["output"] = {"feed_generated_at": document["generated_at"], "events": final[:MAX_SHOWN], "count": len(final)}

    # evidence
    sources = evidence_sources(recorder.bodies, {url})
    for event in final[:MAX_SHOWN]:
        candidate = _find_candidate(event, candidates)
        report["evidence"][event["id"]] = event_evidence(event, {e["id"]: e for e in located}.get(event["id"]), candidate, decisions[event["id"]], classifier.name,
                                                         geo_by_id[event["id"]], sources)
    report["stages"]["discovery"]["modules"] = {key: {"module": title, "implemented_by": files} for key, title, files in STAGES}
    return report


# --- reports ----------------------------------------------------------------------------------------------------------------

def _e(value):
    return html.escape(str(value if value is not None else ""))


def _rows(items, columns):
    head = "".join(f"<th>{_e(c)}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{_e(_short(item.get(c), 200)) if not isinstance(item.get(c), (list, dict)) else _e(_short(item.get(c), 200))}</td>" for c in columns) + "</tr>" for item in items)
    return f"<table><tr>{head}</tr>{body}</table>"


CSS = ("body{font:14px/1.45 system-ui,sans-serif;margin:24px;max-width:1100px;color:#1d2125;background:#fff}h1{font-size:22px}h2{font-size:17px;margin-top:28px;border-bottom:1px solid #ccd}"
       "h3{font-size:15px;margin:18px 0 6px}table{border-collapse:collapse;width:100%;margin:6px 0}td,th{border:1px solid #dde;padding:4px 8px;vertical-align:top;text-align:left;word-break:break-word}"
       "th{background:#f0f2f7}.k{display:inline-block;padding:0 7px;border-radius:9px;background:#eef;font-size:12px}.warn{background:#fff3cd;padding:3px 8px;margin:3px 0;border-left:4px solid #e0a800}"
       "details{margin:6px 0}summary{cursor:pointer;font-weight:600}pre{background:#f6f7fa;padding:8px;overflow:auto;white-space:pre-wrap}.q{background:#e8f5e9;padding:0 3px}.m{color:#555;font-size:12px}"
       "@media (prefers-color-scheme:dark){body{background:#1b1e23;color:#e6e8ec}th{background:#2a2f38}td,th{border-color:#3a4150}pre{background:#252a32}.k{background:#2d3550}.warn{background:#3a3220}.q{background:#244a2c}}")


def render_html(report):
    tool, stages = report["tool"], report["stages"]
    out = [f"<!doctype html><html><head><meta charset='utf-8'><title>Event Analysis Workbench: {_e(tool['target'])}</title><style>{CSS}</style></head><body>"]
    out.append(f"<h1>Event Analysis Workbench</h1><p>{_e(tool['mode'])} analysis of <b>{_e(tool['target'])}</b> · {_e(tool['generated_at'])} · area {_e(tool['area']['display_name'])} · "
               f"{'offline (no network request was made)' if tool['offline'] else 'network through SafeFetcher'}</p>")
    out.append("<p class='m'>Every number below is what the production functions returned; stages without a separate entry point show their existing results. Limits: " + _e(json.dumps(report["limits"])) + "</p>")
    if report["notices"]:
        out.append("<h2>Warnings and refusals</h2>" + "".join(f"<div class='warn'><b>{_e(n['stage'])}</b>{(' · ' + _e(n['event'])) if n.get('event') else ''}: {_e(n['text'])}</div>" for n in report["notices"]))
    d = stages["discovery"]
    out.append(f"<h2>1. Event Discovery</h2><p>{_e(d['status'])} · {d['requests_made']} request(s) · format {_e(d.get('format'))} {_e(json.dumps(d.get('formats')))}"
               f"{' · request or time limit reached' if d['limit_reached'] else ''}</p>")
    out.append("<h3>Pages and documents fetched</h3>" + _rows(d["pages"], ["url", "ok", "status", "content_type", "bytes", "error", "refused"]))
    if d["hub_pages"]:
        out.append("<h3>Pages the site's own links led to</h3>" + _rows(d["hub_pages"], ["url", "score", "events"]))
    if d["embedded_calendars"]:
        out.append("<h3>Embedded calendars</h3>" + _rows(d["embedded_calendars"], ["url", "read", "error"]))
    if d["documents"]:
        out.append("<h3>Linked documents</h3>" + _rows(d["documents"], ["url", "kind", "text", "extracted", "events", "reason"]))
    if d["not_fetched"]:
        out.append("<h3>Deferred or not read, with the reason</h3>" + _rows(d["not_fetched"], ["url", "reason"]))
    x = stages["extraction"]
    out.append(f"<h2>2. Information Extraction</h2><p>{x['candidates_found']} candidate(s), {x['events_produced']} event(s) after the adapter's date, time and duplicate checks; skipped: {_e(json.dumps(x['skipped_with_reasons']))}</p>")
    out.append(_rows(x["candidates"], ["kind", "title", "start", "end", "venue", "address", "reached_output", "source_text"]) + f"<p class='m'>{_e(x['note'])}</p>")
    c = stages["classification"]
    out.append(f"<h2>3. Event Classification</h2><p>classifier: <b>{_e(c['classifier'])}</b></p>")
    for row in c["events"]:
        dec = row["decision"]
        out.append(f"<details open><summary>{_e(row['event'])} → <span class='k'>{_e(dec['category'])}</span> {_e(dec['strength'])}{' (fallback)' if dec['fallback'] else ''}</summary>"
                   f"<p>reasons: {_e('; '.join(dec['reasons']))}</p><p class='m'>scores: {_e(json.dumps(dec.get('scores', {})))}</p>"
                   f"<p class='m'>fields read: title {_e(_short(row['input_fields']['title'], 120))} · description {row['input_fields']['description_chars_available']} chars available, "
                   f"first {len(row['input_fields']['description_read'])} read · tags {_e(row['input_fields']['source_tags'])} · venue {_e(row['input_fields']['venue'])}</p></details>")
    g = stages["geolocation"]
    out.append(f"<h2>4. Geographic Resolution</h2><p>geocoder: {_e(g['geocoder'])} · places: {_e(json.dumps(g['place_summary']))}</p>")
    for row in g["events"]:
        look = row.get("lookup")
        out.append(f"<details open><summary>{_e(row['event'])}: {_e(row['status'])}</summary>")
        out.append(_rows(row["candidates"], ["kind", "text", "found_in", "names"]) if row["candidates"] else "<p>no candidates</p>")
        if look:
            out.append(f"<pre>{_e(json.dumps(look, indent=1, ensure_ascii=False))}</pre>")
        out.append("</details>")
    v = stages["validation"]
    out.append(f"<h2>5. Validation and Deduplication</h2><p>{v['events_accepted']} event(s) accepted · feed: {_e(v['feed_check'])} · outside the area: {v['outside_the_search_area']} · rejected by the adapter: {_e(json.dumps(v['rejected_by_the_adapter']))}"
               f" · dropped when building the feed: {_e(v['dropped_when_building_the_feed'])}</p><p class='m'>{_e(v['duplicates'])}</p>")
    out.append(_rows(v["warnings"], ["event", "missing", "verification", "location_class"]))
    out.append("<h2>6. Final event records and source-text evidence</h2>")
    for event in stages["output"]["events"]:
        out.append(f"<details><summary>{_e(event['title'])} · {_e(event['start'])}</summary><pre>{_e(json.dumps({k: v for k, v in event.items() if k not in ('verification',)}, indent=1, ensure_ascii=False))}</pre>")
        refs = report["evidence"].get(event["id"], [])
        out.append("<table><tr><th>field</th><th>value</th><th>kind</th><th>recorded by</th><th>supporting text</th></tr>")
        for ref in refs:
            quote = f"<span class='q'>{_e(ref['quote'])}</span><br><span class='m'>{_e(ref.get('found_in'))} · {_e(ref.get('source_url'))}<br>…{_e(ref.get('context'))}…</span>" if ref.get("quote") else ""
            note = f"<br><span class='m'>{_e(ref.get('note'))}</span>" if ref.get("note") else ""
            out.append(f"<tr><td>{_e(ref['field'])}</td><td>{_e(_short(ref['value'], 160))}</td><td>{_e(ref['kind'])}</td><td>{_e(ref['recorded_by'])}</td><td>{quote}{note}</td></tr>")
        out.append("</table></details>")
    out.append("</body></html>")
    return "".join(out)


def write_reports(report, directory):
    os.makedirs(directory, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", (urlsplit(report["tool"]["origin_url"]).hostname or "site") + "-" + os.path.splitext(os.path.basename(report["tool"]["target"]))[0].lower()).strip("-")[:50]
    base = os.path.join(directory, f"{slug}-{datetime.now():%Y%m%d-%H%M%S}")
    with open(base + ".json", "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, ensure_ascii=False, default=str)
    with open(base + ".html", "w", encoding="utf-8") as handle:
        handle.write(render_html(report))
    return base + ".json", base + ".html"


def summary(report):
    stages = report["stages"]
    lines = [f"{report['tool']['mode']} analysis of {report['tool']['target']} ({'offline' if report['tool']['offline'] else 'network'})",
             f"  discovery:      {stages['discovery']['status']}, {stages['discovery']['requests_made']} request(s), format {stages['discovery'].get('format')}",
             f"  extraction:     {stages['extraction']['candidates_found']} candidate(s) -> {stages['extraction']['events_produced']} event(s); skipped {stages['extraction']['skipped_with_reasons']}",
             "  classification: " + ", ".join(f"{r['event'][:40]} -> {r['decision']['category']}" for r in stages["classification"]["events"][:5]),
             "  geolocation:    " + ", ".join(f"{r['status'][:48]}" for r in stages["geolocation"]["events"][:5]),
             f"  validation:     {stages['validation']['events_accepted']} accepted, feed {stages['validation']['feed_check']}"]
    lines += [f"  ! {n['stage']}: {n.get('event', '')} {n['text']}" for n in report["notices"][:8]]
    return "\n".join(lines)


def default_output():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "review", "workbench")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="workbench", description="Analyse an event website or a saved page with the production pipeline.")
    sub = parser.add_subparsers(dest="mode", required=True)
    one = sub.add_parser("url", help="analyse a website address (network, through SafeFetcher)")
    one.add_argument("target")
    one.add_argument("--geocode", type=int, default=0, help="verify up to N event locations with the geocoder (15 s apart, as in production); default none")
    two = sub.add_parser("fixture", help="analyse a saved .html page or .json fixture (no network)")
    two.add_argument("target")
    two.add_argument("--base-url", help="the address the page was saved from (relative links and ids need it)")
    for p in (one, two):
        p.add_argument("--area", choices=sorted(AREAS), default="san-diego")
        p.add_argument("--out", default=default_output())
    args = parser.parse_args(argv)
    try:
        report = analyze(args.target, fixture=args.mode == "fixture", base_url=getattr(args, "base_url", None), area=args.area, geocode=getattr(args, "geocode", 0))
    except (ValueError, OSError) as error:
        print(f"workbench: {error}", file=sys.stderr)
        return 2
    paths = write_reports(report, args.out)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # page text may hold characters the console cannot show
    print(summary(report))
    print("reports:\n  " + "\n  ".join(os.path.normpath(p) for p in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
