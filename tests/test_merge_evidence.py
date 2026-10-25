"""Evidence-based deduplication: regression for the Ocean Beach Restaurant Walk plus positive/negative cases."""
import unittest

from area_events.discovery.merge import merge_events

PUBLISHERS = {"kpbs": "kpbs.org", "sandiegoreader": "sandiegoreader.com", "dosd": "dosd.com", "city": "sandiego.gov"}

# --- real cached records (descriptions trimmed to their opening paragraphs) ---------------

KPBS_DESCRIPTION = (
    "On Tuesday, October 20, 5–9 p.m., go for a delicious stroll through Ocean Beach’s vibrant food scene! "
    "This once a year community event brings together over 30 restaurants, breweries, and wine bars — both classic "
    "local favorites and exciting new spots opening around the community! Each event ticket will reserve you a tasting "
    "card so you can enjoy all 30+ participating local restaurants, breweries, and wine bars along both Newport Ave & "
    "Voltaire St!\nFree Trolley: Multiple stops will easily take you to all the restaurants and breweries from Newport "
    "Avenue to Voltaire Street to Sunset Cliffs Blvd.\nAll proceeds support OBCF charitable giving programs. The Ocean "
    "Beach Community Foundation is a 100% volunteer operated registered 501(c)(3) community nonprofit."
)
READER_DESCRIPTION = (
    "Go for a delicious stroll through Ocean Beach’s vibrant food scene. This once a year community event brings "
    "together over 30 restaurants, breweries, and wine bars — both classic local favorites and exciting new spots "
    "opening around the community. Each event ticket will reserve you a tasting card so you can enjoy all 30+ "
    "participating local restaurants, breweries, and wine bars along both Newport Ave & Voltaire St!Free Trolley: "
    "Multiple stops will easily take you to all the restaurants and breweries from Newport Avenue to Voltaire Street to "
    "Sunset Cliffs Blvd.All proceeds support OBCF charitable giving programs. The Ocean Beach Community Foundation is a "
    "100% volunteer operated registered 501(c)(3) community nonprofit."
)


def rec(key, name, title, start, end=None, venue=None, address=None, description=None, image=None, categories=(), evidence=("e",), status="corroborated"):
    return {
        "id": f"{key}:{title}:{start}", "title": title, "start": start, "end": end, "timezone": "America/Los_Angeles",
        "venue": venue, "address": address, "description": description, "image_url": image,
        "image_source": "event page" if image else None, "url": f"https://{key}.example/{title.replace(' ', '-')}-{start}",
        "categories": list(categories), "price": None, "source": {"name": name, "listing_url": f"https://{key}.example/"},
        "retrieved_at": "2026-10-20T10:00:00-07:00",
        "verified": False, "verification": {"status": status, "evidence": list(evidence), "missing": []},
    }


def ob_records():
    walk = dict(start="2026-10-20T17:00:00-07:00", end="2026-10-20T21:00:00-07:00")
    return {
        "kpbs": [rec("kpbs", "KPBS", "Ocean Beach Restaurant Walk 2026", venue="Northside Tavern", address="2228 Bacon St, San Diego",
                     description=KPBS_DESCRIPTION, image="https://k.example/walk.jpg", categories=["Food / Drinks"], **walk)],
        "sandiegoreader": [rec("sandiegoreader", "San Diego Reader", "Ocean Beach Restaurant Walk", venue="Ocean Beach: Newport Ave & Voltaire St",
                               description=READER_DESCRIPTION, image="https://r.example/ob_walk.jpg", **walk)],
        "dosd": [rec("dosd", "DoSD", "Ocean Beach Restaurant Walk 2026", venue="Ocean Beach", address="1950 Abbott St, San Diego",
                     image="https://d.example/walk.jpg", categories=["Food / Drink"], **walk)],
    }


def titles(merged):
    return sorted(e["title"] for e in merged)


class OceanBeachRegression(unittest.TestCase):
    def test_three_listings_of_the_walk_become_one_event(self):
        merged = merge_events(ob_records(), PUBLISHERS)
        self.assertEqual(len(merged), 1)
        event = merged[0]
        self.assertEqual({s["name"] for s in event["sources"]}, {"KPBS", "San Diego Reader", "DoSD"})
        self.assertEqual(len(event["sources"]), 3)
        self.assertEqual(sorted(event["verification"]["independent_publishers"]), ["dosd.com", "kpbs.org", "sandiegoreader.com"])

    def test_kpbs_and_reader_alone_match_on_description_despite_different_venues(self):
        records = ob_records()
        merged = merge_events({"kpbs": records["kpbs"], "sandiegoreader": records["sandiegoreader"]}, PUBLISHERS)
        self.assertEqual(len(merged), 1)
        self.assertTrue(any("description" in line for line in merged[0]["verification"]["match_reasons"]))

    def test_kpbs_and_dosd_alone_match_because_the_area_is_named_in_the_title(self):
        records = ob_records()
        merged = merge_events({"kpbs": records["kpbs"], "dosd": records["dosd"]}, PUBLISHERS)
        self.assertEqual(len(merged), 1)

    def test_merge_keeps_the_richest_metadata_and_all_provenance(self):
        event = merge_events(ob_records(), PUBLISHERS)[0]
        self.assertEqual(event["description"], KPBS_DESCRIPTION)  # the longest
        self.assertEqual(event["address"], "2228 Bacon St, San Diego")
        self.assertTrue(event["image_url"])
        self.assertEqual(sorted(event["images"]), ["https://d.example/walk.jpg", "https://k.example/walk.jpg", "https://r.example/ob_walk.jpg"])
        self.assertEqual(len({s["url"] for s in event["sources"]}), 3)
        for source in event["sources"]:
            self.assertTrue(source["retrieved_at"])
        self.assertIn("Food / Drinks", event["categories"])
        # a description that lost to the longest one is still kept under its source
        reader = next(s for s in event["sources"] if s["name"] == "San Diego Reader")
        self.assertEqual(reader["description"], READER_DESCRIPTION)


class NegativeCases(unittest.TestCase):
    def merge(self, **by_source):
        return merge_events(by_source, PUBLISHERS)

    def test_separate_showtimes_stay_separate(self):
        a = rec("kpbs", "KPBS", "Wings Over Water", "2026-10-20T12:00:00-07:00", venue="Fleet Science Center", address="1875 El Prado")
        b = rec("dosd", "DoSD", "Wings Over Water", "2026-10-20T14:00:00-07:00", venue="Fleet Science Center", address="1875 El Prado")
        self.assertEqual(len(self.merge(kpbs=[a], dosd=[b])), 2)

    def test_different_dates_stay_separate(self):
        a = rec("kpbs", "KPBS", "Ocean Beach Restaurant Walk", "2026-10-20T17:00:00-07:00", end="2026-10-20T21:00:00-07:00", description=KPBS_DESCRIPTION)
        b = rec("dosd", "DoSD", "Ocean Beach Restaurant Walk", "2026-10-27T17:00:00-07:00", end="2026-10-27T21:00:00-07:00", description=KPBS_DESCRIPTION)
        self.assertEqual(len(self.merge(kpbs=[a], dosd=[b])), 2)

    def test_different_acts_at_the_same_venue_and_time_stay_separate(self):
        a = rec("dosd", "DoSD", "Chat Pile", "2026-10-22T19:00:00-07:00", venue="House of Blues", address="1055 5th Ave")
        b = rec("sandiegoreader", "San Diego Reader", "Post Sex Nachos", "2026-10-22T19:00:00-07:00", venue="House of Blues", address="1055 5th Ave")
        self.assertEqual(len(self.merge(dosd=[a], sandiegoreader=[b])), 2)

    def test_generic_titles_need_the_venue_to_agree(self):
        farmers = lambda key, name, venue, addr: rec(key, name, "Farmers Market", "2026-10-21T09:00:00-07:00", end="2026-10-21T13:00:00-07:00", venue=venue, address=addr)
        apart = self.merge(kpbs=[farmers("kpbs", "KPBS", "Hillcrest Plaza", "3960 Normal St")], dosd=[farmers("dosd", "DoSD", "Ocean Beach", "4900 Newport Ave")])
        self.assertEqual(len(apart), 2)
        together = self.merge(kpbs=[farmers("kpbs", "KPBS", "Hillcrest Plaza", "3960 Normal St")], dosd=[farmers("dosd", "DoSD", "Hillcrest Plaza", "3960 Normal St")])
        self.assertEqual(len(together), 1)

    def test_generic_title_with_matching_description_still_needs_a_venue(self):
        text = "A lively weekly gathering with local bands and food for the whole neighborhood to enjoy together every week."
        a = rec("kpbs", "KPBS", "Live Music", "2026-10-21T19:00:00-07:00", venue="Bar One", address="1 A St", description=text)
        b = rec("dosd", "DoSD", "Live Music", "2026-10-21T19:00:00-07:00", venue="Bar Two", address="2 B St", description=text)
        self.assertEqual(len(self.merge(kpbs=[a], dosd=[b])), 2)

    def test_same_title_and_time_but_conflicting_venues_without_other_evidence_stay_separate(self):
        a = rec("kpbs", "KPBS", "Halloween Costume Party", "2026-10-31T20:00:00-07:00", venue="The Rooftop", address="10 Main St")
        b = rec("dosd", "DoSD", "Halloween Costume Party", "2026-10-31T20:00:00-07:00", venue="Balboa Theatre", address="868 4th Ave")
        self.assertEqual(len(self.merge(kpbs=[a], dosd=[b])), 2)

    def test_identical_title_without_description_or_area_evidence_stays_separate(self):
        a = rec("kpbs", "KPBS", "Spring Wine Walk", "2026-10-24T17:00:00-07:00", end="2026-10-24T20:00:00-07:00", venue="Town A")
        b = rec("dosd", "DoSD", "Spring Wine Walk", "2026-10-24T17:00:00-07:00", end="2026-10-24T20:00:00-07:00", venue="Town B")
        self.assertEqual(len(self.merge(kpbs=[a], dosd=[b])), 2)

    def test_missing_times_or_ends_do_not_manufacture_matches(self):
        a = rec("kpbs", "KPBS", "Ocean Beach Restaurant Walk", "2026-10-20T17:00:00-07:00", venue="Northside Tavern")
        b = rec("dosd", "DoSD", "Ocean Beach Restaurant Walk", "2026-10-20T17:00:00-07:00", venue="Ocean Beach")
        self.assertEqual(len(self.merge(kpbs=[a], dosd=[b])), 2)  # no end and no description: not enough evidence

    def test_one_source_never_merges_with_itself(self):
        records = ob_records()["kpbs"] * 2
        self.assertEqual(len(self.merge(kpbs=records)), 2)

    def test_a_group_never_holds_two_records_from_one_source(self):
        r = ob_records()
        second_reader = rec("sandiegoreader", "San Diego Reader", "Ocean Beach Restaurant Walk", "2026-10-20T17:00:00-07:00",
                            end="2026-10-20T21:00:00-07:00", venue="Ocean Beach", description=READER_DESCRIPTION)
        merged = self.merge(kpbs=r["kpbs"], sandiegoreader=r["sandiegoreader"] + [second_reader])
        self.assertEqual(sorted(len(e["sources"]) for e in merged), [1, 2])


class PositiveCases(unittest.TestCase):
    def merge(self, **by_source):
        return merge_events(by_source, PUBLISHERS)

    def test_year_and_annual_suffixes_are_ignored(self):
        a = rec("kpbs", "KPBS", "62nd Annual Chula Vista Starlight Parade", "2026-12-05T15:00:00-08:00", venue="3rd Avenue from E - H St", address="Third Avenue, Chula Vista")
        b = rec("dosd", "DoSD", "Chula Vista Starlight Parade 2026", "2026-12-05T15:00:00-08:00", venue="3rd Avenue from E - H St", address="Third Avenue, Chula Vista")
        self.assertEqual(len(self.merge(kpbs=[a], dosd=[b])), 1)

    def test_punctuation_and_curly_apostrophes_are_ignored(self):
        a = rec("kpbs", "KPBS", "Jeff Ousley's Live Music Showcase", "2026-10-22T18:00:00-07:00", venue="Humphreys Backstage Music Club")
        b = rec("dosd", "DoSD", "Jeff Ousley’s Live Music Showcase!", "2026-10-22T18:00:00-07:00", venue="Humphreys Backstage Live")
        self.assertEqual(len(self.merge(kpbs=[a], dosd=[b])), 1)

    def test_same_venue_and_lineup_extension_merge(self):
        a = rec("sandiegoreader", "San Diego Reader", "Post Sex Nachos", "2026-10-22T19:00:00-07:00", venue="House of Blues")
        b = rec("dosd", "DoSD", "Post Sex Nachos, Hannah Geller", "2026-10-22T19:00:00-07:00", venue="House of Blues - Voodoo Room")
        self.assertEqual(len(self.merge(sandiegoreader=[a], dosd=[b])), 1)

    def test_doors_vs_show_time_at_the_same_venue_merge(self):
        a = rec("sandiegoreader", "San Diego Reader", "Faux Real", "2026-10-20T19:30:00-07:00", venue="Soda Bar", address="3615 El Cajon Blvd")
        b = rec("dosd", "DoSD", "Faux Real", "2026-10-20T20:00:00-07:00", venue="Soda Bar", address="3615 El Cajon Blvd")
        merged = self.merge(sandiegoreader=[a], dosd=[b])
        self.assertEqual(len(merged), 1)
        self.assertTrue(any("doors vs show time" in r for r in merged[0]["verification"]["match_reasons"]))
        starts = {s.get("start", merged[0]["start"]) for s in merged[0]["sources"]}
        self.assertEqual(starts, {"2026-10-20T19:30:00-07:00", "2026-10-20T20:00:00-07:00"})  # each source's own time is kept
        self.assertIn("within 30 minutes", " ".join(merged[0]["verification"]["evidence"]))

    def test_start_times_apart_need_venue_or_description_evidence(self):
        a = rec("kpbs", "KPBS", "Lecture Series Finale", "2026-11-09T18:30:00-08:00", end="2026-11-09T20:00:00-08:00", venue="Central Library", description=KPBS_DESCRIPTION)
        same_end = rec("dosd", "DoSD", "Lecture Series Finale", "2026-11-09T18:45:00-08:00", end="2026-11-09T20:00:00-08:00", venue="Elsewhere Hall", description=KPBS_DESCRIPTION)
        self.assertEqual(len(self.merge(kpbs=[a], dosd=[same_end])), 1)  # identical end plus matching description
        other_place = rec("dosd", "DoSD", "Lecture Series Finale", "2026-11-09T18:45:00-08:00", end="2026-11-09T20:30:00-08:00", venue="Elsewhere Hall")
        self.assertEqual(len(self.merge(kpbs=[a], dosd=[other_place])), 2)  # different venue, end and no description

    def test_a_source_listing_two_showtimes_blocks_the_doors_vs_show_rule(self):
        early = rec("kpbs", "KPBS", "Wings Over Water", "2026-10-20T12:00:00-07:00", venue="Fleet Science Center", address="1875 El Prado")
        late = rec("kpbs", "KPBS", "Wings Over Water", "2026-10-20T12:30:00-07:00", venue="Fleet Science Center", address="1875 El Prado")
        other = rec("dosd", "DoSD", "Wings Over Water", "2026-10-20T12:15:00-07:00", venue="Fleet Science Center", address="1875 El Prado")
        self.assertEqual(len(self.merge(kpbs=[early, late], dosd=[other])), 3)

    def test_match_reasons_are_recorded_and_publisher_independence_is_kept(self):
        a = rec("kpbs", "KPBS", "Maka Roots", "2026-10-21T19:00:00-07:00", venue="Holding Company", address="2122 India St")
        b = rec("kpbs2", "KPBS Again", "Maka Roots", "2026-10-21T19:00:00-07:00", venue="Holding Company", address="2122 India St")
        merged = merge_events({"kpbs": [a], "kpbs2": [b]}, {**PUBLISHERS, "kpbs2": "kpbs.org"})
        self.assertEqual(len(merged), 1)
        self.assertFalse(merged[0]["verified"])  # one publisher, two pages
        self.assertTrue(merged[0]["verification"]["match_reasons"])


class Scale(unittest.TestCase):
    def test_candidate_selection_is_bounded(self):
        import area_events.discovery.merge as merge_module
        calls = []
        original = merge_module.evaluate_pair
        merge_module.evaluate_pair = lambda a, b, *args, **kw: (calls.append(1), original(a, b, *args, **kw))[1]
        try:
            many = {key: [rec(key, key, f"Event {i}", f"2026-10-{20 + i % 9:02d}T{10 + i % 8:02d}:00:00-07:00", venue=f"V{i}") for i in range(300)]
                    for key in ("kpbs", "dosd")}
            merge_events(many, PUBLISHERS)
        finally:
            merge_module.evaluate_pair = original
        self.assertLess(len(calls), 600 * 600 // 20)  # far fewer than all pairs


if __name__ == "__main__":
    unittest.main()
