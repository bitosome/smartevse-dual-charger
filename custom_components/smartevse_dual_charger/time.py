"""Time entities for SmartEVSE Dual Charger (Intelligent charging ready-by times)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import time
from functools import partial

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant

from .const import INTELLIGENT_READY_BY_KEYS
from .data import SmartEVSEDualChargerConfigEntry
from .entity import SmartEVSEDualChargerEntity
from .intelligent import parse_ready_by
from .number import entry_vehicle_names


type TimeSetter = Callable[[str], Awaitable[None]]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartEVSEDualChargerConfigEntry,
    async_add_entities,
) -> None:
    """Set up the per-vehicle ready-by times."""
    controller = entry.runtime_data.controller
    vehicle_names = entry_vehicle_names(entry)
    entities = []
    for settings_key in ("vehicle_1", "vehicle_2", "other"):
        data_key = INTELLIGENT_READY_BY_KEYS[settings_key]
        known_vehicle = settings_key in vehicle_names
        entities.append(
            ControllerTimeEntity(
                entry,
                data_key=data_key,
                translation_key="vehicle_ready_by" if known_vehicle else "other_vehicle_ready_by",
                unique_key=data_key,
                translation_placeholders={"vehicle": vehicle_names[settings_key]} if known_vehicle else None,
                setter=partial(controller.async_set_vehicle_ready_by, settings_key),
            )
        )
    async_add_entities(entities)


class ControllerTimeEntity(SmartEVSEDualChargerEntity, TimeEntity):
    """Wall-clock time of day backed by controller state ("HH:MM")."""

    def __init__(
        self,
        entry: SmartEVSEDualChargerConfigEntry,
        *,
        data_key: str,
        translation_key: str,
        setter: TimeSetter,
        unique_key: str | None = None,
        translation_placeholders: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize the time entity (unique id ``f"{entry_id}_{unique_key or translation_key}"``)."""
        super().__init__(entry)
        self._data_key = data_key
        self._setter = setter
        self._attr_translation_key = translation_key
        if translation_placeholders is not None:
            self._attr_translation_placeholders = dict(translation_placeholders)
        self._attr_unique_id = f"{entry.entry_id}_{unique_key or translation_key}"

    @property
    def native_value(self) -> time | None:
        """Return the stored time of day; None when it cannot be parsed."""
        return parse_ready_by(self.coordinator.data.get(self._data_key))

    async def async_set_value(self, value: time) -> None:
        """Store the time as "HH:MM" (seconds dropped), then refresh the controller."""
        await self._setter(value.strftime("%H:%M"))
        await self._entry.runtime_data.coordinator._async_refresh_now("time_set")
