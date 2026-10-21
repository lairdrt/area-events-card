"""Config flow: found automatically through the app's Supervisor discovery, or entered by hand."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .client import AreaEventsAuthError, AreaEventsClient, AreaEventsConnectionError, normalize_base_url
from .const import CONF_TOKEN, CONF_URL, DOMAIN


class AreaEventsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    _discovered: dict[str, str]
    _app_name = "Area Events"

    async def _check(self, url: str, token: str) -> str | None:
        """None when the app answers and accepts the token, otherwise the error key."""
        try:
            await AreaEventsClient(async_get_clientsession(self.hass), url, token).status()
        except AreaEventsAuthError:
            return "invalid_auth"
        except AreaEventsConnectionError:
            return "cannot_connect"
        return None

    # --- the app announced itself (the normal way) ---------------------------------------------

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """The app sends its hostname, port and token through the Supervisor. There is one app, so one unique id."""
        config = discovery_info.config
        try:
            url = normalize_base_url(f"http://{config['host']}:{int(config['port'])}")
            token = str(config["token"])
        except (KeyError, TypeError, ValueError):
            return self.async_abort(reason="invalid_discovery")
        await self.async_set_unique_id(DOMAIN)
        # An app that restarted with a new hostname or token updates the existing entry without asking again.
        self._abort_if_unique_id_configured(updates={CONF_URL: url, CONF_TOKEN: token})
        self._discovered = {CONF_URL: url, CONF_TOKEN: token}
        self._app_name = discovery_info.name
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            if await self._check(self._discovered[CONF_URL], self._discovered[CONF_TOKEN]):
                return self.async_abort(reason="cannot_connect")
            return self.async_create_entry(title="Area Events", data=self._discovered)
        self._set_confirm_only()
        return self.async_show_form(step_id="hassio_confirm", description_placeholders={"name": self._app_name})

    # --- entered by hand (the app on another host, or a token chosen on purpose) ------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalize_base_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                if error := await self._check(url, user_input[CONF_TOKEN]):
                    errors["base"] = error
                else:
                    return self.async_create_entry(title="Area Events", data={CONF_URL: url, CONF_TOKEN: user_input[CONF_TOKEN]})
        schema = vol.Schema({vol.Required(CONF_URL, default=(user_input or {}).get(CONF_URL, "http://")): str, vol.Required(CONF_TOKEN): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    # --- the token stopped working ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := await self._check(entry.data[CONF_URL], user_input[CONF_TOKEN]):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: user_input[CONF_TOKEN]})
        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}), errors=errors)
