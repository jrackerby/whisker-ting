"""Sensor entities for Whisker Ting."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfElectricPotential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DeviceState
from .coordinator import WhiskerDataUpdateCoordinator
from .entity import WhiskerTingEntity


@dataclass(frozen=True, kw_only=True)
class WhiskerSensorDescription(SensorEntityDescription):
    value_fn: Callable[[DeviceState], float | str | None] = lambda d: None


SENSOR_DESCRIPTIONS: tuple[WhiskerSensorDescription, ...] = (
    WhiskerSensorDescription(
        key="voltage",
        translation_key="voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=lambda d: None if d.voltage.stale else d.voltage.voltage,
    ),
    WhiskerSensorDescription(
        key="voltage_hi",
        translation_key="voltage_hi",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: None if d.voltage.stale else d.voltage.voltage_hi,
    ),
    WhiskerSensorDescription(
        key="voltage_lo",
        translation_key="voltage_lo",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: None if d.voltage.stale else d.voltage.voltage_lo,
    ),
    WhiskerSensorDescription(
        key="average_peaks_max",
        translation_key="average_peaks_max",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: None if d.voltage.stale else d.voltage.average_peaks_max,
    ),
    WhiskerSensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.version,
    ),
    WhiskerSensorDescription(
        key="efh_message",
        translation_key="efh_message",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.fire_hazard.efh.message,
    ),
    WhiskerSensorDescription(
        key="ufh_message",
        translation_key="ufh_message",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.fire_hazard.ufh.message,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WhiskerDataUpdateCoordinator = entry.runtime_data
    entities = [
        WhiskerTingSensor(coordinator, serial, description)
        for serial in (coordinator.data or {})
        for description in SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)


class WhiskerTingSensor(WhiskerTingEntity, SensorEntity):
    entity_description: WhiskerSensorDescription

    def __init__(
        self,
        coordinator: WhiskerDataUpdateCoordinator,
        serial_number: str,
        description: WhiskerSensorDescription,
    ) -> None:
        super().__init__(coordinator, serial_number, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | str | None:
        device = self._device
        return self.entity_description.value_fn(device) if device else None
