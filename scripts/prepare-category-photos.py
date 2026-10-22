"""Prepare the PRIVATE category photographs for a local Home Assistant preview.

The photographs are Pexels photos whose redistribution inside this open-source project is NOT cleared (see
docs/category-photography.md, "Licensing review"). This script therefore writes them only to a folder that Git ignores
(custom_components/area_events/frontend/photos/) from the review copies in review/category-photos/images/ (also ignored),
for the owner's own Home Assistant. Never commit, publish or package its output.

    python scripts/prepare-category-photos.py            # needs Pillow:  pip install pillow
    python scripts/prepare-category-photos.py --check    # lists what would be written, writes nothing

Output: <set>-<n>.webp, about 640 px wide, and index.json (set -> files, alt text, content hash, original Pexels id).
The review originals are never modified. Without this folder the card simply keeps its own illustrations.
"""
import argparse
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(REPO, "review", "category-photos", "images")
OUT = os.path.join(REPO, "custom_components", "area_events", "frontend", "photos")

# (set, review file stem, Pexels id, alt text). The final approved selection: the five replacements are
# theater-2-new (20898292), festival-2-alt (39967166), festival-3-new (27036324), food-farmers-market-2-new (27052301)
# and food-truck-2-new (16782204); the superseded originals are not used.
PHOTOS = [
    ("music", "music-1", 167605, "Band playing on a lit concert stage in front of a crowd"),
    ("music", "music-2", 8040838, "Two musicians playing guitar and keyboard"),
    ("music", "music-3", 1613240, "Singer and guitarist on a stage with colored lights"),
    ("theater", "theater-1", 713149, "Red stage curtain with the silhouettes of the audience"),
    ("theater", "theater-2-new", 20898292, "Costumed actors on a stage under dramatic lighting"),
    ("theater", "theater-3", 16225271, "Actors performing a medieval play on an outdoor stage"),
    ("festival", "festival-1", 36204083, "Dragon dance at a street festival"),
    ("festival", "festival-2-alt", 39967166, "Crowd in traditional dress at a cultural celebration"),
    ("festival", "festival-3-new", 27036324, "Performers on stilts in patriotic costumes at a fair"),
    ("food-restaurant", "food-restaurant-1", 2387675, "Busy restaurant dining room seen from above"),
    ("food-restaurant", "food-restaurant-2", 12181619, "Diners at tables inside a restaurant"),
    ("food-restaurant", "food-restaurant-3", 19648312, "Covered restaurant patio with tables in the evening light"),
    ("food-farmers-market", "food-farmers-market-1", 868110, "Colorful fruit and vegetable stall at a market"),
    ("food-farmers-market", "food-farmers-market-2-new", 27052301, "Shopper buying produce at a market stall"),
    ("food-farmers-market", "food-farmers-market-3", 2924427, "Two women with fresh vegetables at a market stall"),
    ("food-truck", "food-truck-1", 10380371, "People ordering at a food truck between palm trees"),
    ("food-truck", "food-truck-2-new", 16782204, "Customers ordering coffee at a food truck"),
    ("sports", "sports-1", 1884576, "Football match seen from the stands"),
    ("sports", "sports-2", 2820900, "Basketball players going for a rebound outdoors"),
    ("sports", "sports-3", 32471037, "Rugby match under stadium lights at sunset"),
    ("arts", "arts-1", 3724836, "Visitor sitting in a gallery with paintings on red walls"),
    ("arts", "arts-2", 2559741, "Colorful art prints in a gallery"),
    ("arts", "arts-3", 40025998, "Gallery hallway with paintings on easels"),
    ("science", "science-1", 9242834, "Students building a robot together"),
    ("science", "science-2", 7869034, "Hands assembling a small electronic project"),
    ("science", "science-3", 30720197, "Space suit on display in a museum"),
    ("family", "family-1", 10037051, "Family playing tug of war on a lawn"),
    ("family", "family-2", 18379520, "Parents hugging their two daughters in a park"),
    ("family", "family-3", 31075105, "Face painting at a children's art festival"),
    ("outdoors", "outdoors-1", 34207716, "Mountain trail through a forest valley"),
    ("outdoors", "outdoors-2", 32889289, "Coastal park with a lawn and an ocean view"),
    ("outdoors", "outdoors-3", 16344511, "Walking trail through woods"),
    ("community", "community-1", 32788612, "Neighbors jumping rope at a block party"),
    ("community", "community-2", 30712158, "Audience at a community meeting"),
    ("community", "community-3", 6646951, "Volunteers in matching shirts"),
    ("community", "community-4", 34003747, "Aerial view of a street festival"),
    ("general", "general-1", 2419018, "Crowd gathered in an indoor market hall at night"),
    ("general", "general-2", 34766307, "People socializing at an outdoor festival with string lights"),
    ("general", "general-3", 37721064, "Patio with string lights at dusk"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", default=SOURCE)
    parser.add_argument("--out", default=OUT)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--quality", type=int, default=78)
    parser.add_argument("--check", action="store_true", help="list the plan and verify the sources; write nothing")
    args = parser.parse_args()

    missing = [stem for _, stem, _, _ in PHOTOS if not os.path.exists(os.path.join(args.source, stem + ".jpg"))]
    if missing:
        sys.exit(f"missing review photographs in {args.source}: {', '.join(missing)} (nothing is downloaded or invented)")
    counts = {}
    plan = []
    for name, stem, pexels_id, alt in PHOTOS:
        counts[name] = counts.get(name, 0) + 1
        plan.append((name, f"{name}-{counts[name]}.webp", stem, pexels_id, alt))
    print(f"{len(plan)} photographs in {len(counts)} sets -> {args.out}")
    if args.check:
        for name, target, stem, pexels_id, _ in plan:
            print(f"  {target:34} <- {stem}.jpg (Pexels {pexels_id})")
        return

    from PIL import Image  # dev tool only; not a project dependency

    os.makedirs(args.out, exist_ok=True)
    sets, total = {}, 0
    for name, target, stem, pexels_id, alt in plan:
        with Image.open(os.path.join(args.source, stem + ".jpg")) as image:
            image = image.convert("RGB")
            if image.width > args.width:
                image = image.resize((args.width, round(image.height * args.width / image.width)), Image.LANCZOS)
            path = os.path.join(args.out, target)
            image.save(path, "WEBP", quality=args.quality, method=6)
        data = open(path, "rb").read()
        total += len(data)
        sets.setdefault(name, []).append({"file": target, "alt": alt, "v": hashlib.sha1(data).hexdigest()[:8], "pexels": pexels_id})
    with open(os.path.join(args.out, "index.json"), "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "sets": sets}, handle, indent=1)
    print(f"wrote {len(plan)} images, {total / 1024:.0f} KB in total, and index.json")


if __name__ == "__main__":
    main()
