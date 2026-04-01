"""Sensor entities for SmartEVSE Dual Charger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import UnitOfElectricCurrent, UnitOfTime
from homeassistant.core import HomeAssistant

from .const import (
    ATTR_AVAILABLE_CURRENT,
    ATTR_CHARGE_ALLOWED,
    ATTR_CHARGE_REASON,
    ATTR_CONTROLLER_STATE,
    ATTR_EVSE_1_TARGET_CURRENT,
    ATTR_EVSE_2_TARGET_CURRENT,
    ATTR_HOUSE_LOAD,
    ATTR_LAST_CYCLE_REASON,
    ATTR_LAST_METER_PUSH,
    ATTR_LAST_NOTIFICATION,
    ATTR_LAST_WLED_PUSH,
    ATTR_LOW_BUDGET_WINNER,
    ATTR_MAINS_PEAK,
    ATTR_SCHEDULE_WINDOW_ACTIVE,
    ATTR_TIMER_REMAINING,
    ATTR_TIMER_UNTIL,
)
from .data import SmartEVSEDualChargerConfigEntry
from .entity import SmartEVSEDualChargerEntity


@dataclass(frozen=True, kw_only=True)
class ControllerSensorDescription(SensorEntityDescription):
    """Description for a controller sensor."""

    value_key: str


SENSOR_DESCRIPTIONS: tuple[ControllerSensorDescription, ...] = (
    ControllerSensorDescription(
        key="controller_state",
        translation_key="controller_state",
        value_key=ATTR_CONTROLLER_STATE,
    ),
    ControllerSensorDescription(
        key="charge_reason",
        translation_key="charge_reason",
        value_key=ATTR_CHARGE_REASON,
    ),
    ControllerSensorDescription(
        key="available_current",
        translation_key="available_current",
        value_key=ATTR_AVAILABLE_CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ControllerSensorDescription(
        key="house_load",
        translation_key="house_load",
        value_key=ATTR_HOUSE_LOAD,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ControllerSensorDescription(
        key="evse_1_target_current",
        translation_key="evse_1_target_current",
        value_key=ATTR_EVSE_1_TARGET_CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ControllerSensorDescription(
        key="evse_2_target_current",
        translation_key="evse_2_target_current",
        value_key=ATTR_EVSE_2_TARGET_CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ControllerSensorDescription(
        key="timer_remaining",
        translation_key="timer_remaining",
        value_key=ATTR_TIMER_REMAINING,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartEVSEDualChargerConfigEntry,
    async_add_entities,
) -> None:
    """Set up controller sensors."""
    async_add_entities(ControllerSensor(entry, description) for description in SENSOR_DESCRIPTIONS)


class ControllerSensor(SmartEVSEDualChargerEntity, SensorEntity):
    """Sensor bound to controller state."""

    entity_description: ControllerSensorDescription

    def __init__(self, entry: SmartEVSEDualChargerConfigEntry, description: ControllerSensorDescription) -> None:
        """Initialize the sensor."""
        super().__init__(entry)
        self.entity_description = description
        self._attr_translation_key = description.translation_key
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        value = self.coordinator.data.get(self.entity_description.value_key)
        if self.entity_description.key == "timer_remaining":
            return 0 if value is None else int(value)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return useful controller attributes on the state sensor."""
        if self.entity_description.key != "controller_state":
            return None
        return {
            ATTR_CHARGE_ALLOWED: self.coordinator.data.get(ATTR_CHARGE_ALLOWED),
            ATTR_CHARGE_REASON: self.coordinator.data.get(ATTR_CHARGE_REASON),
            ATTR_AVAILABLE_CURRENT: self.coordinator.data.get(ATTR_AVAILABLE_CURRENT),
            ATTR_HOUSE_LOAD: self.coordinator.data.get(ATTR_HOUSE_LOAD),
            ATTR_MAINS_PEAK: self.coordinator.data.get(ATTR_MAINS_PEAK),
            ATTR_EVSE_1_TARGET_CURRENT: self.coordinator.data.get(ATTR_EVSE_1_TARGET_CURRENT),
            ATTR_EVSE_2_TARGET_CURRENT: self.coordinator.data.get(ATTR_EVSE_2_TARGET_CURRENT),
            ATTR_SCHEDULE_WINDOW_ACTIVE: self.coordinator.data.get(ATTR_SCHEDULE_WINDOW_ACTIVE),
            ATTR_LOW_BUDGET_WINNER: self.coordinator.data.get(ATTR_LOW_BUDGET_WINNER),
            ATTR_LAST_CYCLE_REASON: self.coordinator.data.get(ATTR_LAST_CYCLE_REASON),
            ATTR_TIMER_UNTIL: self.coordinator.data.get(ATTR_TIMER_UNTIL),
            ATTR_TIMER_REMAINING: self.coordinator.data.get(ATTR_TIMER_REMAINING),
            ATTR_LAST_METER_PUSH: self.coordinator.data.get(ATTR_LAST_METER_PUSH),
            ATTR_LAST_WLED_PUSH: self.coordinator.data.get(ATTR_LAST_WLED_PUSH),
            ATTR_LAST_NOTIFICATION: self.coordinator.data.get(ATTR_LAST_NOTIFICATION),
        }

