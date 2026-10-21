"""Config flow: the address of the Area Events app and its token."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import AreaEventsAuthError, AreaEventsClient, AreaEventsConnectionError, normalize_base_url
from .const import CONF_TOKEN, CONF_URL, DOMAIN


class AreaEventsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _check(self, url: str, token: str) -> str | None:
        """None when the app answers and accepts the token, otherwise the error key."""
        try:
            await AreaEventsClient(async_get_clientsession(self.hass), url, token).status()
        except AreaEventsAuthError:
            return "invalid_auth"
        except AreaEventsConnectionError:
            return "cannot_connect"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalize_base_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured()
                if error := await self._check(url, user_input[CONF_TOKEN]):
                    errors["base"] = error
                else:
                    return self.async_create_entry(title="Area Events", data={CONF_URL: url, CONF_TOKEN: user_input[CONF_TOKEN]})
        schema = vol.Schema({vol.Required(CONF_URL, default=(user_input or {}).get(CONF_URL, "http://")): str, vol.Required(CONF_TOKEN): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

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
