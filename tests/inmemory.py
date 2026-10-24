"""A second Persistence for tests: the ProcessingStorage port over plain Python dictionaries. It imports no database, so processing, extraction and deduplication can be run
against it to show that none of them depends on SQLite or on the real storage; it applies a deduplication strategy's decisions the same way the real Persistence does."""
import hashlib
import itertools

from discovery.service.contracts import (PROCESSING_SCHEMA, LeaseLost, ProcessingStatus, SubmitAck, WorkItem)
from discovery.service.deduplication import Association, Decision, ExistingEvent, Incoming


class _Lookup:
    def __init__(self, events):
        self._events = events

    def starting_between(self, earliest, latest, after=None, limit=100):
        matches = [self._existing(e) for e in self._events if earliest <= e["clean"].starts_at <= latest]
        matches.sort(key=lambda e: (e.starts_at, e.event_key))
        if after is not None:
            matches = [e for e in matches if (e.starts_at, e.event_key) > after]
        return tuple(matches[:limit])

    @staticmethod
    def _existing(e):
        clean = e["clean"]
        return ExistingEvent(e["event_key"], clean.title, clean.starts_at, clean.ends_at, clean.venue_name, clean.address, clean.location_text, e["revision"])


class InMemoryProcessing:
    """candidates: dicts {"key", "source_key", "payload"} (the stored candidate document)."""

    def __init__(self, deduplicator, candidates):
        self.deduplicator = deduplicator
        self.candidates = {c["key"]: dict(c, hash=hashlib.sha256(c["key"].encode()).hexdigest(), lease=None, done=False) for c in candidates}
        self.events, self.links, self.extractions = [], {}, []   # links: candidate key -> (event_key, role)
        self._tokens = itertools.count(1)

    def claim_work(self, worker, extractor, version, limit, lease_s, new_token, config):
        items = []
        for c in self.candidates.values():
            if len(items) >= limit:
                break
            if not c["done"] and c["lease"] is None:
                c["lease"] = new_token()
                items.append(WorkItem(c["lease"], 10**10, c["key"], c["source_key"], c["hash"], c["payload"], (), None))
        return tuple(items)

    def submit_result(self, candidate_key, extractor, version, lease_token, validated, worker):
        c = self.candidates[candidate_key]
        if c["lease"] != lease_token or c["done"]:
            raise LeaseLost("the lease on this work is not held")
        self.extractions.append((candidate_key, validated.status))
        event_key = revision = None
        outcome = "recorded"
        if validated.event is not None:
            link = self.links.get(candidate_key)
            decision = self.deduplicator.decide(Incoming(candidate_key, validated.event, Association(*link) if link else None), _Lookup(self.events))
            event_key, outcome, revision = self._apply(candidate_key, validated.event, decision)
        c["done"], c["lease"] = True, None
        return SubmitAck(PROCESSING_SCHEMA, candidate_key, len(self.extractions), validated.status, outcome, event_key, revision)

    def _apply(self, candidate_key, clean, decision: Decision):
        if decision.action == "create":
            event = {"event_key": f"ev-{len(self.events) + 1}", "clean": clean, "revision": 1}
            self.events.append(event)
            self.links[candidate_key] = (event["event_key"], "primary")
            return event["event_key"], "created", 1
        event = next(e for e in self.events if e["event_key"] == decision.event_key)
        if decision.action == "link":
            self.links[candidate_key] = (event["event_key"], "duplicate")
            return event["event_key"], "duplicate", event["revision"]
        if decision.action == "update":
            if event["clean"] != clean:
                event.update(clean=clean, revision=event["revision"] + 1)
                return event["event_key"], "updated", event["revision"]
            return event["event_key"], "unchanged", event["revision"]
        return event["event_key"], "duplicate", event["revision"]

    def fail_work(self, candidate_key, extractor, version, lease_token, error, config):
        self.candidates[candidate_key]["lease"] = None
        return "retrying"

    def status(self, extractor, version):
        return ProcessingStatus(PROCESSING_SCHEMA, extractor, version, len(self.candidates), sum(not c["done"] for c in self.candidates.values()), 0, 0, 0,
                                sum(c["done"] for c in self.candidates.values()), len(self.events))
