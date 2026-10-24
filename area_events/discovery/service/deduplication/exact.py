"""The initial default: conservative exact matching. Two descriptions are one event only when they state the same title, the same start instant and the same address
(compared as lower-case alphanumerics). A description with no address is never matched, because nothing is invented to make it comparable."""
import hashlib
import re

from . import Decision, Incoming, retain


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def identity_of(title, starts_at, address):
    """The fingerprint of an event's title, start and address; None without an address."""
    if not address:
        return None
    return hashlib.sha256(f"{_norm(title)}|{starts_at}|{_norm(address)}".encode()).hexdigest()


class ExactMatch:
    name, version = "exact", 1

    def decide(self, incoming: Incoming, lookup):
        event = incoming.event
        identity = identity_of(event.title, event.starts_at, event.address)
        known = retain(incoming)
        if known is not None:
            return Decision(known.action, known.event_key, identity if known.action == "update" else None, known.reason)
        if identity:
            twins = lookup.by_identity(identity)
            if twins:
                return Decision("link", twins[0].event_key, None, "the same title, start and address")
        return Decision("create", None, identity, "no known event states the same title, start and address")
