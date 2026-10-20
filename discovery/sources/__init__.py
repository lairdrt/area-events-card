"""Source adapters. To add one, create a module here that provides:

    KEY, NAME, PUBLISHER
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
