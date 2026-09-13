"""Data coordinator for Whisker Ting.

Two tiers: a REST poll for device/hazard state on the entry's own
`scan_interval` option (DEFAULT_SCAN_INTERVAL when unset), and a
persistent SignalR websocket per device for real-time voltage - opened once
after the first successful REST fetch and left running, reconnecting on its
own (see websocket.py).

WHY THIS ONE DOES GO UNAVAILABLE, unlike host_monitor/kiosk_pi. Those read
other entities and have no service to lose, so a coordinator that vanishes
with its subject is the defect their never-raise contract exists to refuse.
This one reads a vendor cloud that IS the subject: when it is unreachable
there is nothing left that knows whether the hazard flags are current. So a
transient miss is still carried forward - a REST hiccup is not the device
being gone - but the carry-forward is BOUNDED by MAX_CARRY_FORWARD_POLLS,
after which the entities go unavailable rather than keep publishing a
reading nobody can date. LAW §15's "apply a rule where it governs" is the
whole of the difference.

Only a real credential failure escalates to ConfigEntryAuthFailed, since
that is genuinely actionable and ha's own reauth flow exists for it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeviceState, VoltageReading, WhiskerApiClient, WhiskerApiError, WhiskerAuthError
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_CARRY_FORWARD_POLLS,
    TRANSPORT_FAIL_DWELL,
)
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
        # The options flow has always offered scan_interval and nothing ever
        # read it back - the poll ran at a module constant whatever the user
        # set. The update listener in __init__.py reloads the entry on an
        # options change, which rebuilds this coordinator, so reading it here
        # is the whole of what makes that control real.
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{entry.entry_id}",
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.entry = entry
        self.client = client
        self._session = session
        self._ws_manager: WhiskerWebSocketManager | None = None
        # Which stations actually hold a live socket. Per-station, not one
        # global flag: with a single flag a Ting added later never got a
        # socket, because the flag was already True for the others.
        self._ws_stations: set[str] = set()
        self._rest_fails = 0
        # True once the carry-forward ceiling is spent. Read by the voltage
        # callback so a live socket cannot quietly re-publish REST-sourced
        # hazard state that nothing has been able to refresh.
        self._rest_exhausted = False
        # Serials this coordinator has already seen. The difference against a
        # fresh poll is what makes a new Ting appear and a removed one leave.
        self._known_serials: set[str] = set()

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
                # async_set_updated_data sets last_update_success back to True.
                # While the REST carry-forward is spent that would UNDO the
                # unavailability this coordinator just declared, and republish
                # hazard flags nothing has refreshed - the exact defect the
                # ceiling exists to close. The entities are unavailable anyway,
                # so there is nothing to publish to; the value is kept on the
                # dataclass and surfaces on the next successful poll.
                if not self._rest_exhausted:
                    self.async_set_updated_data(self.data)
                break

    async def _sync_websockets(self, data: dict[str, DeviceState]) -> None:
        """Open a socket for every station that lacks one, close the ones whose
        device has left the account. Runs on every poll: a station whose connect
        failed is simply not in _ws_stations, so the next poll retries it."""
        if self._ws_manager is None:
            self._ws_manager = WhiskerWebSocketManager(
                self._session, on_voltage_update=self._handle_voltage_update
            )

        api_key = self.client.api_key
        user_id = self.client.user_id
        if not api_key or not user_id:
            return

        wanted = {device.station_id for device in data.values()}

        for station_id in self._ws_stations - wanted:
            await self._ws_manager.disconnect_device(station_id)
            self._ws_stations.discard(station_id)

        new = sorted(wanted - self._ws_stations)
        if not new:
            return

        results = await asyncio.gather(
            *(self._ws_manager.connect_device(api_key, user_id, station_id) for station_id in new),
            return_exceptions=True,
        )
        connected = [station for station, result in zip(new, results) if result is True]
        self._ws_stations.update(connected)
        if not connected:
            return

        # Give the freshly opened sockets one short window to deliver a first
        # reading, so the entities do not sit at `unknown` for a whole poll.
        await asyncio.gather(
            *(self._ws_manager.wait_for_data(station_id, timeout=5.0) for station_id in connected),
            return_exceptions=True,
        )
        for device_state in data.values():
            if device_state.station_id not in connected:
                continue
            live = self._ws_manager.get_voltage_data(device_state.station_id)
            if live:
                device_state.voltage = VoltageReading(
                    voltage=live.voltage,
                    voltage_hi=live.voltage_hi,
                    voltage_lo=live.voltage_lo,
                    average_peaks_max=live.average_peaks_max,
                    stale=False,
                )

    def _prune_stale_devices(self, serials: set[str]) -> None:
        """A Ting removed from the Whisker account used to leave its registry
        row behind for ever - its entities merely went unavailable, which reads
        identically to a cloud outage. Release the row instead."""
        gone = self._known_serials - serials
        if not gone:
            return
        registry = dr.async_get(self.hass)
        for serial in gone:
            device = registry.async_get_device(identifiers={(DOMAIN, serial)})
            if device is None:
                continue
            # remove_config_entry_id, not async_remove_device: the row is only
            # ours to delete if no other entry still claims it.
            registry.async_update_device(device.id, remove_config_entry_id=self.entry.entry_id)
            _LOGGER.info(
                "Whisker Ting %s is no longer on the account; its device registry row was released",
                serial,
            )

    async def async_shutdown(self) -> None:
        if self._ws_manager:
            await self._ws_manager.disconnect_all()
            self._ws_stations.clear()
        await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, DeviceState]:
        try:
            data = await self.client.get_all_device_states()
        except WhiskerAuthError as err:
            raise ConfigEntryAuthFailed("Whisker Ting credentials rejected") from err
        except WhiskerApiError as err:
            self._rest_fails += 1
            if self._rest_fails == TRANSPORT_FAIL_DWELL:
                _LOGGER.warning(
                    "Whisker Ting REST unreachable for %d polls: %s", TRANSPORT_FAIL_DWELL, err
                )
            if self.data is not None and self._rest_fails < MAX_CARRY_FORWARD_POLLS:
                # Preserve last known state rather than blanking every entity
                # over a transient REST miss - the websocket may still be live.
                return self.data
            if self.data is not None and not self._rest_exhausted:
                self._rest_exhausted = True
                _LOGGER.warning(
                    "Whisker Ting REST unreachable for %d polls; entities go unavailable rather "
                    "than keep publishing state nobody can refresh",
                    self._rest_fails,
                )
            raise UpdateFailed(f"Whisker Ting cloud unreachable: {err}") from err

        if self._rest_fails >= TRANSPORT_FAIL_DWELL:
            _LOGGER.info("Whisker Ting REST reachable again after %d failed polls", self._rest_fails)
        self._rest_fails = 0
        self._rest_exhausted = False

        if self.data:
            for device_id, device_state in data.items():
                existing = self.data.get(device_id)
                if existing and not existing.voltage.stale:
                    device_state.voltage = existing.voltage

        await self._sync_websockets(data)

        serials = set(data)
        self._prune_stale_devices(serials)
        self._known_serials = serials

        return data
