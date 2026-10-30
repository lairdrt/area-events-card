"""Display text: character references are decoded once, in one place; the stored evidence stays as the source wrote it; map labels are short place names, never sentences."""
import json
import sqlite3
import unittest
from unittest.mock import patch

from area_events.extraction.service import VERSION, ExtractionService, LocalProcessingClient, StoredCandidateExtractor
from area_events.extraction.text import decode_entities, place_label, plain_text
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


PB_MARKET = ("The 4400 block of Bayard St from Garnet Ave to Hornblend St<br>The 800 block Hornblend from Bayard to Mission Blvd <br>Both locations have vendors "
             "occupying the entire block <br><br>The 800 block of Hornblend Street between Garnet and Bayard and 4451 Bayard Street between Garnet and Mission")


class PlainText(unittest.TestCase):
    def test_formatting_tags_are_removed_and_their_words_kept(self):
        for raw, expected in (("<b>Jazz</b> Night", "Jazz Night"), ("a <em>very</em> good <a href=\"https://x.org/a?b=1&amp;c=2\">show</a>.", "a very good show."),
                              ("<p><strong>Bring <i>your <u>own</u></i> chair</strong></p>", "Bring your own chair"), ("<span class='x' data-id=\"7\">Free</span>", "Free"),
                              ("<B>Loud</B><BR/>Quiet", "Loud\nQuiet"), ("Picnic<img src=\"a.jpg\" alt=\"\">", "Picnic"), ("<!-- note -->Open Mic", "Open Mic")):
            self.assertEqual(plain_text(raw), expected)

    def test_line_and_paragraph_tags_become_line_breaks(self):
        self.assertEqual(plain_text("Line one<br>Line two<br />Line three"), "Line one\nLine two\nLine three")
        self.assertEqual(plain_text("<p>First paragraph.</p>\n<p>Second paragraph.</p>"), "First paragraph.\n\nSecond paragraph.")
        self.assertEqual(plain_text("<ul><li>Food</li><li>Music</li></ul>"), "Food\n\nMusic")
        self.assertEqual(plain_text(PB_MARKET), "The 4400 block of Bayard St from Garnet Ave to Hornblend St\nThe 800 block Hornblend from Bayard to Mission Blvd\n"
                                                "Both locations have vendors occupying the entire block\n\nThe 800 block of Hornblend Street between Garnet and Bayard and "
                                                "4451 Bayard Street between Garnet and Mission")

    def test_encoded_tags_and_entities_are_decoded_then_cleaned(self):
        self.assertEqual(plain_text("&lt;b&gt;Fish &amp;amp; Chips&lt;/b&gt;&lt;br&gt;Pier&nbsp;1"), "Fish & Chips\nPier 1")
        self.assertEqual(plain_text("Twists, Turns, &amp;amp; Breakthroughs"), "Twists, Turns, & Breakthroughs")
        self.assertEqual(plain_text("Caf&eacute; &#8211; It&#039;s &quot;open&quot;"), "Café – It's \"open\"")
        self.assertEqual(plain_text("&amp;lt;i&amp;gt;Twice&amp;lt;/i&amp;gt; escaped"), "Twice escaped")

    def test_script_and_style_contents_are_left_out(self):
        self.assertEqual(plain_text("Concert<script>alert('x <b>')</script> tonight"), "Concert tonight")
        self.assertEqual(plain_text("<style>p { color: red }</style>Garden tour"), "Garden tour")
        self.assertEqual(plain_text("&lt;SCRIPT type=&quot;text/javascript&quot;&gt;steal()&lt;/SCRIPT&gt;Story time"), "Story time")
        self.assertEqual(plain_text("Talk<script>never closed"), "Talk")

    def test_plain_text_and_literal_angle_brackets_stay_as_written(self):
        for raw in ("Kids < 12 free, adults > 65 half price", "I <3 tacos", "<Ages 5+>", "a<b and c>d", "Score: 3 > 2 -> win", "AT&T Q&A", "Tom & Jerry",
                    "Line one\nLine two", "Doors 7:00 PM, 2026-11-01T19:00:00-07:00", "https://example.org/e?id=5&x=<1>", ""):
            self.assertEqual(plain_text(raw), raw)
        self.assertEqual(plain_text("Kids &lt; 12 &amp; adults &gt; 65"), "Kids < 12 & adults > 65")
        self.assertEqual(plain_text(None), "")

    def test_cleaning_again_changes_nothing(self):
        for raw in (PB_MARKET, "&amp;amp;amp;amp; deep", "&lt;b&gt;x&lt;/b&gt;", "<p> a </p><p></p><br><br>b", "<b>&amp;lt;</b>i&amp;gt;", "I <3 tacos", "  spaced  "):
            once = plain_text(raw)
            self.assertEqual(plain_text(once), once, raw)


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

    def rows(self, sql):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def test_reprocessing_stored_candidates_cleans_markup_and_revises_only_the_events_it_changes(self):
        built = self.build()
        self.two_areas(built)
        self.discover(built)
        self.add_candidate(built, "sandiego_gov", "s:market", candidate_payload("Tuesday Farmers Market", venue=PB_MARKET, address="901 Hornblend St, San Diego, CA"))
        self.add_candidate(built, "austin_fixture", "a:lecture", candidate_payload("Guest Lecture: Twists, Turns, &amp;amp; Breakthroughs", starts_at="2026-07-06T19:00:00-05:00",
                                                                                 description="<p>A <b>talk</b>.</p><p>Free &amp;amp; open.</p>"))
        older = StoredCandidateExtractor()
        older.version = VERSION - 1
        with patch("area_events.extraction.service.plain_text", decode_entities):   # what version 2 did: references decoded once, markup kept
            self.drain(built, ExtractionService(LocalProcessingClient(built.processing), older, self.clock, worker_id="v2"))
        identities = "SELECT e.event_id, e.event_key, c.candidate_key, ec.role FROM events e JOIN event_candidates ec USING (event_id) JOIN candidates c USING (candidate_id) ORDER BY 1, 3"
        before = self.rows(identities)
        revisions = dict(self.rows("SELECT event_key, revision FROM events"))
        payloads = self.rows("SELECT candidate_key, payload_json, payload_hash FROM candidates ORDER BY 1")
        history = self.rows("SELECT * FROM event_revisions ORDER BY 1, 2")
        self.assertIn("<br>", self.rows("SELECT location_text FROM events WHERE location_text LIKE '%Bayard%'")[0][0])

        report = self.drain(built)
        self.assertEqual((report.submitted, sorted(report.outcomes)), (9, ["unchanged"] * 7 + ["updated"] * 2))
        self.assertEqual(self.rows(identities), before, "event ids, event keys and candidate associations are kept")
        self.assertEqual(self.rows("SELECT candidate_key, payload_json, payload_hash FROM candidates ORDER BY 1"), payloads, "the stored candidates keep the source's text")
        after = self.rows("SELECT * FROM event_revisions ORDER BY 1, 2")
        self.assertEqual(([row for row in after if row in history], len(after)), (history, len(history) + 2), "earlier revisions are kept; one is added per changed event")
        changed = {key for key, revision in self.rows("SELECT event_key, revision FROM events") if revision != revisions[key]}
        self.assertEqual(len(changed), 2, "only the events whose text changed are revised")
        market = next(e for e in self.all_events(built) if e.title == "Tuesday Farmers Market")
        lecture = next(e for e in self.all_events(built) if e.title.startswith("Guest Lecture"))
        self.assertEqual(market.location_text, plain_text(PB_MARKET))
        self.assertEqual((lecture.title, lecture.description), ("Guest Lecture: Twists, Turns, & Breakthroughs", "A talk.\n\nFree & open."))
        self.assertFalse([row for row in self.rows("SELECT title, description, venue_name, location_text, address FROM events") if any(v and "<" in v for v in row)])

        self.assertEqual(self.drain(built).claimed, 0, "a second run has nothing to do")
        again = StoredCandidateExtractor()
        again.version = VERSION + 1
        report = self.drain(built, ExtractionService(LocalProcessingClient(built.processing), again, self.clock, worker_id="v4"))
        self.assertEqual((report.submitted, set(report.outcomes)), (9, {"unchanged"}), "processing the cleaned text again changes nothing")
        self.assertEqual(self.rows(identities), before)


if __name__ == "__main__":
    unittest.main()
