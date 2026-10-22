"""Which kind of food event it is, for choosing a category photograph (see docs/category-photography.md).

The classifier (classify.py) decides the category and is not touched here. For events it has put in `food`, this adds
`event["illustration"]["subject"]` when the listing itself says what kind of food event it is:

    farmers-market   a farmers market or produce market
    food-truck       a food-truck gathering
    restaurant       dining, tasting, brunch and the like

Nothing is guessed: an event that says none of these gets no subject (the card then uses its restaurant photographs for
food, the primary food set). Title and source tags decide first; the start of the description only breaks a tie where
the title says nothing, so a long description that mentions a market in passing cannot change the subject.
"""
import re

SUBJECTS = (
    ("farmers-market", re.compile(r"\bfarm(?:er|ers)?['’]?s?['’]?\s*markets?\b|\bfarm\s*stands?\b|\bproduce\s+markets?\b|\bcertified\s+farmers\b|\bgreen\s*markets?\b", re.I)),
    ("food-truck", re.compile(r"\bfood\s*trucks?\b|\btaco\s*trucks?\b|\btruck\s+(?:rally|park|night|festival)\b|\bmobile\s+(?:kitchen|food)\b|\bfood\s+trucks?\b", re.I)),
    ("restaurant", re.compile(r"\brestaurants?\b|\bdinner\b|\bbrunch\b|\bsupper\b|\bwine\s+(?:tasting|dinner|pairing)\b|\bbeer\s+(?:tasting|dinner|pairing)\b|\btasting\s+menu\b|\bdining\b|\bchef['’]?s\b", re.I)),
)
DESCRIPTION_CHARS = 200


def subject_of(event):
    """The subject named by the event's title and tags, else by the start of its description, else None."""
    headline = " ".join([str(event.get("title") or ""), *[str(c) for c in event.get("categories") or []]])
    start = str(event.get("description") or "")[:DESCRIPTION_CHARS]
    for text in (headline, start):
        for subject, pattern in SUBJECTS:
            if pattern.search(text):
                return subject
    return None


def annotate(events):
    """Add the subject to the illustration decision of every food event. Never changes the category or the photo choice."""
    for event in events:
        decision = event.get("illustration")
        if not isinstance(decision, dict) or decision.get("category") != "food":
            continue
        subject = subject_of(event)
        if subject:
            decision["subject"] = subject
    return events
