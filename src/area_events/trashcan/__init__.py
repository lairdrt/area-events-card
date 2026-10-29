"""The trashcan: events an administrator deleted, and the one rule that keeps rediscovery from bringing them back. No database, no I/O.

A deleted event leaves a record of what it was (see persistence/trashcan.py). Before a crawl stores a candidate it has not stored before, the shared crawl storage asks
`suppresses(entry, incoming)` of every trashcan record of that source near the candidate's start. The rule is deliberately small and deterministic:

- the candidate must come from a source that supplied the deleted event;
- the producer's own identity for it (`candidate_key`) within a day of a deleted occurrence is the same occurrence (a corrected time keeps its key);
- otherwise it must start at exactly a deleted occurrence's start, and its title must be one of the deleted titles once case, punctuation, `&`/`and` and spacing are set
  aside; a title one letter away (a typo fixed or made) is accepted only with one more fact in common: the event page's address, or the venue or street address.

Nothing else relates two events: a shared date, time, venue or listing page alone never does, and another year's occurrence never starts at the same instant.
"""
import re
import unicodedata

TRASHCAN_SCHEMA = "discovery.trashcan.v1"
REASONS = {
    "duplicate": "Duplicate event",
    "not_relevant": "Not relevant to my area",
    "incorrect": "Incorrect or misleading information",
    "expired_or_canceled": "Expired or canceled event",
    "advertisement": "Advertisement / not a real event",
    "other": "Other / don't want to see it",
}
DAY = 86400
MIN_FUZZY_LENGTH = 8   # shorter titles ("Run", "Fun Run") are never matched with a letter changed


def normalize(text):
    """Lower case, accents, punctuation, `&` and spacing set aside: "Alias | Live  Music!" -> "alias live music"."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def one_edit_apart(a, b):
    """True when `b` is `a` with at most one letter inserted, removed or replaced."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i + (len(a) == len(b)):] == b[i + 1:]


def suppresses(entry, incoming):
    """Why `incoming` is the deleted event `entry` again, or None. Both are plain dictionaries:
    entry    {"sources", "candidate_keys", "starts", "titles", "detail_urls", "places"}  (as persistence/trashcan.py stores them)
    incoming {"source_key", "candidate_key", "starts_at", "title", "detail_url", "venue", "address"}"""
    if incoming["source_key"] not in entry["sources"] or incoming["starts_at"] is None:
        return None
    start = incoming["starts_at"]
    if incoming["candidate_key"] in entry["candidate_keys"] and any(abs(start - s) < DAY for s in entry["starts"]):
        return "the source's own identity for the deleted occurrence"
    if start not in entry["starts"]:
        return None
    title = normalize(incoming["title"])
    if not title:
        return None
    if title in entry["titles"]:
        return "the deleted title at the deleted start"
    near = len(title) >= MIN_FUZZY_LENGTH and any(len(t) >= MIN_FUZZY_LENGTH and one_edit_apart(title, t) for t in entry["titles"])
    if not near:
        return None
    if incoming.get("detail_url") and incoming["detail_url"] in entry["detail_urls"]:
        return "a title one letter from the deleted title, at the deleted start, with the deleted event page"
    places = {normalize(incoming.get("venue")), normalize(incoming.get("address"))} - {""}
    if places & set(entry["places"]):
        return "a title one letter from the deleted title, at the deleted start, at the deleted place"
    return None
