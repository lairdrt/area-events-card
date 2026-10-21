"""From a card's request to a discovery profile. No Home Assistant imports.

The card sends only what changes which events exist (area, radius, source websites); everything about how they
are shown stays in the card. Without an area the Home Assistant home location is used.
"""
import re

MAX_RADIUS_MILES = 250
MAX_SOURCES = 10
POSTAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{2,10}$")


class ProfileInputError(ValueError):
    """The request cannot be turned into a profile; the message is meant for the card's user."""


def _looks_like_postal_code(area):
    return bool(re.search(r"\d", area)) and "," not in area and bool(POSTAL.match(area)) and not re.match(r"^-?\d+(\.\d+)?\s", area)


def build_profile(request, home, country):
    """`request`: the card's area/radius_miles/sources. `home`: {latitude, longitude, name, time_zone} or None. `country`: HA's 2-letter country."""
    radius = request.get("radius_miles", 25)
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not 1 <= radius <= MAX_RADIUS_MILES:
        raise ProfileInputError(f"radius_miles must be a number from 1 to {MAX_RADIUS_MILES}")
    profile = {"radius_miles": float(radius)}
    area = request.get("area")
    if area is not None and not (isinstance(area, str) and area.strip()):
        raise ProfileInputError("area must be text such as 'San Diego, CA' (a city, a ZIP code or 'latitude, longitude')")
    if area:
        profile["area"] = " ".join(area.split())
        if _looks_like_postal_code(profile["area"]):
            if not country:
                raise ProfileInputError("a ZIP code needs a country; Home Assistant has none configured, so use a city name instead")
            profile["country"] = country
    else:
        valid = (home and isinstance(home.get("latitude"), (int, float)) and isinstance(home.get("longitude"), (int, float))
                 and abs(home["latitude"]) <= 90 and abs(home["longitude"]) <= 180 and (home["latitude"], home["longitude"]) != (0, 0))
        if not valid:
            raise ProfileInputError("No area is set in the card and Home Assistant has no home location.")
        profile.update(latitude=round(home["latitude"], 4), longitude=round(home["longitude"], 4))
        if home.get("name"):
            profile["name"] = home["name"]
        if home.get("time_zone"):
            profile["timezone"] = home["time_zone"]
    sources = request.get("sources") or []
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES or not all(isinstance(s, str) for s in sources):
        raise ProfileInputError(f"sources must be a list of at most {MAX_SOURCES} website addresses")
    if sources:
        profile["sources"] = [s.strip() for s in sources if s.strip()]
    return profile
