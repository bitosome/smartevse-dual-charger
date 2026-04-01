"""Switch entities for SmartEVSE Dual Charger."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant

from .data import SmartEVSEDualChargerConfigEntry
from .entity import SmartEVSEDualChargerEntity


type SwitchSetter = Callable[[bool], Awaitable[None]]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartEVSEDualChargerConfigEntry,
    async_add_entities,
) -> None:
    """Set up controller switches."""
    controller = entry.runtime_data.controller
    async_add_entities(
        [
            ControllerSwitchEntity(entry, "force_charge", "force_charge", controller.async_set_force_charge),
            ControllerSwitchEntity(entry, "force_price", "force_price", controller.async_set_force_price),
            ControllerSwitchEntity(entry, "force_timer", "force_timer", controller.async_set_force_timer),
            ControllerSwitchEntity(entry, "schedule_enabled", "charge_with_schedule", controller.async_set_schedule_enabled),
        ]
    )


class ControllerSwitchEntity(SmartEVSEDualChargerEntity, SwitchEntity):
    """Base switch backed by controller state."""

    def __init__(
        self,
        entry: SmartEVSEDualChargerConfigEntry,
        data_key: str,
        translation_key: str,
        setter: SwitchSetter,
    ) -> None:
        """Initialize the switch."""
        super().__init__(entry)
        self._data_key = data_key
        self._setter = setter
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{entry.entry_id}_{translation_key}"

    @property
    def is_on(self) -> bool:
        """Return the switch state."""
        return bool(self.coordinator.data.get(self._data_key, False))

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the switch on."""
        await self._setter(True)
        await self._entry.runtime_data.coordinator._async_refresh_now("switch_turn_on")

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the switch off."""
        await self._setter(False)
        await self._entry.runtime_data.coordinator._async_refresh_now("switch_turn_off")

