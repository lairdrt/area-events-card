"""The six specific illustrations (farmers_market, carnival, car_show, halloween, christmas, street_festival) are chosen from clear subjects
in the title or description, before the broad categories, and do not capture unrelated events."""
import unittest

from area_events.classification import classify

NEW = ("farmers_market", "carnival", "car_show", "halloween", "christmas", "street_festival")


def pick(title, description="", **extra):
    return classify.classify({"title": title, "description": description, **extra})["category"]


class Selectable(unittest.TestCase):
    def test_all_six_are_in_the_catalog_with_their_own_vocabulary(self):
        for kind in NEW:
            self.assertIn(kind, classify.CATEGORIES)
            self.assertTrue(classify.LEXICON[kind], kind)
        self.assertEqual(len(classify.CATEGORIES), 17)
        self.assertEqual(classify.CATEGORIES[:11], ("music", "theater", "festival", "food", "sports", "arts", "science", "family", "community", "outdoors", "general"))

    def test_clear_subjects_get_their_illustration(self):
        cases = {
            "Hillcrest Farmers Market 2026 - University Ave": "farmers_market", "Certified Farmer's Market": "farmers_market", "Little Italy Mercato": "farmers_market",
            'South Clairemont Rec Ctr & Park "Winter" Carnival': "carnival", "Fall Carnival and Midway": "carnival", "County Fair Ferris Wheel Night": "carnival",
            "Classic Car Show at the Harbor": "car_show", "Cars & Coffee Saturday": "car_show", "Lowrider Hot Rod Meet": "car_show", "Auto Show": "car_show",
            "The Haunted Trail of Balboa Park": "halloween", "Halloween Costume Party": "halloween", "Trunk or Treat": "halloween", "Haunted House at the old mill": "halloween",
            "Christmas Market at the Plaza": "christmas", "Holiday Market": "christmas", "Tree Lighting Ceremony": "christmas", "The Nutcracker": "christmas", "Breakfast with Santa Claus": "christmas",
            "Taste of Little Italy Street Festival": "street_festival", "Gaslamp Street Fair": "street_festival", "Neighborhood Block Party": "street_festival",
        }
        for title, expected in cases.items():
            self.assertEqual(pick(title), expected, title)

    def test_a_description_alone_can_carry_a_clear_subject(self):
        self.assertEqual(pick("Saturday at the park", "Join the farmers market for fresh produce and a farmers market breakfast."), "farmers_market")
        self.assertEqual(pick("Evening of rides", "Carnival rides, a midway and a ferris wheel for the whole family."), "carnival")


class Specific_before_general(unittest.TestCase):
    def test_the_specific_subject_beats_the_broad_category_it_used_to_fall_under(self):
        self.assertEqual(pick("Farmers Market", "Fresh food, wine and music.", categories=["Food & Drink"]), "farmers_market")
        self.assertEqual(pick("Harvest Carnival", "A festival of games and food.", categories=["Festivals"]), "carnival")
        self.assertEqual(pick("Halloween Festival", "Live music and food trucks.", categories=["Festivals"]), "halloween")
        self.assertEqual(pick("Holiday Market and Festival of Lights", categories=["Holidays"]), "christmas")
        self.assertEqual(pick("Street Festival and Parade"), "street_festival")

    def test_the_broad_categories_are_unchanged(self):
        for title, expected in {"Fall Festival": "festival", "Jazz Night at Giuseppe's Restaurant": "music", "Wine tasting and cheese pairing": "food",
                                "Guided hike through the canyon": "outdoors", "Saturday morning 5k fun run": "sports", "Family storytime for toddlers": "family",
                                "Winter Concert": "music", "Day of the Dead Celebration": "festival", "Art Show Opening": "arts"}.items():
            self.assertEqual(pick(title), expected, title)


class Not_captured(unittest.TestCase):
    def test_unrelated_events_keep_their_own_category_or_the_general_one(self):
        cases = {
            "Auto repair workshop for students": "general", "Museum Holiday Closure Notice": "general", "Farm to Table Dinner": "food",
            "Pumpkin Spice Brunch": "food", "La Santa Cecilia: Los Años": "general", "Santa Barbara Symphony": "music", "Winter Quarter Advising": "general",
            "Carnegie Hall Recital": "music", "Cardiology Grand Rounds": "general", "Market Research Seminar": "general", "Monster Truck Rally": "general",
        }
        for title, expected in cases.items():
            result = pick(title)
            self.assertNotIn(result, NEW, title)
            self.assertEqual(result, expected, title)

    def test_a_movie_that_is_both_goes_to_halloween_not_christmas(self):
        self.assertEqual(pick("The Nightmare Before Christmas FREE Spooky Movie Night"), "halloween")


if __name__ == "__main__":
    unittest.main()
