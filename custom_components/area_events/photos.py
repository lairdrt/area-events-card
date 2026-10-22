"""Optional category photographs, served from a local folder that is not part of the repository.

`frontend/photos/` (git-ignored) may hold WebP files and an `index.json` written by `scripts/prepare-category-photos.py`.
Nothing here is required: without the folder the card draws its own illustrations. When it exists and is valid, the
integration serves it at PHOTOS_URL and tells the card which photographs exist, per set:

    {"base": "/area_events/photos/", "sets": {"music": [{"file": "music-1.webp", "alt": "...", "v": "1a2b3c4d"}, ...], ...}}

The index is validated strictly (known set names, plain file names that exist in the folder, short text), so a damaged or
hand-edited file can never make the card request anything but these files.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PHOTOS_URL = "/area_events/photos"
SETS = (
    "music", "theater", "festival", "food-restaurant", "food-farmers-market", "food-truck",
    "sports", "arts", "science", "family", "outdoors", "community", "general",
)
FILE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\.webp$")
VERSION = re.compile(r"^[0-9a-f]{4,16}$")
MAX_PER_SET = 12


def load_index(folder: Path) -> dict | None:
    """The validated index for the card, or None when there is no (usable) photograph folder."""
    try:
        raw = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sets_in = raw.get("sets") if isinstance(raw, dict) else None
    if not isinstance(sets_in, dict):
        return None
    sets: dict[str, list[dict]] = {}
    for name in SETS:
        entries = []
        for item in (sets_in.get(name) or [])[:MAX_PER_SET]:
            if not isinstance(item, dict):
                continue
            file, alt, version = item.get("file"), item.get("alt"), item.get("v")
            if not (isinstance(file, str) and FILE.match(file) and (folder / file).is_file()):
                continue
            if not (isinstance(alt, str) and alt.strip() and isinstance(version, str) and VERSION.match(version)):
                continue
            entries.append({"file": file, "alt": alt.strip()[:160], "v": version})
        if entries:
            sets[name] = entries
    return {"base": PHOTOS_URL + "/", "sets": sets} if sets else None
