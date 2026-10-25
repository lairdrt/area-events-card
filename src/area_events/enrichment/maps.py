"""Location maps as small vector images, drawn from OpenFreeMap vector tiles with the standard library only.

OpenFreeMap (openfreemap.org) serves OpenMapTiles-schema vector tiles from OpenStreetMap data with no key and no account; its
terms allow downloads of at most 3,000 tiles or 50 MB at a time (at most 4 requests in parallel, a unique User-Agent), which is far
more than this needs: one tile at zoom 14 covers about 2.4 km, so a city's venues share a handful of tiles.
Required credit: "OpenFreeMap © OpenMapTiles Data from OpenStreetMap" (shown by the card, never baked into the image).

A map is an SVG of the 480 m x 270 m around a position, in a light and a graphite colour scheme with the same geometry. It
contains no text except street names, no script and no external reference; its colours are fixed in each variant. The pin, the
MAP badge, the venue name and the credit are drawn by the card. Vector output is sharp at any size and pixel density.
Everything read from a tile (names, tags) is untrusted: text is stripped to safe characters and XML-escaped.
"""
import gzip
import math
import re
import struct
import zlib
from xml.sax.saxutils import escape

ZOOM = 14
EXTENT = 4096
SPAN_M = 480.0           # ground width of a map
VIEW_W, VIEW_H = 1280, 720
MAX_TILE_BYTES = 3_000_000
MAX_FEATURES = 6000
FONT_UNITS = 38
SAFE_TEXT = re.compile(r"[^\w .,'’&/#()+-]", re.UNICODE)


# --- Mapbox Vector Tile decoding --------------------------------------------------------------------------------

def _varint(buffer, position):
    shift = result = 0
    while True:
        byte = buffer[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7
        if shift > 63:
            raise ValueError("bad varint")


def _fields(buffer):
    position, end = 0, len(buffer)
    while position < end:
        key, position = _varint(buffer, position)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, position = _varint(buffer, position)
        elif wire == 2:
            size, position = _varint(buffer, position)
            value, position = buffer[position:position + size], position + size
        elif wire == 1:
            value, position = buffer[position:position + 8], position + 8
        elif wire == 5:
            value, position = buffer[position:position + 4], position + 4
        else:
            raise ValueError("unsupported wire type")
        yield field, wire, value


def _packed(buffer):
    position, out = 0, []
    while position < len(buffer):
        value, position = _varint(buffer, position)
        out.append(value)
    return out


def _value(buffer):
    for field, wire, value in _fields(buffer):
        if field == 1 and wire == 2:
            return value.decode("utf-8", "replace")
        if field in (4, 5) and wire == 0:
            return value
        if field == 6 and wire == 0:
            return (value >> 1) ^ -(value & 1)
        if field == 7 and wire == 0:
            return bool(value)
    return None


def _geometry(commands):
    """Lists of (x, y) points: one list per line or ring."""
    parts, current, x, y, i = [], [], 0, 0, 0
    while i < len(commands):
        header = commands[i]
        i += 1
        command, count = header & 7, header >> 3
        for _ in range(count):
            if command in (1, 2):
                dx, dy = commands[i], commands[i + 1]
                i += 2
                x += (dx >> 1) ^ -(dx & 1)
                y += (dy >> 1) ^ -(dy & 1)
                if command == 1 and current:
                    parts.append(current)
                    current = []
                current.append((x, y))
            elif command == 7 and current:
                current.append(current[0])
    if current:
        parts.append(current)
    return parts


def decode_tile(data, wanted=("water", "landcover", "landuse", "park", "building", "transportation", "transportation_name")):
    """{layer: [{"type", "props", "parts"}]} for the layers a map needs. Raises ValueError on malformed data."""
    if data[:2] == b"\x1f\x8b":
        decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
        try:
            data = decompressor.decompress(data, MAX_TILE_BYTES)
        except zlib.error as error:
            raise ValueError(f"malformed vector tile: {error}") from error
        if decompressor.unconsumed_tail:
            raise ValueError("tile too large")
    layers, budget = {}, MAX_FEATURES * 4
    try:
        for field, wire, raw in _fields(data):
            if field != 3 or wire != 2:
                continue
            name, keys, values, features = "", [], [], []
            for f, w, v in _fields(raw):
                if f == 1 and w == 2:
                    name = v.decode("utf-8", "replace")
                elif f == 2 and w == 2:
                    features.append(v)
                elif f == 3 and w == 2:
                    keys.append(v.decode("utf-8", "replace"))
                elif f == 4 and w == 2:
                    values.append(_value(v))
            if name not in wanted:
                continue
            out = []
            for feature in features:
                kind, tags, geometry = 0, [], []
                for f, w, v in _fields(feature):
                    if f == 2 and w == 2:
                        tags = _packed(v)
                    elif f == 3 and w == 0:
                        kind = v
                    elif f == 4 and w == 2:
                        geometry = _packed(v)
                budget -= 1
                if budget < 0:
                    raise ValueError("tile has too many features")
                props = {keys[tags[j]]: values[tags[j + 1]] for j in range(0, len(tags) - 1, 2) if tags[j] < len(keys) and tags[j + 1] < len(values)}
                out.append({"type": kind, "props": props, "parts": _geometry(geometry)})
            layers.setdefault(name, []).extend(out)
    except (IndexError, ValueError, struct.error) as error:
        raise ValueError(f"malformed vector tile: {error}") from error
    return layers


# --- geometry ---------------------------------------------------------------------------------------------------

def tile_of(lat, lon, zoom=ZOOM):
    """(tile x, tile y, fractional x, fractional y) of a position, fractions in tile units (0..EXTENT)."""
    n = 2 ** zoom
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return int(x), int(y), (x - int(x)) * EXTENT, (y - int(y)) * EXTENT


def window(lat, lon, zoom=ZOOM):
    """The map window in global tile units (origin_x, origin_y, width, height) and the tiles it touches."""
    tx, ty, fx, fy = tile_of(lat, lon, zoom)
    metres_per_unit = 40075016.686 * math.cos(math.radians(lat)) / (2 ** zoom * EXTENT)
    width = SPAN_M / metres_per_unit
    height = width * VIEW_H / VIEW_W
    gx, gy = tx * EXTENT + fx, ty * EXTENT + fy
    ox, oy = gx - width / 2, gy - height / 2
    tiles = sorted({(int(px // EXTENT), int(py // EXTENT)) for px in (ox, ox + width) for py in (oy, oy + height)})
    return ox, oy, width, height, tiles


def _clip(points, box):
    """Liang-Barsky clipping of a polyline: the longest piece inside the box."""
    x0, y0, x1, y1 = box
    best, piece = [], []
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        t0, t1, dx, dy = 0.0, 1.0, bx - ax, by - ay
        ok = True
        for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                r = q / p
                if p < 0:
                    t0 = max(t0, r)
                else:
                    t1 = min(t1, r)
        if not ok or t0 > t1:
            if len(piece) > len(best):
                best = piece
            piece = []
            continue
        start, end = (ax + t0 * dx, ay + t0 * dy), (ax + t1 * dx, ay + t1 * dy)
        if not piece or piece[-1] != start:
            if len(piece) > len(best):
                best = piece
            piece = [start]
        piece.append(end)
    return best if len(best) >= len(piece) else piece


def _straightest(points, limit_degrees=32):
    """The longest stretch of a polyline whose total turning stays small, so text along it does not fold over itself."""
    best, best_length = points[:2], 0.0
    for i in range(len(points) - 1):
        turn, length, j = 0.0, 0.0, i + 1
        previous = None
        while j < len(points):
            segment = (points[j][0] - points[j - 1][0], points[j][1] - points[j - 1][1])
            size = math.hypot(*segment)
            if size == 0:
                j += 1
                continue
            heading = math.atan2(segment[1], segment[0])
            if previous is not None:
                delta = abs((heading - previous + math.pi) % (2 * math.pi) - math.pi)
                turn += math.degrees(delta)
                if turn > limit_degrees:
                    break
            previous, length, j = heading, length + size, j + 1
        if length > best_length:
            best, best_length = points[i:j], length
    return best


def _bbox_hits(points, box):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return not (max(xs) < box[0] or min(xs) > box[2] or max(ys) < box[1] or min(ys) > box[3])


# --- drawing ---------------------------------------------------------------------------------------------------

THEMES = {
    "light": {
        "bg": "#f1efe9", "park": "#d9e7cb", "wood": "#cfe0c0", "water": "#b9d6ec", "building": "#d9d4ca", "building_edge": "#c3bdb1",
        "casing": "#c9c4b9", "minor": "#ffffff", "major": "#fde3a7", "highway": "#f6b55c", "path": "#b9b3a8",
        "label": "#3a3d44", "halo": "#ffffff",
    },
    "dark": {
        "bg": "#202328", "park": "#26332b", "wood": "#243027", "water": "#223548", "building": "#32373f", "building_edge": "#444a54",
        "casing": "#15171a", "minor": "#4b525c", "major": "#d9822b", "highway": "#f08a24", "path": "#4b525c",
        "label": "#e6e8ec", "halo": "#202328",
    },
}
ROAD_WIDTH = {"motorway": 15, "trunk": 14, "primary": 13, "secondary": 11, "tertiary": 10, "minor": 8, "service": 5, "track": 4, "path": 3, "pedestrian": 6}
ROAD_ORDER = ["path", "track", "service", "minor", "pedestrian", "tertiary", "secondary", "primary", "trunk", "motorway"]
MAJOR = {"motorway": "highway", "trunk": "highway", "primary": "major", "secondary": "major"}


def safe_text(value):
    text = SAFE_TEXT.sub("", str(value or "")).strip()
    return re.sub(r"\s+", " ", text)[:48]


def _path(parts, ox, oy, scale, close=False):
    out = []
    for part in parts:
        pts = [(round((x - ox) * scale), round((y - oy) * scale)) for x, y in part]
        dedup = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
        if len(dedup) < 2:
            continue
        out.append("M" + "L".join(f"{x} {y}" for x, y in dedup) + ("Z" if close else ""))
    return "".join(out)


def render(tiles_data, lat, lon, theme):
    """The SVG for one colour scheme. `tiles_data` maps (tile x, tile y) to the decoded layers of that tile."""
    colours = THEMES[theme]
    ox, oy, width, height, _ = window(lat, lon)
    scale = VIEW_W / width
    box = (ox - 40, oy - 40, ox + width + 40, oy + height + 40)
    layers = {}
    for (tx, ty), decoded in tiles_data.items():
        for name, features in decoded.items():
            for feature in features:
                parts = [[(x + tx * EXTENT, y + ty * EXTENT) for x, y in part] for part in feature["parts"] if part]
                parts = [p for p in parts if p and _bbox_hits(p, box)]
                if parts:
                    layers.setdefault(name, []).append((feature["type"], feature["props"], parts))
    shapes = [f'<rect width="{VIEW_W}" height="{VIEW_H}" fill="{colours["bg"]}"/>']

    def polygons(name, pick, fill, stroke=None):
        d = "".join(_path(parts, ox, oy, scale, close=True) for kind, props, parts in layers.get(name, []) if kind == 3 and pick(props))
        if d:
            edge = f' stroke="{stroke}" stroke-width="2"' if stroke else ""
            shapes.append(f'<path d="{d}" fill="{fill}"{edge} fill-rule="evenodd"/>')

    polygons("landcover", lambda p: p.get("class") in ("grass", "farmland", "ice", "sand"), colours["park"])
    polygons("landcover", lambda p: p.get("class") in ("wood",), colours["wood"])
    polygons("landuse", lambda p: p.get("class") in ("park", "pitch", "cemetery", "playground", "garden", "recreation_ground", "stadium"), colours["park"])
    polygons("park", lambda p: True, colours["park"])
    polygons("water", lambda p: True, colours["water"])
    polygons("building", lambda p: True, colours["building"], colours["building_edge"])

    roads = [(ROAD_ORDER.index(p.get("class")) if p.get("class") in ROAD_ORDER else -1, p.get("class"), parts)
             for kind, p, parts in layers.get("transportation", []) if kind == 2 and p.get("class") in ROAD_ORDER and p.get("brunnel") != "tunnel"]
    roads.sort(key=lambda r: r[0])
    for casing in (True, False):
        for _, cls, parts in roads:
            d = _path(parts, ox, oy, scale)
            if not d:
                continue
            w = ROAD_WIDTH[cls]
            if cls in ("path", "track", "pedestrian") and not casing:
                shapes.append(f'<path d="{d}" fill="none" stroke="{colours["path"]}" stroke-width="{w}" stroke-linecap="round" stroke-dasharray="{w * 2} {w}"/>')
            elif cls in ("path", "track", "pedestrian"):
                continue
            elif casing:
                shapes.append(f'<path d="{d}" fill="none" stroke="{colours["casing"]}" stroke-width="{w + 4}" stroke-linecap="round" stroke-linejoin="round"/>')
            else:
                fill = colours[MAJOR.get(cls, "minor")]
                shapes.append(f'<path d="{d}" fill="none" stroke="{fill}" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round"/>')

    # street names along the road, only where the name fits on a straight enough stretch inside the map
    labelled, text_items, mids = set(), [], []
    inner = (ox + width * 0.03, oy + height * 0.05, ox + width * 0.97, oy + height * 0.95)
    candidates = []
    for kind, props, parts in layers.get("transportation_name", []):
        name = safe_text(props.get("name:latin") or props.get("name"))
        if kind != 2 or not name or name.lower() in labelled:
            continue
        for part in parts:
            piece = _clip(part, inner)
            if len(piece) < 2:
                continue
            piece = _straightest(piece)
            if len(piece) < 2:
                continue
            length = sum(math.dist(a, b) for a, b in zip(piece, piece[1:])) * scale
            candidates.append((length, name, piece))
    for length, name, piece in sorted(candidates, key=lambda c: -c[0]):
        needed = len(name) * FONT_UNITS * 0.58 + 24
        if length < needed or name.lower() in labelled or len(text_items) >= 6:
            continue
        # trim to the middle stretch that is long enough for the text, so the label sits in the middle of the street
        points = [((x - ox) * scale, (y - oy) * scale) for x, y in piece]
        if points[0][0] > points[-1][0]:
            points.reverse()
        d = "M" + "L".join(f"{round(x)} {round(y)}" for x, y in points)
        mid = points[len(points) // 2]
        if any(math.hypot(mid[0] - m[0], mid[1] - m[1]) < 300 for m in mids):
            continue  # two labels on top of each other are worse than one
        mids.append(mid)
        text_items.append((name, d))
        labelled.add(name.lower())
    defs, texts = [], []
    for i, (name, d) in enumerate(text_items):
        defs.append(f'<path id="s{i}" d="{d}"/>')
        texts.append(f'<text font-size="{FONT_UNITS}" font-weight="600" text-anchor="middle" dy="{round(FONT_UNITS * 0.35)}" fill="{colours["label"]}" '
                     f'stroke="{colours["halo"]}" stroke-width="9" stroke-linejoin="round" paint-order="stroke">'
                     f'<textPath href="#s{i}" startOffset="50%">{escape(name)}</textPath></text>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
            'font-family="system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif">%s%s%s</svg>'
            % (VIEW_W, VIEW_H, VIEW_W, VIEW_H, f'<defs>{"".join(defs)}</defs>' if defs else "", "".join(shapes), "".join(texts)))


def tiles_needed(lat, lon):
    return window(lat, lon)[4]


def decode_gzip(data):
    return gzip.decompress(data)
