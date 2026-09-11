"""Binary sensor entities for Whisker Ting."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DeviceState
from .coordinator import WhiskerDataUpdateCoordinator
from .entity import WhiskerTingEntity

# Read-only entities off one shared coordinator: nothing here talks to the
# cloud per entity, so there is no write to serialise.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class WhiskerBinarySensorDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[DeviceState], bool] = lambda d: False


BINARY_SENSOR_DESCRIPTIONS: tuple[WhiskerBinarySensorDescription, ...] = (
    WhiskerBinarySensorDescription(
        key="fire_hazard",
        translation_key="fire_hazard",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda d: d.is_fire,
    ),
    WhiskerBinarySensorDescription(
        key="electrical_fault_hazard",
        translation_key="electrical_fault_hazard",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda d: bool(d.fire_hazard.efh.level) or (d.fire_hazard.efh.status not in (None, "NoHazard")),
    ),
    WhiskerBinarySensorDescription(
        key="unsafe_frequency_hazard",
        translation_key="unsafe_frequency_hazard",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda d: bool(d.fire_hazard.ufh.level) or (d.fire_hazard.ufh.status not in (None, "NoHazard")),
    ),
    WhiskerBinarySensorDescription(
        key="frozen_pipe",
        translation_key="frozen_pipe",
        device_class=BinarySensorDeviceClass.COLD,
        is_on_fn=lambda d: d.has_frozen_pipe,
    ),
    WhiskerBinarySensorDescription(
        key="learning_mode",
        translation_key="learning_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d.fire_hazard.learning_mode,
    ),
    WhiskerBinarySensorDescription(
        key="hvac_verified",
        translation_key="hvac_verified",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d.is_hvac_verified,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WhiskerDataUpdateCoordinator = entry.runtime_data
    entities = [
        WhiskerTingBinarySensor(coordinator, serial, description)
        for serial in (coordinator.data or {})
        for description in BINARY_SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)


class WhiskerTingBinarySensor(WhiskerTingEntity, BinarySensorEntity):
    entity_description: WhiskerBinarySensorDescription

    def __init__(
        self,
        coordinator: WhiskerDataUpdateCoordinator,
        serial_number: str,
        description: WhiskerBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, serial_number, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        device = self._device
        return self.entity_description.is_on_fn(device) if device else None
