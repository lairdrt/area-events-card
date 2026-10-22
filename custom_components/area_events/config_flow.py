"""Config flow: the Discovery Service's address and tokens are entered by hand (the service runs on its own host, not as a Home Assistant app)."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import ssl
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (CONF_CA_FILE, CONF_CONTROL_TOKEN, CONF_READ_TOKEN, CONF_SCAN_INTERVAL, CONF_URL, DEFAULT_SCAN_INTERVAL, DOMAIN, MAX_SCAN_INTERVAL,
                    MIN_SCAN_INTERVAL)
from .discovery_client import DiscoveryAuthError, DiscoveryConnectionError, DiscoveryControlClient, DiscoveryRequestError, normalize_base_url

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
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        schema = vol.Schema({vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL))})
        return self.async_show_form(step_id="init", data_schema=schema)
