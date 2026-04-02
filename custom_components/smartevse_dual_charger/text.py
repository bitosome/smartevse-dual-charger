"""Text entities for SmartEVSE Dual Charger."""

from __future__ import annotations

import re

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import DEFAULT_FORCE_CHARGE_DURATION_MINUTES
from .data import SmartEVSEDualChargerConfigEntry
from .entity import SmartEVSEDualChargerEntity

_DURATION_PATTERN = re.compile(r"^\d{1,2}:\d{2}$")
_MIN_DURATION_MINUTES = 5
_MAX_DURATION_MINUTES = 720


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartEVSEDualChargerConfigEntry,
    async_add_entities,
) -> None:
    """Set up controller text entities."""
    async_add_entities([ForceChargeDurationText(entry)])


class ForceChargeDurationText(SmartEVSEDualChargerEntity, TextEntity):
    """Editable force-charge duration in H:MM format."""

    _attr_translation_key = "force_charge_duration"
    _attr_mode = TextMode.TEXT
    _attr_native_min = 4
    _attr_native_max = 5
    _attr_pattern = r"^\d{1,2}:\d{2}$"

    def __init__(self, entry: SmartEVSEDualChargerConfigEntry) -> None:
        """Initialize the text entity."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_force_charge_duration"

    @property
    def native_value(self) -> str:
        """Return the configured duration as H:MM."""
        minutes = int(self.coordinator.data.get("force_charge_duration_minutes", DEFAULT_FORCE_CHARGE_DURATION_MINUTES))
        return _format_duration(minutes)

    async def async_set_value(self, value: str) -> None:
        """Set a new duration value."""
        minutes = _parse_duration(value)
        await self._entry.runtime_data.controller.async_set_force_charge_duration(float(minutes))
        await self._entry.runtime_data.coordinator._async_refresh_now("text_set")


def _parse_duration(value: str) -> int:
    """Parse H:MM duration text into minutes."""
    normalized = value.strip()
    if not _DURATION_PATTERN.fullmatch(normalized):
        raise ServiceValidationError("Use H:MM format, for example 3:32")

    hours_text, minutes_text = normalized.split(":", maxsplit=1)
    hours = int(hours_text)
    minutes = int(minutes_text)
    if minutes >= 60:
        raise ServiceValidationError("Minutes must be between 00 and 59")

    total = hours * 60 + minutes
    if total < _MIN_DURATION_MINUTES or total > _MAX_DURATION_MINUTES:
        raise ServiceValidationError("Force charge duration must be between 0:05 and 12:00")
    return total


def _format_duration(total_minutes: int) -> str:
    """Format minutes as H:MM."""
    hours, minutes = divmod(max(0, int(total_minutes)), 60)
    return f"{hours}:{minutes:02d}"
