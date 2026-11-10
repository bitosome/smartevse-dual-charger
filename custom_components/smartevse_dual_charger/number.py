"""Number entities for SmartEVSE Dual Charger."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CURRENTS_PUSH_INTERVAL,
    CONF_DUTY_CYCLE_MINUTES,
    CONF_EV_METER_PUSH_INTERVAL,
    CONF_UPDATE_INTERVAL,
    INTELLIGENT_MINIMUM_KEYS,
    MINIMUM_CHARGE_MAX,
    MINIMUM_CHARGE_MIN,
)
from .data import SmartEVSEDualChargerConfigEntry
from .entity import SmartEVSEDualChargerEntity
from .naming import configured_vehicle_names


type NumberSetter = Callable[[float], Awaitable[None]]
type AfterSetHook = Callable[[], Awaitable[None]]


def entry_vehicle_names(entry: SmartEVSEDualChargerConfigEntry) -> dict[str, str]:
    """Configured known-vehicle names (options override entry data) by vehicle key."""
    values: dict[str, Any] = {
        **dict(getattr(entry, "data", None) or {}),
        **dict(getattr(entry, "options", None) or {}),
    }
    vehicle_1_name, vehicle_2_name = configured_vehicle_names(values)
    return {"vehicle_1": vehicle_1_name, "vehicle_2": vehicle_2_name}


def _minimum_charge_entity(
    entry: SmartEVSEDualChargerConfigEntry,
    controller: Any,
    settings_key: str,
    vehicle_names: Mapping[str, str],
) -> ControllerNumberEntity:
    """Minimum charge (%) for one detected vehicle or the unidentified vehicle."""
    data_key = INTELLIGENT_MINIMUM_KEYS[settings_key]
    known_vehicle = settings_key in vehicle_names
    return ControllerNumberEntity(
        entry,
        data_key=data_key,
        translation_key="vehicle_minimum_charge" if known_vehicle else "other_vehicle_minimum_charge",
        unique_key=data_key,
        translation_placeholders={"vehicle": vehicle_names[settings_key]} if known_vehicle else None,
        setter=partial(controller.async_set_vehicle_minimum_charge, settings_key),
        native_min_value=MINIMUM_CHARGE_MIN,
        native_max_value=MINIMUM_CHARGE_MAX,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.BOX,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartEVSEDualChargerConfigEntry,
    async_add_entities,
) -> None:
    """Set up controller numbers."""
    controller = entry.runtime_data.controller
    coordinator = entry.runtime_data.coordinator
    vehicle_names = entry_vehicle_names(entry)
    async_add_entities(
        [
            ControllerNumberEntity(
                entry,
                data_key="acceptable_price",
                translation_key="acceptable_price",
                setter=controller.async_set_acceptable_price,
                native_min_value=-1.0,
                native_max_value=5.0,
                native_step=0.001,
                native_unit_of_measurement="EUR/kWh",
                mode=NumberMode.BOX,
            ),
            ControllerNumberEntity(
                entry,
                data_key="force_charge_duration_minutes",
                translation_key="force_charge_duration",
                setter=controller.async_set_force_charge_duration,
                native_min_value=5,
                native_max_value=720,
                native_step=1,
                native_unit_of_measurement=UnitOfTime.MINUTES,
                device_class=NumberDeviceClass.DURATION,
                mode=NumberMode.BOX,
            ),
            ControllerNumberEntity(
                entry,
                data_key="duty_cycle_minutes",
                translation_key="duty_cycle_minutes",
                setter=controller.async_set_duty_cycle_minutes,
                options_key=CONF_DUTY_CYCLE_MINUTES,
                native_min_value=1,
                native_max_value=720,
                native_step=1,
                native_unit_of_measurement=UnitOfTime.MINUTES,
                device_class=NumberDeviceClass.DURATION,
                mode=NumberMode.BOX,
            ),
            ControllerNumberEntity(
                entry,
                data_key="update_interval",
                translation_key="update_interval",
                setter=controller.async_set_update_interval,
                after_set=coordinator.async_timing_updated,
                options_key=CONF_UPDATE_INTERVAL,
                native_min_value=1,
                native_max_value=60,
                native_step=1,
                native_unit_of_measurement=UnitOfTime.SECONDS,
                device_class=NumberDeviceClass.DURATION,
                mode=NumberMode.BOX,
            ),
            ControllerNumberEntity(
                entry,
                data_key="currents_push_interval",
                translation_key="currents_push_interval",
                setter=controller.async_set_currents_push_interval,
                after_set=coordinator.async_timing_updated,
                options_key=CONF_CURRENTS_PUSH_INTERVAL,
                native_min_value=1,
                native_max_value=300,
                native_step=1,
                native_unit_of_measurement=UnitOfTime.SECONDS,
                device_class=NumberDeviceClass.DURATION,
                mode=NumberMode.BOX,
            ),
            ControllerNumberEntity(
                entry,
                data_key="ev_meter_push_interval",
                translation_key="ev_meter_push_interval",
                setter=controller.async_set_ev_meter_push_interval,
                after_set=coordinator.async_timing_updated,
                options_key=CONF_EV_METER_PUSH_INTERVAL,
                native_min_value=1,
                native_max_value=300,
                native_step=1,
                native_unit_of_measurement=UnitOfTime.SECONDS,
                device_class=NumberDeviceClass.DURATION,
                mode=NumberMode.BOX,
            ),
            ControllerNumberEntity(
                entry,
                data_key="intelligent_top_up_price",
                translation_key="intelligent_top_up_price",
                setter=controller.async_set_intelligent_top_up_price,
                native_min_value=-1.0,
                native_max_value=5.0,
                native_step=0.001,
                native_unit_of_measurement="EUR/kWh",
                mode=NumberMode.BOX,
            ),
            *(
                _minimum_charge_entity(entry, controller, settings_key, vehicle_names)
                # No minimum for an unidentified car: without a battery level it is planned
                # by its learned session energy, so such a setting would do nothing.
                for settings_key in ("vehicle_1", "vehicle_2")
            ),
        ]
    )


class ControllerNumberEntity(SmartEVSEDualChargerEntity, NumberEntity):
    """Config-style numbers backed by controller state."""

    def __init__(
        self,
        entry: SmartEVSEDualChargerConfigEntry,
        *,
        data_key: str,
        translation_key: str,
        setter: NumberSetter,
        unique_key: str | None = None,
        translation_placeholders: Mapping[str, str] | None = None,
        native_min_value: float,
        native_max_value: float,
        native_step: float,
        native_unit_of_measurement: str,
        device_class: NumberDeviceClass | None = None,
        after_set: AfterSetHook | None = None,
        options_key: str | None = None,
        mode: NumberMode | None = None,
    ) -> None:
        """Initialize the number.

        The unique id stays ``f"{entry_id}_{translation_key}"`` unless an
        explicit ``unique_key`` is given (entities sharing a translation key).
        """
        super().__init__(entry)
        self._data_key = data_key
        self._setter = setter
        self._after_set = after_set
        self._options_key = options_key
        self._attr_translation_key = translation_key
        if translation_placeholders is not None:
            self._attr_translation_placeholders = dict(translation_placeholders)
        self._attr_unique_id = f"{entry.entry_id}_{unique_key or translation_key}"
        self._attr_native_min_value = native_min_value
        self._attr_native_max_value = native_max_value
        self._attr_native_step = native_step
        self._attr_native_unit_of_measurement = native_unit_of_measurement
        self._attr_device_class = device_class
        self._attr_mode = mode

    @property
    def native_value(self) -> float | None:
        """Return the current value (None when the controller has no valid value)."""
        value = self.coordinator.data.get(self._data_key, 0)
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the number value."""
        await self._setter(value)
        if self._options_key is not None:
            options = dict(self._entry.options)
            options[self._options_key] = int(round(value))
            self.hass.config_entries.async_update_entry(self._entry, options=options)
        if self._after_set is not None:
            await self._after_set()
        await self._entry.runtime_data.coordinator._async_refresh_now("number_set")
