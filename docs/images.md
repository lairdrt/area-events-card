# Event images

Every tile shows something relevant and never an empty box, a broken-image icon or a layout shift. The picture comes from this order:

| Tier | Source | Decided in |
|---|---|---|
| 1 | **The event's own image**: the photo on the event's primary source record, if it is a usable photo | engine, `discovery/images.py` |
| 2 | **A matched event's image**: otherwise the best usable photo among the other source records *already matched* to the same event (largest size hint, earlier source on a tie). No new matching happens here | engine |
| 3 | **Organizer or venue image**: reserved. No source supplies one that may be reused, so none is ever invented | – |
| 4 | **Category artwork**: an original illustration for the event's category, labelled "Illustration" | card |
| 5 | **General artwork**: one neutral calendar illustration when no category fits | card |

## What counts as a usable photo

Judged on the address alone (nothing is downloaded): an http(s) address that is not a placeholder (`no-image`, `placeholder`, `events_default_icon` ...), a tracking pixel (`1x1`, `spacer` ...), a logo or site graphic (`logo`, `favicon`, `site-icon` ...), an unsized icon/SVG/GIF, or malformed. For image resizers (`...?url=<original>`) the original's file name is judged, so a CDN path such as `/dims4/default/` is not mistaken for a placeholder. After merging, one picture used by four or more differently named events in a feed (a department banner on every listing) is treated as a banner, not an event photo, and dropped; a recurring event repeating its own poster is fine.

Each merged event records the decision: `image_url` (the chosen photo), `images` (that photo then the other usable candidates, used as the browser's fallback chain), `image_source`, and `image_provenance` (`tier`, `kind` = `event` or `matched`, `source`, and the candidates skipped with the reason).

## In the card

The illustration is always drawn first and a photo fades in over it when it has loaded, so the tile is never blank. If a photo fails to load, the next candidate is tried; when all fail the illustration stays (the broken image is removed). Photos have alt text (`Image for <title>`); illustrations are `role="img"` with `aria-label="Illustration: <category>"` and carry a visible "Illustration" label so they are not mistaken for a photo of the event. `show_images: false` shows illustrations only.

The eleven illustrations (music and concerts, theater and performing arts, festivals, food and drink, sports, arts and exhibitions, science and technology, family activities, community events, outdoors and recreation, general) are original artwork made for this project, embedded as inline SVG in the card file the integration serves. They need no network, add no requests, and take their colours from the Home Assistant theme (tinted backgrounds mix the card background, so they work in light and dark themes). The category is chosen from the event's categories in the order music, theater and comedy, festivals, food and drink, sports, arts, science and technology, family, outdoors, community, markets, education, history.

Photos are third-party images referenced by their original address; nothing is copied or stored, and the referrer is not sent.
