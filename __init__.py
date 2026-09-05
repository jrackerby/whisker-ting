"""The Whisker Ting integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WhiskerApiClient
from .const import CONF_PASSWORD, CONF_USERNAME, PLATFORMS
from .coordinator import WhiskerDataUpdateCoordinator

type WhiskerTingConfigEntry = ConfigEntry[WhiskerDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: WhiskerTingConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = WhiskerApiClient(session, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    coordinator = WhiskerDataUpdateCoordinator(hass, entry, client, session)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WhiskerTingConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: WhiskerTingConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
