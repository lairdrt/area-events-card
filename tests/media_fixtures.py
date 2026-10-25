"""Small deterministic fakes shared by active location and imagery tests."""
import json
import types

from area_events.discovery.safe import FetchError
from area_events.enrichment import maps, media

SD = {"latitude": 32.7157, "longitude": -117.1611, "radius_miles": 25, "display_name": "San Diego, California, United States"}
VIEJAS = (32.7738109, -117.0745404)


def _varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number, wire, payload):
    key = _varint(number << 3 | wire)
    return key + (_varint(len(payload)) + payload if wire == 2 else payload)


def _zigzag(value):
    return (value << 1) ^ (value >> 63)


def _geometry(parts, polygon=False):
    out, x, y = [], 0, 0
    for part in parts:
        out.append(1 | 1 << 3)
        out.extend((_zigzag(part[0][0] - x), _zigzag(part[0][1] - y)))
        x, y = part[0]
        rest = part[1:-1] if polygon else part[1:]
        out.append(2 | len(rest) << 3)
        for px, py in rest:
            out.extend((_zigzag(px - x), _zigzag(py - y)))
            x, y = px, py
        if polygon:
            out.append(7 | 1 << 3)
    return b"".join(_varint(value) for value in out)


def _layer(name, features, extent=4096):
    keys, values, body = [], [], b""
    for kind, props, parts in features:
        tags = []
        for key, value in props.items():
            if key not in keys:
                keys.append(key)
            if value not in values:
                values.append(value)
            tags.extend((keys.index(key), values.index(value)))
        feature = _field(2, 2, b"".join(_varint(tag) for tag in tags)) + _field(3, 0, _varint(kind)) + _field(4, 2, _geometry(parts, polygon=kind == 3))
        body += _field(2, 2, feature)
    body = _field(1, 2, name.encode()) + body
    for key in keys:
        body += _field(3, 2, key.encode())
    for value in values:
        body += _field(4, 2, _field(1, 2, value.encode()))
    return _field(3, 2, body + _field(5, 0, _varint(extent)) + _field(15, 0, _varint(2)))


def sample_tile(venue_x, venue_y, name="Aztec Walk"):
    building = [(venue_x - 120, venue_y - 90), (venue_x + 120, venue_y - 90), (venue_x + 120, venue_y + 90), (venue_x - 120, venue_y + 90), (venue_x - 120, venue_y - 90)]
    road = [(venue_x - 900, venue_y + 150), (venue_x + 900, venue_y + 150)]
    return b"".join((_layer("building", [(3, {"render_height": "10"}, [building])]),
                     _layer("transportation", [(2, {"class": "minor"}, [road]),
                                                (2, {"class": "primary"}, [[(venue_x - 900, venue_y - 150), (venue_x + 900, venue_y - 150)]])]),
                     _layer("transportation_name", [(2, {"name": name, "class": "minor"}, [road])]),
                     _layer("landcover", [(3, {"class": "grass"}, [[(0, 0), (300, 0), (300, 300), (0, 300), (0, 0)]])])))


def nominatim(name, lat, lon, house=None, road=None, category="amenity", kind="theatre", city="San Diego", wikidata=None, osm_id=1):
    return {"lat": str(lat), "lon": str(lon), "name": name, "category": category, "type": kind, "osm_type": "way", "osm_id": osm_id,
            "address": {key: value for key, value in {"house_number": house, "road": road, "city": city}.items() if value},
            "extratags": {"wikidata": wikidata} if wikidata else {}}


def _commons():
    meta = {"LicenseShortName": {"value": "CC BY-SA 3.0"}, "Artist": {"value": "A. Photographer"}, "Restrictions": {"value": ""},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/3.0/"}}
    return {"query": {"pages": {"1": {"imageinfo": [{"mime": "image/jpeg", "width": 2000, "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/F.jpg/640px-F.jpg",
                                                              "descriptionurl": "https://commons.wikimedia.org/wiki/File:F.jpg", "extmetadata": meta}]}}}}


class Wikimedia:
    def __init__(self, qid="Q1", label="Viejas Arena", aliases=("Cox Arena",), coordinate=(32.77381, -117.07454), image="Arena front.jpg", info=None, geosearch=None,
                 bytes_=b"\xff\xd8\xff" + b"x" * 5000):
        claims = {}
        if coordinate:
            claims["P625"] = [{"mainsnak": {"datavalue": {"value": {"latitude": coordinate[0], "longitude": coordinate[1]}}}}]
        if image:
            claims["P18"] = [{"mainsnak": {"datavalue": {"value": image}}}]
        self.calls, self.bytes_ = [], bytes_
        self.entity = {"entities": {qid: {"labels": {"en": {"value": label}}, "aliases": {"en": [{"value": item} for item in aliases]}, "claims": claims}}}
        self.info = info or _commons()
        self.geosearch = geosearch if geosearch is not None else [{"title": "Viejas Arena", "pageid": 7, "dist": 8.0}]
        self.qid = qid

    def json(self, url):
        self.calls.append(url)
        if "list=geosearch" in url:
            return {"query": {"geosearch": self.geosearch}}
        if "prop=pageprops" in url:
            return {"query": {"pages": {"7": {"pageprops": {"wikibase_item": self.qid}}}}}
        if "wbgetentities" in url:
            return self.entity
        if "commons.wikimedia.org" in url:
            return self.info
        raise AssertionError(url)

    def fetch_bytes(self, url, types_):
        self.calls.append(url)
        self.last_types = types_
        return self.bytes_


class FakeFetcher:
    def __init__(self, nominatim_results=None, fail=None, wiki=None, overpass=None):
        self.urls, self.fail, self.wiki, self.overpass = [], fail, wiki, overpass
        self.nominatim_results = nominatim_results if nominatim_results is not None else {}

    def fetch(self, url, check_robots=True, binary=None, timeout=None):
        self.urls.append(url)
        if self.fail and self.fail(url):
            raise FetchError("unavailable")
        if "overpass" in url:
            return types.SimpleNamespace(text=json.dumps({"elements": self.overpass or []}), body=b"")
        if "nominatim" in url:
            for needle, result in self.nominatim_results.items():
                if needle.replace(" ", "+") in url or needle in url:
                    return types.SimpleNamespace(text=json.dumps(result), body=b"")
            return types.SimpleNamespace(text="[]", body=b"")
        if url == media.TILEJSON:
            return types.SimpleNamespace(text=json.dumps({"tiles": ["https://tiles.openfreemap.org/planet/20261018_080001_pt/{z}/{x}/{y}.pbf"]}), body=b"")
        if url.startswith("https://tiles.openfreemap.org/planet/"):
            x, y = (int(value) for value in url[:-4].split("/")[-2:])
            expected_x, expected_y = maps.tile_of(*VIEJAS)[:2]
            return types.SimpleNamespace(text="", body=sample_tile(2000, 2000) if (x, y) == (expected_x, expected_y) else sample_tile(100, 100))
        if self.wiki and ("wikipedia.org" in url or "wikidata.org" in url or "commons.wikimedia" in url):
            return types.SimpleNamespace(text=json.dumps(self.wiki.json(url)), body=b"")
        if self.wiki and "wikimedia.org" in url:
            return types.SimpleNamespace(text="", body=self.wiki.fetch_bytes(url, binary))
        raise AssertionError(url)
