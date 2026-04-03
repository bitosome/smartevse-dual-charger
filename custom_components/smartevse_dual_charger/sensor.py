"""Sensor entities for SmartEVSE Dual Charger."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import UnitOfElectricCurrent, UnitOfTime
from homeassistant.core import HomeAssistant

from .const import (
    ATTR_ACTIVE_SMARTEVSE,
    ATTR_ACTIVE_SMARTEVSE_SINCE,
    ATTR_CHARGE_ALLOWED,
    ATTR_CHARGE_REASON,
    ATTR_CONTROLLER_ERROR,
    ATTR_CONTROLLER_STATE,
    ATTR_DUTY_CYCLE_REMAINING,
    ATTR_LAST_CYCLE_REASON,
    ATTR_LAST_EV_METER_PUSH,
    ATTR_LAST_METER_PUSH,
    ATTR_LAST_NOTIFICATION,
    ATTR_LAST_WLED_PUSH,
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
        key="controller_error",
        translation_key="controller_error",
        value_key=ATTR_CONTROLLER_ERROR,
    ),
    ControllerSensorDescription(
        key="active_smartevse",
        translation_key="active_smartevse",
        value_key=ATTR_ACTIVE_SMARTEVSE,
    ),
    ControllerSensorDescription(
        key="duty_cycle_remaining",
        translation_key="duty_cycle_remaining",
        value_key=ATTR_DUTY_CYCLE_REMAINING,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ControllerSensorDescription(
        key="smartevse_1_state",
        translation_key="smartevse_1_state",
        value_key="smartevse_1_state",
    ),
    ControllerSensorDescription(
        key="smartevse_1_plug_state",
        translation_key="smartevse_1_plug_state",
        value_key="smartevse_1_plug_state",
    ),
    ControllerSensorDescription(
        key="smartevse_1_mode",
        translation_key="smartevse_1_mode",
        value_key="smartevse_1_mode",
    ),
    ControllerSensorDescription(
        key="smartevse_1_charge_current",
        translation_key="smartevse_1_charge_current",
        value_key="smartevse_1_charge_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ControllerSensorDescription(
        key="smartevse_1_max_current",
        translation_key="smartevse_1_max_current",
        value_key="smartevse_1_max_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ControllerSensorDescription(
        key="smartevse_1_override_current",
        translation_key="smartevse_1_override_current",
        value_key="smartevse_1_override_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ControllerSensorDescription(
        key="smartevse_1_error",
        translation_key="smartevse_1_error",
        value_key="smartevse_1_error",
    ),
    ControllerSensorDescription(
        key="smartevse_2_state",
        translation_key="smartevse_2_state",
        value_key="smartevse_2_state",
    ),
    ControllerSensorDescription(
        key="smartevse_2_plug_state",
        translation_key="smartevse_2_plug_state",
        value_key="smartevse_2_plug_state",
    ),
    ControllerSensorDescription(
        key="smartevse_2_mode",
        translation_key="smartevse_2_mode",
        value_key="smartevse_2_mode",
    ),
    ControllerSensorDescription(
        key="smartevse_2_charge_current",
        translation_key="smartevse_2_charge_current",
        value_key="smartevse_2_charge_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ControllerSensorDescription(
        key="smartevse_2_max_current",
        translation_key="smartevse_2_max_current",
        value_key="smartevse_2_max_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ControllerSensorDescription(
        key="smartevse_2_override_current",
        translation_key="smartevse_2_override_current",
        value_key="smartevse_2_override_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ControllerSensorDescription(
        key="smartevse_2_error",
        translation_key="smartevse_2_error",
        value_key="smartevse_2_error",
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
        if self.entity_description.key in {"duty_cycle_remaining", "timer_remaining"}:
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
            ATTR_CONTROLLER_ERROR: self.coordinator.data.get(ATTR_CONTROLLER_ERROR),
            ATTR_ACTIVE_SMARTEVSE: self.coordinator.data.get(ATTR_ACTIVE_SMARTEVSE),
            ATTR_ACTIVE_SMARTEVSE_SINCE: self.coordinator.data.get(ATTR_ACTIVE_SMARTEVSE_SINCE),
            ATTR_DUTY_CYCLE_REMAINING: self.coordinator.data.get(ATTR_DUTY_CYCLE_REMAINING),
            "charge_policy": self.coordinator.data.get("charge_policy"),
            "duty_cycle_minutes": self.coordinator.data.get("duty_cycle_minutes"),
            "update_interval": self.coordinator.data.get("update_interval"),
            "currents_push_interval": self.coordinator.data.get("currents_push_interval"),
            "ev_meter_push_interval": self.coordinator.data.get("ev_meter_push_interval"),
            ATTR_SCHEDULE_WINDOW_ACTIVE: self.coordinator.data.get(ATTR_SCHEDULE_WINDOW_ACTIVE),
            ATTR_LAST_CYCLE_REASON: self.coordinator.data.get(ATTR_LAST_CYCLE_REASON),
            ATTR_TIMER_UNTIL: self.coordinator.data.get(ATTR_TIMER_UNTIL),
            ATTR_TIMER_REMAINING: self.coordinator.data.get(ATTR_TIMER_REMAINING),
            ATTR_LAST_METER_PUSH: self.coordinator.data.get(ATTR_LAST_METER_PUSH),
            ATTR_LAST_EV_METER_PUSH: self.coordinator.data.get(ATTR_LAST_EV_METER_PUSH),
            ATTR_LAST_WLED_PUSH: self.coordinator.data.get(ATTR_LAST_WLED_PUSH),
            ATTR_LAST_NOTIFICATION: self.coordinator.data.get(ATTR_LAST_NOTIFICATION),
            "active_smartevse_raw": self.coordinator.data.get("active_smartevse_raw"),
            "smartevse_1_name": self.coordinator.data.get("smartevse_1_name"),
            "smartevse_1_available": self.coordinator.data.get("smartevse_1_available"),
            "smartevse_1_state": self.coordinator.data.get("smartevse_1_state"),
            "smartevse_1_plug_state": self.coordinator.data.get("smartevse_1_plug_state"),
            "smartevse_1_mode": self.coordinator.data.get("smartevse_1_mode"),
            "smartevse_1_charge_current": self.coordinator.data.get("smartevse_1_charge_current"),
            "smartevse_1_max_current": self.coordinator.data.get("smartevse_1_max_current"),
            "smartevse_1_override_current": self.coordinator.data.get("smartevse_1_override_current"),
            "smartevse_1_error": self.coordinator.data.get("smartevse_1_error"),
            "smartevse_1_session_complete": self.coordinator.data.get("smartevse_1_session_complete"),
            "smartevse_2_name": self.coordinator.data.get("smartevse_2_name"),
            "smartevse_2_available": self.coordinator.data.get("smartevse_2_available"),
            "smartevse_2_state": self.coordinator.data.get("smartevse_2_state"),
            "smartevse_2_plug_state": self.coordinator.data.get("smartevse_2_plug_state"),
            "smartevse_2_mode": self.coordinator.data.get("smartevse_2_mode"),
            "smartevse_2_charge_current": self.coordinator.data.get("smartevse_2_charge_current"),
            "smartevse_2_max_current": self.coordinator.data.get("smartevse_2_max_current"),
            "smartevse_2_override_current": self.coordinator.data.get("smartevse_2_override_current"),
            "smartevse_2_error": self.coordinator.data.get("smartevse_2_error"),
            "smartevse_2_session_complete": self.coordinator.data.get("smartevse_2_session_complete"),
        }
