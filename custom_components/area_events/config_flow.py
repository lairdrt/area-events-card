"""Config flow: the Discovery Service's address and tokens are entered by hand (the service runs on its own host, not as a Home Assistant app)."""
from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any
import ssl
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (CONF_CA_FILE, CONF_CONTROL_TOKEN, CONF_READ_TOKEN, CONF_SCAN_INTERVAL, CONF_URL, DEFAULT_SCAN_INTERVAL, DOMAIN, MAX_SCAN_INTERVAL,
                    MIN_SCAN_INTERVAL)
from .discovery_client import (DiscoveryAuthError, DiscoveryConflictError, DiscoveryConnectionError, DiscoveryControlClient,
                               DiscoveryRequestError, normalize_base_url)

LOOPBACK = ("localhost", "127.0.0.1", "::1")


def transport_note(url: str) -> str:
    """Tokens travel in every request: say so when the address is a plain-http one that leaves this computer."""
    parts = urlsplit(url or "")
    if parts.scheme == "https" or (parts.hostname or "") in LOOPBACK or not parts.hostname:
        return ""
    return "This address uses plain http, so the tokens cross the network unprotected. Use an https address (a TLS reverse proxy in front of the service) outside a fully trusted network."


class AreaEventsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def _check(self, url: str, read_token: str, control_token: str | None, ca_file: str | None = None) -> str | None:
        """None when the service answers and accepts the tokens, otherwise the error key."""
        session = async_get_clientsession(self.hass)
        trust = None
        if ca_file:
            try:
                trust = await self.hass.async_add_executor_job(lambda: ssl.create_default_context(cafile=ca_file))
            except (OSError, ssl.SSLError):
                return "invalid_ca_file"
        try:
            await DiscoveryControlClient(session, url, read_token, trust).status()
            if control_token:
                await DiscoveryControlClient(session, url, control_token, trust).check_control()
        except DiscoveryAuthError as error:
            return "invalid_control_token" if control_token and error.status == 403 else "invalid_auth"
        except DiscoveryConnectionError:
            return "cannot_connect"
        except DiscoveryRequestError:
            return "cannot_connect"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        user_input = user_input or {}
        if user_input:
            try:
                url = normalize_base_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured()
                control = (user_input.get(CONF_CONTROL_TOKEN) or "").strip() or None
                ca_file = (user_input.get(CONF_CA_FILE) or "").strip() or None
                if error := await self._check(url, user_input[CONF_READ_TOKEN], control, ca_file):
                    errors["base"] = error
                else:
                    data = {CONF_URL: url, CONF_READ_TOKEN: user_input[CONF_READ_TOKEN]}
                    if control:
                        data[CONF_CONTROL_TOKEN] = control
                    if ca_file:
                        data[CONF_CA_FILE] = ca_file
                    return self.async_create_entry(title="Area Events", data=data)
        schema = vol.Schema({
            vol.Required(CONF_URL, default=user_input.get(CONF_URL, "https://")): str,
            vol.Required(CONF_READ_TOKEN): str,
            vol.Optional(CONF_CONTROL_TOKEN): str,
            vol.Optional(CONF_CA_FILE): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors,
                                    description_placeholders={"transport_note": transport_note(user_input.get(CONF_URL, ""))})

    # --- a token stopped working ------------------------------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            control = (user_input.get(CONF_CONTROL_TOKEN) or "").strip() or None
            if error := await self._check(entry.data[CONF_URL], user_input[CONF_READ_TOKEN], control, entry.data.get(CONF_CA_FILE)):
                errors["base"] = error
            else:
                updates = {CONF_READ_TOKEN: user_input[CONF_READ_TOKEN], CONF_CONTROL_TOKEN: control}
                return self.async_update_reload_and_abort(entry, data_updates={k: v for k, v in updates.items() if v})
        schema = vol.Schema({vol.Required(CONF_READ_TOKEN): str, vol.Optional(CONF_CONTROL_TOKEN): str})
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return AreaEventsOptionsFlow()


class AreaEventsOptionsFlow(OptionsFlow):
    def __init__(self):
        self._source_snapshot = None
        self._area_snapshot = None
        self._selected_source_key = None
        self._selected_area_key = None

    async def _client(self, control=False):
        token_key = CONF_CONTROL_TOKEN if control else CONF_READ_TOKEN
        token = self.config_entry.data.get(token_key)
        if control and not token:
            raise _ControlTokenMissing
        trust = None
        ca_file = self.config_entry.data.get(CONF_CA_FILE)
        if ca_file:
            trust = await self.hass.async_add_executor_job(lambda: ssl.create_default_context(cafile=ca_file))
        return DiscoveryControlClient(async_get_clientsession(self.hass), self.config_entry.data[CONF_URL], token, trust)

    @staticmethod
    def _error(error):
        if isinstance(error, _ControlTokenMissing):
            return "control_token_required"
        if isinstance(error, DiscoveryConflictError):
            return "revision_conflict"
        if isinstance(error, DiscoveryAuthError):
            return "control_token_required" if error.status == 403 else "invalid_auth"
        if isinstance(error, DiscoveryConnectionError):
            return "cannot_connect"
        if isinstance(error, DiscoveryRequestError):
            return "invalid_request"
        return "unknown"

    async def _finish_mutation(self):
        # Producer and area definitions live only in Cubi. HA options retain the independent scan interval.
        return self.async_create_entry(data=dict(self.config_entry.options))

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            if CONF_SCAN_INTERVAL in user_input:
                try:
                    interval = int(user_input[CONF_SCAN_INTERVAL])
                    if not MIN_SCAN_INTERVAL <= interval <= MAX_SCAN_INTERVAL:
                        raise ValueError
                except (TypeError, ValueError):
                    return await self.async_step_scan_interval(errors={"base": "invalid_scan_interval"})
                return self.async_create_entry(data={CONF_SCAN_INTERVAL: interval})
            action = user_input.get("action")
            if action == "sources":
                return await self.async_step_sources()
            if action == "areas":
                return await self.async_step_area_select()
            if action == "scan_interval":
                return await self.async_step_scan_interval()
        schema = vol.Schema({vol.Required("action"): vol.In({"sources": "Manage producer instances", "areas": "Attach instances to areas",
                                                              "scan_interval": "Change scan interval"})})
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_scan_interval(self, user_input=None, errors=None):
        if user_input is not None:
            return await self.async_step_init({CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL)})
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        schema = vol.Schema({vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL))})
        return self.async_show_form(step_id="scan_interval", data_schema=schema, errors=errors or {})

    async def async_step_sources(self, user_input=None, errors=None):
        try:
            client = await self._client()
            sources = await client.sources()
            status = await client.status()
        except (DiscoveryAuthError, DiscoveryConnectionError, DiscoveryRequestError, _ControlTokenMissing) as error:
            return self.async_show_form(step_id="sources", data_schema=vol.Schema({}), errors={"base": self._error(error)})
        statuses = {row.get("source_key"): row for row in status.get("sources", ())}
        lines = []
        for source in sorted(sources, key=lambda item: item["name"].casefold()):
            state = statuses.get(source["id"], {})
            if not source["enabled"]:
                eligibility = "Disabled"
            elif state.get("eligible"):
                eligibility = "Ready to discover"
            else:
                eligibility = "Waiting for an area assignment"
            # A brief human-readable summary, never raw JSON/URLs in a shared dialog.
            lines.append(f"- **{source['name']}** — {eligibility}; every {source['interval_minutes']} min")
        description = "\n".join(lines) if lines else "No producers configured yet."
        actions = {"create": "Create a producer (advanced)", "edit": "Edit name or schedule", "options": "Edit advanced producer settings", "enable": "Enable a producer", "disable": "Disable a producer", "back": "Back"}
        schema = vol.Schema({vol.Required("action"): vol.In(actions)})
        if user_input is None:
            return self.async_show_form(step_id="sources", data_schema=schema, errors=errors or {}, description_placeholders={"instances": description})
        action = user_input.get("action")
        if action == "create":
            return await self.async_step_source_create()
        if action in ("edit", "options", "enable", "disable"):
            self._source_action = action
            return await self.async_step_source_select()
        return await self.async_step_init()

    async def async_step_source_create(self, user_input=None, errors=None):
        if user_input is not None:
            try:
                options = json.loads(user_input["options"])
                if not isinstance(options, dict):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                return await self.async_step_source_create(errors={"options": "invalid_json"})
            try:
                client = await self._client(control=True)
                await client.create_source(user_input["id"], user_input["type"], user_input["name"], user_input["enabled"],
                                           user_input["interval_minutes"], options)
            except (DiscoveryAuthError, DiscoveryConnectionError, DiscoveryRequestError, DiscoveryConflictError, _ControlTokenMissing) as error:
                return await self.async_step_source_create(errors={"base": self._error(error)})
            return await self._finish_mutation()
        schema = vol.Schema({vol.Required("id"): str, vol.Required("type"): str, vol.Required("name"): str,
                             vol.Required("enabled", default=True): bool,
                             vol.Required("interval_minutes", default=60): vol.All(vol.Coerce(int), vol.Range(min=1)),
                             vol.Required("options", default="{}"): str})
        return self.async_show_form(step_id="source_create", data_schema=schema, errors=errors or {})

    async def async_step_source_select(self, user_input=None, errors=None):
        try:
            sources = await (await self._client()).sources()
        except (DiscoveryAuthError, DiscoveryConnectionError, DiscoveryRequestError) as error:
            return self.async_show_form(step_id="source_select", data_schema=vol.Schema({}), errors={"base": self._error(error)})
        choices = {source["id"]: f"{source['name']} — {'Enabled' if source['enabled'] else 'Disabled'} · {source['interval_minutes']} min" for source in sources}
        if user_input is not None:
            key = user_input["source_id"]
            try:
                source = await (await self._client()).source(key)
            except (DiscoveryAuthError, DiscoveryConnectionError, DiscoveryRequestError) as error:
                return self.async_show_form(step_id="source_select", data_schema=vol.Schema({}), errors={"base": self._error(error)})
            self._selected_source_key, self._source_snapshot = key, source
            if self._source_action == "edit":
                return await self.async_step_source_edit()
            if self._source_action == "options":
                return await self.async_step_source_options()
            return await self.async_step_source_toggle()
        schema = vol.Schema({vol.Required("source_id"): vol.In(choices)})
        return self.async_show_form(step_id="source_select", data_schema=schema, errors=errors or {})

    async def async_step_source_edit(self, user_input=None, errors=None):
        source = self._source_snapshot
        if user_input is not None:
            try:
                options = json.loads(user_input["options"])
                if not isinstance(options, dict):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                return await self.async_step_source_edit(errors={"options": "invalid_json"})
            try:
                client = await self._client(control=True)
                await client.update_source(self._selected_source_key, source["revision"], name=user_input["name"],
                                           interval_minutes=user_input["interval_minutes"], options=options)
            except DiscoveryConflictError:
                return self.async_abort(reason="revision_conflict")
            except (DiscoveryAuthError, DiscoveryConnectionError, DiscoveryRequestError, _ControlTokenMissing) as error:
                return await self.async_step_source_edit(errors={"base": self._error(error)})
            return await self._finish_mutation()
        schema = vol.Schema({vol.Required("name", default=source["name"]): str,
                             vol.Required("interval_minutes", default=source["interval_minutes"]): vol.All(vol.Coerce(int), vol.Range(min=1)),
                             vol.Required("options", default=json.dumps(source["options"], sort_keys=True)): str})
        return self.async_show_form(step_id="source_edit", data_schema=schema, errors=errors or {})

    async def async_step_source_toggle(self, user_input=None, errors=None):
        source = self._source_snapshot
        if user_input is not None:
            try:
                client = await self._client(control=True)
                await client.set_source_enabled(self._selected_source_key, source["revision"], user_input["enabled"])
            except DiscoveryConflictError:
                return self.async_abort(reason="revision_conflict")
            except (DiscoveryAuthError, DiscoveryConnectionError, DiscoveryRequestError, _ControlTokenMissing) as error:
                return await self.async_step_source_toggle(errors={"base": self._error(error)})
            return await self._finish_mutation()
        schema = vol.Schema({vol.Required("enabled", default=not source["enabled"]): bool})
        return self.async_show_form(step_id="source_toggle", data_schema=schema, errors=errors or {},
                                    description_placeholders={"source_id": self._selected_source_key, "enabled": str(source["enabled"]).lower()})

    async def async_step_area_select(self, user_input=None, errors=None):
        try:
            areas = await (await self._client()).areas()
        except (DiscoveryAuthError, DiscoveryConnectionError, DiscoveryRequestError) as error:
            return self.async_show_form(step_id="area_select", data_schema=vol.Schema({}), errors={"base": self._error(error)})
        choices = {area["area_key"]: f"{area['name']} ({'enabled' if area['enabled'] else 'disabled'})" for area in areas}
        if user_input is not None:
            self._selected_area_key = user_input["area_key"]
            try:
                self._area_snapshot = await (await self._client()).area(self._selected_area_key)
            except (DiscoveryAuthError, DiscoveryConnectionError, DiscoveryRequestError) as error:
                return self.async_show_form(step_id="area_select", data_schema=vol.Schema({}), errors={"base": self._error(error)})
            return await self.async_step_area_sources()
        return self.async_show_form(step_id="area_select", data_schema=vol.Schema({vol.Required("area_key"): vol.In(choices)}), errors=errors or {})

    async def async_step_area_sources(self, user_input=None, errors=None):
        area = self._area_snapshot
        try:
            sources = await (await self._client()).sources()
        except (DiscoveryAuthError, DiscoveryConnectionError, DiscoveryRequestError) as error:
            return self.async_show_form(step_id="area_sources", data_schema=vol.Schema({}), errors={"base": self._error(error)})
        choices = {source["id"]: f"{source['name']} ({source['id']})" for source in sources}
        if user_input is not None:
            try:
                client = await self._client(control=True)
                attached = list(area.get("sources", ()))
                key, action = user_input["source_id"], user_input["action"]
                if action == "attach" and key not in attached:
                    attached.append(key)
                elif action == "detach" and key in attached:
                    attached.remove(key)
                else:
                    return await self._finish_mutation()
                await client.update_area(self._selected_area_key, area["revision"], sources=attached)
            except DiscoveryConflictError:
                return self.async_abort(reason="revision_conflict")
            except (DiscoveryAuthError, DiscoveryConnectionError, DiscoveryRequestError, _ControlTokenMissing) as error:
                return await self.async_step_area_sources(errors={"base": self._error(error)})
            return await self._finish_mutation()
        attached = set(area.get("sources", ()))
        actions = {"attach": "Attach", "detach": "Detach"}
        schema = vol.Schema({vol.Required("source_id"): vol.In(choices), vol.Required("action"): vol.In(actions)})
        return self.async_show_form(step_id="area_sources", data_schema=schema, errors=errors or {},
                                    description_placeholders={"area_name": area["name"], "attached": ", ".join(sorted(attached)) or "none"})


class _ControlTokenMissing(Exception):
    pass
