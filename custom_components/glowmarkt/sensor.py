"""Today's usage and cost sensors.

These are for dashboards only. The Energy dashboard should use the
glowmarkt:<fuel>_consumption and glowmarkt:<fuel>_cost statistics, which
are imported at the correct (delayed) timestamps.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import GlowmarktConfigEntry
from .const import DOMAIN
from .coordinator import FuelSummary, GlowmarktCoordinator


@dataclass(frozen=True, kw_only=True)
class GlowmarktSensorDescription(SensorEntityDescription):
    """Describes a per-fuel sensor."""

    value_fn: Callable[[FuelSummary], Any]


SENSORS = (
    GlowmarktSensorDescription(
        key="usage_today",
        name="usage today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda s: s.usage_today,
    ),
    GlowmarktSensorDescription(
        key="cost_today",
        name="cost today",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda s: s.cost_today,
    ),
    GlowmarktSensorDescription(
        key="latest_data",
        name="latest data",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.latest_data,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GlowmarktConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add sensors for each fuel on the account."""
    coordinator = entry.runtime_data
    async_add_entities(
        GlowmarktSensor(coordinator, fuel, description)
        for fuel in coordinator.resources
        for description in SENSORS
    )


class GlowmarktSensor(CoordinatorEntity[GlowmarktCoordinator], SensorEntity):
    """A today-so-far sensor for one fuel."""

    _attr_has_entity_name = True
    entity_description: GlowmarktSensorDescription

    def __init__(
        self,
        coordinator: GlowmarktCoordinator,
        fuel: str,
        description: GlowmarktSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._fuel = fuel
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{fuel}_{description.key}"
        self._attr_name = f"{fuel.capitalize()} {description.name}"
        if description.device_class is SensorDeviceClass.MONETARY:
            self._attr_native_unit_of_measurement = coordinator.hass.config.currency
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Bright",
            manufacturer="Hildebrand",
            model="Glowmarkt DCC",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> Any:
        """Return the value for this fuel."""
        if (summary := self.coordinator.data.get(self._fuel)) is None:
            return None
        return self.entity_description.value_fn(summary)
