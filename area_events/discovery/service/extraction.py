"""Extraction: an independent worker that turns saved discovery candidates into normalized event results.

    ExtractionService(client, extractor, clock)    claim work -> extract -> submit, on its own thread or process
    StoredCandidateExtractor                       the algorithm: a pure function of one stored candidate (no network, no database)
    LocalProcessingClient / HttpProcessingClient   the same Processing contract in process or over the authenticated API

The worker holds no database. It sees a candidate's stored payload, its evidence and the stored text of its page, and it never fetches
a website, geocodes, classifies or validates. Every value in a result comes from those inputs; what is not there stays absent (and is
listed as missing by the service), and a quote exists only where the text was found verbatim.
"""
import dataclasses
import json
import re
import ssl
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from .. import evidence as evidence_text
from .. import textloc
from .control import ValidationFailed
from .processing import LeaseLost, StaleResult

EXTRACTOR = "stored-candidate"
VERSION = 1


class ProcessingUnavailable(Exception):
    """The Processing service could not be reached or answered with a server error; the worker backs off and tries again."""


def _text(value, limit):
    value = re.sub(r"[ \t\r\f\v]+", " ", str(value or "")).strip()
    value = re.sub(r"\n\s*\n+", "\n\n", value)
    return value[:limit] or None


def _epoch(value):
    """A UTC epoch for an ISO-8601 time WITH an offset; None for anything else (a time without an offset is not guessed)."""
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return int(moment.timestamp()) if moment.tzinfo is not None else None


def looks_like_venue(text):
    """A short name (not a sentence or a set of directions) can be a venue name; anything else is kept as location text."""
    words = text.split()
    return len(text) <= 80 and len(words) <= 10 and not text.rstrip().endswith((".", "!", "?")) and not re.search(r"[.!?]\s+[A-Z]", text)


class StoredCandidateExtractor:
    name, version = EXTRACTOR, VERSION

    def extract(self, item):
        payload, issues, fields = item["payload"], [], {}
        by_field = {}
        for ref in item.get("evidence", ()):
            by_field.setdefault(ref["field"], []).append({k: ref.get(k) for k in ("field", "kind", "quote", "url_key", "content_hash", "note")})

        def note(name, origin, *source_fields):
            refs = [r for f in source_fields for r in by_field.get(f, ())]
            fields[name] = {"origin": origin, "evidence": refs[:10]}

        title = _text(payload.get("title"), 300)
        if not title:
            return self._failed("the stored candidate has no title", issues)
        starts_at = _epoch(payload.get("start"))
        if starts_at is None:
            reason = "the stored start time is missing or unreadable" if not payload.get("start") else \
                "the stored start time has no UTC offset, so it cannot be placed in time without guessing"
            return self._failed(reason, issues)
        ends_at = _epoch(payload.get("end")) if payload.get("end") else None
        if payload.get("end") and (ends_at is None or ends_at < starts_at):
            issues.append("end_ignored")
            ends_at = None
        event = {"title": title, "starts_at": starts_at, "ends_at": ends_at, "timezone": _text(payload.get("timezone"), 64), "description": _text(payload.get("description"), 4000),
                 "venue_name": None, "location_text": None, "address": _text(payload.get("address"), 300), "source_url": payload.get("url") if str(payload.get("url") or "").startswith(("http://", "https://")) else None}
        note("title", "payload", "title")
        note("starts_at", "payload", "start")
        if ends_at is not None:
            note("ends_at", "payload")
        if event["description"]:
            note("description", "payload", "description")
        venue = _text(payload.get("venue"), 600)
        if venue and looks_like_venue(venue):
            event["venue_name"] = venue
            note("venue_name", "payload", "venue")
        elif venue:
            event["location_text"] = venue
            issues.append("venue_is_description")
            note("location_text", "payload", "venue")
        if event["address"]:
            note("address", "payload", "address")
        else:
            self._address_from_text(event, item, fields, issues)
        if not (event["address"] or event["venue_name"] or event["location_text"]):
            issues.append("no_location")
        if by_field.get("verification"):
            fields["verification"] = {"origin": "payload", "evidence": by_field["verification"][:10]}
        return {"event": event, "fields": fields, "issues": issues}

    @staticmethod
    def _failed(reason, issues):
        return {"event": None, "failure_reason": reason, "fields": {}, "issues": issues}

    @staticmethod
    def _address_from_text(event, item, fields, issues):
        """An address the event's own words give: used only when its description or location text names exactly one street address."""
        own = " ".join(part for part in (event["description"], event["location_text"]) if part)
        found = textloc.extract_addresses(own)
        if len(found) > 1:
            issues.append("several_addresses_in_text")
            return
        if len(found) == 1:
            address = found[0]["address"]
            hit = evidence_text.locate(address, [(event["source_url"], "visible text", evidence_text.squeeze(own))])
            event["address"] = address
            fields["address"] = {"origin": "description_text", "evidence": [{"field": "address", "kind": "extracted" if hit else "inferred", "quote": hit["quote"] if hit else None,
                                                                              "url_key": (item.get("page") or {}).get("url_key"), "content_hash": None,
                                                                              "note": "read from the event's own description" if hit else "assembled from the event's description; not found verbatim"}]}
            issues.append("address_from_description")


@dataclass(frozen=True)
class ExtractionConfig:
    batch: int = 5
    lease_s: int = 300
    idle_wait_s: float = 5.0
    busy_wait_s: float = 0.05
    error_wait_s: float = 2.0
    error_wait_cap_s: float = 60.0


@dataclass(frozen=True)
class ExtractionReport:
    claimed: int = 0
    submitted: int = 0
    failed: int = 0
    stale: int = 0
    outcomes: tuple = ()


class LocalProcessingClient:
    """The Processing contract in process: calls LocalProcessing and returns plain dicts, exactly what the HTTP client returns."""

    def __init__(self, processing):
        self._processing = processing

    def claim(self, worker, extractor, version, limit, lease_s):
        return [dataclasses.asdict(item) for item in self._processing.claim(worker, extractor, version, limit, lease_s).items]

    def submit(self, worker, extractor, version, candidate_key, lease_token, result):
        return dataclasses.asdict(self._processing.submit(candidate_key, extractor, version, lease_token, result, worker))

    def fail(self, extractor, version, candidate_key, lease_token, error):
        return self._processing.fail(candidate_key, extractor, version, lease_token, error)

    def status(self, extractor, version):
        return dataclasses.asdict(self._processing.status(extractor, version))


class HttpProcessingClient:
    """The Processing contract over the authenticated API, for a worker on another computer. Synchronous: it runs on the worker's own thread,
    never on a Home Assistant event loop. The token is sent only in the Authorization header and never appears in an error."""

    def __init__(self, base_url, token, timeout=30, open_url=urllib.request.urlopen, cafile=None):
        parts = urlsplit(base_url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.query or parts.fragment:
            raise ValueError("enter the Discovery Service address as http://hostname:8780")
        self._base, self._token, self._timeout, self._open = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}", token, timeout, open_url
        self._context = ssl.create_default_context(cafile=cafile) if cafile else None   # a private certificate to trust, in addition to the system's

    def claim(self, worker, extractor, version, limit, lease_s):
        return self._call("POST", "/v1/processing/claim", {"worker": worker, "extractor": extractor, "version": version, "limit": limit, "lease_s": lease_s})["items"]

    def submit(self, worker, extractor, version, candidate_key, lease_token, result):
        return self._call("POST", "/v1/processing/submit", {"worker": worker, "extractor": extractor, "version": version, "candidate_key": candidate_key,
                                                           "lease_token": lease_token, "result": result})

    def fail(self, extractor, version, candidate_key, lease_token, error):
        return self._call("POST", "/v1/processing/fail", {"extractor": extractor, "version": version, "candidate_key": candidate_key, "lease_token": lease_token, "error": error})

    def status(self, extractor, version):
        return self._call("GET", f"/v1/processing/status?extractor={extractor}&version={int(version)}")

    def _call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self._base + path, data=data, method=method,
                                         headers={"Authorization": f"Bearer {self._token}", **({"Content-Type": "application/json"} if data else {})})
        try:
            with self._open(request, timeout=self._timeout, **({"context": self._context} if self._context else {})) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8")).get("error", {})
            except (ValueError, UnicodeDecodeError):
                detail = {}
            finally:
                error.close()
            message = str(detail.get("message") or f"the Discovery Service answered {error.code}")[:200]
            if error.code == 409 and detail.get("code") == "lease_lost":
                raise LeaseLost(message) from None
            if error.code == 409 and detail.get("code") == "stale_result":
                raise StaleResult(message) from None
            if error.code >= 500 or error.code in (401, 403, 429):
                raise ProcessingUnavailable(f"{error.code}: {message}") from None
            raise ValidationFailed(message) from None
        except (OSError, ValueError) as error:
            raise ProcessingUnavailable(f"could not reach the Discovery Service: {type(error).__name__}") from None


class ExtractionService:
    """Claims pending candidates, extracts, submits. Instance-owned state; explicit start and stop; safe to restart at any time, because an
    unfinished item is simply leased again when its lease expires."""

    def __init__(self, client, extractor, clock, config=ExtractionConfig(), worker_id="extractor-1"):
        self._client, self._extractor, self._clock, self.config, self.worker_id = client, extractor, clock, config, worker_id
        self._stop, self._thread, self._lock = threading.Event(), None, threading.Lock()
        self._state = {"state": "stopped", "cycles": 0, "submitted": 0, "failed": 0, "stale": 0, "errors": 0, "last_error": None, "last_cycle_at": None}
        self._error_wait = config.error_wait_s

    def run_once(self):
        """Claim up to one batch, extract and submit each item. Items are independent: one failing does not stop the others."""
        items = self._client.claim(self.worker_id, self._extractor.name, self._extractor.version, self.config.batch, self.config.lease_s)
        submitted = failed = stale = 0
        outcomes = []
        for item in items:
            if self._stop.is_set():
                break  # the rest of the batch keeps its lease and is picked up after it expires
            try:
                result = self._extractor.extract(item)
            except Exception as error:  # the extractor is replaceable code: its failure is the item's failure, not the worker's
                self._report_failure(item, f"{type(error).__name__}: {error}")
                failed += 1
                continue
            try:
                ack = self._client.submit(self.worker_id, self._extractor.name, self._extractor.version, item["candidate_key"], item["lease_token"], result)
                submitted += 1
                outcomes.append(ack["outcome"])
            except (LeaseLost, StaleResult):
                stale += 1  # the work moved on without us; nothing to repair
            except ValidationFailed as error:
                self._report_failure(item, f"the service refused the result: {error}")
                failed += 1
        self._note(cycles=1, submitted=submitted, failed=failed, stale=stale, last_cycle_at=int(self._clock()))
        return ExtractionReport(len(items), submitted, failed, stale, tuple(outcomes))

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="extraction-service", daemon=True)
            self._state["state"] = "running"
            self._thread.start()
        return self

    def stop(self, timeout=10):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def status(self):
        with self._lock:
            return {**self._state, "worker": self.worker_id, "extractor": self._extractor.name, "version": self._extractor.version}

    def _report_failure(self, item, error):
        try:
            self._client.fail(self._extractor.name, self._extractor.version, item["candidate_key"], item["lease_token"], error[:300])
        except (LeaseLost, StaleResult, ValidationFailed):
            pass

    def _note(self, **values):
        with self._lock:
            for key, value in values.items():
                if key in ("cycles", "submitted", "failed", "stale", "errors"):
                    self._state[key] += value
                else:
                    self._state[key] = value

    def _loop(self):
        while not self._stop.is_set():
            try:
                report = self.run_once()
                self._error_wait = self.config.error_wait_s
                wait = self.config.busy_wait_s if report.claimed else self.config.idle_wait_s
            except Exception as error:  # an unreachable service or a broken cycle never ends the worker
                self._note(errors=1, last_error=f"{type(error).__name__}: {str(error)[:200]}")
                wait = self._error_wait
                self._error_wait = min(self._error_wait * 2, self.config.error_wait_cap_s)
            self._stop.wait(wait)
        self._note(state="stopped")

