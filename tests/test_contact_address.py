"""An event that takes place at the website's own establishment inherits the page's contact address.

The listing is a real excerpt of summerspastfarms.com/events.htm (tests/data): its events state no place of their own, but their text puts them at the farm ("Vendors Day at the
Farm", "on our spacious lawn area", "the gardens of Summers Past Farms"), and the page footer gives the farm's one street address. The other pages are synthetic counterexamples
in the same layout. Nothing here is specific to a site: the rule reads the page's <footer>/<address> blocks, the name that spells the website's domain, and the event's own text.
"""
import re
import unittest

from tests.test_event_detail_pages import crawl, detail_page
from tests.test_schedule_titles import EXPECTED, PAGE, SUMMERS, scan

HOME = "15602 Olde Highway 80, Flinn Springs, CA 92021"
FARM = "https://willowcreekfarm.example/events"
FOOTER = "<footer><h2>Contact Us</h2><ul><li>4410 Orchard Lane, Ramona, CA 92065</li><li>760-555-0100</li></ul><p>&copy; Willow Creek Farm 2026</p></footer>"


def farm_page(*events, footer=FOOTER, title="Events | Willow Creek Farm"):
    return f"<html><head><title>{title}</title></head><body><h1>Events</h1>{''.join(events)}{footer}</body></html>"


def event(title, day, text, extra=""):
    return f"<h4>{title}</h4><h5>Saturday December {day}, 2026</h5><p>9:00am-3:00pm</p><p>{text}</p>{extra}"


ON_SITE = event("Wreath Workshop", 5, "Make a wreath with us in the barn at the farm this season.")
LAWN = event("Holiday Market", 12, "Local makers sell holiday gifts on our big front lawn.")


def places(result):
    return {c.title: (c.venue, c.address) for c in result.candidates}


class SummersPastFarms(unittest.TestCase):
    def test_every_event_placed_at_the_farm_inherits_the_footer_address(self):
        result = scan()
        self.assertEqual(len(result.candidates), 8)
        self.assertEqual({c.starts_at: (c.title, c.ends_at, c.venue, c.address) for c in result.candidates},
                         {start: (title, end, None, HOME) for start, (title, end, _) in EXPECTED.items()}, "the address only; no venue is invented")
        for c in result.candidates:
            evidence = "\n".join(c.verification.evidence)
            self.assertIn(f"Address from the contact block (footer/address) of {PAGE}: {HOME}", evidence)
            self.assertIn("The event's own text places it at Summers Past Farms", evidence)
            self.assertIn("The event states no venue or address of its own", evidence)
            self.assertNotIn("address", c.verification.missing)

    def test_titles_times_and_ids_are_those_of_the_page_without_its_footer(self):
        bare = scan(re.sub(r"<footer.*?</footer>", "", SUMMERS, flags=re.S))
        self.assertEqual({c.address for c in bare.candidates}, {None}, "without the contact block nothing is inherited")
        self.assertEqual([(c.local_id, c.title, c.starts_at, c.ends_at) for c in scan().candidates],
                         [(c.local_id, c.title, c.starts_at, c.ends_at) for c in bare.candidates])

    def test_a_second_scan_keeps_every_id_and_adds_nothing(self):
        first = scan()
        again = scan(previous=first.candidates)
        self.assertEqual([(c.local_id, c.address) for c in again.candidates], [(c.local_id, c.address) for c in first.candidates])
        self.assertEqual(again.notes["identity_corrections_ambiguous"], 0)


class Inheritance(unittest.TestCase):
    def test_on_site_phrases_and_the_site_name(self):
        page = farm_page(ON_SITE, LAWN, event("Cider Day", 19, "Pressing apples in the orchard of Willow Creek Farm."))
        self.assertEqual(set(places(scan(page, page=FARM)).values()), {(None, "4410 Orchard Lane, Ramona, CA 92065")})

    def test_an_event_without_on_site_evidence_stays_without_a_place(self):
        page = farm_page(ON_SITE, event("Seed Talk", 12, "A talk about starting seeds indoors for spring."),
                         event("Branch Sale", 19, "Our annual sale moves to our other location in town this year."))
        got = places(scan(page, page=FARM))
        self.assertEqual(got["Wreath Workshop"], (None, "4410 Orchard Lane, Ramona, CA 92065"))
        self.assertEqual(got["Seed Talk"], (None, None))
        self.assertEqual(got["Branch Sale"], (None, None), "the owner's OTHER location is not this one")

    def test_an_off_site_event_in_prose_is_not_given_the_contact_address(self):
        page = farm_page(ON_SITE, event("Garden Club Talk", 12, "Our gardener speaks to the Alpine Garden Club at the Alpine Library."))
        self.assertEqual(places(scan(page, page=FARM))["Garden Club Talk"], (None, None))

    def test_an_event_level_location_wins_and_a_conflicting_one_stops_inheritance_on_the_page(self):
        own = farm_page(ON_SITE, event("Barn Dance", 12, "Dancing in the barn at the farm.", "<p>Location: Willow Creek Farm</p><p>Address: 4410 Orchard Lane, Ramona, CA 92065</p>"))
        got = places(scan(own, page=FARM))
        self.assertEqual(got["Barn Dance"], ("Willow Creek Farm", "4410 Orchard Lane, Ramona, CA 92065"), "the event's own statement is kept as it is")
        self.assertEqual(got["Wreath Workshop"], (None, "4410 Orchard Lane, Ramona, CA 92065"), "the same place stated by an event is no conflict")
        away = farm_page(ON_SITE, event("Garden Club Talk", 12, "Our gardener visits the club.", "<p>Location: Alpine Library</p>"))
        got = places(scan(away, page=FARM))
        self.assertEqual(got["Garden Club Talk"], ("Alpine Library", None))
        self.assertEqual(got["Wreath Workshop"], (None, None), "a page whose events state another place cannot establish one location")

    def test_a_multi_venue_page_inherits_nothing(self):
        page = farm_page(event("Harvest Fair", 5, "Fun at the farm.", "<p>Location: Town Hall</p>"), event("Moonlight Lecture", 12, "Stars at the farm.", "<p>Location: Science Museum</p>"),
                         ON_SITE)
        self.assertEqual(places(scan(page, page=FARM))["Wreath Workshop"], (None, None))
        street = farm_page(ON_SITE, event("Craft Day", 12, "Crafts at 200 Main Street, Ramona, CA 92065 and at the farm."))
        self.assertEqual(set(places(scan(street, page=FARM)).values()), {(None, None)}, "an event's text naming another street address is a conflicting location")

    def test_several_contact_addresses_or_no_site_name_inherit_nothing(self):
        two = FOOTER.replace("</ul>", "<li><address>98 Mill Road, Julian, CA 92036</address></li></ul>")
        self.assertEqual(set(places(scan(farm_page(ON_SITE, LAWN, footer=two), page=FARM)).values()), {(None, None)})
        same_twice = FOOTER + "<address>4410 Orchard Lane<br>Ramona, CA 92065</address>"
        self.assertEqual(set(places(scan(farm_page(ON_SITE, footer=same_twice), page=FARM)).values()), {(None, "4410 Orchard Lane, Ramona, CA 92065")},
                         "one address written twice (also across a line break) is one address")
        anonymous = farm_page(ON_SITE, footer=FOOTER.replace("Willow Creek Farm", "The Family"), title="Events")
        self.assertEqual(set(places(scan(anonymous, page=FARM)).values()), {(None, None)}, "no name on the page spells the website's domain")
        self.assertEqual(set(places(scan(farm_page(ON_SITE, footer="<footer><p>Open daily 9 AM – 5 PM</p></footer>"), page=FARM)).values()), {(None, None)})

    def test_a_card_with_its_own_event_page_takes_its_place_from_that_page(self):
        card = ('<ul><li class="event-item"><a href="/events/barn-dance"><h2>Barn Dance</h2><p>Date: Sat., Dec. 5, 2026</p><p>Time: 7:00pm to 9:00pm</p>'
                '<p>Dancing at the farm.</p></a></li></ul>')
        detail = detail_page("Barn Dance", "Saturday, December 5, 2026", "7:00pm to 9:00pm", location="Willow Creek Farm", address="4410 Orchard Ln, Ramona, CA")
        result, _ = crawl({FARM: farm_page(card), "https://willowcreekfarm.example/events/barn-dance": detail}, urls=(FARM,), max_depth=1, max_pages=3)
        merged, = result.candidates
        self.assertEqual((merged.venue, merged.address), ("Willow Creek Farm", "4410 Orchard Ln, Ramona, CA"))
        evidence = "\n".join(merged.verification.evidence)
        self.assertNotIn("contact block", evidence)
        self.assertNotIn("disagree", evidence, "the listing's contact address is not a second statement about this event")

    def test_the_address_never_leaks_to_another_page_or_website(self):
        other_site = "https://maplehollowfarm.example/events"
        other_page = FARM + "/winter"
        bare = lambda title: f"<html><head><title>{title}</title></head><body><h1>Events</h1>{ON_SITE}</body></html>"   # noqa: E731
        result, _ = crawl({FARM: farm_page(LAWN), other_page: bare("Winter | Willow Creek Farm"), other_site: bare("Events | Maple Hollow Farm")},
                          urls=(FARM, other_page, other_site), max_depth=0, max_pages=3)
        got = {(c.provenance.listing_url, c.title): c.address for c in result.candidates}
        self.assertEqual(got, {(FARM, "Holiday Market"): "4410 Orchard Lane, Ramona, CA 92065", (other_page, "Wreath Workshop"): None, (other_site, "Wreath Workshop"): None})


if __name__ == "__main__":
    unittest.main()
