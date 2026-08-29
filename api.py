"""REST client for Whisker Ting device state."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from .auth import AuthenticationError, WhiskerAuth
from .const import API_BASE_URL, API_USERS_ENDPOINT, NO_HAZARD_MESSAGE

_LOGGER = logging.getLogger(__name__)


@dataclass
class HazardStatus:
    status: str | None = None
    timestamp_utc: str | None = None
    level: int | None = None
    message: str = NO_HAZARD_MESSAGE


@dataclass
class FireHazardStatus:
    learning_mode: bool = False
    message: str = NO_HAZARD_MESSAGE
    efh: HazardStatus = field(default_factory=HazardStatus)
    ufh: HazardStatus = field(default_factory=HazardStatus)


@dataclass
class VoltageReading:
    voltage: float = 0.0
    voltage_hi: float = 0.0
    voltage_lo: float = 0.0
    average_peaks_max: float = 0.0
    stale: bool = True


@dataclass
class DeviceState:
    serial_number: str
    name: str
    device_type: str
    site_id: int
    version: str | None = None
    is_fire: bool = False
    is_hvac_verified: bool = False
    has_frozen_pipe: bool = False
    fire_hazard: FireHazardStatus = field(default_factory=FireHazardStatus)
    voltage: VoltageReading = field(default_factory=VoltageReading)
    group_name: str | None = None

    @property
    def station_id(self) -> str:
        """The websocket subscribes by serial number, not a separate id."""
        return self.serial_number


@dataclass
class UserData:
    user_id: int
    email: str
    devices: list[DeviceState] = field(default_factory=list)


class WhiskerApiError(Exception):
    """Base exception for Whisker API errors."""


class WhiskerAuthError(WhiskerApiError):
    """Credentials rejected - a reauth is required, not a retry."""


class WhiskerConnectionError(WhiskerApiError):
    """Transient - network or server trouble."""


class WhiskerApiClient:
    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._auth = WhiskerAuth(session)

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._api_key: str | None = None
        self._user_id: int | None = None
        self._token_expiry: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def user_id(self) -> int | None:
        return self._user_id

    @property
    def api_key(self) -> str | None:
        return self._api_key

    async def _ensure_token(self) -> str:
        async with self._lock:
            if self._access_token and self._token_expiry and datetime.now() < self._token_expiry - timedelta(minutes=5):
                return self._access_token
            if self._refresh_token:
                try:
                    await self._refresh()
                    return self._access_token  # type: ignore[return-value]
                except AuthenticationError:
                    pass
            await self._authenticate()
            return self._access_token  # type: ignore[return-value]

    async def _authenticate(self) -> None:
        try:
            result = await self._auth.authenticate(self._username, self._password)
        except AuthenticationError as err:
            raise WhiskerAuthError(str(err)) from err

        self._access_token = result["access_token"]
        self._refresh_token = result["refresh_token"]
        self._token_expiry = datetime.now() + timedelta(hours=1)

        attrs = {a["Name"]: a["Value"] for a in result.get("user_attributes", [])}
        self._user_id = int(attrs.get("custom:user_id", 0))
        self._api_key = attrs.get("custom:api_key")

    async def _refresh(self) -> None:
        try:
            result = await self._auth.refresh_tokens(self._refresh_token)  # type: ignore[arg-type]
        except AuthenticationError as err:
            raise WhiskerAuthError(str(err)) from err
        self._access_token = result["AccessToken"]
        self._token_expiry = datetime.now() + timedelta(hours=1)

    async def _request(self, method: str, endpoint: str) -> dict[str, Any]:
        token = await self._ensure_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "x-wl-api-key": self._api_key or "",
        }
        url = f"{API_BASE_URL}{endpoint}"
        try:
            async with self._session.request(method, url, headers=headers) as resp:
                if resp.status == 401:
                    async with self._lock:
                        await self._authenticate()
                    headers["Authorization"] = f"Bearer {self._access_token}"
                    async with self._session.request(method, url, headers=headers) as retry:
                        if retry.status == 401:
                            raise WhiskerAuthError("authentication failed after refresh")
                        retry.raise_for_status()
                        return await retry.json()
                if resp.status != 200:
                    raise WhiskerApiError(f"{method} {endpoint} -> HTTP {resp.status}: {await resp.text()}")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise WhiskerConnectionError(str(err)) from err

    async def get_user_data(self) -> UserData:
        if not self._user_id:
            await self._ensure_token()
        data = await self._request("GET", API_USERS_ENDPOINT.format(user_id=self._user_id))
        return self._parse_user_data(data)

    async def get_all_device_states(self) -> dict[str, DeviceState]:
        return {d.serial_number: d for d in (await self.get_user_data()).devices}

    def _parse_user_data(self, data: dict[str, Any]) -> UserData:
        return UserData(
            user_id=data.get("id", 0),
            email=data.get("email", ""),
            devices=[self._parse_device(d) for d in data.get("devices", [])],
        )

    def _parse_device(self, data: dict[str, Any]) -> DeviceState:
        fhs = data.get("fireHazardStatus", {}) or {}
        group = data.get("group") or {}

        def _hazard(key: str) -> HazardStatus:
            h = fhs.get(key, {}) or {}
            return HazardStatus(
                status=h.get("status"),
                timestamp_utc=h.get("timestampUtc"),
                level=h.get("level"),
                message=h.get("message", NO_HAZARD_MESSAGE),
            )

        return DeviceState(
            serial_number=data.get("serialNumber", ""),
            name=data.get("name") or data.get("serialNumber", ""),
            device_type=data.get("type", "Unknown"),
            site_id=data.get("siteId", 0),
            version=data.get("version"),
            is_fire=bool(data.get("isFire", False)),
            is_hvac_verified=bool(data.get("isHvacVerified", False)),
            has_frozen_pipe=bool(data.get("hasFrozenPipe", False)),
            fire_hazard=FireHazardStatus(
                learning_mode=bool(fhs.get("learningMode", False)),
                message=fhs.get("message", NO_HAZARD_MESSAGE),
                efh=_hazard("efhStatus"),
                ufh=_hazard("ufhStatus"),
            ),
            group_name=group.get("name"),
        )
