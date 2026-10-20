"""Source adapters. To add one, create a module here that provides:

    KEY, NAME, PUBLISHER
    COVERAGE   {"kind": "regional", "region", "latitude", "longitude", "radius_miles"},
               {"kind": "searchable"} (can search any location; receives location=) or
               {"kind": "configured"} (user-supplied; the default when omitted)
    discover(fetcher, previous, details=True, limit=None, **options) -> (events, notes)

then register it in MODULES and list its key in discovery.config.json.
`previous` holds that source's cached events from the last run; `options`
come from its config entry. Events are built with core.make_event().
"""
import importlib

MODULES = {
    "kpbs": "kpbs",
    "sandiegoreader": "sandiegoreader",
    "sandiego_gov": "sandiego_gov",
    "dosd": "dosd",
}


def load(key):
    return importlib.import_module(f".{MODULES[key]}", __name__)
