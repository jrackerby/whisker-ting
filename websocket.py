"""WebSocket transport for the real-time Whisker Ting voltage stream.

Wires signalr_protocol's pure framing/encoding onto an aiohttp WS connection
and manages one connection per device (station), with reconnect-with-backoff
on drop or on data going stale.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import aiohttp

from . import signalr_protocol as sr
from .const import (
    RECONNECT_BACKOFF_FACTOR,
    RECONNECT_MAX_DELAY,
    RECONNECT_MIN_DELAY,
    SIGNALR_ORIGIN,
    SIGNALR_URL,
    STALE_DATA_THRESHOLD,
)

_LOGGER = logging.getLogger(__name__)

VOLTAGE_TARGET = "updateComboBinaryData"

# Field names observed to appear in the Whisker mobile app's own UI copy for
# this stream; tried case-insensitively before falling back to position.
_VOLTAGE_KEYS = ("voltage",)
_VOLTAGE_HI_KEYS = ("voltagehi", "voltage_hi", "vhi")
_VOLTAGE_LO_KEYS = ("voltagelo", "voltage_lo", "vlo")
_PEAKS_KEYS = ("averagepeaksmax", "average_peaks_max", "peaks")


@dataclass
class VoltageData:
    timestamp: datetime
    voltage: float
    voltage_hi: float
    voltage_lo: float
    average_peaks_max: float
    source: str  # "structured" or "positional-fallback" - surfaced for diagnosis


def _lower_key_map(d: dict[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in d.items()}


def _plausible_voltage(v: float) -> bool:
    return 1.0 <= abs(v) <= 1000.0


def _from_structured_dict(d: dict[str, Any]) -> VoltageData | None:
    lowered = _lower_key_map(d)

    def pick(keys: tuple[str, ...]) -> float | None:
        for k in keys:
            if k in lowered and isinstance(lowered[k], (int, float)):
                return float(lowered[k])
        return None

    voltage = pick(_VOLTAGE_KEYS)
    if voltage is None or not _plausible_voltage(voltage):
        return None
    return VoltageData(
        timestamp=datetime.now(),
        voltage=voltage,
        voltage_hi=pick(_VOLTAGE_HI_KEYS) or voltage,
        voltage_lo=pick(_VOLTAGE_LO_KEYS) or voltage,
        average_peaks_max=pick(_PEAKS_KEYS) or 0.0,
        source="structured",
    )


def _from_positional_floats(values: list[float]) -> VoltageData | None:
    if len(values) < 4:
        return None
    voltage, peaks, voltage_hi, voltage_lo = values[:4]
    if not _plausible_voltage(voltage):
        return None
    return VoltageData(
        timestamp=datetime.now(),
        voltage=voltage,
        average_peaks_max=peaks,
        voltage_hi=voltage_hi,
        voltage_lo=voltage_lo,
        source="positional-fallback",
    )


def _scan_raw_float64s(payload: bytes) -> list[float]:
    """Last-resort scan for 0xCB (float64) markers in the raw payload bytes.
    Kept as a fallback ONLY - if the structured-argument parse below can't
    make sense of the invocation, this preserves the previous integrations'
    behavior rather than going dark, but every hit is logged so a live
    install shows which path actually fired."""
    out = []
    pos = 0
    while pos < len(payload) - 8:
        if payload[pos] == 0xCB:
            out.append(struct.unpack(">d", payload[pos + 1 : pos + 9])[0])
            pos += 9
        else:
            pos += 1
    return out


def decode_voltage_invocation(inv: sr.HubInvocation, raw_payload: bytes) -> VoltageData | None:
    """arguments is normally [device_or_metadata, payload] or similar for a
    hub push; try every list/dict argument for a structured match before
    falling back to scanning the raw frame bytes."""
    for arg in inv.arguments:
        if isinstance(arg, dict):
            result = _from_structured_dict(arg)
            if result:
                return result
        elif isinstance(arg, list) and all(isinstance(x, (int, float)) for x in arg):
            result = _from_positional_floats([float(x) for x in arg])
            if result:
                return result

    _LOGGER.warning(
        "voltage push for %s did not match a known structured shape "
        "(arg types: %s) - falling back to raw float64 scan; if this "
        "fires, the payload shape needs a live capture to confirm",
        inv.target,
        [type(a).__name__ for a in inv.arguments],
    )
    scanned = _scan_raw_float64s(raw_payload)
    return _from_positional_floats(scanned)


class WhiskerWebSocket:
    """One SignalR connection, subscribed to one device's voltage stream."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        user_id: int,
        station_id: str,
        on_voltage_update: Callable[[str, VoltageData], None] | None = None,
        on_disconnect: Callable[[str], None] | None = None,
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._user_id = user_id
        self._station_id = station_id
        self._on_voltage_update = on_voltage_update
        self._on_disconnect = on_disconnect
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._connected = False
        self._shutting_down = False
        self._tasks: list[asyncio.Task] = []
        self._first_data = asyncio.Event()
        self._last_data_at: datetime | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        try:
            self._ws = await self._session.ws_connect(
                SIGNALR_URL, headers={"Origin": SIGNALR_ORIGIN}
            )

            await self._ws.send_str(sr.encode_handshake_request())
            handshake_msg = await self._ws.receive(timeout=10)
            handshake_text = (
                handshake_msg.data.decode(errors="replace")
                if isinstance(handshake_msg.data, bytes)
                else str(handshake_msg.data or "")
            )
            error = sr.parse_handshake_response(handshake_text)
            if error:
                _LOGGER.error("SignalR handshake refused for station %s: %s", self._station_id, error)
                self._connected = False
                return False

            init_args = [
                {"StationId": self._station_id, "DataElement": "ComboBinaryData"},
                self._api_key,
                str(self._user_id),
            ]
            await self._ws.send_bytes(sr.encode_invocation("InitializeStreaming", init_args))

            self._connected = True
            self._last_data_at = datetime.now()
            self._tasks = [
                asyncio.create_task(self._receive_loop()),
                asyncio.create_task(self._ping_loop()),
                asyncio.create_task(self._stale_check_loop()),
            ]
            _LOGGER.info("Connected to SignalR hub for station %s", self._station_id)
            return True
        except Exception as err:
            _LOGGER.error("Failed to connect to SignalR hub for station %s: %s", self._station_id, err)
            self._connected = False
            return False

    async def disconnect(self) -> None:
        self._shutting_down = True
        self._connected = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    async def wait_for_data(self, timeout: float = 5.0) -> bool:
        try:
            await asyncio.wait_for(self._first_data.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _receive_loop(self) -> None:
        while self._connected and self._ws and not self._ws.closed:
            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=30)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            if msg.type == aiohttp.WSMsgType.BINARY:
                self._handle_binary(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                _LOGGER.warning("SignalR socket closed/error for station %s: %s", self._station_id, msg.type)
                self._connected = False
                break

        if not self._shutting_down and self._on_disconnect:
            self._on_disconnect(self._station_id)

    def _handle_binary(self, data: bytes) -> None:
        try:
            frames = sr.split_frames(data)
        except ValueError as err:
            _LOGGER.debug("station %s: unparseable frame batch: %s", self._station_id, err)
            return

        for payload in frames:
            decoded = sr.decode_message(payload)
            if isinstance(decoded, sr.HubInvocation) and decoded.target == VOLTAGE_TARGET:
                voltage = decode_voltage_invocation(decoded, payload)
                if voltage is None:
                    continue
                self._last_data_at = datetime.now()
                if self._on_voltage_update:
                    self._on_voltage_update(self._station_id, voltage)
                if not self._first_data.is_set():
                    self._first_data.set()
            elif decoded == sr.MSG_PING:
                _LOGGER.debug("station %s: ping ack", self._station_id)

    async def _stale_check_loop(self) -> None:
        while self._connected and not self._shutting_down:
            try:
                await asyncio.sleep(STALE_DATA_THRESHOLD)
            except asyncio.CancelledError:
                break
            if not self._connected or self._shutting_down:
                break
            if self._last_data_at and (datetime.now() - self._last_data_at).total_seconds() > STALE_DATA_THRESHOLD:
                _LOGGER.warning("station %s: no data in %ss, reconnecting", self._station_id, STALE_DATA_THRESHOLD)
                self._connected = False
                if self._on_disconnect:
                    self._on_disconnect(self._station_id)
                break

    async def _ping_loop(self) -> None:
        while self._connected and self._ws and not self._ws.closed:
            try:
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                break
            if self._connected and self._ws and not self._ws.closed:
                await self._ws.send_bytes(sr.encode_ping())


class WhiskerWebSocketManager:
    """Owns one WhiskerWebSocket per subscribed station, with reconnect."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        on_voltage_update: Callable[[str, VoltageData], None] | None = None,
    ) -> None:
        self._session = session
        self._on_voltage_update = on_voltage_update
        self._connections: dict[str, WhiskerWebSocket] = {}
        self._voltage_data: dict[str, VoltageData] = {}
        self._credentials: dict[str, dict[str, Any]] = {}
        self._reconnect_tasks: dict[str, asyncio.Task] = {}
        self._reconnect_attempts: dict[str, int] = {}
        self._shutting_down = False

    def get_voltage_data(self, station_id: str) -> VoltageData | None:
        return self._voltage_data.get(station_id)

    def _handle_voltage_update(self, station_id: str, data: VoltageData) -> None:
        self._voltage_data[station_id] = data
        self._reconnect_attempts[station_id] = 0
        if self._on_voltage_update:
            self._on_voltage_update(station_id, data)

    def _handle_disconnect(self, station_id: str) -> None:
        if self._shutting_down:
            return
        self._connections.pop(station_id, None)
        if station_id not in self._reconnect_tasks or self._reconnect_tasks[station_id].done():
            self._reconnect_tasks[station_id] = asyncio.create_task(self._reconnect(station_id))

    async def _reconnect(self, station_id: str) -> None:
        creds = self._credentials.get(station_id)
        if not creds:
            return
        attempts = self._reconnect_attempts.get(station_id, 0)
        delay = min(RECONNECT_MIN_DELAY * (RECONNECT_BACKOFF_FACTOR**attempts), RECONNECT_MAX_DELAY)
        _LOGGER.info("Reconnecting to station %s in %.0fs (attempt %d)", station_id, delay, attempts + 1)
        await asyncio.sleep(delay)
        if self._shutting_down:
            return
        self._reconnect_attempts[station_id] = attempts + 1

        ws = WhiskerWebSocket(
            session=self._session,
            api_key=creds["api_key"],
            user_id=creds["user_id"],
            station_id=station_id,
            on_voltage_update=self._handle_voltage_update,
            on_disconnect=self._handle_disconnect,
        )
        if await ws.connect():
            self._connections[station_id] = ws
        elif not self._shutting_down:
            self._reconnect_tasks[station_id] = asyncio.create_task(self._reconnect(station_id))

    async def connect_device(self, api_key: str, user_id: int, station_id: str) -> bool:
        if station_id in self._connections:
            return True
        self._credentials[station_id] = {"api_key": api_key, "user_id": user_id}
        self._reconnect_attempts[station_id] = 0

        ws = WhiskerWebSocket(
            session=self._session,
            api_key=api_key,
            user_id=user_id,
            station_id=station_id,
            on_voltage_update=self._handle_voltage_update,
            on_disconnect=self._handle_disconnect,
        )
        if await ws.connect():
            self._connections[station_id] = ws
            return True
        return False

    async def disconnect_all(self) -> None:
        self._shutting_down = True
        for task in self._reconnect_tasks.values():
            task.cancel()
        for task in list(self._reconnect_tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._reconnect_tasks.clear()
        for station_id, ws in list(self._connections.items()):
            await ws.disconnect()
            del self._connections[station_id]

    async def wait_for_data(self, station_id: str, timeout: float = 5.0) -> bool:
        ws = self._connections.get(station_id)
        return await ws.wait_for_data(timeout=timeout) if ws else False
