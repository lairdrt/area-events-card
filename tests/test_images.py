"""Event image selection: usable photos only, a documented priority order, provenance, no new matching."""
import unittest
from datetime import datetime, timedelta, timezone

from area_events.enrichment import images
from area_events.discovery.core import finish_event, make_event
from area_events.discovery.merge import merge_events

PDT = timezone(timedelta(hours=-7))
START = datetime(2030, 1, 25, 19, 0, tzinfo=PDT)
PUBLISHERS = {"a": "a.example", "b": "b.example", "c": "c.example"}

KPBS_RESIZED = ("https://cdn.kpbs.org/dims4/default/fe3e92a/2147483647/strip/true/crop/876x460+23+0/resize/1200x630!/quality/90/"
                "?url=http%3A%2F%2Fkpbs-brightspot.s3.us-west-2.amazonaws.com%2F20%2F13%2F8b05%2Fonesmallstep.png")


def record(key, title="Chelsea Wolfe", image=None, venue="The Magnolia", start=START):
    event = make_event(key, key.upper(), f"https://{key}.example/events", f"https://{key}.example/{title.replace(' ', '-')}", title, start,
                       local_id=title, venue=venue, image_url=image, image_source="listing" if image else None)
    return finish_event(event, "2030-01-01T00:00:00-08:00", [f"{key} says so"], False)


class Judging(unittest.TestCase):
    def test_ordinary_photo_addresses_are_usable(self):
        for url in ("https://media.sandiegoreader.com/img/events/2026/wings.jpg", KPBS_RESIZED,
                    "https://localist-images.azureedge.net/photos/50959692926893/huge/2ba6bbee.jpg",
                    "https://assets0.dostuffmedia.com/uploads/a/b/c", "http://example.org/photo.png?width=1200"):
            self.assertEqual(images.judge(url), (True, ""), url)

    def test_an_image_resizer_path_called_default_is_not_a_placeholder(self):
        self.assertTrue(images.judge(KPBS_RESIZED)[0])  # "dims4/default/" is the CDN, the image is onesmallstep.png

    def test_placeholders_pixels_logos_and_malformed_addresses_are_refused_with_a_reason(self):
        refused = {
            "https://media.sandiegoreader.com/img/events_default_icon.png": "placeholder",
            "https://x.example/images/no-image.jpg": "placeholder",
            "https://x.example/img/placeholder_event.png": "placeholder",
            "https://x.example/img/image-coming-soon.jpg": "placeholder",
            "https://x.example/t/1x1.gif": "tracking pixel",
            "https://x.example/img/spacer.gif": "tracking pixel",
            "https://x.example/p?w=1&h=1": "tracking pixel",
            "https://media.sandiegoreader.com/img/events/2026/NEW_Formatted_Rooftop_Cinema_Club_logo_for_San_Diego_Reader_pYU0z5J.jpg": "logo or site graphic",
            "https://x.example/favicon.ico": "logo or site graphic",
            "https://x.example/assets/site-icon.png": "logo or site graphic",
            "https://x.example/graphic.svg": "icon or animation, not a photo",
            "data:image/png;base64,AAAA": "not an http(s) address",
            "javascript:alert(1)": "not an http(s) address",
            "ftp://x.example/a.jpg": "not an http(s) address",
            "https://x.example/a b.jpg": "malformed address",
            "": "empty", None: "empty",
        }
        for url, reason in refused.items():
            ok, why = images.judge(url)
            self.assertFalse(ok, url)
            self.assertEqual(why, reason, url)
        self.assertEqual(images.judge("https://x.example/" + "a" * 2100)[1], "malformed address")

    def test_a_sized_svg_or_gif_name_without_size_hint_is_the_only_exclusion_for_those_types(self):
        self.assertTrue(images.judge("https://x.example/art/800x600/banner.gif")[0])


class Choosing(unittest.TestCase):
    def test_the_events_own_image_is_tier_one(self):
        choice = images.choose([record("a", image="https://a.example/own.jpg"), record("b", image="https://b.example/other.jpg")])
        self.assertEqual((choice["url"], choice["tier"], choice["kind"], choice["source"]), ("https://a.example/own.jpg", 1, "event", "A"))
        self.assertEqual(choice["alternates"], ["https://b.example/other.jpg"])

    def test_a_matched_record_supplies_the_image_when_the_primary_has_none_or_a_bad_one(self):
        for primary in (None, "https://a.example/images/no-image.jpg", "https://a.example/logo.png"):
            choice = images.choose([record("a", image=primary), record("b", image="https://b.example/photo.jpg")])
            self.assertEqual((choice["url"], choice["tier"], choice["kind"], choice["source"]), ("https://b.example/photo.jpg", 2, "matched", "B"), primary)

    def test_among_matched_images_the_largest_wins_with_ties_to_the_earlier_source(self):
        small, large = "https://b.example/img/400x300/s.jpg", "https://c.example/img/1200x630/l.jpg"
        self.assertEqual(images.choose([record("a"), record("b", image=small), record("c", image=large)])["url"], large)
        first, second = "https://b.example/one.jpg", "https://c.example/two.jpg"
        self.assertEqual(images.choose([record("a"), record("b", image=first), record("c", image=second)])["url"], first)

    def test_nothing_usable_means_no_photo_and_the_reasons_are_kept(self):
        choice = images.choose([record("a", image="https://a.example/logo.png"), record("b")])
        self.assertEqual((choice["url"], choice["tier"], choice["kind"], choice["alternates"]), (None, None, None, []))
        self.assertEqual(choice["skipped"], [{"url": "https://a.example/logo.png", "source": "A", "reason": "logo or site graphic"}])

    def test_no_organizer_or_venue_image_is_ever_invented(self):
        choice = images.choose([record("a", venue="Balboa Theatre")])
        self.assertIsNone(choice["url"])


class Merging(unittest.TestCase):
    """The choice flows into merged events without changing which records are grouped."""

    def merge(self, **kinds):
        records = {key: [record(key, image=image)] for key, image in kinds.items()}
        return merge_events(records, PUBLISHERS)

    def test_matched_sources_provide_the_photo_and_the_provenance(self):
        merged = self.merge(a=None, b="https://b.example/photo.jpg")
        self.assertEqual(len(merged), 1)  # the same grouping as before
        event = merged[0]
        self.assertEqual((event["image_url"], event["images"]), ("https://b.example/photo.jpg", ["https://b.example/photo.jpg"]))
        self.assertEqual(event["image_provenance"], {"tier": 2, "kind": "matched", "source": "B", "skipped": []})
        self.assertEqual(event["image_source"], "B: listing")
        self.assertNotIn("image_url", event["verification"]["missing"])

    def test_the_own_image_is_kept_and_the_other_remains_a_fallback(self):
        event = self.merge(a="https://a.example/own.jpg", b="https://b.example/photo.jpg")[0]
        self.assertEqual((event["image_url"], event["images"]), ("https://a.example/own.jpg", ["https://a.example/own.jpg", "https://b.example/photo.jpg"]))
        self.assertEqual(event["image_provenance"]["tier"], 1)

    def test_an_event_without_a_usable_photo_reports_it(self):
        event = self.merge(a="https://a.example/images/no-image.png", b=None)[0]
        self.assertEqual((event["image_url"], event["images"], event["image_source"]), (None, [], None))
        self.assertIn("image_url", event["verification"]["missing"])
        self.assertEqual(event["image_provenance"]["skipped"][0]["reason"], "placeholder")


class SharedBanners(unittest.TestCase):
    def event(self, title, *urls):
        return {"title": title, "image_url": urls[0] if urls else None, "images": list(urls), "image_source": "Localist: photo" if urls else None,
                "image_provenance": {"tier": 1, "kind": "event", "source": "Localist", "skipped": []}, "verification": {"missing": []}}

    def test_one_picture_on_many_differently_named_events_is_a_banner_not_a_photo(self):
        banner = "https://cdn.example/photos/department-banner.jpg"
        events = [self.event(f"Committee meeting {i}", banner) for i in range(4)] + [self.event("Piano recital", "https://cdn.example/photos/recital.jpg")]
        self.assertEqual(images.drop_overused(events), 1)
        for event in events[:4]:
            self.assertEqual((event["image_url"], event["images"]), (None, []))
            self.assertEqual(event["image_provenance"]["skipped"][0]["reason"], "shared by unrelated events")
            self.assertIn("image_url", event["verification"]["missing"])
        self.assertEqual(events[4]["image_url"], "https://cdn.example/photos/recital.jpg")

    def test_a_recurring_event_may_repeat_its_own_photo(self):
        poster = "https://cdn.example/photos/market.jpg"
        events = [self.event("Farmers Market", poster) for _ in range(12)]
        self.assertEqual(images.drop_overused(events), 0)
        self.assertTrue(all(event["image_url"] == poster for event in events))

    def test_a_better_photo_is_promoted_when_the_shared_one_is_dropped(self):
        banner, photo = "https://cdn.example/banner.jpg", "https://cdn.example/real.jpg"
        events = [self.event(f"Session {i}", banner) for i in range(3)] + [self.event("Special lecture", banner, photo)]
        images.drop_overused(events)
        self.assertEqual((events[3]["image_url"], events[3]["images"]), (photo, [photo]))
        self.assertEqual(events[3]["image_provenance"]["kind"], "matched")
        self.assertIsNone(events[3]["image_source"])


if __name__ == "__main__":
    unittest.main()
