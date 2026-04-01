"""Number entities for SmartEVSE Dual Charger."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.number import NumberEntity
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant

from .data import SmartEVSEDualChargerConfigEntry
from .entity import SmartEVSEDualChargerEntity


type NumberSetter = Callable[[float], Awaitable[None]]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartEVSEDualChargerConfigEntry,
    async_add_entities,
) -> None:
    """Set up controller numbers."""
    controller = entry.runtime_data.controller
    async_add_entities(
        [
            ControllerNumberEntity(
                entry,
                data_key="balance_percent",
                translation_key="balance_percent",
                setter=controller.async_set_balance_percent,
                native_min_value=0,
                native_max_value=100,
                native_step=1,
                native_unit_of_measurement="%",
            ),
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
                data_key="force_charge_duration_minutes",
                translation_key="force_charge_duration_minutes",
                setter=controller.async_set_force_charge_duration,
                native_min_value=5,
                native_max_value=720,
                native_step=5,
                native_unit_of_measurement="min",
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
    ) -> None:
        """Initialize the number."""
        super().__init__(entry)
        self._data_key = data_key
        self._setter = setter
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{entry.entry_id}_{translation_key}"
        self._attr_native_min_value = native_min_value
        self._attr_native_max_value = native_max_value
        self._attr_native_step = native_step
        self._attr_native_unit_of_measurement = native_unit_of_measurement

    @property
    def native_value(self) -> float:
        """Return the current value."""
        return float(self.coordinator.data.get(self._data_key, 0))

    async def async_set_native_value(self, value: float) -> None:
        """Set the number value."""
        await self._setter(value)
        await self._entry.runtime_data.coordinator._async_refresh_now("number_set")

