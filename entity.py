"""Shared entity base - DeviceInfo is defined once, here."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import DeviceState
from .const import DOMAIN
from .coordinator import WhiskerDataUpdateCoordinator


class WhiskerTingEntity(CoordinatorEntity[WhiskerDataUpdateCoordinator]):
    """entity_id becomes <serial>_<key> via _attr_has_entity_name, same
    reasoning as host_monitor/kiosk_pi entity.py in this repo."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: WhiskerDataUpdateCoordinator, serial_number: str, key: str) -> None:
        super().__init__(coordinator)
        self._serial_number = serial_number
        self._key = key
        self._attr_unique_id = f"{serial_number.lower()}_{key}"

    @property
    def _device(self) -> DeviceState | None:
        return (self.coordinator.data or {}).get(self._serial_number)

    @property
    def device_info(self) -> DeviceInfo:
        device = self._device
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial_number)},
            name=(device.name if device else self._serial_number),
            manufacturer="Whisker Labs",
            model=(device.device_type if device else None),
            sw_version=(device.version if device else None),
        )

    @property
    def available(self) -> bool:
        return self._device is not None and super().available
