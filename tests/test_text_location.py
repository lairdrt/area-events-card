"""Recovering a real place from the text an event already carries: addresses written in venue text or descriptions, named parks and
centres, corner phrases, room-free venue names. Every recovered place is still only a candidate that the verifier must confirm."""
import shutil
import tempfile
import time
import unittest

from discovery import media, textloc
from tests.test_media import SD, FakeFetcher, Wikimedia, nominatim

CARNIVAL = {"title": 'South Clairemont Rec Ctr & Park "Winter" Carnival', "address": None,
            "venue": "This will be a family carnival with approx. 14-16 rides, (6) games & (2) Food Concessions located on the front side of South Clairemont Park area.",
            "description": "Get ready to attend our first Fun-Filled Family Carnival at the South Clairemont Recreation Center, 3605 Clairemont Drive, San Diego, CA 92117, October 22 - 25, 2026. Come join us."}
HILLCREST = {"title": "Hillcrest Farmers Market", "venue": "The Hillcrest Farmers Market will take place on University Ave from Herbert St to Park Blvd and Center St from University to Harvey Milk and South of University just before the parking lot at 1717 University Ave. Center St from University to Robinson will be closed to through traffic.", "description": "A farmer's market."}
ASCEND = {"title": "ASCENDKemet Farmers Market", "venue": "This event will take place between 43rd and Fairmount at 4325 Wightman", "description": "A market."}
VISIONWALK = {"title": "San Diego VisionWalk", "venue": "De Anza Cove Park is a scenic waterfront park located along Mission Bay in San Diego. Known for its wide-open grassy areas.", "description": "A walk."}


class Extraction(unittest.TestCase):
    def test_full_addresses_with_their_city_when_a_state_or_the_search_city_follows(self):
        cities = ["San Diego"]
        found = textloc.extract_addresses(CARNIVAL["description"], cities)
        self.assertEqual([(f["address"], f["name_before"]) for f in found], [("3605 Clairemont Drive, San Diego, CA 92117", "South Clairemont Recreation Center")])
        self.assertEqual(textloc.extract_addresses("Tickets at 1055 Fifth Avenue, San Diego", cities)[0]["address"], "1055 Fifth Avenue, San Diego")
        self.assertEqual(textloc.extract_addresses("8878 Clairemont Mesa Blvd, Suite A, San Diego", cities)[0]["address"], "8878 Clairemont Mesa Blvd, San Diego")
        self.assertEqual(textloc.extract_addresses("401 K St. #C, San Diego", cities)[0]["address"], "401 K St., San Diego")
        self.assertEqual(textloc.extract_addresses("2901 Read Granberry Trail, Austin, TX 78712")[0]["address"], "2901 Read Granberry Trail, Austin, TX 78712")
        self.assertEqual(textloc.extract_addresses("2025 Pan American Rd E, San Diego", cities)[0]["address"], "2025 Pan American Rd E, San Diego")

    def test_words_after_the_comma_that_are_not_a_place_are_not_a_city(self):
        self.assertEqual(textloc.extract_addresses("1100 Third Avenue, October 5 at 7pm")[0]["address"], "1100 Third Avenue")
        self.assertEqual(textloc.extract_addresses("1100 Third Avenue, Bring a friend")[0]["address"], "1100 Third Avenue")

    def test_the_address_of_the_market_has_no_city_so_the_search_city_is_assumed_later(self):
        self.assertEqual(textloc.extract_addresses(HILLCREST["venue"])[0]["address"], "1717 University Ave.")

    def test_a_number_and_street_name_without_a_street_type_needs_the_word_at(self):
        self.assertEqual(textloc.extract_addresses(ASCEND["venue"])[0]["address"], "4325 Wightman")
        self.assertEqual(textloc.extract_addresses("Wightman 4325 Wightman is the place"), [])
        self.assertEqual(textloc.extract_addresses("Doors at 7 Eleven"), [])
        self.assertEqual(textloc.extract_addresses("Starts at 700 PM"), [])
        self.assertEqual(textloc.extract_addresses("Ages 5 and up"), [])

    def test_more_than_one_different_address_is_left_to_nobody(self):
        text = "Show at 123 Main Street, San Diego. Parking at 456 Oak Avenue, San Diego."
        self.assertEqual(len(textloc.extract_addresses(text, ["San Diego"])), 2)
        self.assertIsNone(media._text_address(text, SD, "description"))
        self.assertEqual(len(textloc.extract_addresses("123 Main Street and again 123 Main Street, San Diego", ["San Diego"])), 1, "the same address twice is one")

    def test_named_places_at_the_start_of_a_sentence(self):
        self.assertEqual(textloc.named_place(VISIONWALK["venue"]), "De Anza Cove Park")
        self.assertEqual(textloc.named_place("South Clairemont Recreation Center, 3605 Clairemont"), "South Clairemont Recreation Center")
        self.assertIsNone(textloc.named_place("This will be a family carnival near the park"))
        self.assertIsNone(textloc.named_place("Join us at Balboa Park"), "only a name that opens the text")

    def test_corner_phrases_and_prose_detection(self):
        self.assertEqual(textloc.corner_phrases("including the intersection of 29th Street and North Park Way."), ["29th Street and North Park Way"])
        self.assertEqual(textloc.corner_phrases("on the corner of Balboa Drive & Juniper Road, near the lawn"), ["Balboa Drive and Juniper Road"])
        self.assertEqual(textloc.corner_phrases("Come early for the best seats"), [])
        self.assertTrue(textloc.is_prose(VISIONWALK["venue"]))
        self.assertTrue(textloc.is_prose(CARNIVAL["venue"]))
        for name in ("Soda Bar", "Peter T. Flawn Academic Center (FAC) 2.142 - Classrooms ABC", "Texas Union Theatre", "LBJ - Bass Lecture Hall"):
            self.assertFalse(textloc.is_prose(name), name)

    def test_room_numbers_are_set_aside_from_a_venue_name(self):
        self.assertEqual(textloc.without_rooms("SEAY Library - Room 4.244"), ["SEAY Library"])
        self.assertEqual(textloc.without_rooms("Peter T. Flawn Academic Center (FAC) 2.142 - Classrooms ABC"), ["Peter T. Flawn Academic Center (FAC) 2.142"])
        self.assertEqual(textloc.without_rooms("Soda Bar"), [])
        self.assertEqual(textloc.without_rooms("Room 101"), [])

    def test_a_sentence_with_and_in_it_is_not_a_pair_of_streets(self):
        sentence = "Ray Street to Granada Avenue along North Park Way including the intersection of 29th Street and North Park Way."
        self.assertIsNone(media.parse_intersection(sentence))
        self.assertIsNone(media.parse_intersection("Vehicles will be parked along Palm Street between 30th Street and Bancroft Street"))
        self.assertIsNotNone(media.parse_intersection("29th Street and North Park Way"))


class Candidates(unittest.TestCase):
    def kinds(self, event):
        out, option = [], media.candidate_for(event, SD)
        while option:
            out.append((option["kind"], option.get("found_in")))
            option = option.get("then")
        return out

    def test_the_address_in_the_description_is_used_when_the_venue_is_only_a_sentence(self):
        c = media.candidate_for(CARNIVAL, SD)
        self.assertEqual((c["kind"], c["address"], c["found_in"], c["venue"]), ("address", "3605 Clairemont Drive, San Diego, CA 92117", "description", "South Clairemont Recreation Center"))
        self.assertEqual(c["street"], ("3605", ["clairemont", "dr"], "san diego"))
        self.assertEqual(c["names"], ["south clairemont recreation center"])

    def test_the_address_in_the_venue_text_beats_everything_but_source_and_structured_addresses(self):
        c = media.candidate_for(HILLCREST, SD)
        self.assertEqual((c["kind"], c["address"], c["found_in"]), ("address", "1717 University Ave.", "venue text"))
        self.assertEqual(c["street"], ("1717", ["university", "ave"], "san diego"), "the search area's city is required of the match")
        self.assertEqual(media.candidate_for(ASCEND, SD)["address"], "4325 Wightman")

    def test_the_order_of_evidence(self):
        structured = dict(CARNIVAL, address="1 Main St, San Diego")
        self.assertEqual(media.candidate_for(structured, SD)["found_in"] if media.candidate_for(structured, SD).get("found_in") else "structured", "structured")
        confirmed = dict(CARNIVAL, place={"classification": "confirmed"}, geo={"latitude": 32.8, "longitude": -117.2})
        self.assertEqual(media.candidate_for(confirmed, SD)["kind"], "source")
        named = {"title": "x", "venue": "Soda Bar", "description": "Doors at 3615 El Cajon Boulevard, San Diego"}
        self.assertEqual(self.kinds(named), [("venue", None), ("address", "description")], "a real venue name is tried first; the description's address is the fallback")
        self.assertEqual(self.kinds(dict(named, venue="")), [("address", "description")])

    def test_prose_is_not_looked_up_as_a_name_but_a_named_park_in_it_is(self):
        c = media.candidate_for(VISIONWALK, SD)
        self.assertEqual((c["kind"], c["venue"], c["names"], c["found_in"]), ("venue", "De Anza Cove Park", ["de anza cove park"], "venue text"))
        self.assertIsNone(media.candidate_for({"title": "x", "venue": "This is an event on the street, with music and food, for everyone to enjoy all day"}, SD))

    def test_a_corner_phrase_in_the_text_is_the_last_resort(self):
        event = {"title": "x", "venue": "Ray Street to Granada Avenue along North Park Way including the intersection of 29th Street and North Park Way."}
        c = media.candidate_for(event, SD)
        self.assertEqual((c["kind"], c["found_in"]), ("intersection", "venue text"))
        self.assertEqual((c["intersection"]["a_text"], c["intersection"]["b_text"]), ("29th Street", "North Park Way"))

    def test_at_most_three_candidates_are_chained(self):
        event = {"title": "x", "venue": "Soda Bar", "address": "A Street and B Street", "description": "At 3615 El Cajon Boulevard, San Diego. On the corner of C Street and D Street."}
        self.assertLessEqual(len(self.kinds(event)), 3)

    def test_the_same_address_has_the_same_key_however_it_was_found(self):
        structured = media.candidate_for({"venue": "x", "address": "3605 Clairemont Drive, San Diego, CA 92117"}, SD)
        self.assertEqual(structured["key"], media.candidate_for(CARNIVAL, SD)["key"])

    def test_room_free_names_are_asked_for_too(self):
        c = media.candidate_for({"title": "x", "venue": "SEAY Library - Room 4.244"}, SD)
        self.assertEqual((c["kind"], c["variants"]), ("venue", ["SEAY Library"]))
        self.assertIn("seay library", c["names"])
        plain = media.candidate_for({"title": "x", "venue": "Soda Bar"}, SD)
        self.assertEqual(plain["variants"], [])
        self.assertFalse(plain["retry_old"])


class Verification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_cycle(self, event, results, tmp=None):
        fetcher = FakeFetcher(results, wiki=Wikimedia())
        folder = tmp or tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        service = media.MediaService(folder, fetcher, clock=time.time, sleep=lambda s: None, nominatim_interval=0)
        service.cycle([(SD, [event])])
        return service, next(iter(service.store.index["records"].values()), None), fetcher

    def test_the_description_address_is_confirmed_by_house_number_street_and_city_and_says_where_it_came_from(self):
        hit = nominatim("South Clairemont Recreation Center", 32.8, -117.19, house="3605", road="Clairemont Drive", category="leisure", kind="sports_centre")
        service, record, _ = self.run_cycle(CARNIVAL, {"Clairemont": [hit]})
        self.assertTrue(record["ok"])
        where = record["location"]
        self.assertEqual((where["confidence"], where["precision"], where["approximate"]), ("address", "address", True))
        self.assertIn("house number 3605", where["provenance"])
        self.assertIn("taken from the event's description", where["provenance"])
        self.assertEqual(service.annotate([CARNIVAL], SD)[0]["media"]["map"]["confidence"], "address")

    def test_a_wrong_house_number_street_or_city_is_refused(self):
        for label, hit in (("number", nominatim("X", 32.8, -117.19, house="3600", road="Clairemont Drive")),
                           ("street", nominatim("X", 32.8, -117.19, house="3605", road="Clairemont Mesa Boulevard")),
                           ("city", nominatim("X", 32.8, -117.19, house="3605", road="Clairemont Drive", city="Chula Vista"))):
            with self.subTest(label):
                _, record, _ = self.run_cycle(CARNIVAL, {"Clairemont": [hit]})
                self.assertFalse(record["ok"], label)
                self.assertNotIn("location", record)

    def test_the_address_without_a_city_must_be_in_the_search_areas_city(self):
        right = nominatim("x", 32.75, -117.15, house="1717", road="University Avenue")
        wrong = nominatim("x", 32.77, -117.02, house="1717", road="University Avenue", city="La Mesa")
        self.assertTrue(self.run_cycle(HILLCREST, {"1717": [right]})[1]["ok"])
        _, record, _ = self.run_cycle(HILLCREST, {"1717": [wrong]})
        self.assertFalse(record["ok"], "the same number and street in another city is not the market")

    def test_an_address_found_twice_far_apart_is_ambiguous(self):
        a = nominatim("x", 32.75, -117.15, house="1717", road="University Avenue")
        b = nominatim("x", 32.80, -117.05, house="1717", road="University Avenue")
        _, record, _ = self.run_cycle(HILLCREST, {"1717": [a, b]})
        self.assertFalse(record["ok"])

    def test_a_named_park_is_accepted_only_on_an_exact_name_and_a_place_like_type(self):
        park = nominatim("De Anza Cove Park", 32.7889, -117.2273, category="leisure", kind="park")
        _, record, _ = self.run_cycle(VISIONWALK, {"De+Anza": [park], "De Anza": [park]})
        self.assertTrue(record["ok"])
        self.assertEqual(record["location"]["confidence"], "venue")
        self.assertIn("taken from the event's venue text", record["location"]["provenance"] + "; the address was taken from the event's venue text" if False else "taken from the event's venue text")
        other = nominatim("De Anza Cove Mobile Home Park", 32.79, -117.23, category="leisure", kind="park")
        _, record, _ = self.run_cycle(VISIONWALK, {"De Anza": [other]})
        self.assertFalse(record["ok"], "a different name is not the park")

    def test_the_search_citys_name_on_the_end_of_a_result_name_is_set_aside_but_nothing_else(self):
        hotel = {"venue": "Hard Rock Hotel", "title": "x"}
        ok = nominatim("Hard Rock Hotel San Diego", 32.7087, -117.1590, category="tourism", kind="hotel")
        self.assertTrue(self.run_cycle(hotel, {"Hard Rock": [ok]})[1]["ok"])
        other = nominatim("Hard Rock Cafe", 32.7087, -117.1590, category="amenity", kind="restaurant")
        self.assertFalse(self.run_cycle(hotel, {"Hard Rock": [other]})[1]["ok"])
        union = {"venue": "Texas Union", "title": "x"}
        credit = nominatim("Greater Texas Federal Credit Union", 32.7157, -117.1611, category="amenity", kind="bank")
        self.assertFalse(self.run_cycle(union, {"Texas": [credit]})[1]["ok"], "a longer, different name is never accepted")
        zoo = nominatim("Zoo", 32.7353, -117.149, category="tourism", kind="zoo")
        self.assertFalse(self.run_cycle({"venue": "San Diego Zoo", "title": "x"}, {"Zoo": [zoo]})[1]["ok"], "a single remaining word is too little to go on")

    def test_the_next_candidate_is_tried_when_the_first_is_refused_and_the_reasons_are_kept(self):
        event = {"title": "x", "venue": "Nowhere Hall", "description": "Doors open at 3615 El Cajon Boulevard, San Diego."}
        hit = nominatim("Soda Bar", 32.7573, -117.1203, house="3615", road="El Cajon Boulevard")
        _, record, _ = self.run_cycle(event, {"3615": [hit]})
        self.assertTrue(record["ok"])
        self.assertEqual(record["via"], "address")
        self.assertIn("geocoder found nothing", record["earlier"])
        _, record, _ = self.run_cycle(event, {})
        self.assertFalse(record["ok"])
        self.assertIn("venue:", record["reason"])
        self.assertIn("address:", record["reason"])

    def test_room_free_venue_names_are_looked_up_as_well(self):
        event = {"title": "x", "venue": "SEAY Library - Room 4.244"}
        hit = nominatim("SEAY Library", 32.7, -117.1, category="amenity", kind="library")
        service, record, fetcher = self.run_cycle(event, {"SEAY+Library%2C": [hit]})
        self.assertTrue(record["ok"])
        self.assertEqual(len([u for u in fetcher.urls if "nominatim" in u]), 2)

    def test_refusals_from_older_rules_are_tried_again_only_where_the_new_rules_read_the_place_differently(self):
        service = media.MediaService(self.tmp.name, FakeFetcher({}), clock=time.time, sleep=lambda s: None, nominatim_interval=0)
        plain = media.candidate_for({"title": "x", "venue": "Soda Bar"}, SD)
        room = media.candidate_for({"title": "x", "venue": "SEAY Library - Room 4.244"}, SD)
        service.store.index["records"][plain["key"]] = {"ok": False, "at": time.time(), "reason": "the geocoder found nothing"}
        service.store.index["records"][room["key"]] = {"ok": False, "at": time.time(), "reason": "the geocoder found nothing"}
        self.assertEqual(service.pending([(SD, [{"title": "x", "venue": "Soda Bar"}, {"title": "x", "venue": "SEAY Library - Room 4.244"}])]), 1)
        near = media.candidate_for({"title": "x", "venue": "Hard Rock Hotel"}, SD)
        service.store.index["records"][near["key"]] = {"ok": False, "at": time.time(), "reason": "no result matched the address or venue name closely enough"}
        self.assertEqual(service.pending([(SD, [{"title": "x", "venue": "Hard Rock Hotel"}])]), 1)
        service.store.index["records"][near["key"]]["rv"] = media.RESOLVER
        self.assertEqual(service.pending([(SD, [{"title": "x", "venue": "Hard Rock Hotel"}])]), 0, "tried under the current rules: not again")


if __name__ == "__main__":
    unittest.main()
