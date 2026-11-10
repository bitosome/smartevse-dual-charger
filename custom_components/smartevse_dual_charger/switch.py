"""Switch entities for SmartEVSE Dual Charger."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

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
            ControllerSwitchEntity(entry, "schedule_price", "schedule_price", controller.async_set_schedule_price),
            ControllerSwitchEntity(
                entry,
                "intelligent_enabled",
                "intelligent_charge",
                controller.async_set_intelligent_enabled,
            ),
            ControllerSwitchEntity(
                entry,
                "intelligent_top_up",
                "intelligent_top_up",
                controller.async_set_intelligent_top_up,
            ),
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
        *,
        unique_key: str | None = None,
        translation_placeholders: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize the switch.

        The unique id stays ``f"{entry_id}_{translation_key}"`` unless an
        explicit ``unique_key`` is given (entities sharing a translation key).
        """
        super().__init__(entry)
        self._data_key = data_key
        self._setter = setter
        self._attr_translation_key = translation_key
        if translation_placeholders is not None:
            self._attr_translation_placeholders = dict(translation_placeholders)
        self._attr_unique_id = f"{entry.entry_id}_{unique_key or translation_key}"

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
