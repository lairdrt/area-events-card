"""Review samples of the production map and venue-photo design (visual evaluation only; images are git-ignored).

The maps are the real output of area_events/discovery/maps.py (OpenFreeMap vector tiles -> SVG); this script rasterises that SVG
with resvg (node: npm i @resvg/resvg-js, investigation tooling only) and composes the card's frontend elements over it with Pillow
the way the card draws them: a pin at the exact centre, a MAP badge, the venue name and the provider credit.

    python experiments/location-imagery/make_samples_vector.py <folder with media files> <samples folder>
"""
import json
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ORANGE, GRAPHITE = (240, 138, 36), (37, 40, 45)
W, H = 1280, 720


def font(size, bold=False):
    for name in ("segoeuib.ttf" if bold else "segoeui.ttf", "arialbd.ttf" if bold else "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def rasterise(svg_path, png_path, width, tool):
    subprocess.run(["node", os.path.join(tool, "raster.mjs"), svg_path, png_path, str(width)], check=True)


def compose(base, theme, venue, credit, badge="MAP", approximate=False):
    s = W / 640
    img = base.convert("RGBA").resize((W, H), Image.LANCZOS)
    d = ImageDraw.Draw(img, "RGBA")
    cx, cy = W // 2, H // 2
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse((cx - 16, cy - 6, cx + 16, cy + 6), fill=(0, 0, 0, 130))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(5)))
    d = ImageDraw.Draw(img, "RGBA")
    r = int(17 * s)
    head = (cx, cy - int(40 * s))
    d.polygon([(head[0] - r + 6, head[1] + 12), (head[0] + r - 6, head[1] + 12), (cx, cy - 3)], fill=ORANGE + (255,))
    d.ellipse((head[0] - r - 3, head[1] - r - 3, head[0] + r + 3, head[1] + r + 3), fill=(255, 255, 255, 255))
    d.ellipse((head[0] - r, head[1] - r, head[0] + r, head[1] + r), fill=ORANGE + (255,))
    d.ellipse((head[0] - 8 * s, head[1] - 8 * s, head[0] + 8 * s, head[1] + 8 * s), fill=GRAPHITE + (255,))
    tag = font(int(12 * s), True)
    tw = d.textlength(badge, font=tag)
    d.rounded_rectangle((12 * s, 12 * s, 12 * s + tw + 22 * s, 12 * s + 28 * s), radius=14 * s, fill=GRAPHITE + (235,), outline=(255, 255, 255, 70))
    d.text((23 * s, 12 * s + 4 * s), badge, fill=ORANGE + (255,), font=tag)
    if venue:
        chip = font(int(13 * s), True)
        label = venue + ("  (approximate)" if approximate else "")
        cw = d.textlength(label, font=chip)
        d.rounded_rectangle((W - cw - 34 * s, 12 * s, W - 12 * s, 12 * s + 28 * s), radius=14 * s, fill=GRAPHITE + (225,))
        d.text((W - cw - 23 * s, 12 * s + 4 * s), label, fill=(240, 241, 244, 255), font=chip)
    small = font(int(10 * s))
    cw = d.textlength(credit, font=small)
    dark = theme == "dark"
    d.rounded_rectangle((8 * s, H - 24 * s, 8 * s + cw + 12 * s, H - 8 * s), radius=8 * s, fill=(GRAPHITE + (215,)) if dark else (255, 255, 255, 215))
    d.text((14 * s, H - 22 * s), credit, fill=(220, 222, 228, 255) if dark else (50, 52, 58, 255), font=small)
    return img.convert("RGB")


def main(media_folder, out, tool):
    os.makedirs(out, exist_ok=True)
    index = json.load(open(os.path.join(media_folder, "..", "index.json"), encoding="utf-8"))["records"]
    for key, record in index.items():
        if not record.get("ok"):
            continue
        venue = record["venue"].split(",")[0].strip() if record.get("venue") else ""
        approx = record["location"]["approximate"]
        for theme in ("light", "dark"):
            name = record["map"]["files"][theme]
            png = os.path.join(out, "_" + name[:-4] + ".png")
            rasterise(os.path.join(media_folder, name), png, W, tool)
            label = "".join(c if c.isalnum() else "-" for c in venue.lower()).strip("-") or "place"
            composed = compose(Image.open(png), theme, venue, record["map"]["credit"], approximate=approx)
            composed.save(os.path.join(out, f"map-{label}-{theme}.png"), optimize=True)
            os.remove(png)
        if record.get("photo"):
            photo = Image.open(os.path.join(media_folder, record["photo"]["file"])).convert("RGB")
            scale = max(W / photo.width, H / photo.height)  # cover, as the card's object-fit does
            photo = photo.resize((round(photo.width * scale), round(photo.height * scale)), Image.LANCZOS)
            left, top = (photo.width - W) // 2, (photo.height - H) // 2
            photo = photo.crop((left, top, left + W, top + H))
            d = ImageDraw.Draw(photo, "RGBA")
            tag, small = font(15, True), font(13)
            tw = d.textlength("VENUE", font=tag)
            d.rounded_rectangle((12, 12, 12 + tw + 22, 40), radius=14, fill=GRAPHITE + (235,), outline=(255, 255, 255, 70))
            d.text((23, 16), "VENUE", fill=ORANGE + (255,), font=tag)
            credit = "Photo: " + (record["photo"]["creator"][:60] or "Wikimedia Commons") + "  ·  " + record["photo"]["license"] + "  ·  Wikimedia Commons"
            cw = d.textlength(credit, font=small)
            d.rounded_rectangle((10, H - 34, 10 + cw + 18, H - 10), radius=10, fill=GRAPHITE + (215,))
            d.text((19, H - 31), credit, fill=(225, 227, 232, 255), font=small)
            photo.save(os.path.join(out, f"venue-photo-{label}.jpg"), quality=88)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ".")
