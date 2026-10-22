import os as _os

REPO = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
LOC_DIR = _os.environ.get("AE_LOC_DIR") or _os.path.join(REPO, "review", "location-imagery")  # git-ignored working folder
"""Prototype: a USGS National Map static image centred on a venue, with a locally drawn pin and an honest label. Scratch only."""
import io, math, os, sys, time, urllib.parse, urllib.request
from PIL import Image, ImageDraw, ImageFont

OUT = _os.path.join(LOC_DIR, "maps")
UA = "area-events-card-research/0.1 (investigation; contact robin.laird@proton.me)"
SERVICE = "https://basemap.nationalmap.gov/arcgis/rest/services/{}/MapServer/export"


def mercator(lat, lon):
    x = lon * 20037508.342789244 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) * 6378137
    return x, y


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=40) as response:
        return response.read()


def static_map(lat, lon, span_m, service="USGSTopo", size=(640, 360)):
    x, y = mercator(lat, lon)
    # Web Mercator metres stretch with latitude; scale so span_m is a true ground distance
    k = 1 / math.cos(math.radians(lat))
    half_w, half_h = span_m * k / 2, span_m * k * size[1] / size[0] / 2
    query = urllib.parse.urlencode({"bbox": f"{x - half_w},{y - half_h},{x + half_w},{y + half_h}", "bboxSR": 3857, "imageSR": 3857,
                                    "size": f"{size[0]},{size[1]}", "dpi": 96, "format": "png", "transparent": "false", "f": "image"})
    return Image.open(io.BytesIO(fetch(SERVICE.format(service) + "?" + query))).convert("RGB")


def pin(image, label_text="Map: event location"):
    w, h = image.size
    d = ImageDraw.Draw(image, "RGBA")
    cx, cy = w // 2, h // 2
    r = 13
    tip = (cx, cy)
    head = (cx, cy - 30)
    d.ellipse((cx - 7, cy - 4, cx + 7, cy + 3), fill=(0, 0, 0, 70))           # ground shadow
    d.polygon([(head[0] - r + 3, head[1] + 7), (head[0] + r - 3, head[1] + 7), tip], fill=(232, 93, 4, 255), outline=(255, 255, 255, 255))
    d.ellipse((head[0] - r, head[1] - r, head[0] + r, head[1] + r), fill=(232, 93, 4, 255), outline=(255, 255, 255, 255), width=2)
    d.ellipse((head[0] - 5, head[1] - 5, head[0] + 5, head[1] + 5), fill=(255, 255, 255, 255))
    font = ImageFont.load_default(size=13)
    small = ImageFont.load_default(size=11)
    tw = d.textlength(label_text, font=font)
    d.rounded_rectangle((8, 8, 8 + tw + 14, 8 + 22), radius=11, fill=(255, 255, 255, 235), outline=(0, 0, 0, 60))
    d.text((15, 11), label_text, fill=(40, 40, 40, 255), font=font)
    credit = "USGS The National Map; U.S. Census Bureau; NOAA; Natural Earth"
    cw = d.textlength(credit, font=small)
    d.rectangle((0, h - 17, cw + 10, h), fill=(255, 255, 255, 215))
    d.text((5, h - 15), credit, fill=(60, 60, 60, 255), font=small)
    return image


def make(name, lat, lon, spans=(450, 900, 1800), service="USGSTopo"):
    paths = []
    for span in spans:
        image = pin(static_map(lat, lon, span, service))
        png = os.path.join(OUT, f"{name}-{span}m.png")
        image.save(png)
        webp = os.path.join(OUT, f"{name}-{span}m.webp")
        image.save(webp, "WEBP", quality=80, method=6)
        print(f"{name} {span} m: png {os.path.getsize(png) // 1024} KB, webp {os.path.getsize(webp) // 1024} KB")
        paths.append(png)
        time.sleep(1.0)
    return paths


def sheet(paths, out, tile_w=300):
    thumbs = [Image.open(p).resize((tile_w, round(tile_w * 360 / 640)), Image.LANCZOS) for p in paths]
    full = [Image.open(p) for p in paths]
    cols = len(paths)
    canvas = Image.new("RGB", (cols * 646 + 6, 360 + 6 + thumbs[0].height + 12), (30, 30, 30))
    for i, im in enumerate(full):
        canvas.paste(im, (6 + i * 646, 6))
    for i, im in enumerate(thumbs):
        canvas.paste(im, (6 + i * 646, 372))
    canvas.save(out, quality=88)


if __name__ == "__main__":
    name, lat, lon = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    sheet(make(name, lat, lon), os.path.join(OUT, f"{name}-sheet.jpg"))
