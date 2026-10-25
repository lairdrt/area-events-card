"""Display text: character references are decoded once, in one place; the stored evidence stays as the source wrote it; map labels are short place names, never sentences."""
import json
import sqlite3
import unittest

from area_events.extraction.text import decode_entities, place_label
from tests.test_extraction import Fixture, candidate_payload

SENTENCE = "The Little Italy Mercato operates on West Date Street from the cul de sac at Kettner to India Street, every Saturday morning."


class DecodeEntities(unittest.TestCase):
    def test_common_references_become_the_characters_they_stand_for(self):
        for raw, expected in (("Children&#039;s Museum", "Children's Museum"), ("Fish &amp; Chips", "Fish & Chips"), ("&quot;Jazz&quot;", '"Jazz"'),
                              ("It&#x27;s", "It's"), ("Caf&eacute;", "Café"), ("&#128512;", "\U0001F600"), ("a&nbsp;b", "a b")):
            self.assertEqual(decode_entities(raw), expected)

    def test_text_that_is_not_a_complete_reference_is_left_alone(self):
        for raw in ("AT&T", "Q&A; session", "&unknown;", "&#0;", "Tom & Jerry", "", "100% & more"):
            self.assertEqual(decode_entities(raw), raw)
        self.assertEqual(decode_entities(None), "")

    def test_decoding_happens_once_and_never_produces_markup_to_run(self):
        self.assertEqual(decode_entities("&amp;#039;"), "&#039;", "double-encoded text is decoded one level, not repeatedly")
        self.assertEqual(decode_entities("&lt;script&gt;alert(1)&lt;/script&gt;"), "<script>alert(1)</script>", "it is text; the card renders text nodes only")


class PlaceLabels(unittest.TestCase):
    def test_a_venue_name_is_the_label(self):
        self.assertEqual(place_label({"venue": "Cactus Cafe, Texas Union", "address": None}), "Cactus Cafe")
        self.assertEqual(place_label({"venue": "Children&#039;s Park", "address": None}), "Children's Park")

    def test_a_sentence_is_never_the_label(self):
        label = place_label({"venue": SENTENCE, "address": None})
        self.assertTrue(label is None or (len(label) <= 60 and "operates" not in label), label)
        self.assertEqual(place_label({"venue": SENTENCE, "address": "W Date St, San Diego, CA"}), "Little Italy Mercato")

    def test_the_street_of_the_address_names_a_place_without_inventing_one(self):
        self.assertEqual(place_label({"venue": None, "address": "500 Harbor Drive, San Diego, CA"}), "Harbor Drive")
        self.assertIsNone(place_label({"venue": None, "address": None}))
        self.assertIsNone(place_label({"venue": "Meet near the fountain on the north side of the park.", "address": None}))


class Extraction(Fixture):
    def test_events_are_decoded_but_the_stored_candidate_keeps_the_source_text(self):
        built = self.build()
        payload = candidate_payload("Children&#039;s Museum &amp; Play", venue="Cafe &quot;Luna&quot;", description="Bring the kids &#8211; it&#039;s free.")
        self.add_candidate(built, "sandiego_gov", "s:entity", payload)
        self.drain(built)
        event = next(e for e in self.all_events(built) if e.title.startswith("Children"))
        self.assertEqual(event.title, "Children's Museum & Play")
        self.assertEqual(event.venue_name, 'Cafe "Luna"')
        self.assertEqual(built.events.get(event.event_key).event.title, "Children's Museum & Play")
        connection = sqlite3.connect(self.db)
        try:
            stored = json.loads(connection.execute("SELECT payload_json FROM candidates WHERE candidate_key='s:entity'").fetchone()[0])
        finally:
            connection.close()
        self.assertEqual(stored["title"], "Children&#039;s Museum &amp; Play", "the evidence is exactly what the source said")


if __name__ == "__main__":
    unittest.main()
