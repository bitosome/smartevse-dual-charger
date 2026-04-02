"""Time entities for SmartEVSE Dual Charger."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import DEFAULT_FORCE_CHARGE_DURATION_MINUTES
from .data import SmartEVSEDualChargerConfigEntry
from .entity import SmartEVSEDualChargerEntity

_MIN_DURATION_MINUTES = 5
_MAX_DURATION_MINUTES = 720


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartEVSEDualChargerConfigEntry,
    async_add_entities,
) -> None:
    """Set up controller time entities."""
    async_add_entities([ForceChargeDurationTime(entry)])


class ForceChargeDurationTime(SmartEVSEDualChargerEntity, TimeEntity):
    """Editable force-charge duration as a native Home Assistant time."""

    _attr_translation_key = "force_charge_duration"

    def __init__(self, entry: SmartEVSEDualChargerConfigEntry) -> None:
        """Initialize the time entity."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_force_charge_duration"

    @property
    def native_value(self) -> time:
        """Return the configured duration as HH:MM."""
        minutes = int(
            self.coordinator.data.get(
                "force_charge_duration_minutes",
                DEFAULT_FORCE_CHARGE_DURATION_MINUTES,
            )
        )
        hours, remainder = divmod(max(0, minutes), 60)
        return time(hour=min(hours, 23), minute=remainder)

    async def async_set_value(self, value: time) -> None:
        """Set a new duration."""
        total_minutes = (value.hour * 60) + value.minute
        if total_minutes < _MIN_DURATION_MINUTES or total_minutes > _MAX_DURATION_MINUTES:
            raise ServiceValidationError("Force charge duration must be between 00:05 and 12:00")
        await self._entry.runtime_data.controller.async_set_force_charge_duration(float(total_minutes))
        await self._entry.runtime_data.coordinator._async_refresh_now("time_set")
