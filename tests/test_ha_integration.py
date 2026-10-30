"""Milestone 2D: the Home Assistant integration over the Discovery Service.

Two layers are tested, with different strength of evidence:
  * the integration's own logic (client, hub, feed adapter) runs against the REAL Discovery Service: its HTTP API on a loopback port, its
    SQLite database, discovery, extraction and the change feed. Only the websites are simulated (saved pages), and the Austin source is a
    fixture of the City adapter, not live Austin discovery.
  * the Home Assistant glue (config flow, setup/unload, coordinator, WebSocket commands) runs against minimal stand-ins shaped after the
    Home Assistant core API. They show the glue is consistent; they do NOT prove it works inside a real Home Assistant 2026.10.2, which must
    be checked on the device (see docs/ha-integration-2d.md)."""
import ast
import asyncio
import importlib
import importlib.util
import json
import os
import sys
import types
import unittest
from functools import wraps
from unittest import mock

from tests.db import database
from area_events import app as app_module
from area_events.api.http_api import ApiTokens
from area_events.discovery.host import HostConfig
from tests.test_discovery_control import CONTROL_TOKEN, READ_TOKEN, UrllibSession
from tests.test_extraction import PROCESS_TOKEN, Fixture
from tests.city_fixtures import CITY_URL

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components", "area_events")


# --- Home Assistant stand-ins (installed once; only what the glue calls) --------------------------------------------------

def module(name, **attributes):
    created = types.ModuleType(name)
    created.__dict__.update(attributes)
    sys.modules[name] = created
    return created


class Subscriptable:
    def __class_getitem__(cls, item):
        return cls


class AbortFlow(Exception):
    def __init__(self, reason):
        self.reason = reason


class FakeConfigFlow:
    def __init_subclass__(cls, domain=None, **kwargs):
        cls.domain = domain

    def __init__(self):
        self.hass, self.context, self.unique_id, self.entries = types.SimpleNamespace(), {}, None, []

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self, updates=None, reload_on_update=True):
        if any(entry.unique_id == self.unique_id for entry in self.entries):
            raise AbortFlow("already_configured")

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def _get_reauth_entry(self):
        return self.reauth_entry

    def async_update_reload_and_abort(self, entry, data_updates=None, **kwargs):
        entry.data = {**entry.data, **(data_updates or {})}
        return {"type": "abort", "reason": "reauth_successful"}


class FakeOptionsFlow:
    config_entry = None

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}


class FakeCoordinator(Subscriptable):
    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass, self.config_entry, self.name, self.update_interval, self.listeners = hass, config_entry, name, update_interval, []

    def async_add_listener(self, callback):
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    async def async_config_entry_first_refresh(self):
        await self._async_update_data()


class ConfigEntryAuthFailed(Exception):
    pass


class ConfigEntryNotReady(Exception):
    pass


class UpdateFailed(Exception):
    pass


class FakeVol:
    """voluptuous is not installed here; schemas are declared, never evaluated."""

    class Schema:
        def __init__(self, schema):
            self.schema = schema

    Required = Optional = staticmethod(lambda key, default=None: key)
    All = staticmethod(lambda *args: args)
    Length = Range = Coerce = In = staticmethod(lambda *args, **kwargs: None)


def install_stand_ins():
    registered = []

    def require_admin(func):
        @wraps(func)
        async def check(hass, connection, msg):
            if not connection.user.is_admin:
                connection.send_error(msg["id"], "unauthorized", "Unauthorized")
                return
            await func(hass, connection, msg)
        return check

    websocket_api = module(
        "homeassistant.components.websocket_api",
        websocket_command=lambda schema: (lambda func: setattr(func, "schema", schema) or func),
        async_response=lambda func: func, require_admin=require_admin,
        async_register_command=lambda hass, handler: registered.append(handler),
        event_message=lambda iden, event: {"id": iden, "type": "event", "event": event}, ActiveConnection=object,
    )
    module("homeassistant")
    components = module("homeassistant.components", websocket_api=websocket_api)
    components.panel_custom = module("homeassistant.components.panel_custom", async_register_panel=mock.AsyncMock())
    components.frontend = module("homeassistant.components.frontend", add_extra_js_url=mock.Mock())
    components.http = module("homeassistant.components.http", StaticPathConfig=lambda *args, **kwargs: (args, kwargs), HomeAssistantView=type("HomeAssistantView", (), {}))

    class Response:
        def __init__(self, body=None, status=200, content_type=None, headers=None):
            self.body, self.status, self.content_type, self.headers = body, status, content_type, headers or {}

    module("aiohttp", ClientError=OSError, web=types.SimpleNamespace(Response=Response))
    module("homeassistant.config_entries", ConfigFlow=FakeConfigFlow, OptionsFlow=FakeOptionsFlow, ConfigFlowResult=dict, ConfigEntry=Subscriptable,
           ConfigEntryState=types.SimpleNamespace(LOADED="loaded"))
    module("homeassistant.core", HomeAssistant=object, callback=lambda func: func)
    module("homeassistant.exceptions", ConfigEntryAuthFailed=ConfigEntryAuthFailed, ConfigEntryNotReady=ConfigEntryNotReady)
    helpers = module("homeassistant.helpers")
    helpers.aiohttp_client = module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: "session")
    helpers.config_validation = module("homeassistant.helpers.config_validation", config_entry_only_config_schema=lambda domain: ("schema", domain))
    helpers.typing = module("homeassistant.helpers.typing", ConfigType=dict)
    helpers.update_coordinator = module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=FakeCoordinator, UpdateFailed=UpdateFailed)
    module("voluptuous", **{k: getattr(FakeVol, k) for k in ("Schema", "Required", "Optional", "All", "Length", "Range", "Coerce", "In")})
    return registered


REGISTERED = install_stand_ins()
package = types.ModuleType("area_events_ha")
package.__path__ = [ROOT]
sys.modules["area_events_ha"] = package
const = importlib.import_module("area_events_ha.const")
client_module = importlib.import_module("area_events_ha.discovery_client")
feed_module = importlib.import_module("area_events_ha.feed")
hub_module = importlib.import_module("area_events_ha.hub")
flow_module = importlib.import_module("area_events_ha.config_flow")
coordinator_module = importlib.import_module("area_events_ha.coordinator")
media_view_module = importlib.import_module("area_events_ha.media_view")
websocket_module = importlib.import_module("area_events_ha.websocket_api")
spec = importlib.util.spec_from_file_location("area_events_ha_init", os.path.join(ROOT, "__init__.py"), submodule_search_locations=[ROOT])
init = importlib.util.module_from_spec(spec)
init.__package__ = "area_events_ha"
sys.modules["area_events_ha_init"] = init
spec.loader.exec_module(init)


def run(coroutine):
    return asyncio.run(coroutine)


# --- helpers ------------------------------------------------------------------------------------------------------------

class CountingClient:
    """The real client, counting calls (what a card could cost the service) and able to simulate an unreachable host."""

    def __init__(self, client):
        self._client, self.calls, self.down = client, [], False

    def __getattr__(self, name):
        method = getattr(self._client, name)

        async def call(*args, **kwargs):
            self.calls.append(name)
            if self.down:
                raise client_module.DiscoveryConnectionError("connection refused")
            return await method(*args, **kwargs)
        return call


class Service(Fixture):
    """A real Discovery Service (API + SQLite) with a San Diego area and an Austin area, and a hub talking to it."""

    def setUp(self):
        super().setUp()
        self.built = self.build(host_config=HostConfig(idle_wait_s=0.05, busy_wait_s=0.01))
        self.sd, self.austin = self.two_areas(self.built)
        self.discover(self.built)
        self.drain(self.built)
        self.start_api()

    def start_api(self, port=0):
        self.server = app_module.build_api(self.built, ApiTokens(CONTROL_TOKEN, READ_TOKEN, PROCESS_TOKEN), clock=self.clock, port=port).start()
        self.addCleanup(self.server.stop)
        self.url = "http://%s:%s" % self.server.address[:2]

    def hub(self, control=True, read_token=READ_TOKEN, control_token=CONTROL_TOKEN):
        session = UrllibSession()
        self.read = CountingClient(client_module.DiscoveryControlClient(session, self.url, read_token))
        self.control = CountingClient(client_module.DiscoveryControlClient(session, self.url, control_token)) if control else None
        return hub_module.DiscoveryHub(self.read, self.control, clock=self.clock)

    def change_listing(self, hour):
        self.pages[CITY_URL] = self.pages[CITY_URL].replace("10:00 AM - 2:00 PM", f"{hour}:00 AM - {hour + 1}:00 PM").replace("6:30 PM - 9:00 PM", f"{hour}:00 AM - {hour + 1}:00 PM")
        self.built.service.make_due("sandiego_gov")
        self.assertIn("completed", [r.outcome for r in self.built.service.run_until_idle(10)])
        self.drain(self.built)


class Inbox:
    """What one card receives."""

    def __init__(self):
        self.messages = []

    def __call__(self, message):
        self.messages.append(message)

    @property
    def last(self):
        return self.messages[-1]

    def titles(self):
        return sorted(e["title"] for e in self.last["events"])


# --- the integration's logic against the real service --------------------------------------------------------------------

class TwoCards(Service):
    def test_two_cards_get_their_own_areas_from_one_database_and_one_service(self):
        async def scenario():
            hub = self.hub()
            san_diego, austin = Inbox(), Inbox()
            await hub.subscribe("San Diego", san_diego)
            await hub.subscribe("austin", austin)   # names are matched ignoring case
            return san_diego, austin

        san_diego, austin = run(scenario())
        self.assertEqual(san_diego.titles(), [f"Event {i}" for i in range(4)])
        self.assertEqual(austin.titles(), [f"Austin Fest {i}" for i in range(3)])
        self.assertEqual((san_diego.last["area"]["name"], austin.last["area"]["name"]), ("San Diego", "Austin"))
        self.assertEqual(self.count("events"), 7, "one database holds both areas' events, once each")
        for inbox in (san_diego, austin):
            self.assertEqual(inbox.last["location"]["coverage"], "sources", "matched by source, not by a verified radius")
            self.assertTrue(all(e["location_status"] in ("address", "venue", "description_only", "unknown") for e in inbox.last["events"]))
        self.assertIn("demonstration data", austin.last["notice"], "fixture events are never presented as live discoveries")
        self.assertNotIn("demonstration", san_diego.last["notice"] or "")
        self.assertEqual(austin.last["events"][0]["source"]["name"], "Austin (fixture) (fixture data, not live)", "the configured name, still marked as fixture data")
        self.assertTrue(san_diego.last["generated_at"], "freshness is when the sources were last read")

    def test_an_area_with_nothing_discovered_says_so_instead_of_implying_nothing_exists(self):
        async def scenario():
            hub = self.hub()
            empty = await hub.create_area(name="Boise", latitude=43.6, longitude=-116.2, radius=20, sources=["sandiego_gov"])
            nothing = await hub.create_area(name="Nowhere", latitude=1.0, longitude=1.0, radius=5)
            inbox, bare = Inbox(), Inbox()
            await hub.subscribe(empty["area"]["name"], inbox)
            await hub.subscribe(nothing["area"]["name"], bare)
            return inbox, bare

        inbox, bare = run(scenario())
        self.assertIn("Boise", inbox.last["area"]["name"])
        self.assertEqual(len(inbox.last["events"]), 4, "a shared source serves every area that uses it, without copying its events")
        self.assertEqual(bare.last["events"], [])
        self.assertIn("No event sources are attached", bare.last["notice"])
        self.assertEqual(self.count("events"), 7)

    def test_an_unknown_area_lists_what_exists(self):
        with self.assertRaises(hub_module.UnknownArea) as caught:
            run(self.hub().subscribe("Atlantis", Inbox()))
        self.assertIn("Austin", str(caught.exception))
        self.assertIn("San Diego", str(caught.exception))

    def test_pages_and_event_detail_come_straight_from_the_store(self):
        async def scenario():
            hub = self.hub()
            first = await hub.page("San Diego", limit=3)
            second = await hub.page("San Diego", after=first["next_cursor"], limit=3)
            detail = await hub.event(first["events"][0]["id"])
            return first, second, detail

        first, second, detail = run(scenario())
        self.assertEqual((len(first["events"]), len(second["events"]), second["next_cursor"]), (3, 1, None))
        self.assertEqual(detail["event"]["id"], first["events"][0]["id"])
        self.assertEqual([h["revision"] for h in detail["history"]], [1])
        self.assertIn("title", detail["evidence"])


class SourceNames(Service):
    def test_tiles_and_summary_show_the_configured_name_and_a_rename_reaches_the_card_without_any_event_change(self):
        async def scenario():
            hub = self.hub()
            card = Inbox()
            await hub.subscribe("San Diego", card)
            first = card.last
            source = await self.control.source("sandiego_gov")
            await hub.update_source("sandiego_gov", source["revision"], name="Local Craft Fairs")
            self.read.calls.clear()
            await hub.refresh()
            renamed_calls = list(self.read.calls)
            self.read.calls.clear()
            await hub.refresh()
            return first, card, renamed_calls, list(self.read.calls)

        first, card, renamed_calls, quiet_calls = run(scenario())
        self.assertEqual({e["source"]["name"] for e in first["events"]}, {"City of San Diego"})
        self.assertEqual(first["sources"]["sandiego_gov"]["name"], "City of San Diego")
        self.assertEqual(len(card.messages), 2, "the rename was pushed")
        self.assertEqual({e["source"]["name"] for e in card.last["events"]}, {"Local Craft Fairs"}, "every tile shows the new name")
        self.assertEqual(card.last["sources"]["sandiego_gov"]["name"], "Local Craft Fairs", "the summary shows the same name")
        self.assertEqual({e["source"]["key"] for e in card.last["events"]}, {"sandiego_gov"}, "the identity key is unchanged")
        self.assertEqual([(e["id"], e["revision"]) for e in card.last["events"]], [(e["id"], e["revision"]) for e in first["events"]], "no event changed")
        self.assertEqual((renamed_calls.count("sources"), renamed_calls.count("events")), (1, 0), "one names read, no event re-read")
        self.assertNotIn("sources", quiet_calls, "the names are not read again once current")


class SharedUpdates(Service):
    def test_twenty_cards_cost_the_service_two_snapshots_and_one_status_check(self):
        async def scenario():
            hub = self.hub()
            inboxes = [Inbox() for _ in range(20)]
            for i, inbox in enumerate(inboxes):
                await hub.subscribe("San Diego" if i % 2 else "Austin", inbox)
            snapshot_calls = self.read.calls.count("events")
            fetched = len(self.client.urls)
            self.read.calls.clear()
            for _ in range(5):
                await hub.refresh()
            return hub, inboxes, snapshot_calls, fetched

        hub, inboxes, snapshot_calls, fetched = run(scenario())
        self.assertEqual(snapshot_calls, 2, "one read per followed area, not per card")
        self.assertEqual(self.read.calls.count("events"), 0, "an unchanged feed reads no events")
        self.assertEqual((self.read.calls.count("status"), self.read.calls.count("changes")), (5, 5), "one status call and one change-feed call per refresh, for all cards")
        self.assertEqual([len(i.messages) for i in inboxes], [1] * 20, "nobody was sent anything: nothing changed")
        self.assertEqual(hub.followed, (2, 20))
        self.assertEqual(len(self.client.urls), fetched, "cards and refreshes never crawl")

    def test_an_event_revision_reaches_only_the_cards_that_follow_that_area_and_starts_no_crawl(self):
        async def scenario():
            hub = self.hub()
            sd_cards, austin_cards = [Inbox() for _ in range(3)], [Inbox() for _ in range(3)]
            for inbox in sd_cards:
                await hub.subscribe("San Diego", inbox)
            for inbox in austin_cards:
                await hub.subscribe("Austin", inbox)
            before = {e["id"]: e["start"] for e in sd_cards[0].last["events"]}
            urls = len(self.client.urls)
            self.change_listing(7)   # the source says the events start at another time; discovery and extraction commit it in the service
            crawled_for_change = len(self.client.urls) - urls
            self.assertEqual(sd_cards[0].last["events"][0]["revision"], 1, "the card still shows the committed revision until the hub hears of the change")
            self.read.calls.clear()
            await hub.refresh()
            return hub, sd_cards, austin_cards, before, crawled_for_change

        hub, sd_cards, austin_cards, before, crawled_for_change = run(scenario())
        self.assertGreater(crawled_for_change, 0, "the service itself re-read its source; the hub did not")
        self.assertEqual(self.read.calls.count("events"), 1, "one re-read of the changed area, shared by its three cards")
        self.assertEqual([len(c.messages) for c in sd_cards], [2, 2, 2])
        self.assertEqual([len(c.messages) for c in austin_cards], [1, 1, 1], "the other area's cards heard nothing")
        updated = {e["id"]: e for e in sd_cards[1].last["events"]}
        self.assertEqual({e["revision"] for e in updated.values()}, {2})
        self.assertTrue(all(updated[k]["start"] != before[k] for k in before))
        self.assertEqual(sd_cards[0].last, sd_cards[2].last, "every card of the area sees the same view")

    def test_a_card_that_unsubscribes_is_forgotten_and_an_idle_hub_asks_only_for_status(self):
        async def scenario():
            hub = self.hub()
            inbox = Inbox()
            unsubscribe = await hub.subscribe("Austin", inbox)
            unsubscribe()
            unsubscribe()   # harmless twice
            self.read.calls.clear()
            await hub.refresh()
            return hub

        hub = run(scenario())
        self.assertEqual(hub.followed, (0, 0))
        self.assertEqual(self.read.calls, ["status"])

    def test_one_broken_card_connection_does_not_stop_the_others(self):
        async def scenario():
            hub = self.hub()
            good = Inbox()

            def broken(message):
                if len(good.messages) > 0 and broken.armed:
                    raise RuntimeError("socket closed")
            broken.armed = False
            await hub.subscribe("San Diego", broken)
            await hub.subscribe("San Diego", good)
            broken.armed = True
            self.change_listing(8)
            await hub.refresh()
            return hub, good

        hub, good = run(scenario())
        self.assertEqual(len(good.messages), 2)
        self.assertEqual(hub.followed[1], 1, "the broken subscription was dropped")


class Outages(Service):
    def test_an_unreachable_service_is_reported_honestly_and_the_cards_catch_up_when_it_returns(self):
        async def scenario():
            hub = self.hub()
            card = Inbox()
            await hub.subscribe("San Diego", card)
            self.read.down = True
            with self.assertRaises(client_module.DiscoveryConnectionError):
                await hub.refresh()
            down = card.last
            self.change_listing(9)   # discovery and extraction carry on while Home Assistant cannot reach them
            with self.assertRaises(client_module.DiscoveryConnectionError):
                await hub.refresh()
            self.assertEqual(len(card.messages), 2, "told once that it is down, not on every failed check")
            self.read.down = False
            await hub.refresh()
            return hub, card, down

        hub, card, down = run(scenario())
        self.assertEqual((down["state"], down["connected"], bool(down["message"])), ("unavailable", False, True))
        self.assertEqual(len(down["events"]), 4, "the last known events are kept, not blanked")
        self.assertEqual((card.last["state"], card.last["connected"]), ("ready", True))
        self.assertEqual({e["revision"] for e in card.last["events"]}, {2}, "the revision committed during the outage was not missed")

    def test_an_outage_after_an_administrator_change_still_reaches_the_cards(self):
        """Found on the real Cubi: a successful change used to forget the area list, so a later outage told no card."""
        async def scenario():
            hub = self.hub()
            card = Inbox()
            await hub.subscribe("San Diego", card)
            await hub.set_settings((await hub.status())["settings_revision"], intensity="high")
            self.read.down = True
            with self.assertRaises(client_module.DiscoveryConnectionError):
                await hub.refresh()
            return card

        card = run(scenario())
        self.assertEqual((card.last["state"], card.last["connected"], len(card.last["events"])), ("unavailable", False, 4))

    def test_nothing_is_reported_saved_while_the_service_cannot_be_reached(self):
        async def scenario():
            hub = self.hub()
            await hub.areas()
            self.control.down = True
            with self.assertRaises(client_module.DiscoveryConnectionError):
                await hub.set_settings(1, intensity="high")
            with self.assertRaises(client_module.DiscoveryConnectionError):
                await hub.pause()
            self.control.down = False
            return (await hub.status())

        status = run(scenario())
        self.assertFalse(status["paused"])
        self.assertEqual(status["settings_revision"], 1, "nothing was changed")

    def test_a_restarted_integration_resynchronizes_from_the_store_while_discovery_kept_running(self):
        self.built.host.start()
        self.addCleanup(self.built.host.stop, 5)
        first = Inbox()
        run(self.hub().subscribe("San Diego", first))
        self.change_listing(10)   # committed while no Home Assistant hub exists at all
        second = Inbox()
        run(self.hub().subscribe("San Diego", second))   # a new hub instance, as after a Home Assistant restart
        self.assertEqual({e["revision"] for e in first.last["events"]}, {1})
        self.assertEqual({e["revision"] for e in second.last["events"]}, {2}, "the new subscription starts from the authoritative store, nothing lost")
        self.assertEqual(self.built.host.status()["state"], "running", "Discovery's lifecycle is independent of Home Assistant's")

    def test_the_discovery_host_restarting_is_survived(self):
        port = int(self.url.rsplit(":", 1)[1])

        async def scenario():
            hub = self.hub()
            card = Inbox()
            await hub.subscribe("San Diego", card)
            self.server.stop()
            with self.assertRaises(client_module.DiscoveryConnectionError):
                await hub.refresh()
            self.built.close()
            self.built = self.build()   # a new process on the same database
            self.start_api(port)
            await hub.refresh()
            return card

        card = run(scenario())
        self.assertEqual([m["connected"] for m in card.messages], [True, False, True])
        self.assertEqual(len(card.last["events"]), 4)

    def test_a_rejected_token_is_an_authentication_failure_not_an_outage(self):
        hub = self.hub(read_token="x" * 40)
        with self.assertRaises(client_module.DiscoveryAuthError):
            run(hub.status())
        with self.assertRaises(client_module.DiscoveryAuthError):
            run(hub.refresh())


class SharedSettings(Service):
    def test_administrators_change_shared_settings_through_versioned_commands(self):
        async def scenario():
            hub = self.hub()
            areas = await hub.areas(reload=True)
            key = next(a["area_key"] for a in areas if a["name"] == "Austin")
            updated = await hub.update_area(key, 1, intensity="high")
            with self.assertRaises(client_module.DiscoveryConflictError) as conflict:
                await hub.update_area(key, 1, radius=60)   # a stale edit
            disabled = await hub.update_area(key, 2, enabled=False)
            settings = await hub.set_settings((await hub.status())["settings_revision"], mode="manual", intensity="low")
            paused = await hub.pause()
            status = await hub.status()
            return updated, conflict.exception, disabled, settings, paused, status

        updated, conflict, disabled, settings, paused, status = run(scenario())
        self.assertEqual((updated["revision"], updated["area"]["intensity"]), (2, "high"))
        self.assertEqual(conflict.current_revision, 2)
        self.assertEqual(disabled["area"]["enabled"], False)
        self.assertEqual((settings["settings"]["mode"], settings["settings"]["intensity"]), ("manual", "low"))
        self.assertTrue(paused["settings"]["paused"])
        self.assertEqual((status["paused"], status["mode"]), (True, "manual"))
        self.assertEqual(self.built.control.get_status().settings.paused, True, "the service itself persisted it")

    def test_a_change_is_saved_when_acknowledged_not_when_a_crawl_finishes(self):
        urls = len(self.client.urls)
        run(self.hub().pause())
        self.assertTrue(self.built.control.get_status().settings.paused)
        self.assertEqual(len(self.client.urls), urls, "no crawl was started or awaited")

    def test_the_read_token_can_never_change_settings_and_no_control_token_means_read_only(self):
        read_only = self.hub(control=False)
        with self.assertRaises(hub_module.ControlNotConfigured):
            run(read_only.pause())
        impostor = self.hub(control_token=READ_TOKEN)   # a read token entered in the control field
        with self.assertRaises(client_module.DiscoveryAuthError) as caught:
            run(impostor.pause())
        self.assertEqual(caught.exception.status, 403)
        self.assertFalse(self.built.control.get_status().settings.paused)
        client = client_module.DiscoveryControlClient(UrllibSession(), self.url, CONTROL_TOKEN)
        run(client.check_control())   # the probe passes for a control token and changes nothing
        with self.assertRaises(client_module.DiscoveryAuthError):
            run(client_module.DiscoveryControlClient(UrllibSession(), self.url, READ_TOKEN).check_control())
        self.assertEqual(self.built.control.get_status().settings.revision, 1)


JPEG = b"\xff\xd8\xff\xe0" + b"photo" * 400


class Imagery(Service):
    """The pictures path through the real service API and the integration: cached by the service, relayed by name, never by path or token."""

    def setUp(self):
        super().setUp()
        store = self.built.enrichment._resolver.store
        store.put_file("p0123456789abcdef01234567.jpg", JPEG)
        store.put_file("mabcdef0123456789-light.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        with database(self.built).read() as connection:
            self.candidate = connection.execute("SELECT candidate_id, payload_hash FROM candidates WHERE candidate_key LIKE 'sandiego_gov:%' ORDER BY candidate_id LIMIT 1").fetchone()
        self.built.media_work.finish([{"candidate_id": self.candidate["candidate_id"], "payload_hash": self.candidate["payload_hash"], "complete": True,
                                       "illustration": {"category": "markets", "strength": "strong", "reasons": ["title"], "fallback": False},
                                       "media": {"image": {"url": "/area_events/media/p0123456789abcdef01234567.jpg", "credit": "City of San Diego", "license": "as published by the source",
                                                           "source_url": "https://www.sandiego.gov/event/e0"},
                                                 "map": {"light": "/area_events/media/mabcdef0123456789-light.svg", "dark": "/area_events/media/mabcdef0123456789-light.svg",
                                                         "approximate": True, "confidence": "address", "precision": "address", "credit": "OpenFreeMap © OpenMapTiles Data from OpenStreetMap"}}}])

    def test_the_hub_relays_a_cached_picture_and_refuses_what_is_not_one(self):
        async def scenario():
            hub = self.hub()
            data, kind = await hub.media("p0123456789abcdef01234567.jpg")
            with self.assertRaises(client_module.DiscoveryRequestError) as missing:
                await hub.media("p0000000000000000000000ff.jpg")
            with self.assertRaises(client_module.DiscoveryRequestError):
                await hub.media("..%2fdiscovery.db")
            with self.assertRaises(client_module.DiscoveryAuthError):
                await self.hub(read_token="x" * 40).media("p0123456789abcdef01234567.jpg")
            return data, kind, missing.exception.status

        data, kind, status = run(scenario())
        self.assertEqual((data, kind, status), (JPEG, "image/jpeg", 404))

    def test_the_card_receives_the_picture_as_a_local_address_with_its_credit_and_never_a_service_address_or_token(self):
        async def scenario():
            hub = self.hub()
            inbox = Inbox()
            await hub.subscribe("San Diego", inbox)
            return inbox.last

        view = run(scenario())
        with_image = [e for e in view["events"] if e["images"]][0]
        self.assertEqual(with_image["images"], ["/area_events/media/p0123456789abcdef01234567.jpg"])
        self.assertEqual(with_image["image_credit"], {"text": "City of San Diego", "license": "as published by the source", "source_url": "https://www.sandiego.gov/event/e0"})
        self.assertTrue(with_image["media"]["map"]["approximate"])
        self.assertEqual(with_image["illustration"]["category"], "markets")
        others = [e for e in view["events"] if not e["images"]]
        self.assertTrue(others and all(e["media"] is None for e in others), "events without imagery say so; the card falls back to its illustration")
        text = json.dumps(view)
        for secret in (READ_TOKEN, CONTROL_TOKEN, PROCESS_TOKEN, self.url):
            self.assertNotIn(secret, text)

    def test_the_adapter_maps_media_without_adding_anything(self):
        bare = {"event_key": "ev-1", "title": "T", "starts_at": 1_800_000_000, "sources": ["ut_austin"], "location_status": "unknown"}
        event = feed_module.to_feed_event(bare)
        self.assertEqual((event["images"], event["image_credit"], event["media"], event["illustration"]), ([], None, None, None))
        shown = feed_module.to_feed_event({**bare, "media": {"photo": {"url": "/area_events/media/pabc.jpg"}}, "illustration": {"category": "music"}})
        self.assertEqual((shown["images"], list(shown["media"]), shown["illustration"]), ([], ["photo"], {"category": "music"}))
        typed = feed_module.to_feed_event({**bare, "event_type": "nasa.mission", "details": {"status": "Planning"}})
        self.assertEqual((typed["event_type"], typed["details"]), ("nasa.mission", {"status": "Planning"}))
        self.assertNotIn("event_type", event, "the current service contract does not acquire an inferred type")


class MediaView(unittest.TestCase):
    """The Home Assistant view that relays pictures (against stand-ins; the real-HA run is the on-device check)."""

    def view(self, hub=None, loaded=True):
        entry = types.SimpleNamespace(state="loaded" if loaded else "setup_error", runtime_data=types.SimpleNamespace(hub=hub))
        return media_view_module.AreaEventsMediaView(types.SimpleNamespace(config_entries=types.SimpleNamespace(async_entries=lambda domain: [entry])))

    def hub(self, answer=None, error=None):
        calls = []

        class Hub:
            async def media(self, name):
                calls.append(name)
                if error:
                    raise error
                return answer

        hub = Hub()
        hub.calls = calls
        return hub

    def get(self, view, name):
        return run(view.get(None, name))

    def test_a_picture_is_relayed_with_a_type_from_its_name_and_a_year_of_caching(self):
        hub = self.hub((JPEG, "text/html"))   # whatever the upstream header says, the name decides
        answer = self.get(self.view(hub), "p0123456789abcdef01234567.jpg")
        self.assertEqual((answer.status, answer.body, answer.content_type), (200, JPEG, "image/jpeg"))
        self.assertIn("immutable", answer.headers["Cache-Control"])
        self.assertEqual(answer.headers["X-Content-Type-Options"], "nosniff")
        svg = self.get(self.view(self.hub((b"<svg/>", "image/svg+xml"))), "mabcdef0123456789-light.svg")
        self.assertIn("sandbox", svg.headers["Content-Security-Policy"])

    def test_only_picture_names_are_answered_and_the_service_is_not_asked_about_anything_else(self):
        hub = self.hub((JPEG, "image/jpeg"))
        view = self.view(hub)
        for bad in ("../../etc/passwd", "p0123.jpg/../x", "UPPER.jpg", "a.exe", "noextension", "p" * 80 + ".jpg", "..", "x.jpg\x00"):
            self.assertEqual(self.get(view, bad).status, 404, bad)
        self.assertEqual(hub.calls, [])

    def test_missing_pictures_and_an_unreachable_service_are_told_apart(self):
        self.assertEqual(self.get(self.view(self.hub(error=client_module.DiscoveryRequestError(404, "not_found", "x"))), "p0123456789abcdef01234567.jpg").status, 404)
        down = self.get(self.view(self.hub(error=client_module.DiscoveryConnectionError("down"))), "p0123456789abcdef01234567.jpg")
        self.assertEqual((down.status, down.headers["Retry-After"]), (503, "30"))
        self.assertEqual(self.get(self.view(self.hub(error=client_module.DiscoveryAuthError(401, "no"))), "p0123456789abcdef01234567.jpg").status, 503)
        self.assertEqual(self.get(self.view(None, loaded=False), "p0123456789abcdef01234567.jpg").status, 503)

    def test_the_view_is_public_by_design_and_says_why(self):
        view = self.view()
        self.assertFalse(view.requires_auth)
        self.assertEqual(view.url, "/area_events/media/{name}")
        self.assertIn("<img> cannot authenticate", open(os.path.join(ROOT, "media_view.py"), encoding="utf-8").read().replace("an <img> cannot", "an <img> cannot"))


class Tokens(unittest.TestCase):
    def test_the_service_keeps_three_separate_tokens_in_private_files_and_never_changes_them(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            tokens, path = app_module.load_tokens(folder, read=True, process=True)
            self.assertEqual(sorted(os.listdir(folder)), ["api-process-token", "api-read-token", "api-token"])
            values = {name: open(os.path.join(folder, name), encoding="utf-8").read().strip() for name in os.listdir(folder)}
            self.assertEqual(len(set(values.values())), 3)
            self.assertTrue(all(len(v) >= 32 for v in values.values()))
            scopes = {name: tokens.scope_of(f"Bearer {value}") for name, value in values.items()}
            self.assertEqual(scopes, {"api-token": "control", "api-read-token": "read", "api-process-token": "process"})
            again, _ = app_module.load_tokens(folder, read=True, process=True)
            self.assertEqual(again.scope_of(f"Bearer {values['api-read-token']}"), "read")
            self.assertEqual(os.path.basename(path), "api-token")


def make_certificate(folder, address="127.0.0.1"):
    """A throwaway self-signed certificate for the loopback address (made with the openssl command line; None when it is not installed)."""
    import shutil
    import subprocess
    if not shutil.which("openssl"):
        return None
    cert, key = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
    done = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "2", "-subj", "/CN=area-events-test",
                           "-addext", f"subjectAltName=IP:{address}"], capture_output=True, timeout=60)
    return (cert, key) if done.returncode == 0 else None


class Transport(Service):
    """Bearer tokens only travel over TLS beyond this computer; a private certificate is trusted explicitly."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.certs = tempfile.TemporaryDirectory()
        self.addCleanup(self.certs.cleanup)
        self.pair = make_certificate(self.certs.name)
        if not self.pair:
            self.skipTest("openssl is not available to make a test certificate")
        self.server.stop()
        self.server = app_module.build_api(self.built, ApiTokens(CONTROL_TOKEN, READ_TOKEN, PROCESS_TOKEN), clock=self.clock, port=0, tls=self.pair).start()
        self.addCleanup(self.server.stop)
        self.url = "https://%s:%s" % self.server.address[:2]

    def test_an_https_service_is_read_with_the_trusted_certificate_and_refused_without_it(self):
        import ssl
        trust = ssl.create_default_context(cafile=self.pair[0])
        trusted = client_module.DiscoveryControlClient(UrllibSession(), self.url, READ_TOKEN, trust)
        self.assertIn("settings", run(trusted.status()))
        untrusting = client_module.DiscoveryControlClient(UrllibSession(), self.url, READ_TOKEN, ssl.create_default_context())
        with self.assertRaises(client_module.DiscoveryConnectionError) as caught:
            run(untrusting.status())
        self.assertNotIn(READ_TOKEN, str(caught.exception))
        import contextlib
        import io
        plain = client_module.DiscoveryControlClient(UrllibSession(), self.url.replace("https", "http"), READ_TOKEN)
        noise = io.StringIO()
        with contextlib.redirect_stderr(noise), self.assertRaises(client_module.DiscoveryConnectionError):
            run(plain.status())   # speaking plain http to the TLS port gets nothing
        import time
        time.sleep(0.3)
        self.assertEqual(noise.getvalue(), "", "a protocol mismatch is not logged as a traceback")

    def test_the_setup_form_and_entry_use_the_certificate_file(self):
        hass = types.SimpleNamespace(async_add_executor_job=lambda func, *a: asyncio.sleep(0, func(*a)))

        def flow():
            created = flow_module.AreaEventsConfigFlow()
            created.hass = hass
            return created

        with mock.patch.object(flow_module, "async_get_clientsession", lambda hass: UrllibSession()):
            created = run(flow().async_step_user({"url": self.url, "read_token": READ_TOKEN, "control_token": CONTROL_TOKEN, "ca_file": self.pair[0]}))
            self.assertEqual((created["type"], created["data"]["ca_file"]), ("create_entry", self.pair[0]))
            bad = run(flow().async_step_user({"url": self.url, "read_token": READ_TOKEN, "ca_file": os.path.join(self.certs.name, "none.pem")}))
            self.assertEqual(bad["errors"], {"base": "invalid_ca_file"})
            untrusted = run(flow().async_step_user({"url": self.url, "read_token": READ_TOKEN}))
            self.assertEqual(untrusted["errors"], {"base": "cannot_connect"}, "without the certificate the private https address is not trusted")
        entry = Entry({"url": self.url, "read_token": READ_TOKEN, "control_token": CONTROL_TOKEN, "ca_file": self.pair[0]})
        with mock.patch.object(init, "async_get_clientsession", lambda hass: UrllibSession()):
            self.assertTrue(run(init.async_setup_entry(hass, entry)))
            self.assertTrue(run(entry.runtime_data.hub.status())["connected"])
            broken = Entry({"url": self.url, "read_token": READ_TOKEN, "ca_file": os.path.join(self.certs.name, "none.pem")})
            with self.assertRaises(ConfigEntryNotReady):
                run(init.async_setup_entry(hass, broken))

    def test_a_remote_extraction_worker_trusts_the_same_certificate(self):
        from area_events.extraction.service import HttpProcessingClient
        worker = HttpProcessingClient(self.url, PROCESS_TOKEN, cafile=self.pair[0])
        self.assertIn("pending", worker.status("stored-candidate", 1))


class Migration(unittest.TestCase):
    def test_an_entry_of_the_earlier_app_is_refused_with_instructions_and_a_current_one_is_kept(self):
        old, current = Entry({"url": "http://app:8765", "token": "t"}), Entry({"url": "https://h:1", "read_token": "r"})
        old.version, current.version = 1, 2
        with self.assertLogs(init._LOGGER, "ERROR") as logged:
            self.assertFalse(run(init.async_migrate_entry(Hass(), old)))
        self.assertIn("Remove it", logged.output[0])
        self.assertTrue(run(init.async_migrate_entry(Hass(), current)))


class Feed(unittest.TestCase):
    def test_the_adapter_adds_nothing_the_service_did_not_say(self):
        record = {"event_key": "ev-1", "title": "T", "starts_at": 1_800_000_000, "ends_at": None, "timezone": "America/Los_Angeles", "venue_name": None,
                  "location_text": "Along Oak Street.", "address": None, "location_status": "description_only", "source_url": "https://x.example/e",
                  "description": None, "missing": ["ends_at", "description"], "revision": 3, "updated_at": 5, "sources": ["sandiego_gov"]}
        event = feed_module.to_feed_event(record)
        self.assertEqual((event["id"], event["venue"], event["address"], event["end"], event["revision"]), ("ev-1", "Along Oak Street.", None, None, 3))
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("America/Los_Angeles")
        except Exception:  # this development machine has no time-zone database; Home Assistant's host does
            self.assertTrue(event["start"].endswith("+00:00"), "without a zone database the instant is still right, expressed in UTC")
        else:
            self.assertTrue(event["start"].endswith("-08:00"))
        self.assertEqual((event["categories"], event["verified"], event["location_verified"]), ([], False, False))
        self.assertNotIn("latitude", event)
        self.assertEqual(event["source"], {"name": "City of San Diego", "key": "sandiego_gov"})
        self.assertEqual(event["source_keys"], ["sandiego_gov"])
        bad_zone = feed_module.to_feed_event({**record, "timezone": "Not/AZone"})
        self.assertTrue(bad_zone["start"].endswith("+00:00"))

    def test_a_configured_source_name_wins_over_any_name_made_from_the_key(self):
        record = {"event_key": "ev-2", "title": "Halloween Finale", "starts_at": 1_800_000_000, "timezone": "UTC", "sources": ["mission_trails"]}
        names = {"mission_trails": "Local Craft Fairs", "sandiego_gov": "City Events Calendar"}
        self.assertEqual(feed_module.to_feed_event(record, names)["source"], {"name": "Local Craft Fairs", "key": "mission_trails"})
        self.assertEqual(feed_module.source_name("sandiego_gov", names), "City Events Calendar", "the built-in default never overrides the configured name")
        self.assertEqual(feed_module.source_name("mission_trails"), "Mission Trails", "a name from the key only when the service listed none")

    def test_the_adapter_preserves_every_source_association(self):
        record = {"event_key": "shared-event", "title": "Shared", "starts_at": 1_800_000_000, "timezone": "UTC",
                  "sources": ["calendar-one", "calendar-two"]}
        event = feed_module.to_feed_event(record)
        self.assertEqual(event["source_keys"], ["calendar-one", "calendar-two"])
        self.assertEqual(event["source"]["key"], "calendar-one")


# --- the Home Assistant glue against stand-ins ----------------------------------------------------------------------

class Entry:
    def __init__(self, data=None, options=None, unique_id="http://host:8780"):
        self.data, self.options, self.unique_id, self.state, self.runtime_data = data or {}, options or {}, unique_id, "loaded", None
        self.entry_id, self.unloads, self.update_listeners, self.reauth = "entry1", [], [], 0

    def async_on_unload(self, remove):
        self.unloads.append(remove)

    def add_update_listener(self, listener):
        self.update_listeners.append(listener)
        return lambda: self.update_listeners.remove(listener)

    def async_start_reauth(self, hass):
        self.reauth += 1


class Hass:
    def __init__(self, entries=()):
        self.entries, self.reloads, self.data = list(entries), [], {}
        self.config_entries = types.SimpleNamespace(async_entries=lambda domain: self.entries, async_reload=self.reload)

    async def reload(self, entry_id):
        self.reloads.append(entry_id)


class Flow(unittest.TestCase):
    def flow(self, status_error=None, control_error=None, entries=()):
        flow = flow_module.AreaEventsConfigFlow()
        flow.entries = list(entries)
        calls = []

        class Client:
            def __init__(self, session, url, token, trust=None):
                self.url, self.token = url, token
                calls.append((url, token))

            async def status(self):
                if status_error:
                    raise status_error

            async def check_control(self):
                if control_error:
                    raise control_error

        patcher = mock.patch.object(flow_module, "DiscoveryControlClient", Client)
        patcher.start()
        self.addCleanup(patcher.stop)
        flow.calls = calls
        return flow

    def test_setup_checks_both_tokens_and_stores_them_in_the_entry_only(self):
        flow = self.flow()
        created = run(flow.async_step_user({"url": "HTTPS://Events.Example:8780/", "read_token": "r" * 40, "control_token": "c" * 40}))
        self.assertEqual(created["data"], {"url": "https://events.example:8780", "read_token": "r" * 40, "control_token": "c" * 40})
        self.assertEqual(flow.unique_id, "https://events.example:8780")
        self.assertEqual(len(flow.calls), 2)
        read_only = self.flow()
        self.assertNotIn("control_token", run(read_only.async_step_user({"url": "https://h:1", "read_token": "r" * 40}))["data"])

    def test_wrong_addresses_and_tokens_are_reported_with_the_right_field(self):
        down = run(self.flow(status_error=client_module.DiscoveryConnectionError("x")).async_step_user({"url": "https://h:1", "read_token": "r" * 40}))
        self.assertEqual(down["errors"], {"base": "cannot_connect"})
        bad_read = run(self.flow(status_error=client_module.DiscoveryAuthError(401, "no")).async_step_user({"url": "https://h:1", "read_token": "r" * 40}))
        self.assertEqual(bad_read["errors"], {"base": "invalid_auth"})
        bad_control = run(self.flow(control_error=client_module.DiscoveryAuthError(403, "no")).async_step_user({"url": "https://h:1", "read_token": "r" * 40, "control_token": "c" * 40}))
        self.assertEqual(bad_control["errors"], {"base": "invalid_control_token"})
        self.assertEqual(run(self.flow().async_step_user({"url": "nonsense", "read_token": "r" * 40}))["errors"], {"url": "invalid_url"})
        with self.assertRaises(AbortFlow):
            run(self.flow(entries=[Entry(unique_id="https://h:1")]).async_step_user({"url": "https://h:1", "read_token": "r" * 40}))

    def test_plain_http_beyond_this_computer_carries_a_warning_and_https_does_not(self):
        self.assertIn("unprotected", flow_module.transport_note("http://cubi.local:8780"))
        for safe in ("https://cubi.local", "http://localhost:8780", "http://127.0.0.1:8780", ""):
            self.assertEqual(flow_module.transport_note(safe), "")
        shown = run(self.flow().async_step_user())
        self.assertEqual(shown["step_id"], "user")

    def test_reauthentication_replaces_the_tokens_and_the_options_hold_a_bounded_interval(self):
        flow = self.flow()
        flow.reauth_entry = Entry({"url": "https://h:1", "read_token": "old"})
        result = run(flow.async_step_reauth_confirm({"read_token": "n" * 40, "control_token": "m" * 40}))
        self.assertEqual((result["reason"], flow.reauth_entry.data["read_token"], flow.reauth_entry.data["control_token"]), ("reauth_successful", "n" * 40, "m" * 40))
        bad = self.flow(status_error=client_module.DiscoveryAuthError(401, "no"))
        bad.reauth_entry = Entry({"url": "https://h:1"})
        self.assertEqual(run(bad.async_step_reauth_confirm({"read_token": "x"}))["errors"], {"base": "invalid_auth"})
        options = flow_module.AreaEventsOptionsFlow()
        options.config_entry = Entry(options={"scan_interval": 30})
        self.assertEqual(run(options.async_step_init())["step_id"], "init")
        self.assertEqual(run(options.async_step_init({"scan_interval": 20}))["data"], {"scan_interval": 20})
        self.assertEqual((const.MIN_SCAN_INTERVAL, const.MAX_SCAN_INTERVAL), (5, 300))


class ProducerOptions(Service):
    """OptionsFlow administration exercised against the real authenticated SQLite-backed API."""

    def options_flow(self, control=True):
        data = {"url": self.url, "read_token": READ_TOKEN}
        if control:
            data["control_token"] = CONTROL_TOKEN
        flow = flow_module.AreaEventsOptionsFlow()
        flow.config_entry = Entry(data, options={"scan_interval": 30})
        flow.hass = types.SimpleNamespace()
        return flow

    def api_client(self, control=True):
        return client_module.DiscoveryControlClient(UrllibSession(), self.url, CONTROL_TOKEN if control else READ_TOKEN)

    def with_client_session(self, action):
        with mock.patch.object(flow_module, "async_get_clientsession", lambda hass: UrllibSession()):
            return run(action)

    def test_options_create_edit_enable_disable_and_persist_in_cubi(self):
        flow = self.options_flow()
        created = self.with_client_session(flow.async_step_source_create({
            "id": "calendar-one", "type": "localist", "name": "Calendar One", "enabled": False,
            "interval_minutes": 45, "options": '{"url":"https://calendar.example"}',
        }))
        self.assertEqual((created["type"], created["data"]), ("create_entry", {"scan_interval": 30}))
        source = self.with_client_session(self.api_client().source("calendar-one"))
        self.assertEqual((source["type"], source["name"], source["enabled"], source["interval_minutes"]),
                         ("localist", "Calendar One", False, 45))

        flow = self.options_flow()
        flow._source_action = "edit"
        self.with_client_session(flow.async_step_source_select({"source_id": "calendar-one"}))
        form = self.with_client_session(flow.async_step_source_edit())
        self.assertNotIn("options", [getattr(key, "schema", key) for key in form["data_schema"].schema],
                         "regular producer edits must not expose JSON")
        edited = self.with_client_session(flow.async_step_source_edit({
            "name": "Calendar One Updated", "interval_minutes": 90,
        }))
        self.assertEqual(edited["data"], {"scan_interval": 30})
        source = self.with_client_session(self.api_client().source("calendar-one"))
        self.assertEqual((source["name"], source["interval_minutes"], source["options"]),
                         ("Calendar One Updated", 90, {"url": "https://calendar.example"}))

        flow = self.options_flow()
        flow._source_action = "options"
        advanced = self.with_client_session(flow.async_step_source_select({"source_id": "calendar-one"}))
        self.assertEqual(advanced["step_id"], "source_options")
        rejected = self.with_client_session(flow.async_step_source_options({"options": "not-json"}))
        self.assertEqual(rejected["errors"], {"options": "invalid_json"})
        saved = self.with_client_session(flow.async_step_source_options({
            "options": '{"url":"https://calendar.example","days":30}',
        }))
        self.assertEqual(saved["type"], "create_entry")
        source = self.with_client_session(self.api_client().source("calendar-one"))
        self.assertEqual((source["name"], source["interval_minutes"], source["options"]["days"]),
                         ("Calendar One Updated", 90, 30))

        for enabled in (True, False):
            flow = self.options_flow()
            flow._source_action = "enable" if enabled else "disable"
            self.with_client_session(flow.async_step_source_select({"source_id": "calendar-one"}))
            preview = self.with_client_session(flow.async_step_source_toggle())
            self.assertEqual(preview["step_id"], "source_toggle")
            self.assertIn("confirm", [getattr(key, "schema", key) for key in preview["data_schema"].schema])
            self.with_client_session(flow.async_step_source_toggle({"confirm": True}))
            source = self.with_client_session(self.api_client().source("calendar-one"))
            self.assertEqual(source["enabled"], enabled)

        flow = self.options_flow()
        flow._source_action = "enable"
        already = self.with_client_session(flow.async_step_source_select({"source_id": "calendar-one"}))
        preview = self.with_client_session(flow.async_step_source_toggle())
        self.assertEqual(preview["step_id"], "source_toggle")
        # Canceling the confirmation must leave the producer disabled.
        canceled = self.with_client_session(flow.async_step_source_toggle({"confirm": False}))
        self.assertEqual(canceled["step_id"], "sources")
        self.assertFalse(self.with_client_session(self.api_client().source("calendar-one"))["enabled"])

        # Known-source registration during service startup does not replace user-owned instance configuration.
        self.built.register_known_sources()
        persisted = self.with_client_session(self.api_client().source("calendar-one"))
        self.assertEqual((persisted["name"], persisted["interval_minutes"], persisted["enabled"]),
                         ("Calendar One Updated", 90, False))
        restarted = app_module.build(self.db, clock=self.clock, fetch_client=self.client)
        try:
            restarted.register_known_sources()
            persisted = restarted.control.get_source("calendar-one")
            self.assertEqual((persisted.name, persisted.interval_minutes, persisted.enabled), ("Calendar One Updated", 90, False))
        finally:
            restarted.close()

    def test_area_attachment_preserves_other_sources_and_rejects_stale_revision(self):
        control = self.api_client()
        self.with_client_session(control.create_source("calendar-one", "localist", "Calendar One", True, 60,
                                                       {"url": "https://calendar.example"}))
        area_key = self.sd.area_key
        flow = self.options_flow()
        self.with_client_session(flow.async_step_area_select({"area_key": area_key}))
        self.with_client_session(flow.async_step_area_sources({"source_id": "calendar-one", "action": "attach"}))
        area = self.with_client_session(self.api_client(False).area(area_key))
        self.assertEqual(set(area["sources"]), {"sandiego_gov", "calendar-one"})

        flow = self.options_flow()
        self.with_client_session(flow.async_step_area_select({"area_key": area_key}))
        current = self.with_client_session(control.area(area_key))
        self.with_client_session(control.update_area(area_key, current["revision"], intensity="high"))
        conflict = self.with_client_session(flow.async_step_area_sources({"source_id": "calendar-one", "action": "detach"}))
        self.assertEqual((conflict["type"], conflict["reason"]), ("abort", "revision_conflict"))
        area = self.with_client_session(self.api_client(False).area(area_key))
        self.assertEqual(set(area["sources"]), {"sandiego_gov", "calendar-one"})
        self.assertEqual(area["intensity"], "high", "the concurrent area update remains intact")

        flow = self.options_flow()
        self.with_client_session(flow.async_step_area_select({"area_key": area_key}))
        detached = self.with_client_session(flow.async_step_area_sources({"source_id": "calendar-one", "action": "detach"}))
        self.assertEqual(detached["type"], "create_entry")
        area = self.with_client_session(self.api_client(False).area(area_key))
        self.assertEqual(area["sources"], ["sandiego_gov"])

    def test_read_only_flow_lists_but_cannot_mutate_and_api_errors_are_shown(self):
        flow = self.options_flow(control=False)
        listing = self.with_client_session(flow.async_step_sources())
        self.assertEqual(listing["step_id"], "sources")
        description = listing["description_placeholders"]["instances"]
        self.assertIn("City of San Diego", description)
        self.assertIn("Ready to discover", description)
        self.assertNotIn("options=", description, "producer list must not dump raw JSON")
        self.assertNotIn("https://", description, "producer list must not dump URLs")
        self.assertEqual(
            self.with_client_session(flow.async_step_sources({"action": "options"}))["step_id"],
            "source_select",
        )
        denied = self.with_client_session(flow.async_step_source_create({
            "id": "calendar-one", "type": "localist", "name": "Calendar One", "enabled": True,
            "interval_minutes": 60, "options": '{"url":"https://calendar.example"}',
        }))
        self.assertEqual(denied["errors"], {"base": "control_token_required"})
        self.assertNotIn("calendar-one", [source["id"] for source in self.with_client_session(self.api_client(False).sources())])

        flow = self.options_flow()
        rejected = self.with_client_session(flow.async_step_source_create({
            "id": "calendar-one", "type": "not-installed", "name": "Calendar One", "enabled": True,
            "interval_minutes": 60, "options": "{}",
        }))
        self.assertEqual(rejected["errors"], {"base": "invalid_request"})
        self.assertNotIn("calendar-one", [source["id"] for source in self.with_client_session(self.api_client(False).sources())])


class Lifecycle(Service):
    def entry(self, **extra):
        return Entry({"url": self.url, "read_token": READ_TOKEN, "control_token": CONTROL_TOKEN, **extra})

    def setup(self, entry):
        with mock.patch.object(init, "async_get_clientsession", lambda hass: UrllibSession()):
            return run(init.async_setup_entry(Hass([entry]), entry))

    def test_setup_builds_one_hub_one_coordinator_and_keeps_tokens_in_the_backend(self):
        entry = self.entry(**{})
        self.assertTrue(self.setup(entry))
        runtime = entry.runtime_data
        self.assertIsInstance(runtime, coordinator_module.RuntimeData)
        self.assertTrue(runtime.hub.can_control)
        self.assertEqual(runtime.coordinator.update_interval.total_seconds(), const.DEFAULT_SCAN_INTERVAL)
        self.assertEqual((len(runtime.coordinator.listeners), len(entry.update_listeners), len(entry.unloads)), (1, 1, 2))
        read_only = Entry({"url": self.url, "read_token": READ_TOKEN}, options={"scan_interval": 30})
        self.setup(read_only)
        self.assertFalse(read_only.runtime_data.hub.can_control)
        self.assertEqual(read_only.runtime_data.coordinator.update_interval.total_seconds(), 30)

    def test_setup_failures_map_to_retry_and_reauth(self):
        for kwargs, expected in (({"url": "http://127.0.0.1:9"}, ConfigEntryNotReady), ({"read_token": "x" * 40}, ConfigEntryAuthFailed)):
            entry = self.entry(**kwargs)
            with self.assertRaises(expected):
                self.setup(entry)

    def test_unloading_tells_the_cards_and_leaves_discovery_alone(self):
        entry = self.entry()
        self.setup(entry)
        card = Inbox()
        run(entry.runtime_data.hub.subscribe("San Diego", card))
        self.assertTrue(run(init.async_unload_entry(Hass([entry]), entry)))
        self.assertEqual((card.last["state"], card.last["connected"]), ("unavailable", False))
        self.assertEqual(entry.runtime_data.hub.followed, (0, 0))
        self.assertEqual(self.count("events"), 7, "the database is untouched by Home Assistant unloading")
        hass = Hass([entry])
        run(init._options_updated(hass, entry))
        self.assertEqual(hass.reloads, ["entry1"], "changing options reloads the entry (and so its cards' subscriptions)")

    def test_the_coordinator_maps_failures_and_reports_what_it_serves(self):
        entry = self.entry()
        self.setup(entry)
        coordinator = entry.runtime_data.coordinator
        run(entry.runtime_data.hub.subscribe("Austin", Inbox()))
        self.assertEqual(run(coordinator._async_update_data()), {"followed_areas": 1, "card_subscriptions": 1})
        for error, expected in ((client_module.DiscoveryAuthError(401, "no"), ConfigEntryAuthFailed), (client_module.DiscoveryConnectionError("down"), UpdateFailed),
                                (client_module.DiscoveryRequestError(500, "x", "y"), UpdateFailed)):
            with mock.patch.object(entry.runtime_data.hub, "refresh", mock.AsyncMock(side_effect=error)):
                with self.assertRaises(expected):
                    run(coordinator._async_update_data())


class FakeConnection:
    def __init__(self, admin=False):
        self.sent, self.subscriptions, self.user = [], {}, types.SimpleNamespace(is_admin=admin)

    def send_result(self, iden, result=None):
        self.sent.append(("result", iden, result))

    def send_error(self, iden, code, message):
        self.sent.append(("error", iden, code, message))

    def send_message(self, message):
        self.sent.append(("message", message))


class WebSocket(Service):
    def setUp(self):
        super().setUp()
        self.entry = Entry({"url": self.url, "read_token": READ_TOKEN, "control_token": CONTROL_TOKEN})
        with mock.patch.object(init, "async_get_clientsession", lambda hass: UrllibSession()):
            run(init.async_setup_entry(Hass([self.entry]), self.entry))
        self.hass = Hass([self.entry])

    def call(self, handler, message, admin=False, connection=None):
        connection = connection or FakeConnection(admin)
        run(handler(self.hass, connection, {"id": 5, "type": "x", **message}))
        return connection

    def test_every_command_is_registered_with_its_schema_and_the_mutations_need_an_admin(self):
        before = len(REGISTERED)
        websocket_module.async_register(self.hass)
        names = {h.schema["type"] for h in REGISTERED[before:]}
        self.assertEqual(names, {"area_events/areas", "area_events/status", "area_events/events", "area_events/event", "area_events/subscribe", "area_events/area_create",
                                 "area_events/area_update", "area_events/settings", "area_events/pause", "area_events/resume",
                                 "area_events/management", "area_events/source_command", "area_events/event_delete"})
        for handler in (websocket_module.ws_area_create, websocket_module.ws_area_update, websocket_module.ws_settings, websocket_module.ws_pause,
                        websocket_module.ws_resume, websocket_module.ws_management, websocket_module.ws_source_command, websocket_module.ws_event_delete):
            denied = self.call(handler, {"area_key": "abcdefgh", "expected_revision": 1, "name": "x", "latitude": 1, "longitude": 1, "radius": 1,
                                         "event_key": "ev-00000000", "reason": "other"}, admin=False)
            self.assertEqual(denied.sent[0][:3], ("error", 5, "unauthorized"), handler)
        self.assertFalse(self.built.control.get_status().settings.paused, "a non-administrator changed nothing")

    def test_management_panel_returns_contracts_and_requires_admin(self):
        denied = self.call(websocket_module.ws_management, {}, admin=False)
        self.assertEqual(denied.sent[0][2], "unauthorized")
        result = self.call(websocket_module.ws_management, {}, admin=True).sent[0]
        self.assertEqual(result[0], "result")
        snapshot = result[2]
        self.assertEqual(snapshot["schema"], "area_events.management.v1")
        self.assertIn("sandiego_gov", {s["id"] for s in snapshot["sources"]})
        self.assertEqual(snapshot["producer_types"]["schema"], "area_events.producer_forms.v1")
        types_by_id = {item["id"]: item for item in snapshot["producer_types"]["types"]}
        self.assertIn("localist", types_by_id)
        self.assertIn("days", {f["name"] for f in types_by_id["localist"]["fields"]})
        self.assertNotIn(CONTROL_TOKEN, json.dumps(snapshot))
        self.assertNotIn(READ_TOKEN, json.dumps(snapshot))

    def test_the_backend_build_reaches_the_management_overview(self):
        from area_events.api.http_api import backend_build
        snapshot = self.call(websocket_module.ws_management, {}, admin=True).sent[0][2]
        self.assertEqual(snapshot["status"]["build"], backend_build(), "passed through the existing snapshot unchanged")
        self.assertRegex(snapshot["status"]["build"], r"^AREA-EVENTS-BACKEND \d+(\.\d+)*-[0-9a-f]{8}$")

    def test_administrator_can_manage_sources_without_exposing_control_tokens(self):
        create = {"action": "create", "source_id": "new-campus", "producer_type": "localist",
                  "name": "New Campus", "enabled": False, "interval_minutes": 30,
                  "options": {"url": "https://calendar.example"}}
        self.assertEqual(self.call(websocket_module.ws_source_command, create, admin=False).sent[0][2], "unauthorized")
        made = self.call(websocket_module.ws_source_command, create, admin=True).sent[0]
        self.assertEqual(made[0], "result")
        source = made[2]["source"]
        self.assertFalse(source["enabled"])
        bad = self.call(websocket_module.ws_source_command, {"action": "toggle", "source_id": "new-campus",
                        "expected_revision": 1, "enabled": True}, admin=True).sent[0]
        self.assertEqual(bad[0], "result")
        self.assertTrue(bad[2]["source"]["enabled"])
        stale = self.call(websocket_module.ws_source_command, {"action": "toggle", "source_id": "new-campus",
                          "expected_revision": 1, "enabled": False}, admin=True).sent[0]
        self.assertEqual(stale[2], "revision_conflict")
        self.assertNotIn(CONTROL_TOKEN, json.dumps([made, bad, stale]))

    def test_an_administrator_deletes_an_event_into_the_trashcan_and_every_card_loses_it(self):
        hub = self.entry.runtime_data.hub
        card = Inbox()
        run(hub.subscribe("Austin", card))
        key = card.last["events"][0]["id"]
        before = len(card.last["events"])
        refused = self.call(websocket_module.ws_event_delete, {"event_key": key, "reason": "nonsense"}, admin=True).sent[0]   # (HA's schema refuses it first; the service too)
        self.assertEqual(refused[:3], ("error", 5, "rejected"))
        self.assertIn(key, [e["id"] for e in card.last["events"]])
        deleted = self.call(websocket_module.ws_event_delete, {"event_key": key, "reason": "advertisement"}, admin=True).sent[0]
        self.assertEqual((deleted[0], deleted[2]["schema"], deleted[2]["state"]), ("result", "discovery.trashcan.v1", "deleted"))
        self.assertEqual(len(card.last["events"]), before - 1)
        self.assertNotIn(key, [e["id"] for e in card.last["events"]])
        self.assertEqual(self.call(websocket_module.ws_event_delete, {"event_key": key, "reason": "other"}, admin=True).sent[0][2]["state"], "already_deleted")
        self.assertEqual(self.call(websocket_module.ws_event, {"event_key": key}).sent[0][2], "not_found")
        run(hub.refresh())
        self.assertNotIn(key, [e["id"] for e in card.last["events"]], "the change feed agrees")
        trash = self.call(websocket_module.ws_management, {}, admin=True).sent[0][2]["trashcan"]
        self.assertEqual((trash["total"], trash["by_reason"]["advertisement"], trash["events"][0]["event_key"]), (1, 1, key))
        read_only = Entry({"url": self.url, "read_token": READ_TOKEN})
        with mock.patch.object(init, "async_get_clientsession", lambda hass: UrllibSession()):
            run(init.async_setup_entry(Hass([read_only]), read_only))
        self.hass.entries[:] = [read_only]
        other = card.last["events"][0]["id"]
        self.assertEqual(self.call(websocket_module.ws_event_delete, {"event_key": other, "reason": "other"}, admin=True).sent[0][2], "control_not_configured")

    def test_reads_are_open_to_any_signed_in_user(self):
        areas = self.call(websocket_module.ws_areas, {}).sent[0]
        self.assertEqual(sorted(a["name"] for a in areas[2]["areas"]), ["Austin", "San Diego"])
        self.assertTrue(areas[2]["service"]["connected"])
        page = self.call(websocket_module.ws_events, {"area": "Austin", "limit": 2}).sent[0][2]
        self.assertEqual((len(page["events"]), bool(page["next_cursor"])), (2, True))
        detail = self.call(websocket_module.ws_event, {"event_key": page["events"][0]["id"]}).sent[0][2]
        self.assertEqual(detail["event"]["id"], page["events"][0]["id"])
        self.assertEqual(self.call(websocket_module.ws_events, {"area": "Atlantis"}).sent[0][2], "unknown_area")
        self.assertEqual(self.call(websocket_module.ws_event, {"event_key": "ev-doesnotexist"}).sent[0][2], "not_found")

    def test_administrators_change_settings_and_every_failure_has_a_code_the_user_can_act_on(self):
        key = next(a["area_key"] for a in run(self.entry.runtime_data.hub.areas()) if a["name"] == "Austin")
        saved = self.call(websocket_module.ws_area_update, {"area_key": key, "expected_revision": 1, "intensity": "high"}, admin=True).sent[0]
        self.assertEqual((saved[0], saved[2]["revision"]), ("result", 2))
        stale = self.call(websocket_module.ws_area_update, {"area_key": key, "expected_revision": 1, "radius": 5}, admin=True).sent[0]
        self.assertEqual(stale[2], "revision_conflict")
        self.assertIn("current revision 2", stale[3])
        created = self.call(websocket_module.ws_area_create, {"name": "Denver", "latitude": 39.7, "longitude": -105.0, "radius": 20, "request_id": "r1"}, admin=True).sent[0]
        self.assertEqual(created[2]["area"]["name"], "Denver")
        self.assertEqual(self.call(websocket_module.ws_settings, {"expected_revision": 1, "intensity": "low"}, admin=True).sent[0][2]["settings"]["intensity"], "low")
        self.assertTrue(self.call(websocket_module.ws_pause, {}, admin=True).sent[0][2]["settings"]["paused"])
        self.assertFalse(self.call(websocket_module.ws_resume, {}, admin=True).sent[0][2]["settings"]["paused"])
        self.assertEqual(self.call(websocket_module.ws_area_create, {"name": "x", "latitude": 1, "longitude": 1, "radius": 1, "sources": ["nope"]}, admin=True).sent[0][2], "not_found")
        self.assertEqual(self.call(websocket_module.ws_area_update, {"area_key": "abcdefgh1", "expected_revision": 1, "radius": 5}, admin=True).sent[0][2], "not_found")

    def test_service_side_refusals_and_outages_reach_the_user_as_distinct_errors(self):
        hub = self.entry.runtime_data.hub
        read_only = Entry({"url": self.url, "read_token": READ_TOKEN})
        with mock.patch.object(init, "async_get_clientsession", lambda hass: UrllibSession()):
            run(init.async_setup_entry(Hass([read_only]), read_only))
        self.hass.entries[:] = [read_only]
        self.assertEqual(self.call(websocket_module.ws_pause, {}, admin=True).sent[0][2], "control_not_configured")
        impostor = Entry({"url": self.url, "read_token": READ_TOKEN, "control_token": READ_TOKEN})
        with mock.patch.object(init, "async_get_clientsession", lambda hass: UrllibSession()):
            run(init.async_setup_entry(Hass([impostor]), impostor))
        self.hass.entries[:] = [impostor]
        self.assertEqual(self.call(websocket_module.ws_pause, {}, admin=True).sent[0][2], "forbidden")
        self.hass.entries[:] = [self.entry]
        with mock.patch.object(hub, "areas", mock.AsyncMock(side_effect=client_module.DiscoveryConnectionError("host down"))):
            self.assertEqual(self.call(websocket_module.ws_areas, {}).sent[0][2:], ("unavailable", "host down"))
        with mock.patch.object(hub, "areas", mock.AsyncMock(side_effect=client_module.DiscoveryAuthError(401, "no"))):
            self.assertEqual(self.call(websocket_module.ws_areas, {}).sent[0][2], "unauthorized")
        self.assertEqual(self.entry.reauth, 1)
        self.hass.entries[:] = []
        self.assertEqual(self.call(websocket_module.ws_areas, {}).sent[0][2], "not_configured")
        self.assertEqual(self.call(websocket_module.ws_subscribe, {"area": "Austin"}).sent[0][2], "not_configured")

    def test_a_subscription_confirms_first_then_streams_changes_and_ends_with_the_connection(self):
        connection = self.call(websocket_module.ws_subscribe, {"area": "San Diego"})
        self.assertEqual([item[0] for item in connection.sent], ["result", "message"], "the card is told the subscription exists before its first view")
        first = connection.sent[1][1]
        self.assertEqual((first["id"], first["type"]), (5, "event"))
        self.assertEqual(len(first["event"]["events"]), 4)
        self.assertIn(5, connection.subscriptions)
        self.change_listing(11)
        run(self.entry.runtime_data.hub.refresh())
        self.assertEqual(len(connection.sent), 3, "one pushed update, no polling by the card")
        self.assertEqual({e["revision"] for e in connection.sent[2][1]["event"]["events"]}, {2})
        connection.subscriptions[5]()   # Home Assistant calls this when the card's connection closes
        self.assertEqual(self.entry.runtime_data.hub.followed, (0, 0))
        run(self.entry.runtime_data.hub.refresh())
        self.assertEqual(len(connection.sent), 3)

    def test_twenty_cards_over_the_websocket_share_one_hub_and_never_see_a_token(self):
        connections = []
        for i in range(20):
            connection = FakeConnection()
            run(websocket_module.ws_subscribe(self.hass, connection, {"id": 100 + i, "type": "area_events/subscribe", "area": "San Diego" if i < 10 else "Austin"}))
            connections.append(connection)
        self.assertEqual(self.entry.runtime_data.hub.followed, (2, 20))
        self.assertTrue(all(c.sent[1][1]["event"]["events"][0]["title"].startswith("Event" if i < 10 else "Austin") for i, c in enumerate(connections)))
        everything = json.dumps([c.sent for c in connections], default=str)
        for secret in (READ_TOKEN, CONTROL_TOKEN, PROCESS_TOKEN):
            self.assertNotIn(secret, everything)
        self.assertNotIn(self.url, everything)

    def test_the_card_script_and_commands_are_registered_once_and_the_served_card_follows_the_source(self):
        paths = []

        async def register(configs):
            paths.extend(configs)

        hass = types.SimpleNamespace(http=types.SimpleNamespace(async_register_static_paths=register, register_view=lambda view: None), async_add_executor_job=lambda func, *a: asyncio.sleep(0, func(*a)), data={})
        before = len(REGISTERED)
        self.assertTrue(run(init.async_setup(hass, {})))
        self.assertEqual(len(REGISTERED) - before, 13)
        self.assertEqual(paths[0][0][0], "/area_events/area-events-card.js")
        self.assertEqual(paths[1][0][0], "/area_events/manager.js")
        self.assertEqual(paths[2][0][0], "/area_events/presentation")
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "frontend", "presentation", "registry.js")))
        served = open(os.path.join(ROOT, "frontend", "area-events-card.js"), encoding="utf-8").read()
        self.assertIn("area_events/subscribe", served)
        self.assertNotIn("area_events/events", served)
        manifest = json.load(open(os.path.join(ROOT, "manifest.json"), encoding="utf-8"))
        self.assertTrue({"frontend", "http", "websocket_api", "panel_custom"} <= set(manifest["dependencies"]))
        strings = json.load(open(os.path.join(ROOT, "strings.json"), encoding="utf-8"))
        self.assertEqual(strings, json.load(open(os.path.join(ROOT, "translations", "en.json"), encoding="utf-8")))
        self.assertTrue({"read_token", "control_token", "url"} <= set(strings["config"]["step"]["user"]["data"]))


class NoGlobalState(unittest.TestCase):
    ALLOWED = ("frozenset", "tuple", "types.MappingProxyType", "logging.getLogger", "itertools.count", "re.compile")

    def test_the_integration_modules_hold_no_mutable_module_level_state(self):
        for name in sorted(os.listdir(ROOT)):
            if not name.endswith(".py") or name == "photos.py":
                continue
            with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in tree.body:
                self.assertFalse(isinstance(node, ast.Global), name)
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    value = node.value
                    self.assertFalse(isinstance(value, (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)), f"{name}: mutable module-level value")
                    if isinstance(value, ast.Call):
                        callee = ast.unparse(value.func)
                        self.assertTrue(callee in self.ALLOWED or callee.startswith("vol.") or callee.startswith("cv."), f"{name}: module-level call {callee}")

    def test_the_removed_legacy_modules_are_gone(self):
        for name in ("client.py", "profile.py"):
            self.assertFalse(os.path.exists(os.path.join(ROOT, name)), name)

    def test_the_integration_never_touches_a_database(self):
        for name in sorted(os.listdir(ROOT)):
            if name.endswith(".py"):
                with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
                    source = handle.read().lower()
                self.assertNotIn("sqlite", source, name)
                self.assertNotIn("requests.", source, name)
                self.assertNotIn("urllib.request", source, name)


if __name__ == "__main__":
    unittest.main()
