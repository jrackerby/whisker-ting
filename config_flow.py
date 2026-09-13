"""Config flow for Whisker Ting."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

import aiohttp

from .api import UserData, WhiskerApiClient, WhiskerAuthError, WhiskerConnectionError
from .auth import AuthenticationError
from .const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .websocket import WhiskerWebSocket

_LOGGER = logging.getLogger(__name__)

_SCHEMA = vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str})


class WhiskerTingConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    async def _probe_signalr(
        session: aiohttp.ClientSession, client: WhiskerApiClient, user_data: UserData
    ) -> bool:
        """Certify the channel this integration will actually stream on.

        LAW §9: a setup check that exercises a different channel than the one
        that will be used certifies nothing, and certifies it GREEN. Cognito
        plus one REST GET was the whole of the old check, while the primary
        data path is the SignalR hub - a different endpoint, on a different
        credential (`custom:api_key`, not the bearer token), which 403s
        without its own Origin header. An entry could therefore be created
        green while the voltage stream could not connect at all, and the only
        sign of it was a log line after the fact.

        Handshake and InitializeStreaming only; this deliberately does NOT
        wait for a reading. Voltage arrives when the hub sends it, so waiting
        would make the outcome depend on timing rather than on reachability.
        """
        if not user_data.devices:
            # No Ting on the account, so there is no station to subscribe to
            # and nothing the hub could refuse. The REST check already covered
            # everything this entry will read.
            _LOGGER.debug("no devices on the account; skipping the SignalR probe")
            return True

        api_key = client.api_key
        user_id = client.user_id
        if not api_key or not user_id:
            _LOGGER.warning("Cognito returned no custom:api_key; the SignalR hub cannot be reached")
            return False

        ws = WhiskerWebSocket(session, api_key, user_id, user_data.devices[0].station_id)
        try:
            return await ws.connect()
        finally:
            # connect() leaves three background tasks running on success.
            await ws.disconnect()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = WhiskerApiClient(session, user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            try:
                user_data = await client.get_user_data()
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except WhiskerAuthError:
                errors["base"] = "invalid_auth"
            except WhiskerConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during Whisker Ting login")
                errors["base"] = "unknown"
            else:
                if not await self._probe_signalr(session, client, user_data):
                    errors["base"] = "cannot_connect_stream"
                else:
                    await self.async_set_unique_id(str(user_data.user_id))
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"Whisker Ting ({user_input[CONF_USERNAME]})",
                        data={CONF_USERNAME: user_input[CONF_USERNAME], CONF_PASSWORD: user_input[CONF_PASSWORD]},
                    )

        return self.async_show_form(step_id="user", data_schema=_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = WhiskerApiClient(session, user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            try:
                await client.get_user_data()
            except (AuthenticationError, WhiskerAuthError):
                errors["base"] = "invalid_auth"
            except WhiskerConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during Whisker Ting reauth")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={CONF_USERNAME: user_input[CONF_USERNAME], CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )

        return self.async_show_form(step_id="reauth_confirm", data_schema=_SCHEMA, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return WhiskerTingOptionsFlow()


class WhiskerTingOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                        vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                    )
                }
            ),
        )
