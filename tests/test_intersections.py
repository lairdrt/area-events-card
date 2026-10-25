"""Locations given as the corner of two streets ("Balboa Drive and Eighth Drive"): reading the text, finding where the streets meet in
OpenStreetMap's road network, and refusing everything that cannot be verified. Runs on fixtures; nothing contacts any service."""
import tempfile
import time
import unittest

from area_events.enrichment import media
from tests.media_fixtures import SD, FakeFetcher


def way(way_id, name, node_ids, highway="residential"):
    return {"type": "way", "id": way_id, "nodes": node_ids, "tags": {"highway": highway, "name": name}}


def node(node_id, lat, lon):
    return {"type": "node", "id": node_id, "lat": lat, "lon": lon}


CORNER = (32.7314, -117.1595)  # Laurel Street and 6th Avenue, San Diego


def crossing_elements(name_a="Laurel Street", name_b="6th Avenue", shared=True):
    """Two streets that share a node (a junction) or only pass near each other."""
    nodes = [node(1, CORNER[0], CORNER[1] - 0.003), node(2, CORNER[0], CORNER[1]), node(3, CORNER[0], CORNER[1] + 0.003),
             node(4, CORNER[0] - 0.003, CORNER[1] + 0.0001), node(5, CORNER[0] + 0.003, CORNER[1] + 0.0001), node(6, CORNER[0] + 0.0006, CORNER[1])]
    if shared:
        return [way(10, name_a, [1, 2, 3]), way(11, name_b, [4, 2, 5]), *nodes]
    return [way(10, name_a, [1, 6, 3]), way(11, name_b, [4, 2, 5]), *nodes]


def street(name="Laurel Street", city="San Diego", lat=CORNER[0], lon=CORNER[1], category="highway"):
    return {"lat": str(lat), "lon": str(lon), "name": name, "category": category, "type": "residential", "osm_type": "way", "osm_id": 10,
            "address": {"road": name, "city": city}}


class Parsing(unittest.TestCase):
    def test_the_common_intersection_formats(self):
        for text in ("Laurel Street and 6th Avenue", "Laurel St & 6th Ave", "Laurel Street at Sixth Avenue", "Corner of Laurel Street and 6th Avenue",
                     "On the corner of Laurel Street and Sixth Avenue.", "at the intersection of Laurel St and 6th Ave", "Laurel Street @ 6th Avenue",
                     "Laurel Street / 6th Avenue", "Laurel Street and 6th Avenue, San Diego, California", "LAUREL STREET AND 6TH AVENUE"):
            with self.subTest(text):
                found = media.parse_intersection(text)
                self.assertEqual((found["a"], found["b"]), (["laurel", "st"], ["6", "ave"]))
        self.assertEqual(media.parse_intersection("Laurel Street and 6th Avenue, San Diego, California")["locality"], "san diego")
        self.assertEqual(media.parse_intersection("Laurel Street and 6th Avenue, San Diego, CA 92101")["locality"], "san diego")

    def test_ordinals_match_in_every_spelling(self):
        for spelling in ("8th Avenue", "Eighth Avenue", "8 Avenue"):
            self.assertEqual(media.street_tokens(spelling), ["8", "ave"], spelling)
        self.assertEqual([media.numeric_ordinals(t) for t in ("Fifth Avenue", "Eighth Drive", "Twelfth St", "First Street", "Second Ave", "Third Ave")],
                         ["5th Avenue", "8th Drive", "12th St", "1st Street", "2nd Ave", "3rd Ave"])

    def test_things_that_are_not_intersections(self):
        for text in ("Arts and Crafts Center", "Rock & Roll Hall", "Joe and Jane Cafe", "Laurel Street", "Laurel Street and Laurel Street",
                     "5500 Canyon Crest Dr and Fifth St", "Join us at Main Street Park and Gardens", "", "A and B and C Street",
                     "Dinner and Dancing on Main Street", "Music at the Park"):
            with self.subTest(text):
                self.assertIsNone(media.parse_intersection(text))

    def test_street_names_match_by_name_with_a_note_when_only_the_type_differs(self):
        self.assertEqual(media.street_like(["balboa", "dr"], "Balboa Drive"), (True, ""))
        self.assertEqual(media.street_like(["8", "dr"], "8th Avenue"), (True, "the listing says dr, the map says ave"))
        self.assertFalse(media.street_like(["8", "st"], "28th Street")[0])
        self.assertFalse(media.street_like(["e", "8", "st"], "West 8th Street")[0], "opposite directions are different streets")
        self.assertTrue(media.street_like(["8", "st"], "West 8th Street")[0])
        self.assertTrue(media.street_like(["laurel", "st"], "Laurel Place")[0])
        self.assertFalse(media.street_like(["laurel", "st"], "Laurel Canyon Road")[0])

    def test_candidates_use_the_intersection_only_when_there_is_no_street_number(self):
        crossing = media.candidate_for({"venue": "On the corner of Balboa Drive and Eighth Drive."}, SD)
        self.assertEqual((crossing["kind"], crossing["names"], crossing["intersection"]["a_text"], crossing["intersection"]["b_text"]),
                         ("intersection", [], "Balboa Drive", "Eighth Drive"))
        self.assertEqual(media.candidate_for({"venue": "Park", "address": "Laurel St & 6th Ave, San Diego"}, SD)["kind"], "intersection")
        self.assertEqual(media.candidate_for({"venue": "Hall", "address": "5500 Canyon Crest Dr, San Diego"}, SD)["kind"], "address")
        source = media.candidate_for({"venue": "Laurel Street and 6th Avenue", "place": {"classification": "confirmed"},
                                      "geo": {"latitude": 32.7, "longitude": -117.1}}, SD)
        self.assertEqual(source["kind"], "source", "coordinates from the source come first")
        self.assertNotEqual(crossing["key"], media.candidate_for({"venue": "Balboa Drive and Ninth Drive"}, SD)["key"])
        self.assertEqual(crossing["key"], media.candidate_for({"venue": "corner of Balboa Dr & 8th Dr"}, SD)["key"], "one corner written two ways is one place")
        self.assertEqual(media.candidate_for({"venue": "Soda Bar"}, SD)["kind"], "venue")


class SharedNodes(unittest.TestCase):
    A, B = ["laurel", "st"], ["6", "ave"]

    def test_a_junction_is_found_with_its_real_node(self):
        places = media.shared_nodes(crossing_elements(), self.A, self.B)
        self.assertEqual(len(places), 1)
        self.assertEqual((places[0]["node"], places[0]["names"], places[0]["note"]), (2, ("Laurel Street", "6th Avenue"), ""))
        self.assertAlmostEqual(places[0]["lat"], CORNER[0], places=4)

    def test_streets_that_only_pass_near_each_other_do_not_meet(self):
        self.assertEqual(media.shared_nodes(crossing_elements(shared=False), self.A, self.B), [])

    def test_a_different_street_type_still_meets_and_says_so(self):
        places = media.shared_nodes(crossing_elements("Balboa Drive", "8th Avenue"), ["balboa", "dr"], ["8", "dr"])
        self.assertEqual((len(places), places[0]["note"]), (1, "the listing says dr, the map says ave"))

    def test_two_junctions_far_apart_are_ambiguous_and_neighbouring_nodes_are_one(self):
        far = [way(10, "Laurel Street", [1, 2, 3, 7]), way(11, "6th Avenue", [4, 2, 5, 7]), *crossing_elements()[2:], node(7, CORNER[0] + 0.02, CORNER[1])]
        self.assertEqual(len(media.shared_nodes(far, self.A, self.B)), 2, "two shared nodes 2 km apart")
        near = [way(10, "Laurel Street", [1, 2, 9, 3]), way(11, "6th Avenue", [4, 2, 9, 5]), *crossing_elements()[2:], node(9, CORNER[0] + 0.0002, CORNER[1])]
        self.assertEqual(len(media.shared_nodes(near, self.A, self.B)), 1, "a dual carriageway's two nodes are one corner")

    def test_wrong_names_and_hostile_data_produce_nothing(self):
        self.assertEqual(media.shared_nodes(crossing_elements(), self.A, ["7", "ave"]), [])
        self.assertEqual(media.shared_nodes(crossing_elements("Laurel Canyon Road", "6th Avenue"), self.A, self.B), [])
        for bad in (None, "x", [], [None, 5, {"type": "way"}, {"type": "way", "nodes": "x", "tags": {}}, {"type": "way", "nodes": [1], "tags": {"name": 5}}], {"elements": []}):
            self.assertEqual(media.shared_nodes(bad, ["a", "st"], ["b", "st"]), [])

    def test_the_query_is_a_loose_name_filter_built_only_from_word_characters(self):
        box = media.bounding_box([(32.74, -117.16)], 1500)
        query = media.overpass_query(box, media.street_tokens("Eighth Drive"), media.street_tokens("Balboa Drive"))
        self.assertIn("(8|eighth)", query)
        self.assertIn("(balboa)", query)
        self.assertTrue(query.startswith("[out:json]") and query.endswith("out skel qt;"))
        hostile = media.parse_intersection('Evil"]);out; Street and Main Street')
        built = media.overpass_query(box, hostile["a"], hostile["b"])
        self.assertEqual(built.count('"'), 12, "no quote from the listing reaches the query")

    def test_the_search_box_is_bounded(self):
        self.assertIsNotNone(media.bounding_box([(32.74, -117.16), (32.75, -117.15)], 1500))
        self.assertIsNone(media.bounding_box([(32.74, -117.16), (32.9, -117.15)], 1500), "points spread over many kilometres are not checked")


class IntersectionService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.event = {"venue": "Laurel Street at Sixth Avenue", "title": "Haunted something"}

    def service(self, fetcher, folder=None, **options):
        return media.MediaService(folder or self.tmp.name, fetcher, clock=options.pop("clock", time.time), sleep=options.pop("sleep", lambda s: None), nominatim_interval=0)

    def resolve(self, fetcher, event=None):
        """Resolve one event in a fresh folder; (service, the stored record or None)."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        service = self.service(fetcher, folder.name)
        service.cycle([(SD, [event or self.event])])
        return service, next(iter(service.store.index["records"].values()), None)

    def test_a_verified_intersection_becomes_an_approximate_pin_and_a_map(self):
        service, record = self.resolve(FakeFetcher({"Laurel": [street()]}, overpass=crossing_elements()))
        self.assertTrue(record["ok"])
        where = record["location"]
        self.assertEqual((where["confidence"], where["precision"], where["approximate"]), ("intersection", "intersection", True))
        self.assertAlmostEqual(where["lat"], CORNER[0], places=4)
        self.assertIn("OpenStreetMap node 2 is shared by both streets", where["provenance"])
        self.assertNotIn("photo", record, "an intersection is not a venue: no photograph is sought")
        shown = service.annotate([self.event], SD)[0]["media"]
        self.assertEqual((shown["map"]["confidence"], shown["map"]["precision"], shown["map"]["approximate"]), ("intersection", "intersection", True))
        self.assertNotIn("photo", shown)
        self.assertTrue(service.store.read_file(record["map"]["files"]["light"]))

    def test_a_corner_the_map_data_does_not_contain_is_refused_with_its_reason_and_never_estimated(self):
        for label, overpass in (("not connected", crossing_elements(shared=False)), ("no such street", [])):
            with self.subTest(label):
                service, record = self.resolve(FakeFetcher({"Laurel": [street()]}, overpass=overpass))
                self.assertFalse(record["ok"])
                self.assertIn("do not meet in OpenStreetMap's road network", record["reason"])
                self.assertNotIn("location", record)
                self.assertEqual(service.annotate([self.event], SD), [self.event])

    def test_the_first_street_must_be_found_in_the_right_place(self):
        event = {"venue": "Laurel Street at Sixth Avenue, San Diego"}
        for label, results in (("not found", []), ("another city", [street(city="Oceanside")]), ("outside the area", [street(lat=45.0, lon=-100.0)]),
                               ("not a road", [street(category="amenity")]), ("another name", [street(name="Laurel Canyon Road")])):
            with self.subTest(label):
                _, record = self.resolve(FakeFetcher({"Laurel": results}, overpass=crossing_elements()), event)
                self.assertFalse(record["ok"], label)
                self.assertIn("was not found in the locality", record["reason"])
        _, record = self.resolve(FakeFetcher({"Laurel": [street()]}, overpass=crossing_elements()), event)
        self.assertTrue(record["ok"], "the same event is accepted in the right city")

    def test_two_corners_are_ambiguous_and_an_intersection_outside_the_area_is_refused(self):
        two = [way(10, "Laurel Street", [1, 2, 3, 7]), way(11, "6th Avenue", [4, 2, 5, 7]), *crossing_elements()[2:], node(7, CORNER[0] + 0.02, CORNER[1])]
        _, record = self.resolve(FakeFetcher({"Laurel": [street()]}, overpass=two))
        self.assertIn("more than one place", record["reason"])
        far = [{**e, "lat": 45.0, "lon": -100.0} if e["type"] == "node" else e for e in crossing_elements()]
        _, record = self.resolve(FakeFetcher({"Laurel": [street()]}, overpass=far))
        self.assertIn("outside the search area", record["reason"])

    def test_an_unavailable_overpass_is_retried_later_not_remembered_as_a_refusal(self):
        fetcher = FakeFetcher({"Laurel": [street()]}, overpass=crossing_elements(), fail=lambda url: "overpass" in url)
        service = self.service(fetcher)
        self.assertEqual(service.cycle([(SD, [self.event])]), 0)
        self.assertEqual((service.stats["failed"], service.store.index["records"]), (1, {}))
        fetcher.fail = None
        self.assertEqual(self.service(fetcher).cycle([(SD, [self.event])]), 1)

    def test_ordinal_words_are_asked_for_as_numerals_and_overpass_is_spaced_out(self):
        sleeps = []
        fetcher = FakeFetcher({"5th": [street("5th Avenue")], "Laurel": [street()]}, overpass=crossing_elements())
        service = self.service(fetcher, clock=lambda: 1_000_000.0, sleep=sleeps.append)
        service.cycle([(SD, [{"venue": "Fifth Avenue and Market Street"}, {"venue": "Laurel Street and Fourth Avenue"}])])
        self.assertTrue([u for u in fetcher.urls if "nominatim" in u and "5th" in u], "Fifth is asked for as 5th")
        self.assertEqual(len([u for u in fetcher.urls if "overpass" in u]), 2)
        self.assertEqual(sleeps, [media.OVERPASS_INTERVAL], "the second query to the shared Overpass service waits")

    def test_the_haunted_trail_text_is_an_intersection_and_a_failed_check_leaves_no_pin(self):
        event = {"title": "The Haunted Trail of Balboa Park", "venue": "On the corner of Balboa Drive and Eighth Drive.",
                 "place": {"classification": "unknown", "latitude": None, "longitude": None}}
        candidate = media.candidate_for(event, SD)
        self.assertEqual(candidate["kind"], "intersection")
        self.assertEqual((candidate["intersection"]["a"], candidate["intersection"]["b"]), (["balboa", "dr"], ["8", "dr"]))
        balboa = {"Balboa": [street("Balboa Drive", lat=32.7408, lon=-117.1595)]}
        stub = way(20, "Balboa Drive", [1, 2], "tertiary")
        nodes = [node(1, 32.7408, -117.1595), node(2, 32.7408, -117.1594), node(3, 32.7435, -117.1572), node(4, 32.7440, -117.1572)]
        service, record = self.resolve(FakeFetcher(balboa, overpass=[stub, way(21, "8th Avenue", [3, 4]), *nodes]), event)
        self.assertFalse(record["ok"])
        self.assertEqual(service.annotate([event], SD), [event], "no coordinates are invented")
        _, record = self.resolve(FakeFetcher(balboa, overpass=[stub, way(21, "8th Avenue", [2, 4]), *nodes]), event)
        self.assertTrue(record["ok"])
        self.assertIn("the listing says dr, the map says ave", record["location"]["provenance"])
        self.assertTrue(record["location"]["approximate"])

    def test_addresses_and_venues_are_unchanged_by_intersection_support(self):
        for event, kind in (({"venue": "Hall", "address": "5500 Canyon Crest Dr, San Diego"}, "address"), ({"venue": "Soda Bar"}, "venue"),
                            ({"venue": "Arts and Crafts Center"}, "venue")):
            self.assertEqual(media.candidate_for(event, SD)["kind"], kind)


if __name__ == "__main__":
    unittest.main()
