"""What is an event about? Picks the illustration category for events that have no usable photo.

The card has eleven illustrations (music, theater, festival, food, sports, arts, science, family, community, outdoors,
general). This module decides which one an event gets, in the engine, from the evidence the listing itself gives:

    1. the title (the subject of the event)          full weight
    2. the description                                 reduced weight, capped (descriptions are noisy)
    3. structured tags from the source                 specific tags ("Music") count; generic ones ("Campus & Community") barely
    4. the venue                                       weak supporting evidence only

Every term has a weight for the category it points to. A category's score is the sum of its distinct matched terms; the
best category wins only if it has enough evidence AND clearly beats the runner-up. Otherwise the event gets the general
illustration and says so (`fallback`). `strength` is a label for the amount of evidence, not a probability: nothing here
is calibrated, so no percentage is ever reported.

Only the standard library is used (see docs/images.md for why no machine-learning package is).
"""
import re
import unicodedata

CATEGORIES = ("music", "theater", "festival", "food", "sports", "arts", "science", "family", "community", "outdoors", "general")

TITLE_FACTOR = 1.0
DESCRIPTION_FACTOR = 0.45
DESCRIPTION_CAP = 3.0
VENUE_FACTOR = 0.25
VENUE_CAP = 1.0
TAG_CAP = 4.0
DESCRIPTION_CHARS = 600
MIN_SCORE = 2.5       # less evidence than this is not enough to pick anything
MIN_MARGIN = 1.0      # the winner must beat the runner-up by this much ...
MIN_RATIO = 1.6       # ... or be this many times as strong
TITLE_LEAD = 1.5      # ... or be what the title is clearly about (this far ahead of anything else in the title)

# term -> weight, per category. Matching is on lower-cased, accent-free word stems, so "hiking" matches "hike".
LEXICON = {
    "music": {
        "concert": 4, "symphony": 4, "orchestra": 4, "philharmonic": 4, "jazz": 4, "blues": 3.5, "bluegrass": 4, "opera": 3, "choir": 4,
        "chorale": 4, "recital": 4, "songwriter": 4, "singer": 3.5, "live music": 4, "album release": 4, "album": 2.5, "ensemble": 3,
        "band": 3, "quartet": 3.5, "trio": 3, "dj": 3, "dj set": 4, "karaoke": 3.5, "open mic": 2, "acoustic": 3, "vocalist": 3.5,
        "violinist": 4, "pianist": 4, "guitarist": 4, "drummer": 3, "rapper": 3.5, "hip hop": 3.5, "rock": 2.5, "folk": 2.5,
        "country music": 4, "indie": 2, "edm": 3, "reggae": 3.5, "mariachi": 4, "punk": 2.5, "metal": 1.5, "soul": 1.5, "gospel": 3,
        "north american tour": 3.5, "world tour": 3.5, "anniversary tour": 3, "tour": 0.5, "live at": 1.5,
        "dance party": 3, "disco": 3, "music": 2.5, "musician": 3.5, "composer": 3, "setlist": 3, "doors open": 1,
    },
    "theater": {
        "theater": 4, "theatre": 4, "playhouse": 3.5, "play": 1, "musical": 3, "ballet": 4, "dance performance": 3.5, "shakespeare": 4,
        "drama": 3, "comedy": 3.5, "comedian": 4, "stand up": 4, "standup": 4, "improv": 4, "cabaret": 3.5, "circus": 3, "magic show": 3.5,
        "puppet": 3, "broadway": 3.5, "one act": 3, "performance": 1.5, "performing arts": 3, "monologue": 3, "sketch": 2, "burlesque": 3,
        "screening": 0.5, "stage": 1, "cast": 1, "live podcast": 2.5,
    },
    "festival": {
        "festival": 4, "carnival": 4, "parade": 3.5, "fiesta": 4, "block party": 3.5, "street fair": 3.5, "fireworks": 3.5, "oktoberfest": 4,
        "mardi gras": 4, "day of the dead": 3.5, "haunt": 2.5, "haunted trail": 4, "haunted house": 4, "haunted maze": 4, "haunted attraction": 4,
        "halloween": 1.5, "celebration": 1, "fair": 1.5, "fair and": 1, "flea market": 3, "night market": 3, "artisan market": 3,
        "holiday market": 3, "tailgate": 2.5, "pride": 1.5, "fest": 3.5, "palooza": 3, "pumpkin": 1, "lantern": 2.5,
    },
    "food": {
        "food": 3, "farmers market": 4, "dinner": 2.5, "brunch": 3.5, "lunch": 2, "tasting": 3.5, "wine": 3, "beer": 3,
        "brewery": 3, "brew": 2, "cocktail": 3.5, "happy hour": 3.5, "yappy hour": 3.5, "chef": 3.5, "cooking": 3.5, "culinary": 4,
        "restaurant": 0.5, "taco": 3.5, "pizza": 3.5, "bbq": 3.5, "barbecue": 3.5, "food truck": 4, "coffee": 2, "bake": 3, "bakery": 3,
        "ice cream": 3.5, "popsicle": 3.5, "paleta": 3.5, "gelato": 3.5, "dessert": 3, "sushi": 3, "supper club": 4, "distillery": 3, "cider": 3, "spirits": 1.5,
        "dining": 3, "menu": 1, "eat": 2, "drink": 2, "grill": 1, "kitchen": 1, "tea": 1.5, "chocolate": 3,
    },
    "sports": {
        "softball": 4, "baseball": 4, "basketball": 4, "volleyball": 4, "football": 4, "soccer": 4, "hockey": 4, "tennis": 4, "golf": 4,
        "swimming": 4, "diving": 2.5, "track and field": 4, "rugby": 4, "lacrosse": 4, "wrestling": 4, "gymnastics": 4, "rowing": 3.5,
        "marathon": 4, "half marathon": 4, "5k": 4, "10k": 4, "triathlon": 4, "fun run": 3.5, "race": 1.5, "run": 1, "yoga": 3, "pilates": 3,
        "zumba": 3, "crossfit": 3.5, "fitness": 3, "workout": 3, "tournament": 3.5, "league": 2.5, "match": 1.5, "athletic": 3, "sport": 3,
        "pickleball": 4, "home game": 3, "away game": 3, "opponent": 2.5, "skate": 2.5, "boxing": 3.5, "martial art": 3, "team": 1, "game": 1, "coach": 1.5, "gym": 2.5, "cheer": 1.5,
    },
    "arts": {
        "exhibit": 4, "exhibition": 4, "gallery": 3.5, "museum": 1.5, "art walk": 4, "painting": 3.5, "sculpture": 4, "photography": 3.5,
        "ceramic": 3.5, "pottery": 3.5, "artist talk": 4, "artist": 2, "art show": 4, "mural": 3.5, "drawing": 2.5, "craft": 2.5, "zine": 3.5,
        "on view": 3.5, "opening reception": 3, "art": 2, "poetry": 3.5, "poet": 3.5, "book signing": 3, "author": 2, "literary": 3,
        "novel": 2.5, "reading": 1, "printmaking": 4, "textile": 3, "collage": 3.5, "illustration": 3, "design": 1.5, "art history": 3,
        "visual art": 4, "art museum": 3.5, "museum of art": 3.5, "mannerism": 3.5, "baroque": 3, "impressionism": 3, "painter": 3, "documentary": 2, "storytelling": 1.5, "calligraphy": 3.5, "sketching": 3,
    },
    "science": {
        "astronomy": 4, "astronomical": 4, "planetarium": 4, "telescope": 4, "stargazing": 3, "physics": 3.5, "chemistry": 3.5, "biology": 3.5,
        "science": 3, "stem": 4, "robotics": 4, "robot": 3.5, "engineering": 3, "technology": 2.5, "tech": 2, "software": 3, "coding": 3.5,
        "programming": 3.5, "machine learning": 4, "artificial intelligence": 3.5, "ai": 2.5, "data science": 4, "cyber": 3, "cybersecurity": 3.5,
        "hackathon": 4, "laboratory": 3, "lab": 2, "startup": 2, "innovation": 2, "quantum": 4, "nasa": 4, "geology": 3.5, "climate": 2.5,
        "neuroscience": 4, "genetic": 3.5, "microbiology": 4, "mathematics": 3, "math": 2.5, "computer": 2.5, "computing": 3, "semiconductor": 3.5,
        "space": 1, "research": 1.5, "scientist": 3, "experiment": 3, "aquarium": 2, "ocean": 1.5, "marine": 2, "ecology": 3,
    },
    "family": {
        "kid": 3, "child": 3, "children": 3, "toddler": 3.5, "preschool": 3.5, "story time": 3.5, "storytime": 3.5, "family": 2.5,
        "family friendly": 1.5, "teen": 2, "youth": 2, "all ages": 1.5, "kid friendly": 2.5, "petting zoo": 3.5, "playground": 3.5,
        "bounce house": 3.5, "trunk or treat": 3.5, "trick or treat": 3, "parent": 2, "baby": 2.5, "nursery": 2, "storybook": 3, "lego": 3, "play date": 3.5,
        "elementary": 2.5, "school age": 3, "camp": 1,
    },
    "community": {
        "volunteer": 3, "cleanup": 3.5, "clean up": 3.5, "charity": 3, "fundraiser": 3, "fundraising": 3, "town hall": 3.5, "neighborhood": 2,
        "community meeting": 3.5, "support group": 3.5, "recovery": 2.5, "info session": 2.5, "information session": 2.5, "committee": 2.5,
        "meeting": 2, "voter": 2.5, "vote": 2, "election": 2.5, "civic": 3, "nonprofit": 3, "career fair": 2.5, "job fair": 2.5, "networking": 2.5,
        "advocacy": 3, "student government": 3, "student affair": 1.5, "assembly": 2.5, "public assembly": 3.5, "summit": 1.5, "housing": 1.5, "council": 2, "caucus": 3, "memorial": 2, "vigil": 3, "walk": 0.5, "rally": 2, "forum": 1.5, "mutual aid": 3.5,
        "veterans": 2, "orientation": 2, "open house": 2, "workshop": 0.5, "drop in": 1, "peer led": 2, "wellness": 1.5,
    },
    "outdoors": {
        "hike": 4, "trail": 2.5, "camping": 4, "campover": 4, "campout": 4, "kayak": 4, "paddle": 3, "paddleboard": 4, "surf": 3.5,
        "beach": 2.5, "garden": 2.5, "nature": 3, "wildlife": 3.5, "birding": 3.5, "nature walk": 4, "bike ride": 3.5,
        "cycling": 3, "fishing": 3.5, "farm": 1.5, "ranch": 2, "zoo": 2, "botanical": 3, "trailhead": 3.5, "canyon": 2.5, "campfire": 3.5,
        "sunrise": 1, "sunset": 1, "outdoor": 2.5, "backpacking": 4, "climbing": 3, "stargazing outdoors": 2, "wilderness": 3.5, "nature preserve": 3.5,
        "sanctuary": 2, "park ranger": 3.5, "state park": 2.5, "national park": 2.5, "bat walk": 3.5, "tide pool": 4, "sailing": 3, "boat": 1.5,
    },
}

# Structured tags from the sources. A tag only helps when it says something specific about the subject.
TAGS = {
    "music": {"music": 3, "concerts": 3, "concert": 3, "live music": 3, "jazz": 3},
    "theater": {"theater": 3, "theatre": 3, "comedy": 3, "dance": 2, "performing arts": 3, "performance": 2, "stand up": 3},
    "festival": {"festivals": 3, "festival": 3, "holidays": 1, "halloween": 1.5, "fairs": 2},
    "food": {"food drinks": 3, "food": 3, "drink": 2.5, "drinks": 2.5, "food and drink": 3, "dining": 3, "wine": 2.5, "beer": 2.5},
    "sports": {"sports": 2.5, "fitness": 3, "athletics": 3, "sports recreation": 0.8, "recreation": 0.3},
    "arts": {"arts": 3, "art": 3, "exhibits": 3, "museums": 2, "galleries": 3, "literature": 2.5, "arts humanities": 1, "photography": 3},
    "science": {"science": 3, "technology": 3, "science technology": 3, "stem": 3},
    "family": {"family": 3, "kids": 3, "children": 3, "kids family": 3},
    "community": {"community": 0.3, "campus community": 0.3, "volunteer": 3, "civic": 3, "nonprofit": 3, "health wellness": 0.5},
    "outdoors": {"outdoors": 3, "nature": 3, "hiking": 3, "environment": 2, "animals": 1.5, "parks": 2},
}

# Phrases that look like a category word but are not (removed before matching).
NEGATIVE = re.compile(r"\b(political science|social science|computer science education|science fiction|sci fi|dance floor|play(ing)? (?:a )?role|"
                      r"fair (?:use|share|housing|trade)|lunch (?:and|n) learn|run of show|team building|food (?:pantry|bank|insecurity))\b")
VERSUS = re.compile(r"\b(vs|v|versus)\b\.?")

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def _stem(word):
    """A small, deterministic stemmer: hiking/hike/hikes -> hik, painting/paintings -> paint."""
    word = word.replace("'s", "").replace("'", "")
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith("ed"):
        word = word[:-2]
    elif len(word) > 4 and word.endswith("es"):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    return word


def _tokens(text):
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower().replace("&", " and ")
    return _WORD.findall(NEGATIVE.sub(" ", " ".join(text.split())))


def _build_index():
    index = {}
    for category, terms in LEXICON.items():
        for term, weight in terms.items():
            key = tuple(_stem(w) for w in _WORD.findall(term))
            entries = index.setdefault(key, [])
            if any(c == category for c, _, _ in entries):
                raise ValueError(f"duplicate lexicon term for {category}: {term}")  # spelling variants must be one entry
            entries.append((category, weight, term))
    return index, max(len(k) for k in index)


INDEX, LONGEST = _build_index()


def _tag_key(tag):
    """'Sports & Recreation' and 'sports recreation' are the same tag: stems of the words, without 'and'."""
    return " ".join(_stem(w) for w in _tokens(tag) if w != "and")


TAG_INDEX = {category: {_tag_key(tag): weight for tag, weight in tags.items()} for category, tags in TAGS.items()}


def _matches(text):
    """Distinct (category, weight, term) lexicon hits in `text`, longest phrase first at each position."""
    words = [_stem(w) for w in _tokens(text)]
    found, seen, i = [], set(), 0
    while i < len(words):
        for size in range(min(LONGEST, len(words) - i), 0, -1):
            hits = INDEX.get(tuple(words[i:i + size]))
            if hits:
                for category, weight, term in hits:
                    if (category, term) not in seen:
                        seen.add((category, term))
                        found.append((category, weight, term))
                i += size
                break
        else:
            i += 1
    return found


def classify(event):
    """The illustration category for one event, with the evidence behind it.

    Returns {"category", "strength", "reasons", "fallback", "scores"}; `strength` is "strong", "moderate", "weak" (the
    evidence was thin) or "none" (the general illustration was used because nothing, or nothing clear, was found).
    """
    evidence = {category: [] for category in CATEGORIES if category != "general"}   # category -> [(points, text)]
    in_title = dict.fromkeys(evidence, 0.0)

    title = str(event.get("title") or "")
    for category, weight, term in _matches(title):
        evidence[category].append((weight * TITLE_FACTOR, f'title "{term}"'))
        in_title[category] += weight * TITLE_FACTOR
    if VERSUS.search(title.lower()):
        evidence["sports"].append((3.5, 'title "vs"'))
        in_title["sports"] += 3.5

    described = {}
    for category, weight, term in _matches(str(event.get("description") or "")[:DESCRIPTION_CHARS]):
        described.setdefault(category, []).append((weight * DESCRIPTION_FACTOR, f'description "{term}"'))
    for category, items in described.items():
        items.sort(reverse=True)
        total = 0.0
        for points, label in items:
            if total >= DESCRIPTION_CAP:
                break
            evidence[category].append((min(points, DESCRIPTION_CAP - total), label))
            total += points

    tags = {}
    for tag in event.get("categories") or []:
        if isinstance(tag, str):
            tags.setdefault(_tag_key(tag), tag.strip())      # reasons quote the tag as the source wrote it
    for category, known in TAG_INDEX.items():
        for key, shown in tags.items():
            if key in known:
                evidence[category].append((min(known[key], TAG_CAP), f'source tag "{shown}"'))

    venue_points = {}
    for category, weight, term in _matches(str(event.get("venue") or "")):
        venue_points.setdefault(category, []).append((weight * VENUE_FACTOR, f'venue "{term}"'))
    for category, items in venue_points.items():
        total = 0.0
        for points, label in sorted(items, reverse=True):
            if total >= VENUE_CAP:
                break
            evidence[category].append((min(points, VENUE_CAP - total), label))
            total += points

    scores = {category: round(sum(points for points, _ in items), 2) for category, items in evidence.items()}
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    (best, top), (_, second) = ranked[0], ranked[1]
    # The subject named in the title comes first: when the title points clearly at one category (and only weakly or
    # not at any other) the description and venue cannot overturn it, as in "Jazz Night at Giuseppe's Restaurant".
    by_title = sorted(in_title.items(), key=lambda item: -item[1])
    titled = by_title[0][1] >= MIN_SCORE and by_title[0][1] - by_title[1][1] >= TITLE_LEAD and by_title[0][0] == best
    clear = top >= MIN_SCORE and (top - second >= MIN_MARGIN or top >= MIN_RATIO * second or titled)
    if not clear:
        if top < MIN_SCORE:
            why = "not enough evidence about what the event is"
        else:
            why = f"conflicting evidence ({best} {top:g} vs {ranked[1][0]} {second:g})"
        return {"category": "general", "strength": "none", "reasons": [why], "fallback": True,
                "scores": {k: v for k, v in scores.items() if v}}
    margin = top - second
    strength = "strong" if top >= 6 and margin >= 3 else "moderate" if top >= 3.5 and margin >= 1.5 else "weak"
    reasons = [label for _, label in sorted(evidence[best], reverse=True)[:3]]
    return {"category": best, "strength": strength, "reasons": reasons, "fallback": False, "scores": {k: v for k, v in scores.items() if v}}


def annotate(events):
    """Record the illustration decision on every event (`event["illustration"]`). Never touches the photo choice."""
    for event in events:
        decision = classify(event)
        event["illustration"] = {key: decision[key] for key in ("category", "strength", "reasons", "fallback")}
    return events
