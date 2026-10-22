"""Finished sample event-location maps from the USGS National Map export service (visual evaluation only; images are git-ignored).

    python experiments/location-imagery/make_samples.py            # writes review/location-imagery/samples/

Each sample is 640x360: a street map centred on the venue, a pin, a discreet "MAP" tag and the data acknowledgment. Two looks are made:
`graphite` (the USGS reference map recoloured to the Area Events dark/orange palette) and `light` (the USGS colours unchanged).
Needs Pillow and numpy (investigation tooling only, not project dependencies).
"""
import io
import math
import os
import sys
import time
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.environ.get("AE_LOC_DIR") or os.path.join(REPO, "review", "location-imagery")
SAMPLES = os.path.join(OUT, "samples")
UA = "area-events-card-research/0.1 (investigation; contact robin.laird@proton.me)"
EXPORT = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/export"
ACK = "Map services and data available from U.S. Geological Survey, National Geospatial Program"
GRAPHITE, ORANGE = (37, 40, 45), (240, 138, 36)
W, H = 640, 360


def mercator(lat, lon):
    return lon * 20037508.342789244 / 180, math.log(math.tan((90 + lat) * math.pi / 360)) * 6378137


def fetch_map(lat, lon, span_m, dpi):
    """One export request: a W x H image whose width is `span_m` metres on the ground; `dpi` scales the labels."""
    x, y = mercator(lat, lon)
    k = 1 / math.cos(math.radians(lat))  # Web Mercator metres stretch away from the equator
    half_w, half_h = span_m * k / 2, span_m * k * H / W / 2
    query = urllib.parse.urlencode({"bbox": f"{x - half_w},{y - half_h},{x + half_w},{y + half_h}", "bboxSR": 3857, "imageSR": 3857,
                                    "size": f"{W},{H}", "dpi": dpi, "format": "png", "transparent": "false", "f": "image"})
    started = time.time()
    with urllib.request.urlopen(urllib.request.Request(EXPORT + "?" + query, headers={"User-Agent": UA}), timeout=40) as response:
        data = response.read()
    return Image.open(io.BytesIO(data)).convert("RGB"), time.time() - started, len(data)


def graphite(image):
    """Recolour the light reference map: dark graphite ground, light linework and labels, orange main roads, muted green and blue."""
    rgb = np.asarray(image).astype(np.float32)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    ink = np.clip((1 - lum) / 0.55, 0, 1) ** 0.85            # 0 on the pale ground, 1 on dark text and lines
    dark, light = np.array([34, 37, 42], np.float32), np.array([222, 224, 228], np.float32)
    chroma = rgb.max(-1) - rgb.min(-1)
    # contour lines are thin tan strokes: fade them toward the ground so they texture the map without competing with streets and labels
    tan = np.clip((r - b - 10) / 15, 0, 1) * np.clip((120 - (r - b)) / 30, 0, 1) * np.clip((g - b - 3) / 10, 0, 1) * np.clip((r - g) / 8, 0, 1) * (lum > 0.45) * (1 - np.clip((r - np.maximum(g, b) - 25) / 70, 0, 1))
    ink = ink * (1 - 0.9 * tan)
    out = dark + (light - dark) * ink[..., None]
    red = np.clip((r - np.maximum(g, b) - 25) / 70, 0, 1)    # main roads are drawn in red/pink
    out = out * (1 - red[..., None]) + np.array([240, 138, 36], np.float32) * red[..., None]
    green = np.clip((g - np.maximum(r, b) - 6) / 30, 0, 1) * np.clip(chroma / 40, 0, 1)
    out = out * (1 - 0.8 * green[..., None]) + np.array([48, 72, 58], np.float32) * 0.8 * green[..., None]
    blue = np.clip((b - r - 26) / 40, 0, 1) * np.clip((b - g) / 12, 0, 1)
    out = out * (1 - 0.8 * blue[..., None]) + np.array([40, 66, 92], np.float32) * 0.8 * blue[..., None]
    # buildings are pale lavender on the original: a slightly lifted graphite keeps them visible without competing with labels
    lav = np.clip((b - r - 8) / 20, 0, 1) * np.clip(1 - np.abs(lum - 0.85) / 0.2, 0, 1) * (1 - blue)
    out = out * (1 - 0.7 * lav[..., None]) + np.array([58, 62, 72], np.float32) * 0.7 * lav[..., None]
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def font(size, bold=False):
    for name in ("segoeuib.ttf" if bold else "segoeui.ttf", "arialbd.ttf" if bold else "arial.ttf", "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def finish(image, look, venue):
    """Pin at the centre, a MAP tag and the acknowledgment, drawn at 2x and reduced for smooth edges."""
    s = 2
    big = image.resize((W * s, H * s), Image.LANCZOS).convert("RGBA")
    d = ImageDraw.Draw(big, "RGBA")
    cx, cy = W * s // 2, H * s // 2
    # soft ground shadow and a halo so the pin reads on any background
    shadow = Image.new("RGBA", big.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.ellipse((cx - 11 * s, cy - 4 * s, cx + 11 * s, cy + 4 * s), fill=(0, 0, 0, 120))
    big.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(3 * s)))
    d = ImageDraw.Draw(big, "RGBA")
    r = 15 * s
    head = (cx, cy - 34 * s)
    d.ellipse((cx - 5 * s, cy - 5 * s, cx + 5 * s, cy + 5 * s), fill=(255, 255, 255, 230))                       # the exact spot
    d.polygon([(head[0] - (r - 4 * s), head[1] + 8 * s), (head[0] + (r - 4 * s), head[1] + 8 * s), (cx, cy - 2 * s)], fill=ORANGE + (255,), outline=(255, 255, 255, 255))
    d.ellipse((head[0] - r - 2 * s, head[1] - r - 2 * s, head[0] + r + 2 * s, head[1] + r + 2 * s), fill=(255, 255, 255, 255))
    d.ellipse((head[0] - r, head[1] - r, head[0] + r, head[1] + r), fill=ORANGE + (255,))
    d.ellipse((head[0] - 6 * s, head[1] - 6 * s, head[0] + 6 * s, head[1] + 6 * s), fill=GRAPHITE + (255,))
    # MAP tag (top left): graphite pill, orange text
    tag_font = font(11 * s, bold=True)
    text = "MAP"
    tw = d.textlength(text, font=tag_font)
    d.rounded_rectangle((8 * s, 8 * s, 8 * s + tw + 16 * s, 8 * s + 22 * s), radius=11 * s, fill=GRAPHITE + (235,), outline=(255, 255, 255, 60))
    d.text((16 * s, 8 * s + 2 * s), text, fill=ORANGE + (255,), font=tag_font)
    # acknowledgment (bottom): a translucent strip with the USGS wording
    ack_font = font(9 * s)
    d.rectangle((0, (H - 15) * s, W * s, H * s), fill=GRAPHITE + (215,) if look == "graphite" else (255, 255, 255, 215))
    d.text((6 * s, (H - 13) * s), ACK, fill=(215, 218, 224, 255) if look == "graphite" else (50, 52, 58, 255), font=ack_font)
    return big.resize((W, H), Image.LANCZOS).convert("RGB")


def make(name, lat, lon, venue, span_m=520, dpi=150):
    os.makedirs(SAMPLES, exist_ok=True)
    base, seconds, size = fetch_map(lat, lon, span_m, dpi)
    results = []
    for look, image in (("graphite", graphite(base)), ("light", base)):
        final = finish(image, look, venue)
        path = os.path.join(SAMPLES, f"{name}-{look}-{span_m}m.jpg")
        final.save(path, quality=86, optimize=True, progressive=True)
        webp = path[:-4] + ".webp"
        final.save(webp, "WEBP", quality=82, method=6)
        results.append((path, os.path.getsize(path), os.path.getsize(webp)))
        print(f"{os.path.basename(path)}: jpg {os.path.getsize(path) // 1024} KB, webp {os.path.getsize(webp) // 1024} KB (service {seconds:.2f} s, {size // 1024} KB png)")
    return results


if __name__ == "__main__":
    make("sandiego-viejas-arena", 32.7738109, -117.0745404, "Viejas Arena")
    time.sleep(1)
    make("austin-bass-concert-hall", 30.285816, -97.73098, "Bass Concert Hall")
