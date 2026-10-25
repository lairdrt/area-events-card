"""Offline HTML fixtures for the active City of San Diego producer tests."""
from datetime import timedelta

from area_events.common.timeutil import now_pacific

CITY_URL = "https://www.sandiego.gov/events"


def city_listing(items):
    blocks = []
    for slug, title, days in items:
        moment = now_pacific() + timedelta(days=days)
        blocks.append(f'<div><div class="grid-x grid-margin-x event-listing-large"><h2>{title}</h2><p class="post__date"><strong>{moment:%A}, {moment:%B} {moment.day}, {moment.year}, '
                      f'10:00 AM - 2:00 PM</strong></p><p>About {title}.</p><p><a href="/event/{slug}">More</a></p></div></div>')
    return "<html><body>" + "".join(blocks) + "</body></html>"


def city_detail(location, directions):
    return f"<html><main><p>page</p> Location: {location} --> {directions} Get Directions </main></html>"
