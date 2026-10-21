# Event images

Every tile shows something relevant and never an empty box, a broken-image icon or a layout shift. The picture comes from this order:

| Tier | Source | Decided in |
|---|---|---|
| 1 | **The event's own image**: the photo on the event's primary source record, if it is a usable photo | engine, `discovery/images.py` |
| 2 | **A matched event's image**: otherwise the best usable photo among the other source records *already matched* to the same event (largest size hint, earlier source on a tie). No new matching happens here | engine |
| 3 | **Organizer or venue image**: reserved. No source supplies one that may be reused, so none is ever invented | – |
| 4 | **Category artwork**: an original illustration for what the event is about, labelled "Illustration" | engine chooses (`classify.py`), card draws |
| 5 | **General artwork**: one neutral calendar illustration when the evidence is insufficient or conflicting | engine chooses, card draws |

## What counts as a usable photo

Judged on the address alone (nothing is downloaded): an http(s) address that is not a placeholder (`no-image`, `placeholder`, `events_default_icon` ...), a tracking pixel (`1x1`, `spacer` ...), a logo or site graphic (`logo`, `favicon`, `site-icon` ...), an unsized icon/SVG/GIF, or malformed. For image resizers (`...?url=<original>`) the original's file name is judged, so a CDN path such as `/dims4/default/` is not mistaken for a placeholder. After merging, one picture used by four or more differently named events in a feed (a department banner on every listing) is treated as a banner, not an event photo, and dropped; a recurring event repeating its own poster is fine.

Each merged event records the decision: `image_url` (the chosen photo), `images` (that photo then the other usable candidates, used as the browser's fallback chain), `image_source`, and `image_provenance` (`tier`, `kind` = `event` or `matched`, `source`, and the candidates skipped with the reason).

## In the card

The illustration is always drawn first and a photo fades in over it when it has loaded, so the tile is never blank. If a photo fails to load, the next candidate is tried; when all fail the illustration stays (the broken image is removed). Photos have alt text (`Image for <title>`); illustrations are `role="img"` with `aria-label="Illustration: <category>"` and carry a visible "Illustration" label so they are not mistaken for a photo of the event. `show_images: false` shows illustrations only.

The eleven illustrations (music and concerts, theater and performing arts, festivals, food and drink, sports, arts and exhibitions, science and technology, family activities, community events, outdoors and recreation, general) are original artwork made for this project, embedded as inline SVG in the card file the integration serves. They need no network, add no requests, and take their colours from the Home Assistant theme (tinted backgrounds mix the card background, so they work in light and dark themes). The card does not decide which one an event gets: it draws `event.illustration.category` and uses the general one when that is missing or unknown.

Photos are third-party images referenced by their original address; nothing is copied or stored, and the referrer is not sent.

## Which illustration an event gets

The engine decides, in `discovery/classify.py`, when it builds the feed (after photo selection, which it never touches). Evidence, in this order of influence:

1. **The title**: the subject of the event. When the title points clearly at one category, nothing in the description or venue can overturn it ("Jazz Night at Giuseppe's Restaurant" is music).
2. **The description** (first 600 characters): reduced weight and capped, because descriptions are noisy.
3. **Structured source tags**: only specific ones count ("Music", "Food / Drinks"). Generic ones ("Campus & Community", "Sports & Recreation") carry almost no weight, so they cannot decide anything alone.
4. **The venue**: weak supporting evidence only.

Each term has a weight for the category it points to; a category's score is the sum of its distinct matched terms (words are compared as accent-free stems, so *hiking* matches *hike*). The best category wins only with enough evidence and a clear lead over the runner-up; otherwise the event gets the **general** illustration and says why. Phrases that only look like a category word are ignored ("political science", "fair housing", "lunch and learn").

Every event records `illustration`: `category`, `strength` (`strong`, `moderate`, `weak`, or `none` when the general illustration was used), `reasons` (up to three pieces of evidence, such as `title "astronomy"`) and `fallback` (true when evidence was insufficient or conflicting). `strength` describes how much evidence there was; it is not a probability and nothing is calibrated, so no percentage is reported.

### Why no machine-learning library

Evaluated on 579 real San Diego and Austin listings (no ground-truth labels exist, so none were invented):

| Option | Result | Decision |
|---|---|---|
| Weighted lexicon on stems (standard library) | 0.2 s for all 579 events; every decision traceable | **Used** |
| RapidFuzz (fuzzy matching, 2.4 MB wheel) | changed 3 to 11 of 579 results depending on threshold, none convincingly better ("Graduate Fellowship" became science, "As You Like It" lost theater), about 5 times slower | Not added |
| TF-IDF similarity (what scikit-learn would provide, tried zero-shot) | agreed with the lexicon on 42% of events and disagreed badly ("Weezer" as a festival) | Not added; supervised training would need labelled events we do not have, and scikit-learn brings numpy and scipy (about 50 MB of wheels) |
| spaCy | not tried: about 35 MB plus a model for a task a word list handles | Not added |

No new dependency, nothing to install on the Raspberry Pi, no start-up cost.
