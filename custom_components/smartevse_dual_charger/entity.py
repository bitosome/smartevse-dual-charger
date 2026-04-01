"""Base entity for SmartEVSE Dual Charger."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .data import SmartEVSEDualChargerConfigEntry


class SmartEVSEDualChargerEntity(CoordinatorEntity):
    """Base entity bound to the controller device."""

    _attr_has_entity_name = True

    def __init__(self, entry: SmartEVSEDualChargerConfigEntry) -> None:
        """Initialize the entity."""
        super().__init__(entry.runtime_data.coordinator)
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Custom",
            model="SmartEVSE Dual Charger Controller",
            entry_type=None,
        )

