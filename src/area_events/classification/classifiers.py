"""Event classification as a replaceable module.

Responsibility: decide which illustration category an event is about, from the evidence the event record carries, and say why.

Contract (what any implementation must honour)

    input    one event record in the Area Events schema (a dict). A classifier may read any field (today: title, description, categories,
             venue) and must not change the event.
    output   a decision, a dict with
                 category   one of `classify.CATEGORIES` (the illustrations the card can draw); "general" when nothing clear is found
                 strength   "strong", "moderate", "weak" or "none"
                 reasons    a list of short texts saying what the decision rests on (for example 'title "concert"')
                 fallback   True when the general category was used because the evidence was thin
                 scores     optional: {category: number}, the evidence per category, for diagnostics
    errors   `check_decision` lists what is wrong with a decision; `annotate` never publishes an invalid one: it uses the general category
             and records why.

Consumers call `annotate` or `current().classify` and know nothing about the algorithm, so a
different classifier is installed with `use(...)` (or `set_classifier`) without touching them. The keyword lexicon in `classify.py` is the
production implementation, wrapped unchanged by `KeywordClassifier`.
"""
import contextlib

from . import classify

STRENGTHS = ("strong", "moderate", "weak", "none")
DECISION_FIELDS = ("category", "strength", "reasons", "fallback")


class KeywordClassifier:
    """The production classifier: the weighted keyword lexicon of `classify.py`, unchanged."""

    name = "keyword-lexicon"

    def classify(self, event):
        return classify.classify(event)


_current = KeywordClassifier()


def current():
    return _current


def set_classifier(classifier):
    """Install `classifier` (an object with `name` and `classify(event)`); returns the one it replaces."""
    global _current
    if not callable(getattr(classifier, "classify", None)):
        raise TypeError("a classifier needs a classify(event) method")
    previous, _current = _current, classifier
    return previous


@contextlib.contextmanager
def use(classifier):
    previous = set_classifier(classifier)
    try:
        yield classifier
    finally:
        set_classifier(previous)


def check_decision(decision):
    """The problems with a decision, as a list of texts (empty when it honours the contract)."""
    if not isinstance(decision, dict):
        return ["the decision is not a dict"]
    problems = []
    if decision.get("category") not in classify.CATEGORIES:
        problems.append(f"unknown category {decision.get('category')!r}")
    if decision.get("strength") not in STRENGTHS:
        problems.append(f"strength must be one of {', '.join(STRENGTHS)}")
    reasons = decision.get("reasons")
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        problems.append("reasons must be a list of texts")
    if not isinstance(decision.get("fallback"), bool):
        problems.append("fallback must be true or false")
    scores = decision.get("scores", {})
    if not isinstance(scores, dict) or any(k not in classify.CATEGORIES or isinstance(v, bool) or not isinstance(v, (int, float)) for k, v in scores.items()):
        problems.append("scores must map known categories to numbers")
    return problems


def decide(event, classifier=None):
    """The classifier's decision for one event, or a general-category decision saying why when it broke the contract or failed."""
    classifier = classifier or _current
    try:
        decision = classifier.classify(event)
    except Exception as error:  # a replacement must never cost the feed its events
        return {"category": "general", "strength": "none", "reasons": [f"the classifier failed: {type(error).__name__}"], "fallback": True}
    problems = check_decision(decision)
    if problems:
        return {"category": "general", "strength": "none", "reasons": ["the classifier returned an invalid decision: " + "; ".join(problems)[:200]], "fallback": True}
    return decision


def annotate(events, classifier=None):
    """Record the illustration decision on every event (`event["illustration"]`). Never touches the photo choice."""
    for event in events:
        decision = decide(event, classifier)
        event["illustration"] = {key: decision[key] for key in DECISION_FIELDS}
    return events
