"""Number entities for SmartEVSE Dual Charger."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CURRENTS_PUSH_INTERVAL,
    CONF_DUTY_CYCLE_MINUTES,
    CONF_EV_METER_PUSH_INTERVAL,
    CONF_UPDATE_INTERVAL,
)
from .data import SmartEVSEDualChargerConfigEntry
from .entity import SmartEVSEDualChargerEntity


type NumberSetter = Callable[[float], Awaitable[None]]
type AfterSetHook = Callable[[], Awaitable[None]]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartEVSEDualChargerConfigEntry,
    async_add_entities,
) -> None:
    """Set up controller numbers."""
    controller = entry.runtime_data.controller
    coordinator = entry.runtime_data.coordinator
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
                native_unit_of_measurement="min",
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
                native_unit_of_measurement="s",
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
                native_unit_of_measurement="s",
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
                native_unit_of_measurement="s",
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
        native_min_value: float,
        native_max_value: float,
        native_step: float,
        native_unit_of_measurement: str,
        after_set: AfterSetHook | None = None,
        options_key: str | None = None,
        mode: NumberMode | None = None,
    ) -> None:
        """Initialize the number."""
        super().__init__(entry)
        self._data_key = data_key
        self._setter = setter
        self._after_set = after_set
        self._options_key = options_key
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{entry.entry_id}_{translation_key}"
        self._attr_native_min_value = native_min_value
        self._attr_native_max_value = native_max_value
        self._attr_native_step = native_step
        self._attr_native_unit_of_measurement = native_unit_of_measurement
        self._attr_mode = mode

    @property
    def native_value(self) -> float:
        """Return the current value."""
        return float(self.coordinator.data.get(self._data_key, 0))

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
