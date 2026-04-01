"""Button entities for SmartEVSE Dual Charger."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant

from .data import SmartEVSEDualChargerConfigEntry
from .entity import SmartEVSEDualChargerEntity


type ButtonPress = Callable[[], Awaitable[None]]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartEVSEDualChargerConfigEntry,
    async_add_entities,
) -> None:
    """Set up controller buttons."""
    coordinator = entry.runtime_data.coordinator
    controller = entry.runtime_data.controller
    async_add_entities(
        [
            ControllerButtonEntity(
                entry,
                translation_key="refresh",
                press=lambda: coordinator._async_refresh_now("button_refresh"),
            ),
            ControllerButtonEntity(
                entry,
                translation_key="reset_sessions",
                press=lambda: _reset_and_refresh(entry),
            ),
        ]
    )


async def _reset_and_refresh(entry: SmartEVSEDualChargerConfigEntry) -> None:
    """Reset session flags and refresh the coordinator."""
    await entry.runtime_data.controller.async_reset_sessions()
    await entry.runtime_data.coordinator._async_refresh_now("button_reset_sessions")


class ControllerButtonEntity(SmartEVSEDualChargerEntity, ButtonEntity):
    """Controller utility button."""

    def __init__(
        self,
        entry: SmartEVSEDualChargerConfigEntry,
        *,
        translation_key: str,
        press: ButtonPress,
    ) -> None:
        """Initialize the button."""
        super().__init__(entry)
        self._press = press
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{entry.entry_id}_{translation_key}"

    async def async_press(self) -> None:
        """Handle button press."""
        await self._press()

