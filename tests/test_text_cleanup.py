"""HTML entities that sources leave in listings are decoded once, strictly, in the fields people read, and nothing else changes."""
import unittest
from datetime import datetime, timedelta, timezone

from discovery import refresh, registry
from discovery.core import clean_text, finish_event, make_event, normalize_event_text


class CleanText(unittest.TestCase):
    def test_named_numeric_and_hexadecimal_entities(self):
        cases = {
            "Porto Vista Hotel &amp; Suites": "Porto Vista Hotel & Suites",
            "Rock &amp; Roll": "Rock & Roll",
            "Caf&eacute; Rio": "Café Rio",
            "It&#39;s &#x27;open&#X27;": "It's 'open'",
            "&quot;Quoted&quot; &lsquo;a&rsquo; &ndash; &mdash; &hellip;": "\"Quoted\" ‘a’ – — …",
            "5 &lt; 6 &gt; 4": "5 < 6 > 4",
            "Fish&nbsp;&amp;&nbsp;Chips": "Fish\xa0&\xa0Chips",
            "&copy; 2026 &euro;5": "© 2026 €5",
        }
        for raw, expected in cases.items():
            with self.subTest(raw):
                self.assertEqual(clean_text(raw), expected)

    def test_double_encoded_entities_are_decoded_twice_and_no_more(self):
        self.assertEqual(clean_text("Hotel &amp;amp; Suites"), "Hotel & Suites")
        self.assertEqual(clean_text("It&amp;#39;s"), "It's")
        self.assertEqual(clean_text("Tom &amp;amp;amp; Jerry"), "Tom &amp; Jerry", "only one level of double encoding is undone")

    def test_valid_text_is_not_damaged_by_decoding(self):
        for text in ("Tom & Jerry", "AT&T Park", "R&B Night", "Fish & Chips", "1 < 2", "A &amp B", "Q&A", "Price: $5 &", "café"):
            with self.subTest(text):
                self.assertEqual(clean_text(text), text)

    def test_unknown_or_invalid_entities_stay_as_written(self):
        for text in ("&notanentity; here", "x&notin y", "&#0; &#xD800; &#99999999;", "&#xZZ;", "&madeup;", "&;"):
            with self.subTest(text):
                self.assertEqual(clean_text(text), text)
        self.assertEqual(clean_text("Brown &amp; Sons &notanentity; &amp; Co"), "Brown & Sons &notanentity; & Co")

    def test_the_result_is_stable_when_applied_again(self):
        for raw in ("Hotel &amp; Suites", "Hotel &amp;amp; Suites", "It&#39;s", "Tom & Jerry", "plain"):
            once = clean_text(raw)
            self.assertEqual(clean_text(once), once, raw)

    def test_markup_is_text_not_something_to_run_or_strip(self):
        self.assertEqual(clean_text("&lt;script&gt;alert(1)&lt;/script&gt;"), "<script>alert(1)</script>")
        self.assertEqual(clean_text("<b>Bold</b> &amp; plain"), "<b>Bold</b> & plain", "tags are left for the card, which shows text as text")

    def test_control_characters_go_but_ordinary_whitespace_stays(self):
        self.assertEqual(clean_text("a\x00b\x07c\x1fd\x7fe"), "abcde")
        self.assertEqual(clean_text("line one\nline two\ttab"), "line one\nline two\ttab")

    def test_values_that_are_not_text_pass_through(self):
        for value in (None, 5, 1.5, ["a"], {"a": "&amp;"}, ""):
            self.assertEqual(clean_text(value), value)


class NormalizeEvent(unittest.TestCase):
    def event(self):
        return {"title": "Rock &amp; Roll", "venue": "Porto Vista Hotel &amp; Suites", "address": "1 Main St &amp; 2nd Ave", "description": "Food &amp; drink &#8211; all night",
                "organizer": "Smith &amp; Sons", "price": "$5 &amp; up", "neighborhood": "Little Italy &amp; Core", "categories": ["Food &amp; Drink", "music"],
                "source": {"name": "Reader &amp; Co", "url": "https://x.example/?a=1&amp;b=2"}, "url": "https://x.example/e?a=1&amp;b=2",
                "image": "https://x.example/i.jpg?w=1&amp;h=2", "id": "abc&amp;def", "geo": {"latitude": 1.0, "longitude": 2.0}}

    def test_every_visible_field_is_decoded(self):
        event = normalize_event_text(self.event())
        self.assertEqual((event["title"], event["venue"], event["address"]), ("Rock & Roll", "Porto Vista Hotel & Suites", "1 Main St & 2nd Ave"))
        self.assertEqual((event["description"], event["organizer"], event["price"], event["neighborhood"]),
                         ("Food & drink – all night", "Smith & Sons", "$5 & up", "Little Italy & Core"))
        self.assertEqual(event["categories"], ["Food & Drink", "music"])
        self.assertEqual(event["source"]["name"], "Reader & Co")

    def test_web_addresses_and_identifiers_are_never_changed(self):
        before = self.event()
        event = normalize_event_text(self.event())
        for field in ("url", "image", "id", "geo"):
            self.assertEqual(event[field], before[field], field)
        self.assertEqual(event["source"]["url"], before["source"]["url"])

    def test_events_with_missing_or_odd_fields_are_fine(self):
        self.assertEqual(normalize_event_text({}), {})
        odd = normalize_event_text({"title": None, "categories": "not a list", "source": "plain", "venue": 5})
        self.assertEqual(odd, {"title": None, "categories": "not a list", "source": "plain", "venue": 5})


class Pipeline(unittest.TestCase):
    def cached(self, **fields):
        start = datetime.now(timezone.utc) + timedelta(days=2)
        event = finish_event(make_event("web_t", "Reader &amp; Co", "https://t.example", "https://t.example/e?a=1&amp;b=2", "Rock &amp; Roll", start, local_id="1"),
                             "2030-01-01T00:00:00+00:00", ["x"], False)
        event.update(fields)
        return event

    def document(self, event):
        config = {"sources": [{"id": "web_t", "name": "T", "adapter": "web", "url": "https://t.example", "coverage": {"type": "global"}}]}
        now = datetime.now(timezone.utc)
        cache = {"web_t": {"events": [event], "ok": True, "last_success_at": now.isoformat()}}
        return refresh.build_document(config, cache, now, registry.load_sources(config))["events"]

    def test_the_published_feed_has_clean_text_including_events_cached_before_the_fix(self):
        merged = self.document(self.cached(venue="Porto Vista Hotel &amp; Suites", description="Food &amp;amp; drink"))
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0]["title"], merged[0]["venue"], merged[0]["description"]), ("Rock & Roll", "Porto Vista Hotel & Suites", "Food & drink"))
        self.assertIn("&amp;", merged[0]["url"], "the web address is left exactly as the source gave it")

    def test_feed_text_with_no_entities_is_unchanged(self):
        merged = self.document(self.cached(title="Tom & Jerry at AT&T Park", venue="Café <Rio>"))
        self.assertEqual((merged[0]["title"], merged[0]["venue"]), ("Tom & Jerry at AT&T Park", "Café <Rio>"))


if __name__ == "__main__":
    unittest.main()
