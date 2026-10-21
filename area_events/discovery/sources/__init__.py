"""Source adapters. An adapter module provides

    discover(fetcher, previous, details=True, limit=None, source_id=..., source_name=..., url=..., **options)
        -> (events, notes)

and may set ACCEPTS_LOCATION = True (to receive `location=`) and MULTIPLE = True (more than one
registry entry may use it). Which sites to read, where they apply and how they are named lives in the
configuration's source registry (see discovery/registry.py), not in the adapters. Register a new
adapter type in ADAPTERS below, then add a registry entry for it in discovery.config.json.
`previous` holds that source's cached events from the last run. Events are built with core.make_event().
"""
import importlib

ADAPTERS = {
    "kpbs": "kpbs",
    "sandiegoreader": "sandiegoreader",
    "dosd": "dosd",
    "sandiego_gov": "sandiego_gov",
    "localist": "localist",
}


def adapter_module(kind):
    target = ADAPTERS[kind]
    return importlib.import_module(f".{target}", __name__) if isinstance(target, str) else target
