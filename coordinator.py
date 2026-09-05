"""Data coordinator for Whisker Ting.

Two tiers: a REST poll on UPDATE_INTERVAL for device/hazard state, and a
persistent SignalR websocket per device for real-time voltage - opened once
after the first successful REST fetch and left running, reconnecting on its
own (see websocket.py). Same "never raise UpdateFailed on a transient miss"
posture as host_monitor/kiosk_pi in this repo: a coordinator that goes
unavailable takes every entity with it, and a REST hiccup is not the same
fact as the device being gone. Only a real credential failure escalates to
ConfigEntryAuthFailed, since that is genuinely actionable and ha's own
reauth flow exists for it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import DeviceState, VoltageReading, WhiskerApiClient, WhiskerApiError, WhiskerAuthError
from .const import DOMAIN, TRANSPORT_FAIL_DWELL, UPDATE_INTERVAL
from .websocket import VoltageData, WhiskerWebSocketManager

_LOGGER = logging.getLogger(__name__)


class WhiskerDataUpdateCoordinator(DataUpdateCoordinator[dict[str, DeviceState]]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: WhiskerApiClient,
        session: aiohttp.ClientSession,
    ) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}:{entry.entry_id}", update_interval=UPDATE_INTERVAL)
        self.entry = entry
        self.client = client
        self._session = session
        self._ws_manager: WhiskerWebSocketManager | None = None
        self._ws_connected = False
        self._rest_fails = 0

    @callback
    def _handle_voltage_update(self, station_id: str, voltage_data: VoltageData) -> None:
        if self.data is None:
            return
        for device_state in self.data.values():
            if device_state.station_id == station_id:
                device_state.voltage = VoltageReading(
                    voltage=voltage_data.voltage,
                    voltage_hi=voltage_data.voltage_hi,
                    voltage_lo=voltage_data.voltage_lo,
                    average_peaks_max=voltage_data.average_peaks_max,
                    stale=False,
                )
                self.async_set_updated_data(self.data)
                break

    async def _connect_websockets(self, data: dict[str, DeviceState]) -> None:
        if self._ws_manager is None:
            self._ws_manager = WhiskerWebSocketManager(self._session, on_voltage_update=self._handle_voltage_update)
        if self._ws_connected or not data:
            return

        api_key = self.client.api_key
        user_id = self.client.user_id
        if not api_key or not user_id:
            return

        results = await asyncio.gather(
            *(
                self._ws_manager.connect_device(api_key, user_id, device.station_id)
                for device in data.values()
            ),
            return_exceptions=True,
        )
        if any(r is True for r in results):
            self._ws_connected = True

    async def async_shutdown(self) -> None:
        if self._ws_manager:
            await self._ws_manager.disconnect_all()
            self._ws_connected = False
        await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, DeviceState]:
        try:
            data = await self.client.get_all_device_states()
        except WhiskerAuthError as err:
            raise ConfigEntryAuthFailed("Whisker Ting credentials rejected") from err
        except WhiskerApiError as err:
            self._rest_fails += 1
            if self._rest_fails == TRANSPORT_FAIL_DWELL:
                _LOGGER.warning("Whisker Ting REST unreachable for %d polls: %s", TRANSPORT_FAIL_DWELL, err)
            if self.data is not None:
                # Preserve last known state rather than blanking every entity
                # over a transient REST miss - the websocket may still be live.
                return self.data
            raise

        if self._rest_fails >= TRANSPORT_FAIL_DWELL:
            _LOGGER.info("Whisker Ting REST reachable again after %d failed polls", self._rest_fails)
        self._rest_fails = 0

        if self.data:
            for device_id, device_state in data.items():
                existing = self.data.get(device_id)
                if existing and not existing.voltage.stale:
                    device_state.voltage = existing.voltage

        if not self._ws_connected:
            await self._connect_websockets(data)
            if self._ws_connected and self._ws_manager:
                await asyncio.gather(
                    *(
                        self._ws_manager.wait_for_data(d.station_id, timeout=5.0)
                        for d in data.values()
                    ),
                    return_exceptions=True,
                )
                for device_state in data.values():
                    live = self._ws_manager.get_voltage_data(device_state.station_id)
                    if live:
                        device_state.voltage = VoltageReading(
                            voltage=live.voltage,
                            voltage_hi=live.voltage_hi,
                            voltage_lo=live.voltage_lo,
                            average_peaks_max=live.average_peaks_max,
                            stale=False,
                        )

        return data
