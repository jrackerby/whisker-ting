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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DeviceState
from .coordinator import WhiskerDataUpdateCoordinator
from .entity import WhiskerTingEntity

# Read-only entities off one shared coordinator: nothing here talks to the
# cloud per entity, so there is no write to serialise.
PARALLEL_UPDATES = 0


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
    known: set[str] = set()

    @callback
    def _add_new_devices() -> None:
        """A Ting plugged in after setup used to be invisible until the entry
        was reloaded by hand: the entity list was built once, here, from
        whatever the first refresh happened to hold. The coordinator sees the
        new serial on its very next poll, so listen for that instead."""
        current = set(coordinator.data or {})
        # Drop serials that have LEFT the account as well as adding the ones
        # that arrived. The coordinator releases their registry rows, which
        # takes the entities with them - keeping the serial here would mean a
        # Ting removed and later plugged back in never came back, because it
        # would no longer read as new.
        known.intersection_update(current)
        serials = current - known
        if not serials:
            return
        known.update(serials)
        async_add_entities(
            WhiskerTingSensor(coordinator, serial, description)
            for serial in sorted(serials)
            for description in SENSOR_DESCRIPTIONS
        )

    _add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


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
