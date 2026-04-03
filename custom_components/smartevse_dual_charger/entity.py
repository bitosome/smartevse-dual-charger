"""Base entity for SmartEVSE Dual Charger."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_SMARTEVSE_1_NAME, CONF_SMARTEVSE_2_NAME
from .const import DOMAIN
from .data import SmartEVSEDualChargerConfigEntry
from .naming import configured_smartevse_name


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
            manufacturer="Home Assistant",
            model="SmartEVSE Dual Charger Controller",
            entry_type=DeviceEntryType.SERVICE,
        )

    def _configured_smartevse_name(self, smartevse_key: str) -> str:
        """Return the configured alias for one SmartEVSE."""
        values = {
            **self._entry.data,
            **self._entry.options,
            CONF_SMARTEVSE_1_NAME: self._entry.options.get(CONF_SMARTEVSE_1_NAME, self._entry.data.get(CONF_SMARTEVSE_1_NAME)),
            CONF_SMARTEVSE_2_NAME: self._entry.options.get(CONF_SMARTEVSE_2_NAME, self._entry.data.get(CONF_SMARTEVSE_2_NAME)),
        }
        return configured_smartevse_name(values, smartevse_key)
