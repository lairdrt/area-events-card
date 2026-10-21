"""Runs the Home Assistant glue (config flow, setup, coordinator, WebSocket command) against minimal stand-ins.

The stand-ins implement only what the glue calls, shaped after the Home Assistant core source and developer documentation.
They show the glue is internally consistent (no typos, correct step and abort logic, the right arguments); they do NOT
prove it works inside a real Home Assistant, which must be checked on the device.
"""
import asyncio
import dataclasses
import importlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from datetime import timedelta
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components", "area_events")


def module(name, **attributes):
    created = types.ModuleType(name)
    created.__dict__.update(attributes)
    sys.modules[name] = created
    return created


class Subscriptable:
    def __class_getitem__(cls, item):
        return cls


class FakeConfigFlow:
    def __init_subclass__(cls, domain=None, **kwargs):
        cls.domain = domain

    def __init__(self):
        self.hass, self.context, self.unique_id, self.entries = types.SimpleNamespace(), {}, None, []
        self.confirm_only = False

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self, updates=None, reload_on_update=True):
        for entry in self.entries:
            if entry.unique_id == self.unique_id:
                if updates:
                    entry.data = {**entry.data, **updates}
                raise AbortFlow("already_configured")

    def _set_confirm_only(self):
        self.confirm_only = True

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_abort(self, reason):
        return {"type": "abort", "reason": reason}

    def _get_reauth_entry(self):
        return self.reauth_entry

    def async_update_reload_and_abort(self, entry, data_updates=None, **kwargs):
        entry.data = {**entry.data, **(data_updates or {})}
        return {"type": "abort", "reason": "reauth_successful"}


class AbortFlow(Exception):
    def __init__(self, reason):
        self.reason = reason


class FakeCoordinator(Subscriptable):
    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass, self.config_entry, self.name, self.update_interval = hass, config_entry, name, update_interval
        self.listeners, self.refreshes = [], 0

    def async_add_listener(self, callback):
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    async def async_config_entry_first_refresh(self):
        await self._async_update_data()

    async def async_request_refresh(self):
        self.refreshes += 1


class ConfigEntryAuthFailed(Exception):
    pass


class ConfigEntryNotReady(Exception):
    pass


class UpdateFailed(Exception):
    pass


class FakeVol:
    """voluptuous is not installed here; the schema is only declared, never evaluated."""

    class Schema:
        def __init__(self, schema):
            self.schema = schema

    Required = Optional = staticmethod(lambda key, default=None: key)
    All = staticmethod(lambda *args: args)
    Length = Range = Coerce = staticmethod(lambda *args, **kwargs: None)


@dataclasses.dataclass
class HassioServiceInfo:
    config: dict
    name: str = "Area Events"
    slug: str = "abc12345_area_events"
    uuid: str = "0" * 32


def install_stand_ins():
    registered = []
    websocket_api = module(
        "homeassistant.components.websocket_api",
        websocket_command=lambda schema: (lambda func: setattr(func, "schema", schema) or func),
        async_response=lambda func: func,
        async_register_command=lambda hass, handler: registered.append(handler),
        ActiveConnection=object,
    )
    module("homeassistant")
    components = module("homeassistant.components", websocket_api=websocket_api)
    components.frontend = module("homeassistant.components.frontend", add_extra_js_url=mock.Mock())
    components.http = module("homeassistant.components.http", StaticPathConfig=lambda *args, **kwargs: (args, kwargs))
    module("homeassistant.config_entries", ConfigFlow=FakeConfigFlow, ConfigFlowResult=dict, ConfigEntry=Subscriptable,
           ConfigEntryState=types.SimpleNamespace(LOADED="loaded"))
    module("homeassistant.core", HomeAssistant=object, callback=lambda func: func)
    module("homeassistant.exceptions", ConfigEntryAuthFailed=ConfigEntryAuthFailed, ConfigEntryNotReady=ConfigEntryNotReady)
    helpers = module("homeassistant.helpers")
    helpers.aiohttp_client = module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: "session")
    helpers.config_validation = module("homeassistant.helpers.config_validation", config_entry_only_config_schema=lambda domain: ("schema", domain))
    helpers.typing = module("homeassistant.helpers.typing", ConfigType=dict)
    helpers.update_coordinator = module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=FakeCoordinator, UpdateFailed=UpdateFailed)
    helpers.service_info = module("homeassistant.helpers.service_info")
    helpers.service_info.hassio = module("homeassistant.helpers.service_info.hassio", HassioServiceInfo=HassioServiceInfo)
    module("voluptuous", **{k: getattr(FakeVol, k) for k in ("Schema", "Required", "Optional", "All", "Length", "Range", "Coerce")})
    return registered


REGISTERED = install_stand_ins()
package = types.ModuleType("area_events_glue")
package.__path__ = [ROOT]
sys.modules["area_events_glue"] = package
client = importlib.import_module("area_events_glue.client")
flow_module = importlib.import_module("area_events_glue.config_flow")
spec = importlib.util.spec_from_file_location("area_events_glue_init", os.path.join(ROOT, "__init__.py"), submodule_search_locations=[ROOT])
init = importlib.util.module_from_spec(spec)
init.__package__ = "area_events_glue"
sys.modules["area_events_glue_init"] = init
spec.loader.exec_module(init)
coordinator_module = importlib.import_module("area_events_glue.coordinator")
websocket_module = importlib.import_module("area_events_glue.websocket_api")

TOKEN = "k" * 43


def run(coroutine):
    return asyncio.run(coroutine)


class Entry:
    def __init__(self, data=None, unique_id="area_events"):
        self.data, self.unique_id, self.state, self.runtime_data = data or {}, unique_id, "loaded", None
        self.unloads, self.started_reauth, self.tasks = [], 0, []

    def async_on_unload(self, remove):
        self.unloads.append(remove)

    def async_start_reauth(self, hass):
        self.started_reauth += 1

    def async_create_background_task(self, hass, coroutine, name):
        coroutine.close()
        self.tasks.append(name)


class ConfigFlowSteps(unittest.TestCase):
    def flow(self, status=None, entries=()):
        flow = flow_module.AreaEventsConfigFlow()
        flow.entries = list(entries)
        calls = []

        class Client:
            def __init__(self, session, url, token):
                calls.append((url, token))

            async def status(self):
                if status:
                    raise status
                return {}

        flow_module.AreaEventsClient = Client
        flow.calls = calls
        return flow

    def test_the_app_is_found_confirmed_and_set_up_without_typing_anything(self):
        flow = self.flow()
        shown = run(flow.async_step_hassio(HassioServiceInfo({"host": "abc12345-area-events", "port": 8765, "token": TOKEN})))
        self.assertEqual((shown["type"], shown["step_id"], flow.confirm_only), ("form", "hassio_confirm", True))
        self.assertEqual(flow.unique_id, "area_events")
        created = run(flow.async_step_hassio_confirm({}))
        self.assertEqual(created["type"], "create_entry")
        self.assertEqual(created["data"], {"url": "http://abc12345-area-events:8765", "token": TOKEN})

    def test_a_restarted_app_updates_the_existing_entry_without_asking(self):
        entry = Entry({"url": "http://old-host:8765", "token": "old"})
        flow = self.flow(entries=[entry])
        with self.assertRaises(AbortFlow) as aborted:
            run(flow.async_step_hassio(HassioServiceInfo({"host": "new-host", "port": 8765, "token": TOKEN})))
        self.assertEqual(aborted.exception.reason, "already_configured")
        self.assertEqual(entry.data, {"url": "http://new-host:8765", "token": TOKEN})

    def test_incomplete_or_unreachable_discovery_aborts_clearly(self):
        self.assertEqual(run(self.flow().async_step_hassio(HassioServiceInfo({"host": "x"}))), {"type": "abort", "reason": "invalid_discovery"})
        flow = self.flow(status=client.AreaEventsConnectionError("down"))
        run(flow.async_step_hassio(HassioServiceInfo({"host": "x", "port": 8765, "token": TOKEN})))
        self.assertEqual(run(flow.async_step_hassio_confirm({})), {"type": "abort", "reason": "cannot_connect"})

    def test_manual_setup_reports_wrong_address_or_token(self):
        flow = self.flow(status=client.AreaEventsAuthError("no"))
        self.assertEqual(run(flow.async_step_user({"url": "http://host:8765", "token": "x"}))["errors"], {"base": "invalid_auth"})
        self.assertEqual(run(flow.async_step_user({"url": "nonsense", "token": "x"}))["errors"], {"url": "invalid_url"})
        ok = self.flow()
        self.assertEqual(run(ok.async_step_user({"url": "HTTP://Host:8765/", "token": TOKEN}))["data"], {"url": "http://host:8765", "token": TOKEN})
        with self.assertRaises(AbortFlow):
            run(self.flow(entries=[Entry()]).async_step_user({"url": "http://host:8765", "token": TOKEN}))

    def test_reauthentication_replaces_only_the_token(self):
        flow = self.flow()
        flow.reauth_entry = Entry({"url": "http://host:8765", "token": "old"})
        result = run(flow.async_step_reauth_confirm({"token": TOKEN}))
        self.assertEqual((result["reason"], flow.reauth_entry.data), ("reauth_successful", {"url": "http://host:8765", "token": TOKEN}))
        bad = self.flow(status=client.AreaEventsAuthError("no"))
        bad.reauth_entry = Entry({"url": "http://host:8765", "token": "old"})
        self.assertEqual(run(bad.async_step_reauth_confirm({"token": "x"}))["errors"], {"base": "invalid_auth"})


class Setup(unittest.TestCase):
    def test_the_card_is_served_and_the_command_registered_once(self):
        paths = []

        async def register(configs):
            paths.extend(configs)

        hass = types.SimpleNamespace(http=types.SimpleNamespace(async_register_static_paths=register), async_add_executor_job=lambda func: asyncio.sleep(0, func()))
        self.assertTrue(run(init.async_setup(hass, {})))
        self.assertEqual(paths[0][0][0], "/area_events/area-events-card.js")
        self.assertTrue(paths[0][0][1].endswith("area-events-card.js"))
        self.assertIn(websocket_module.ws_events, REGISTERED)
        url = sys.modules["homeassistant.components.frontend"].add_extra_js_url.call_args[0][1]
        self.assertTrue(url.startswith("/area_events/area-events-card.js?v="))
        self.assertTrue(os.path.exists(os.path.join(ROOT, "frontend", "area-events-card.js")), "the built card ships with the integration")

    def test_the_card_script_is_registered_after_its_file_is_served_and_follows_the_file(self):
        """Registration lifecycle: the static path exists before the page can reference it, it is never cached stale, and a changed file gets a new URL."""
        order, urls = [], []

        async def register(configs):
            order.append(("static", configs))

        announce = lambda hass, url: (order.append(("script", url)), urls.append(url))
        hass = types.SimpleNamespace(http=types.SimpleNamespace(async_register_static_paths=register), async_add_executor_job=lambda func: asyncio.sleep(0, func()))
        with tempfile.TemporaryDirectory() as folder:
            card = pathlib.Path(folder, "area-events-card.js")
            card.write_text("// v1")
            with mock.patch.object(init, "CARD_FILE", card), mock.patch.object(init, "add_extra_js_url", announce):
                os.utime(card, ns=(1_000_000_000, 1_000_000_000))
                self.assertTrue(run(init.async_setup(hass, {})))
                os.utime(card, ns=(2_000_000_000, 2_000_000_000))
                self.assertTrue(run(init.async_setup(hass, {})))
        self.assertEqual([kind for kind, _ in order[:2]], ["static", "script"])  # served first, announced to the frontend second
        (path, file_path), options = order[0][1][0]
        self.assertEqual((path, options), ("/area_events/area-events-card.js", {"cache_headers": False}))  # the file is revalidated, never served stale
        self.assertEqual(urls, ["/area_events/area-events-card.js?v=1000000000", "/area_events/area-events-card.js?v=2000000000"])
        self.assertTrue(all(url.split("?")[0] == path for url in urls))

    def test_the_integration_depends_on_the_frontend_so_the_script_list_exists_when_it_is_added(self):
        manifest = json.load(open(os.path.join(ROOT, "manifest.json"), encoding="utf-8"))
        self.assertIn("frontend", manifest["dependencies"])
        self.assertIn("http", manifest["dependencies"])

    def test_the_served_card_is_the_built_card_and_is_not_the_placeholder(self):
        served = open(os.path.join(ROOT, "frontend", "area-events-card.js"), encoding="utf-8").read()
        self.assertNotIn("__AREA_EVENTS_CARD_BUILD__", served)
        self.assertIn("customElements.define(CARD_TAG, AreaEventsCard)", served)
        self.assertIn("__AREA_EVENTS_CARD_LOAD__", served)

    def test_setup_connects_polls_and_keeps_the_coordinator_listening(self):
        entry = Entry({"url": "http://host:8765", "token": TOKEN})
        statuses = []

        async def status(self):
            statuses.append(1)
            return {}

        with mock.patch.object(client.AreaEventsClient, "status", status):
            self.assertTrue(run(init.async_setup_entry(types.SimpleNamespace(), entry)))
        self.assertEqual(len(statuses), 2)  # the setup check and the first refresh
        self.assertEqual(len(entry.runtime_data.listeners), 1)  # without a listener a coordinator never polls on its own
        self.assertEqual(len(entry.unloads), 1)

    def test_setup_failures_map_to_retry_and_reauth(self):
        for error, expected in ((client.AreaEventsConnectionError("down"), ConfigEntryNotReady), (client.AreaEventsAuthError("no"), ConfigEntryAuthFailed)):
            async def fail(self, error=error):
                raise error

            with mock.patch.object(client.AreaEventsClient, "status", fail):
                with self.assertRaises(expected):
                    run(init.async_setup_entry(types.SimpleNamespace(), Entry({"url": "http://host:8765", "token": TOKEN})))


class Coordinator(unittest.TestCase):
    def make(self, hub):
        entry = Entry()
        coordinator = coordinator_module.AreaEventsCoordinator(types.SimpleNamespace(), entry, hub)
        return coordinator, entry

    def hub(self, busy=False, error=None):
        class Hub:
            profiles = 2

            def __init__(self):
                self.busy = busy

            async def refresh(self):
                if error:
                    raise error

        return Hub()

    def test_polls_quickly_only_while_something_is_being_discovered(self):
        hub = self.hub(busy=True)
        coordinator, _ = self.make(hub)
        self.assertEqual(run(coordinator._async_update_data()), {"profiles": 2})
        self.assertEqual(coordinator.update_interval, timedelta(seconds=5))
        hub.busy = False
        run(coordinator._async_update_data())
        self.assertEqual(coordinator.update_interval, timedelta(minutes=5))

    def test_errors_map_to_reauth_and_retry(self):
        for error, expected in ((client.AreaEventsAuthError("no"), ConfigEntryAuthFailed), (client.AreaEventsConnectionError("down"), UpdateFailed)):
            coordinator, _ = self.make(self.hub(error=error))
            with self.assertRaises(expected):
                run(coordinator._async_update_data())

    def test_boost_requests_one_refresh_and_only_when_not_already_fast(self):
        coordinator, entry = self.make(self.hub())
        coordinator.async_boost()
        coordinator.async_boost()
        self.assertEqual((coordinator.update_interval, entry.tasks), (timedelta(seconds=5), ["area_events boost"]))


class WebSocket(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.connection = types.SimpleNamespace(send_result=lambda i, result: self.sent.append(("result", i, result)),
                                                send_error=lambda i, code, message: self.sent.append(("error", i, code, message)))

    def hass(self, entry, home=(30.26715, -97.74306)):
        entries = [entry] if entry else []
        return types.SimpleNamespace(
            config_entries=types.SimpleNamespace(async_entries=lambda domain: entries),
            config=types.SimpleNamespace(latitude=home[0], longitude=home[1], location_name="Home", time_zone="America/Chicago", country="US"))

    def entry_with_hub(self, **hub_behaviour):
        entry = Entry()
        requests = []

        class Hub:
            busy = hub_behaviour.get("busy", False)

            async def request(self, profile, since):
                requests.append((profile, since))
                if hub_behaviour.get("error"):
                    raise hub_behaviour["error"]
                return {"state": "ready", "events": []}

        boosts = []
        entry.runtime_data = types.SimpleNamespace(hub=Hub(), async_boost=lambda: boosts.append(1))
        entry.requests, entry.boosts = requests, boosts
        return entry

    def call(self, hass, message):
        run(websocket_module.ws_events(hass, self.connection, {"id": 7, "type": "area_events/events", **message}))
        return self.sent[-1]

    def test_the_card_gets_an_answer_for_the_profile_it_described(self):
        entry = self.entry_with_hub()
        self.assertEqual(self.call(self.hass(entry), {"area": "Austin, TX", "radius_miles": 30.0, "since": "s"}), ("result", 7, {"state": "ready", "events": []}))
        self.assertEqual(entry.requests, [({"radius_miles": 30.0, "area": "Austin, TX"}, "s")])

    def test_without_an_area_the_home_location_is_the_profile(self):
        entry = self.entry_with_hub()
        self.call(self.hass(entry), {})
        self.assertEqual(entry.requests[0][0], {"radius_miles": 25.0, "latitude": 30.2672, "longitude": -97.7431, "name": "Home", "timezone": "America/Chicago"})
        self.assertEqual(self.call(self.hass(entry, home=(0, 0)), {})[2:], ("invalid_profile", "No area is set in the card and Home Assistant has no home location."))

    def test_discovery_in_progress_speeds_up_polling(self):
        entry = self.entry_with_hub(busy=True)
        self.call(self.hass(entry), {"area": "Austin, TX"})
        self.assertEqual(entry.boosts, [1])

    def test_errors_reach_the_card_with_a_code_and_a_message(self):
        self.assertEqual(self.call(self.hass(None), {})[2], "not_configured")
        refused = self.entry_with_hub(error=client.AreaEventsRequestError(422, "could not resolve location"))
        self.assertEqual(self.call(self.hass(refused), {"area": "x"})[2:], ("rejected", "could not resolve location"))
        full = self.entry_with_hub(error=client.AreaEventsRequestError(429, "too many different areas"))
        self.assertEqual(self.call(self.hass(full), {"area": "x"})[2:], ("capacity", "too many different areas"))
        denied = self.entry_with_hub(error=client.AreaEventsAuthError("no"))
        self.assertEqual(self.call(self.hass(denied), {"area": "x"})[2], "unauthorized")
        self.assertEqual(denied.started_reauth, 1)

    def test_the_command_declares_the_fields_the_card_sends(self):
        schema = websocket_module.ws_events.schema
        self.assertEqual(sorted(k for k in schema if isinstance(k, str)), ["area", "radius_miles", "since", "sources", "type"])
        self.assertEqual(schema["type"], "area_events/events")


if __name__ == "__main__":
    unittest.main()
