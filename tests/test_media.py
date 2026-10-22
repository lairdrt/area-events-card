"""Location imagery: vector map drawing, the trust rules for a pin, venue-photograph verification, the background service and its cache,
and how the result is served. Everything runs on fixtures; nothing contacts OpenFreeMap, Nominatim or Wikimedia."""
import asyncio
import gzip
import json
import os
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from discovery import engine as engine_module
from discovery import maps, media
from discovery.core import FetchError
from discovery.safe import SafeFetcher, State
from discovery.server import Api, Raw

SD = {"latitude": 32.7157, "longitude": -117.1611, "radius_miles": 25, "display_name": "San Diego, California, United States"}
VIEJAS = (32.7738109, -117.0745404)


# --- a tiny vector-tile encoder (the inverse of maps.decode_tile) -------------------------------------------------

def varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def field(number, wire, payload):
    key = varint(number << 3 | wire)
    return key + (varint(len(payload)) + payload if wire == 2 else payload)


def zigzag(n):
    return (n << 1) ^ (n >> 63)


def geometry(parts, polygon=False):
    out, x, y = [], 0, 0
    for part in parts:
        out.append(1 | 1 << 3)
        out += [zigzag(part[0][0] - x), zigzag(part[0][1] - y)]
        x, y = part[0]
        rest = part[1:-1] if polygon else part[1:]
        out.append(2 | len(rest) << 3)
        for px, py in rest:
            out += [zigzag(px - x), zigzag(py - y)]
            x, y = px, py
        if polygon:
            out.append(7 | 1 << 3)
    return b"".join(varint(v) for v in out)


def layer(name, features, extent=4096):
    keys, values, body = [], [], b""
    for kind, props, parts in features:
        tags = []
        for k, v in props.items():
            keys.append(k) if k not in keys else None
            values.append(v) if v not in values else None
            tags += [keys.index(k), values.index(v)]
        feature = field(2, 2, b"".join(varint(t) for t in tags)) + field(3, 0, varint(kind)) + field(4, 2, geometry(parts, polygon=kind == 3))
        body += field(2, 2, feature)
    body = field(1, 2, name.encode()) + body
    for k in keys:
        body += field(3, 2, k.encode())
    for v in values:
        body += field(4, 2, field(1, 2, v.encode()))
    return field(3, 2, body + field(5, 0, varint(extent)) + field(15, 0, varint(2)))


def tile_bytes(*layers):
    return b"".join(layers)


def sample_tile(venue_x, venue_y, name="Aztec Walk"):
    """A tile with a building around (venue_x, venue_y), a long road with a name, and a park."""
    building = [(venue_x - 120, venue_y - 90), (venue_x + 120, venue_y - 90), (venue_x + 120, venue_y + 90), (venue_x - 120, venue_y + 90), (venue_x - 120, venue_y - 90)]
    road = [(venue_x - 900, venue_y + 150), (venue_x + 900, venue_y + 150)]
    return tile_bytes(layer("building", [(3, {"render_height": "10"}, [building])]),
                      layer("transportation", [(2, {"class": "minor"}, [road]), (2, {"class": "primary"}, [[(venue_x - 900, venue_y - 150), (venue_x + 900, venue_y - 150)]])]),
                      layer("transportation_name", [(2, {"name": name, "class": "minor"}, [road])]),
                      layer("landcover", [(3, {"class": "grass"}, [[(0, 0), (300, 0), (300, 300), (0, 300), (0, 0)]])]))


class VectorTiles(unittest.TestCase):
    def test_decoding_reads_geometry_and_properties(self):
        decoded = maps.decode_tile(sample_tile(2000, 2000))
        self.assertEqual(sorted(decoded), ["building", "landcover", "transportation", "transportation_name"])
        building = decoded["building"][0]
        self.assertEqual((building["type"], building["props"]["render_height"]), (3, "10"))
        self.assertEqual(building["parts"][0][0], (1880, 1910))
        self.assertEqual(decoded["transportation_name"][0]["props"]["name"], "Aztec Walk")

    def test_gzip_is_read_and_malformed_or_oversized_tiles_are_refused(self):
        raw = sample_tile(2000, 2000)
        self.assertEqual(sorted(maps.decode_tile(gzip.compress(raw))), sorted(maps.decode_tile(raw)))
        for bad in (b"\xff\xff\xff\xff\xff", raw[:25], b"\x1f\x8b not gzip"):
            with self.subTest(size=len(bad)), self.assertRaises(ValueError):
                maps.decode_tile(bad)
        with mock.patch.object(maps, "MAX_TILE_BYTES", 1000), self.assertRaises(ValueError):
            maps.decode_tile(gzip.compress(b"\x00" * 100000))

    def test_too_many_features_are_refused(self):
        many = layer("building", [(3, {}, [[(0, 0), (9, 0), (9, 9), (0, 0)]])] * 50)
        with mock.patch.object(maps, "MAX_FEATURES", 5), self.assertRaises(ValueError):
            maps.decode_tile(many)


class Drawing(unittest.TestCase):
    def render(self, theme, name="Aztec Walk"):
        lat, lon = VIEJAS
        tx, ty, fx, fy = maps.tile_of(lat, lon)
        return maps.render({(tx, ty): maps.decode_tile(sample_tile(round(fx), round(fy), name))}, lat, lon, theme)

    def test_the_svg_has_the_map_and_no_active_content(self):
        svg = self.render("light")
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn('viewBox="0 0 1280 720"', svg)
        self.assertIn("Aztec Walk", svg)
        self.assertIn("<textPath", svg)
        for forbidden in ("<script", "http://", "https://", "javascript:", "<image", "<foreignObject", "onload", "<style"):
            self.assertNotIn(forbidden, svg.replace('xmlns="http://www.w3.org/2000/svg"', ""))

    def test_light_and_graphite_share_every_coordinate(self):
        import re
        light, dark = self.render("light"), self.render("dark")
        self.assertNotEqual(light, dark)
        strip = lambda svg: re.sub(r'(fill|stroke)="#[0-9a-f]{6}"', "", svg)
        self.assertEqual(strip(light), strip(dark), "same extent, centre and geometry; only colours differ")
        self.assertIn("#202328", dark)
        self.assertIn("#f1efe9", light)
        self.assertIn("#d9822b", dark, "orange accents for main roads in the graphite scheme")

    def test_names_from_a_tile_are_untrusted_text(self):
        svg = self.render("light", name='Main <script>alert(1)</script> & "St"')
        self.assertNotIn("<script", svg)
        self.assertNotIn("alert(1)</", svg.replace("alert1", ""))
        self.assertEqual(maps.safe_text("A<b>\u0000B[31m & C"), "AbB31m & C")

    def test_the_map_is_centred_on_the_position_at_about_480_metres_across(self):
        lat, lon = VIEJAS
        ox, oy, width, height, tiles = maps.window(lat, lon)
        tx, ty, fx, fy = maps.tile_of(lat, lon)
        self.assertAlmostEqual(ox + width / 2, tx * maps.EXTENT + fx, places=3)
        self.assertAlmostEqual(oy + height / 2, ty * maps.EXTENT + fy, places=3)
        metres = width * 40075016.686 * __import__("math").cos(__import__("math").radians(lat)) / (2 ** maps.ZOOM * maps.EXTENT)
        self.assertAlmostEqual(metres, maps.SPAN_M, delta=1)
        self.assertEqual(tiles, [(tx, ty)])

    def test_a_position_near_a_tile_edge_needs_the_neighbouring_tile(self):
        n = 2 ** maps.ZOOM
        lon = (2863 / n) * 360 - 180 + 0.0001  # a hair inside tile x=2863
        tiles = maps.window(32.77, lon)[4]
        self.assertIn((2862, maps.tile_of(32.77, lon)[1]), tiles)
        self.assertGreater(len(tiles), 1)

    def test_text_is_only_placed_where_it_fits_and_never_twice_on_top_of_itself(self):
        lat, lon = VIEJAS
        tx, ty, fx, fy = maps.tile_of(lat, lon)
        cx, cy = round(fx), round(fy)
        short_road = tile_bytes(layer("transportation", [(2, {"class": "minor"}, [[(cx - 60, cy + 150), (cx + 60, cy + 150)]])]),
                                layer("transportation_name", [(2, {"name": "Aztec Walk"}, [[(cx - 60, cy + 150), (cx + 60, cy + 150)]])]))
        svg = maps.render({(tx, ty): maps.decode_tile(short_road)}, lat, lon, "light")
        self.assertNotIn("<textPath", svg, "a name that does not fit on the street is left off")
        twice = tile_bytes(layer("transportation_name", [(2, {"name": "Aztec Walk"}, [[(cx - 900, cy + 150), (cx + 900, cy + 150)]]),
                                                         (2, {"name": "Aztec Walk"}, [[(cx - 900, cy + 160), (cx + 900, cy + 160)]]),
                                                         (2, {"name": "Other Street"}, [[(cx - 900, cy + 165), (cx + 900, cy + 165)]])]))
        svg = maps.render({(tx, ty): maps.decode_tile(twice)}, lat, lon, "light")
        self.assertEqual(svg.count("<textPath"), 1, "the same name once, and labels that would overlap are dropped")
        self.assertEqual(self.render("light").count("<textPath"), 1)


# --- the trust rules ----------------------------------------------------------------------------------------------

def nominatim(name, lat, lon, house=None, road=None, category="amenity", kind="theatre", city="San Diego", wikidata=None, osm_id=1):
    return {"lat": str(lat), "lon": str(lon), "name": name, "category": category, "type": kind, "osm_type": "way", "osm_id": osm_id,
            "address": {k: v for k, v in {"house_number": house, "road": road, "city": city}.items() if v}, "extratags": {"wikidata": wikidata} if wikidata else {}}


class Names(unittest.TestCase):
    def test_normalisation(self):
        self.assertEqual(media.norm_name("The  Viejas Arena (SDSU)"), "viejas arena")
        self.assertEqual(media.venue_names("Bass Concert Hall, Performing Arts Center (PAC)"), ["bass concert hall performing arts center", "bass concert hall"])
        self.assertEqual(media.venue_names("Caf&eacute; Rio"), ["café rio"])
        self.assertEqual(media.venue_names(""), [])

    def test_street_addresses(self):
        self.assertEqual(media.parse_street("5500 Canyon Crest Dr., San Diego, CA 92182"), ("5500", ["canyon", "crest", "dr"], "san diego"))
        self.assertEqual(media.parse_street("2247 Guadalupe, Austin, Texas 78705")[:2], ("2247", ["guadalupe"]))
        self.assertIsNone(media.parse_street("Austin, TX"))
        self.assertIsNone(media.parse_street("Room 101, Main Hall"))
        self.assertTrue(media.streets_match(["108", "dean", "keeton", "st", "e"][1:], ["e", "dean", "keeton", "st"]))
        self.assertFalse(media.streets_match(["canyon", "crest", "dr"], ["canyon", "view", "dr"]))
        self.assertFalse(media.streets_match([], []))


class Candidates(unittest.TestCase):
    def event(self, **fields):
        return {"title": "x", **fields}

    def test_the_three_trusted_kinds(self):
        source = media.candidate_for(self.event(venue="Hall", place={"classification": "confirmed"}, geo={"latitude": 30.2858, "longitude": -97.731, "source": "Localist API geo"}), SD)
        self.assertEqual((source["kind"], round(source["lat"], 4)), ("source", 30.2858))
        address = media.candidate_for(self.event(venue="Viejas Arena", address="5500 Canyon Crest Dr., San Diego"), SD)
        self.assertEqual(address["kind"], "address")
        venue = media.candidate_for(self.event(venue="Soda Bar"), SD)
        self.assertEqual((venue["kind"], venue["names"]), ("venue", ["soda bar"]))

    def test_a_city_or_a_vague_word_is_not_a_venue(self):
        for text in ("Austin, TX", "San Diego", "San Diego, California", "Online", "Virtual", "TBA", "Off Campus"):
            with self.subTest(text):
                self.assertIsNone(media.candidate_for(self.event(venue=text), SD if "Austin" not in text else {**SD, "display_name": "Austin, Texas, United States"}))
        with_address = media.candidate_for(self.event(venue="Austin, TX", address="500 W 2nd St, Austin, TX"), {**SD, "display_name": "Austin, Texas, United States"})
        self.assertEqual((with_address["kind"], with_address["names"]), ("address", []), "the address can still earn a map, but a city name never earns a photograph")
        self.assertEqual(media.find_venue_photo(with_address, {"lat": 30.27, "lon": -97.74, "wikidata": "Q1"}, lambda url: 1 / 0, lambda url, types: 1 / 0)[0], None)

    def test_estimates_and_nothing_are_not_candidates(self):
        self.assertIsNone(media.candidate_for(self.event(place={"classification": "estimated"}, geo={"latitude": 32.7, "longitude": -117.1}), SD))
        self.assertIsNone(media.candidate_for(self.event(), SD))
        self.assertIsNone(media.candidate_for(self.event(venue="1234"), SD), "a number is not a venue")
        self.assertIsNone(media.candidate_for(self.event(place={"classification": "confirmed"}, geo={"latitude": 999, "longitude": 0}), SD))

    def test_keys_are_stable_and_distinguish_places(self):
        a = media.candidate_for(self.event(venue="Soda Bar"), SD)["key"]
        self.assertEqual(a, media.candidate_for(self.event(venue="Soda  Bar!"), SD)["key"])
        self.assertNotEqual(a, media.candidate_for(self.event(venue="Soda Bar"), {**SD, "latitude": 30.27, "longitude": -97.74})["key"])
        self.assertNotEqual(a, media.candidate_for(self.event(venue="Belly Up"), SD)["key"])


class Acceptance(unittest.TestCase):
    def address(self, text="5500 Canyon Crest Dr., San Diego"):
        return media.candidate_for({"venue": "Viejas Arena", "address": text}, SD)

    def venue(self, name="Soda Bar"):
        return media.candidate_for({"venue": name}, SD)

    def test_a_street_address_needs_the_house_number_the_street_and_the_locality(self):
        good = nominatim("Viejas Arena", *VIEJAS, house="5500", road="Canyon Crest Drive", category="leisure", kind="sports_centre")
        location, why = media.accept_geocode(self.address(), [good])
        self.assertEqual((location["confidence"], location["precision"], location["approximate"]), ("address", "address", True))
        self.assertIn("house number 5500", location["provenance"])
        for label, result in (("number", nominatim("x", *VIEJAS, house="5501", road="Canyon Crest Drive")),
                              ("street", nominatim("x", *VIEJAS, house="5500", road="Canyon View Drive")),
                              ("no number", nominatim("x", *VIEJAS, road="Canyon Crest Drive")),
                              ("locality", nominatim("x", *VIEJAS, house="5500", road="Canyon Crest Drive", city="Chula Vista")),
                              ("outside the area", nominatim("x", 40.0, -100.0, house="5500", road="Canyon Crest Drive"))):
            with self.subTest(label):
                self.assertIsNone(media.accept_geocode(self.address(), [result])[0])

    def test_an_address_in_a_neighbourhood_of_the_right_city_is_accepted(self):
        result = nominatim("x", *VIEJAS, house="5500", road="Canyon Crest Drive", city="San Diego")
        result["address"]["suburb"] = "Montezuma Mesa"
        self.assertIsNotNone(media.accept_geocode(media.candidate_for({"venue": "V", "address": "5500 Canyon Crest Drive, Montezuma Mesa"}, SD), [result])[0])

    def test_a_named_place_needs_its_exact_name_and_a_place_like_type(self):
        good = nominatim("Soda Bar", 32.7549, -117.1155)
        location, _ = media.accept_geocode(self.venue(), [good])
        self.assertEqual((location["confidence"], location["precision"]), ("venue", "poi"))
        rejected = {"another name": nominatim("Soda Bar & Grill", 32.7549, -117.1155), "a credit union": nominatim("Greater Soda Bar Credit Union", 32.7549, -117.1155),
                    "a city": nominatim("Soda Bar", 32.7549, -117.1155, category="boundary", kind="administrative"),
                    "a road": nominatim("Soda Bar", 32.7549, -117.1155, category="highway", kind="residential"),
                    "outside the area": nominatim("Soda Bar", 45.0, -100.0)}
        for label, result in rejected.items():
            with self.subTest(label):
                self.assertIsNone(media.accept_geocode(self.venue(), [result])[0])
        self.assertIsNone(media.accept_geocode(self.venue("Texas Union"), [nominatim("Greater Texas Federal Credit Union", 32.7, -117.1)])[0])

    def test_a_name_found_in_two_places_is_ambiguous(self):
        a, b = nominatim("Soda Bar", 32.7549, -117.1155), nominatim("Soda Bar", 32.9, -117.2, osm_id=2)
        location, why = media.accept_geocode(self.venue(), [a, b])
        self.assertIsNone(location)
        self.assertIn("more than one location", why)
        near = nominatim("Soda Bar", 32.7550, -117.1156, osm_id=3)  # the same place mapped twice a few metres apart is fine
        self.assertIsNotNone(media.accept_geocode(self.venue(), [a, near])[0])

    def test_nothing_found_is_a_refusal_with_a_reason(self):
        self.assertEqual(media.accept_geocode(self.venue(), []), (None, "the geocoder found nothing"))


# --- venue photographs -----------------------------------------------------------------------------------------------

def commons(license="CC BY-SA 3.0", artist="<a href='x'>A. Photographer</a>", mime="image/jpeg", width=2000, thumb="https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/F.jpg/640px-F.jpg",
            page="https://commons.wikimedia.org/wiki/File:F.jpg", restrictions=""):
    meta = {"LicenseShortName": {"value": license}, "Artist": {"value": artist}, "Restrictions": {"value": restrictions}, "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/3.0/"}}
    return {"query": {"pages": {"1": {"imageinfo": [{"mime": mime, "width": width, "thumburl": thumb, "descriptionurl": page, "extmetadata": meta}]}}}}


class Wikimedia:
    """Canned API answers; `calls` records what was asked."""

    def __init__(self, qid="Q1", label="Viejas Arena", aliases=("Cox Arena",), coordinate=(32.77381, -117.07454), image="Arena front.jpg", info=None, geosearch=None, bytes_=b"\xff\xd8\xff" + b"x" * 5000):
        self.calls, self.bytes_ = [], bytes_
        claims = {}
        if coordinate:
            claims["P625"] = [{"mainsnak": {"datavalue": {"value": {"latitude": coordinate[0], "longitude": coordinate[1]}}}}]
        if image:
            claims["P18"] = [{"mainsnak": {"datavalue": {"value": image}}}]
        self.entity = {"entities": {qid: {"labels": {"en": {"value": label}}, "aliases": {"en": [{"value": a} for a in aliases]}, "claims": claims}}}
        self.info = info or commons()
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

    def fetch_bytes(self, url, types):
        self.calls.append(url)
        self.last_types = types
        return self.bytes_


class VenuePhotos(unittest.TestCase):
    candidate = {"venue": "Viejas Arena", "names": ["viejas arena"]}
    location = {"lat": VIEJAS[0], "lon": VIEJAS[1], "wikidata": None}

    def find(self, wiki=None, location=None, candidate=None, **kwargs):
        wiki = wiki or Wikimedia(**kwargs)
        return media.find_venue_photo(candidate or self.candidate, location or self.location, wiki.json, wiki.fetch_bytes), wiki

    def test_an_exact_article_a_matching_wikidata_item_and_a_free_photo_are_verified(self):
        (record, reason), wiki = self.find()
        self.assertIsNone(reason)
        self.assertEqual((record["qid"], record["license"], record["creator"]), ("Q1", "CC BY-SA 3.0", "A. Photographer"))
        self.assertEqual(record["source_url"], "https://commons.wikimedia.org/wiki/File:F.jpg")
        self.assertTrue(record["attribution_required"])
        self.assertTrue(record["file"].startswith("p") and record["file"].endswith(".jpg"))
        self.assertEqual(wiki.last_types, ("image/jpeg",))
        evidence = " ".join(record["evidence"])
        for needle in ("Wikipedia article 'Viejas Arena' has exactly the venue's name", "label or an alias equals", "coordinates agree within"):
            self.assertIn(needle, evidence)

    def test_an_openstreetmap_wikidata_link_replaces_the_article_search_but_not_the_other_checks(self):
        (record, _), wiki = self.find(location={**self.location, "wikidata": "Q1"})
        self.assertIn("OpenStreetMap link wikidata=Q1", " ".join(record["evidence"]))
        self.assertFalse([c for c in wiki.calls if "list=geosearch" in c])
        (record, why), _ = self.find(location={**self.location, "wikidata": "Q1"}, label="Another Venue", aliases=())
        self.assertIsNone(record)
        self.assertIn("label and aliases do not equal", why)

    def test_an_alias_can_carry_the_identity(self):
        (record, _), _ = self.find(label="Cox Arena", aliases=("Viejas Arena",))
        self.assertIsNotNone(record)

    def test_ambiguity_and_inexact_names_are_rejected(self):
        for label, geosearch in (("two articles with the name", [{"title": "Viejas Arena", "pageid": 7, "dist": 8}, {"title": "Viejas Arena", "pageid": 8, "dist": 30}]),
                                 ("a similar name", [{"title": "Viejas Arena Parking", "pageid": 7, "dist": 8}]),
                                 ("none nearby", [])):
            with self.subTest(label):
                (record, why), _ = self.find(geosearch=geosearch)
                self.assertIsNone(record)
                self.assertIn("exact name", why)

    def test_each_link_of_the_chain_is_required(self):
        cases = {"far coordinates": dict(coordinate=(33.5, -117.5)), "no coordinates": dict(coordinate=None), "no image": dict(image=None),
                 "a logo file": dict(image="Viejas logo.jpg"), "not a jpeg name": dict(image="Arena front.png"),
                 "no Wikidata label match": dict(label="Aztec Bowl", aliases=())}
        for label, kwargs in cases.items():
            with self.subTest(label):
                self.assertIsNone(self.find(**kwargs)[0][0])

    def test_licences_and_files(self):
        for good in ("CC BY-SA 4.0", "CC BY 2.0", "CC0", "Public domain", "PD-USGov-NASA"):
            with self.subTest(good):
                self.assertIsNotNone(self.find(info=commons(license=good))[0][0])
        for bad in ("CC BY-NC 4.0", "CC BY-ND 2.0", "GFDL", "Fair use", "", "All rights reserved"):
            with self.subTest(bad):
                self.assertIsNone(self.find(info=commons(license=bad))[0][0])
        self.assertIsNone(self.find(info=commons(mime="image/png"))[0][0])
        self.assertIsNone(self.find(info=commons(width=400))[0][0], "too small to be a good photograph")
        self.assertIsNone(self.find(info=commons(restrictions="trademarked"))[0][0])
        self.assertIsNone(self.find(info=commons(thumb="https://evil.example/x.jpg"))[0][0])
        self.assertIsNone(self.find(info=commons(page="https://evil.example/File:F.jpg"))[0][0])

    def test_creator_is_cleaned_and_may_be_missing(self):
        (record, _), _ = self.find(info=commons(artist="<b>Jane</b>  <i>Doe</i> &amp; Co"))
        self.assertEqual(record["creator"], "Jane Doe & Co")
        (record, _), _ = self.find(info=commons(artist="https://www.flickr.com/photos/michael-seljos/"))
        self.assertEqual(record["creator"], "michael-seljos (flickr.com)")
        (record, _), _ = self.find(info=commons(artist=""))
        self.assertEqual(record["creator"], "")
        self.assertTrue(record["attribution_required"], "the credit then names Wikimedia Commons and links the file page")
        (record, _), _ = self.find(info=commons(license="CC0", artist=""))
        self.assertFalse(record["attribution_required"])

    def test_downloaded_bytes_must_be_a_sensible_jpeg(self):
        for bad in (b"GIF89a" + b"x" * 5000, b"\xff\xd8\xff", b"\xff\xd8\xff" + b"x" * 800_000, b"<svg onload=alert(1)>" + b"x" * 5000):
            with self.subTest(len(bad)):
                self.assertIsNone(self.find(bytes_=bad)[0][0])


# --- the service, its cache and what it serves ------------------------------------------------------------------------

class FakeFetcher:
    """Answers Nominatim, the OpenFreeMap tile list and tiles, and Wikimedia from fixtures; every request is recorded."""

    def __init__(self, nominatim_results=None, fail=None, wiki=None):
        self.urls, self.fail, self.wiki = [], fail, wiki
        self.nominatim_results = nominatim_results if nominatim_results is not None else {}

    def fetch(self, url, check_robots=True, binary=None):
        self.urls.append(url)
        if self.fail and self.fail(url):
            raise FetchError("unavailable")
        if "nominatim" in url:
            for needle, result in self.nominatim_results.items():
                if needle.replace(" ", "+") in url or needle in url:
                    return types.SimpleNamespace(text=json.dumps(result), body=b"")
            return types.SimpleNamespace(text="[]", body=b"")
        if url == media.TILEJSON:
            return types.SimpleNamespace(text=json.dumps({"tiles": ["https://tiles.openfreemap.org/planet/20261018_080001_pt/{z}/{x}/{y}.pbf"]}), body=b"")
        if url.startswith("https://tiles.openfreemap.org/planet/"):
            x, y = (int(v) for v in url[:-4].split("/")[-2:])
            ex, ey = maps.tile_of(*VIEJAS)[:2]
            return types.SimpleNamespace(text="", body=sample_tile(2000, 2000) if (x, y) == (ex, ey) else sample_tile(100, 100))
        if self.wiki and ("wikipedia.org" in url or "wikidata.org" in url or "commons.wikimedia" in url):
            return types.SimpleNamespace(text=json.dumps(self.wiki.json(url)), body=b"")
        if self.wiki and "wikimedia.org" in url:
            return types.SimpleNamespace(text="", body=self.wiki.fetch_bytes(url, binary))
        raise AssertionError(url)


class Service(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = [time.time()]  # tile files carry real modification times
        self.viejas = {"venue": "Viejas Arena", "address": "5500 Canyon Crest Dr., San Diego", "title": "Weezer"}
        self.result = {"Viejas Arena": [nominatim("Viejas Arena", *VIEJAS, house="5500", road="Canyon Crest Drive", category="leisure", kind="sports_centre", wikidata="Q1")]}

    def service(self, fetcher):
        return media.MediaService(self.tmp.name, fetcher, clock=lambda: self.clock[0], sleep=lambda s: None, nominatim_interval=0)

    def test_a_cycle_resolves_a_place_into_a_map_and_a_venue_photo_and_records_the_evidence(self):
        fetcher = FakeFetcher(self.result, wiki=Wikimedia())
        service = self.service(fetcher)
        self.assertEqual(service.cycle([(SD, [self.viejas])]), 1)
        record = next(iter(service.store.index["records"].values()))
        self.assertTrue(record["ok"])
        self.assertEqual((record["location"]["confidence"], record["location"]["precision"], record["location"]["approximate"]), ("address", "address", True))
        self.assertIn("house number 5500", record["location"]["provenance"])
        self.assertEqual(record["photo"]["license"], "CC BY-SA 3.0")
        self.assertIn("Wikidata", " ".join(record["photo"]["evidence"]))
        self.assertEqual(record["map"]["credit"], "OpenFreeMap © OpenMapTiles Data from OpenStreetMap")
        for name in (*record["map"]["files"].values(), record["photo"]["file"]):
            self.assertTrue(service.store.read_file(name), name)
        svg = service.store.read_file(record["map"]["files"]["dark"]).decode()
        self.assertTrue(svg.startswith("<svg"))

    def test_results_are_cached_and_nothing_is_fetched_twice(self):
        fetcher = FakeFetcher(self.result, wiki=Wikimedia())
        service = self.service(fetcher)
        service.cycle([(SD, [self.viejas])])
        requests = len(fetcher.urls)
        self.assertEqual(service.cycle([(SD, [self.viejas, {**self.viejas, "title": "Another show"}])]), 0)
        self.assertEqual(len(fetcher.urls), requests, "the same venue costs no further requests")
        again = self.service(fetcher)  # a restart reads the cache from disk
        self.assertEqual(again.cycle([(SD, [self.viejas])]), 0)
        self.assertEqual(len(fetcher.urls), requests)

    def test_the_geocoder_is_limited_to_the_area_s_country_when_it_is_known(self):
        fetcher = FakeFetcher({})
        service = self.service(fetcher)
        service.cycle([({**SD, "country": "US"}, [{"venue": "Soda Bar"}]), ({**SD, "latitude": 51.5, "longitude": -0.12, "country": ""}, [{"venue": "The Pub"}])])
        urls = [u for u in fetcher.urls if "nominatim" in u]
        self.assertTrue(any("countrycodes=us" in u for u in urls))
        self.assertTrue(any("countrycodes" not in u for u in urls))

    def test_lookups_to_the_public_geocoder_are_spaced_to_its_policy(self):
        sleeps = []
        service = media.MediaService(self.tmp.name, FakeFetcher({}), clock=lambda: self.clock[0], sleep=lambda s: (sleeps.append(s), self.clock.__setitem__(0, self.clock[0] + s))[0])
        service.cycle([(SD, [{"venue": "One Club"}, {"venue": "Two Club"}, {"venue": "Three Club"}])])
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(abs(s - media.NOMINATIM_INTERVAL) < 0.01 for s in sleeps), "15 s between lookups: 4 a minute")

    def test_places_sharing_a_tile_share_one_tile_request_and_one_list_request(self):
        other = {"venue": "Soda Bar", "address": "3615 El Cajon Blvd, San Diego"}
        results = {**self.result, "Soda Bar": [nominatim("Soda Bar", VIEJAS[0] + 0.001, VIEJAS[1] + 0.001, house="3615", road="El Cajon Boulevard")]}
        fetcher = FakeFetcher(results, wiki=Wikimedia(geosearch=[]))
        self.service(fetcher).cycle([(SD, [self.viejas, other])])
        unique = set(maps.tiles_needed(*VIEJAS)) | set(maps.tiles_needed(VIEJAS[0] + 0.001, VIEJAS[1] + 0.001))
        self.assertEqual(len([u for u in fetcher.urls if u.startswith("https://tiles.openfreemap.org/planet/2")]), len(unique), "each tile once, however many places use it")
        self.assertEqual(fetcher.urls.count(media.TILEJSON), 1)

    def test_work_per_cycle_is_bounded_and_the_busiest_places_come_first(self):
        events = [{"venue": f"Venue {i}"} for i in range(20)]
        events += [{"venue": "Popular Hall"}] * 5
        service = self.service(FakeFetcher({}))
        self.assertEqual(service.cycle([(SD, events)], limit=3), 3)
        names = [r["venue"] for r in service.store.index["records"].values()]
        self.assertIn("Popular Hall", names)
        self.assertEqual(len(names), 3)
        self.assertEqual(service.pending([(SD, events)]), 18)

    def test_refusals_are_cached_with_their_reason_and_never_become_a_map(self):
        service = self.service(FakeFetcher({}))
        event = {"venue": "Mystery Club"}
        service.cycle([(SD, [event])])
        record = next(iter(service.store.index["records"].values()))
        self.assertFalse(record["ok"])
        self.assertTrue(record["reason"])
        self.assertEqual(service.annotate([event], SD), [event], "no imagery for a refused place")
        before = len(service.fetcher.urls)
        service.cycle([(SD, [event])])
        self.assertEqual(len(service.fetcher.urls), before, "a refusal is not retried every cycle")
        self.clock[0] += media.REFUSAL_TTL + 1
        service.store.prune()
        self.assertEqual(service.store.index["records"], {}, "but it expires")

    def test_unavailable_services_cost_nothing_and_are_retried_later(self):
        fetcher = FakeFetcher(self.result, fail=lambda url: "nominatim" in url, wiki=Wikimedia())
        service = self.service(fetcher)
        self.assertEqual(service.cycle([(SD, [self.viejas])]), 0)
        self.assertEqual((service.store.index["records"], service.stats["failed"]), ({}, 1))
        fetcher.fail = None
        self.assertEqual(self.service(fetcher).cycle([(SD, [self.viejas])]), 1)

    def test_when_wikimedia_is_down_the_map_still_works_and_the_photo_is_tried_again_only_by_ttl(self):
        fetcher = FakeFetcher(self.result, fail=lambda url: "wiki" in url)
        service = self.service(fetcher)
        service.cycle([(SD, [self.viejas])])
        record = next(iter(service.store.index["records"].values()))
        self.assertTrue(record["ok"])
        self.assertNotIn("photo", record)
        self.assertIn("lookup failed", record["photo_reason"])

    def test_when_the_tile_service_is_down_nothing_is_cached_and_nothing_breaks(self):
        fetcher = FakeFetcher(self.result, wiki=Wikimedia(), fail=lambda url: "openfreemap" in url)
        service = self.service(fetcher)
        self.assertEqual(service.cycle([(SD, [self.viejas])]), 0)
        self.assertEqual(service.store.index["records"], {})

    def test_source_coordinates_need_no_geocoding(self):
        event = {"venue": "Bass Concert Hall", "place": {"classification": "confirmed"}, "geo": {"latitude": VIEJAS[0], "longitude": VIEJAS[1], "source": "Localist API geo"}}
        fetcher = FakeFetcher({}, wiki=Wikimedia(geosearch=[]))
        service = self.service(fetcher)
        service.cycle([(SD, [event])])
        self.assertFalse([u for u in fetcher.urls if "nominatim" in u])
        record = next(iter(service.store.index["records"].values()))
        self.assertEqual((record["location"]["confidence"], record["location"]["approximate"]), ("source", False))

    def test_annotation_adds_serving_urls_and_leaves_the_feed_alone(self):
        service = self.service(FakeFetcher(self.result, wiki=Wikimedia()))
        service.cycle([(SD, [self.viejas])])
        original = dict(self.viejas)
        shown = service.annotate([self.viejas, {"venue": "Elsewhere"}], SD)
        self.assertEqual(self.viejas, original, "the stored event is not modified")
        self.assertNotIn("media", shown[1])
        item = shown[0]["media"]
        self.assertTrue(item["map"]["light"].startswith("/area_events/media/m") and item["map"]["light"].endswith("-light.svg"))
        self.assertTrue(item["map"]["dark"].endswith("-dark.svg"))
        self.assertEqual((item["map"]["confidence"], item["map"]["precision"], item["map"]["approximate"]), ("address", "address", True))
        self.assertEqual((item["photo"]["license"], item["photo"]["venue"]), ("CC BY-SA 3.0", "Viejas Arena"))
        self.assertNotIn("evidence", item["photo"])
        self.assertNotIn("provenance", json.dumps(item), "internal provenance is stored, not sent to the card")

    def test_the_revision_changes_with_the_imagery_and_is_zero_without_any(self):
        service = self.service(FakeFetcher(self.result, wiki=Wikimedia()))
        self.assertEqual(service.revision, 0)
        service.cycle([(SD, [self.viejas])])
        first = service.revision
        self.assertGreater(first, 0)
        self.clock[0] += 100
        service.cycle([(SD, [{"venue": "Nowhere Club"}])])
        self.assertGreater(service.revision, first)

    def test_storage_is_bounded_and_unreferenced_files_are_removed(self):
        service = self.service(FakeFetcher(self.result, wiki=Wikimedia()))
        service.cycle([(SD, [self.viejas])])
        service.store.put_file("m0000000000000000-light.svg", b"<svg/>")
        service.store.prune()
        self.assertIsNone(service.store.read_file("m0000000000000000-light.svg"))
        with mock.patch.object(media, "MAX_RECORDS", 1):
            service.store.index["records"]["zzz"] = {"at": 1.0, "ok": False}
            service.store.prune()
        self.assertEqual(len(service.store.index["records"]), 1)

    def test_only_safe_file_names_are_stored_or_read(self):
        store = self.service(FakeFetcher({})).store
        for bad in ("../x.svg", "a/b.jpg", "x.png", "..\\x.jpg", "", None, "m" * 80 + ".svg", "UPPER.svg", ".hidden.svg"):
            with self.subTest(bad):
                self.assertIsNone(store.read_file(bad))
                with self.assertRaises((ValueError, TypeError)):
                    store.put_file(bad, b"x")

    def test_tiles_are_cached_on_disk_and_bounded(self):
        fetcher = FakeFetcher(self.result, wiki=Wikimedia())
        service = self.service(fetcher)
        service.cycle([(SD, [self.viejas])])
        files = os.listdir(service.tiles_dir)
        self.assertEqual(len(files), 1)
        with mock.patch.object(media, "MAX_TILE_FILES", 2):
            for i in range(5):
                open(os.path.join(service.tiles_dir, f"14-{i}-0.pbf"), "wb").close()
                os.utime(os.path.join(service.tiles_dir, f"14-{i}-0.pbf"), (i, i))
            service._prune_tiles()
        self.assertEqual(len(os.listdir(service.tiles_dir)), 2)

    def test_an_unexpected_tile_address_is_not_followed(self):
        class Evil(FakeFetcher):
            def fetch(self, url, check_robots=True, binary=None):
                if url == media.TILEJSON:
                    return types.SimpleNamespace(text=json.dumps({"tiles": ["https://evil.example/{z}/{x}/{y}.pbf"]}), body=b"")
                return super().fetch(url, check_robots, binary)
        service = self.service(Evil(self.result, wiki=Wikimedia()))
        self.assertEqual(service.cycle([(SD, [self.viejas])]), 0)
        self.assertFalse([u for u in service.fetcher.urls if "evil" in u])


class Binary(unittest.TestCase):
    """The safe fetcher's binary mode used for tiles and photographs."""

    def fetcher(self, content_type, body=b"\xff\xd8\xff data"):
        def send(scheme, host, port, ip, target, headers, timeout, limit):
            if target == "/robots.txt":
                return 404, {}, b""
            return 200, {"Content-Type": content_type}, body
        return SafeFetcher(0, resolver=lambda host, port, type=None: [(2, 1, 6, "", ("93.184.216.34", port))], send=send, clock=lambda: 0.0, sleep=lambda s: None, state=State())

    def test_only_the_expected_types_are_accepted(self):
        self.assertEqual(self.fetcher("image/jpeg").fetch("https://upload.wikimedia.org/a.jpg", check_robots=False, binary=("image/jpeg",)).body[:3], b"\xff\xd8\xff")
        with self.assertRaisesRegex(FetchError, "unexpected content type"):
            self.fetcher("text/html").fetch("https://upload.wikimedia.org/a.jpg", check_robots=False, binary=("image/jpeg",))
        with self.assertRaisesRegex(FetchError, "not a text document"):
            self.fetcher("image/jpeg").fetch("https://upload.wikimedia.org/a.jpg", check_robots=False)


# --- serving -----------------------------------------------------------------------------------------------------------

class Serving(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = engine_module.Engine(self.tmp.name, clock=lambda: 1_800_000_000.0)
        self.engine.media.store.put_file("mabcdef0123456789-dark.svg", b"<svg/>")
        self.engine.media.store.put_file("pabcdef0123456789.jpg", b"\xff\xd8\xff")
        self.api = Api(self.engine, "t" * 40)

    def get(self, path, token=True):
        headers = {"Authorization": "Bearer " + "t" * 40} if token else {}
        return self.api.handle("GET", path, headers, b"", "127.0.0.1")

    def test_stored_files_are_served_with_their_type_and_only_with_the_token(self):
        status, payload = self.get("/api/v1/media/mabcdef0123456789-dark.svg")
        self.assertEqual((status, payload.content_type, payload.data), (200, "image/svg+xml", b"<svg/>"))
        self.assertEqual(self.get("/api/v1/media/pabcdef0123456789.jpg")[1].content_type, "image/jpeg")
        self.assertEqual(self.get("/api/v1/media/mabcdef0123456789-dark.svg", token=False)[0], 401)

    def test_other_names_are_not_found(self):
        for name in ("missing0123456789.jpg", "../token", "..%2Ftoken", "x.png", "pabcdef0123456789.jpg/..", ""):
            with self.subTest(name):
                self.assertEqual(self.get("/api/v1/media/" + name)[0], 404)
        self.assertNotIsInstance(self.get("/api/v1/status")[1], Raw)

    def test_snapshots_carry_media_and_the_revision_keeps_a_card_up_to_date(self):
        feed = {"generated_at": "2030-01-01T00:00:00+00:00", "location": SD, "sources": {}, "events": [{"id": "1", "title": "Weezer", "start": "2030-01-02T00:00:00+00:00", "url": "https://x.example",
                                                                                                          "venue": "Viejas Arena", "address": "5500 Canyon Crest Dr., San Diego"}]}
        record = {"id": "p1", "profile": {}, "state": "ready", "queued": False, "report": [], "progress": None, "requested_at": 1.0, "error": None}
        self.engine._profiles["p1"] = record
        self.engine._feed = lambda pid: feed
        plain = self.engine.snapshot("p1")
        self.assertEqual((plain["revision"], "media" in plain["events"][0]), (0, False))
        self.assertEqual(self.engine.snapshot("p1", since=feed["generated_at"]).get("unchanged"), True, "no imagery: the old token still works")
        key = media.make_key("a", "5500", "canyon crest dr", "san diego", media._area_key(SD))
        self.engine.media.store.index["records"][key] = {"ok": True, "at": 1.0, "venue": "Viejas Arena", "location": {"lat": 1, "lon": 2, "precision": "address", "confidence": "address", "approximate": True, "provenance": "x"},
                                                         "map": {"files": {"light": "mabcdef0123456789-light.svg", "dark": "mabcdef0123456789-dark.svg"}, "credit": media.MAP_CREDIT}}
        self.engine.media.store.index["saved_at"] = 1_800_000_100
        shown = self.engine.snapshot("p1", since=feed["generated_at"])
        self.assertNotIn("unchanged", shown, "new imagery is sent even though the feed itself did not change")
        self.assertIn("media", shown["events"][0])
        self.assertEqual(shown["revision"], 1_800_000_100)
        self.assertTrue(self.engine.snapshot("p1", since=engine_module.snapshot_key(feed["generated_at"], shown["revision"]))["unchanged"])

    def test_a_failure_in_imagery_never_costs_the_events(self):
        feed = {"generated_at": "g", "location": SD, "sources": {}, "events": [{"id": "1", "title": "x", "start": "s", "url": "u"}]}
        self.engine._profiles["p1"] = {"id": "p1", "profile": {}, "state": "ready", "queued": False, "report": [], "progress": None, "requested_at": 1.0, "error": None}
        self.engine._feed = lambda pid: feed
        with mock.patch.object(self.engine.media, "annotate", side_effect=RuntimeError("boom")):
            self.assertEqual(self.engine.snapshot("p1")["events"], feed["events"])


class Switch(unittest.TestCase):
    """The app option location_imagery turns every lookup off."""

    def test_settings(self):
        from discovery import server
        self.assertTrue(server.load_settings({}, "missing")["location_imagery"])
        for value in ("false", "0", "No", "off"):
            self.assertFalse(server.load_settings({"AREA_EVENTS_LOCATION_IMAGERY": value}, "missing")["location_imagery"], value)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "options.json")
            json.dump({"location_imagery": False}, open(path, "w"))
            self.assertFalse(server.load_settings({}, path)["location_imagery"])

    def test_off_means_no_imagery_attached_and_no_background_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = types.SimpleNamespace(annotate=lambda events, area: [{**e, "media": {}} for e in events], revision=99, cycle=lambda f: 1 / 0, pending=lambda f: 0)
            engine = engine_module.Engine(tmp, media=fake, media_enabled=False, clock=lambda: 1_800_000_000.0)
            feed = {"generated_at": "g", "location": SD, "sources": {}, "events": [{"id": "1", "title": "x", "start": "s", "url": "u"}]}
            engine._profiles["p"] = {"id": "p", "profile": {}, "state": "ready", "queued": False, "report": [], "progress": None, "requested_at": 1.0, "error": None}
            engine._feed = lambda pid: feed
            shown = engine.snapshot("p")
            self.assertEqual((shown["revision"], shown["events"]), (0, feed["events"]))
            started = []
            with mock.patch.object(threading, "Thread", side_effect=lambda *a, **k: started.append(k.get("name")) or mock.Mock()):
                stop = threading.Event()
                stop.set()
                engine.run_forever(stop)
            self.assertNotIn("media", started)


class Background(unittest.TestCase):
    """Imagery work happens beside discovery, only when the engine is idle."""

    def test_the_loop_waits_while_discovery_runs_and_works_when_idle(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        calls = []
        fake = types.SimpleNamespace(cycle=lambda feeds: calls.append("cycle") or 0, pending=lambda feeds: 0, revision=0, annotate=lambda events, area: events)
        engine = engine_module.Engine(tmp.name, media=fake, clock=lambda: 1_800_000_000.0)
        stop = threading.Event()
        waits = []
        stop.wait = lambda timeout=None: (waits.append(timeout), len(waits) >= 3 and stop.set())[1]
        engine._running = "busy"
        engine._media_loop(stop)
        self.assertEqual(calls, [], "no imagery work while a profile is being discovered")
        self.assertEqual(waits[:2], [30, 15])
        stop.clear()
        waits.clear()
        engine._running = None
        engine._media_loop(stop)
        self.assertEqual(calls, ["cycle", "cycle"][:len(calls)])
        self.assertGreaterEqual(len(calls), 1)

    def test_a_crashing_cycle_does_not_stop_the_loop(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        def boom(feeds):
            raise RuntimeError("boom")
        engine = engine_module.Engine(tmp.name, media=types.SimpleNamespace(cycle=boom, pending=lambda f: 0, revision=0, annotate=lambda e, a: e), clock=lambda: 1_800_000_000.0)
        stop = threading.Event()
        count = []
        stop.wait = lambda timeout=None: (count.append(1), len(count) >= 3 and stop.set())[1]
        engine._media_loop(stop)
        self.assertGreaterEqual(len(count), 3)


# --- the Home Assistant side -------------------------------------------------------------------------------------------

from tests import test_ha_glue as glue  # noqa: E402

media_view = glue.importlib.import_module("area_events_glue.media_view")
client_module = glue.client


class FakeResponse:
    def __init__(self, status, body=b"", kind=""):
        self.status, self._body, self.headers = status, body, {"Content-Type": kind}

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.urls = response, error, []

    def get(self, url, headers=None):
        self.urls.append((url, headers))
        if self.error:
            raise self.error
        return self.response


class HaServing(unittest.TestCase):
    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def client(self, session):
        return client_module.AreaEventsClient(session, "http://app:8765", "secret-token")

    def test_the_client_fetches_with_the_token_and_maps_failures(self):
        session = FakeSession(FakeResponse(200, b"<svg/>", "image/svg+xml"))
        data, kind = self.run_async(self.client(session).get_media("mabcdef0123456789-dark.svg"))
        self.assertEqual((data, kind), (b"<svg/>", "image/svg+xml"))
        self.assertEqual(session.urls[0], ("http://app:8765/api/v1/media/mabcdef0123456789-dark.svg", {"Authorization": "Bearer secret-token"}))
        for status, error in ((404, client_module.AreaEventsRequestError), (401, client_module.AreaEventsAuthError), (500, client_module.AreaEventsConnectionError)):
            with self.subTest(status), self.assertRaises(error):
                self.run_async(self.client(FakeSession(FakeResponse(status))).get_media("mabcdef0123456789-dark.svg"))
        with self.assertRaises(client_module.AreaEventsConnectionError):
            self.run_async(self.client(FakeSession(error=OSError("refused"))).get_media("mabcdef0123456789-dark.svg"))

    def view(self, session=None):
        entry = types.SimpleNamespace(state="loaded", runtime_data=types.SimpleNamespace(hub=types.SimpleNamespace(client=self.client(session or FakeSession(FakeResponse(200, b"<svg/>", "image/svg+xml"))))))
        hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_entries=lambda domain: [entry]))
        return media_view.AreaEventsMediaView(hass)

    def test_the_view_needs_no_login_serves_only_exact_names_and_forbids_active_content(self):
        view = self.view()
        self.assertFalse(view.requires_auth)
        self.assertEqual(view.url, "/area_events/media/{name}")
        ok = self.run_async(view.get(None, "mabcdef0123456789-dark.svg"))
        self.assertEqual((ok.body, ok.content_type), (b"<svg/>", "image/svg+xml"))
        self.assertIn("immutable", ok.headers["Cache-Control"])
        self.assertIn("default-src 'none'", ok.headers["Content-Security-Policy"])
        self.assertEqual(ok.headers["X-Content-Type-Options"], "nosniff")
        for name in ("../secret.svg", "a.svg/../b", "x.png", "UPPER.svg", "a.svg%00", "m" * 70 + ".svg", ""):
            with self.subTest(name):
                self.assertEqual(self.run_async(view.get(None, name)).status, 404)

    def test_the_view_reports_a_missing_image_and_an_unreachable_app(self):
        self.assertEqual(self.run_async(self.view(FakeSession(FakeResponse(404))).get(None, "mabcdef0123456789-dark.svg")).status, 404)
        self.assertEqual(self.run_async(self.view(FakeSession(error=OSError("down"))).get(None, "mabcdef0123456789-dark.svg")).status, 502)

    def test_the_view_is_registered_once_in_setup(self):
        views = []
        hass = types.SimpleNamespace(http=types.SimpleNamespace(async_register_static_paths=lambda configs: asyncio.sleep(0), register_view=views.append), data={},
                                     async_add_executor_job=lambda func, *args: asyncio.sleep(0, func(*args)))
        self.assertTrue(asyncio.run(glue.init.async_setup(hass, {})))
        self.assertEqual([type(v).__name__ for v in views], ["AreaEventsMediaView"])


if __name__ == "__main__":
    unittest.main()
