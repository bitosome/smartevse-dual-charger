"""Select entities for SmartEVSE Dual Charger."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant

from .const import ChargePolicy
from .data import SmartEVSEDualChargerConfigEntry
from .entity import SmartEVSEDualChargerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartEVSEDualChargerConfigEntry,
    async_add_entities,
) -> None:
    """Set up select entities."""
    async_add_entities([ChargePolicySelect(entry)])


class ChargePolicySelect(SmartEVSEDualChargerEntity, SelectEntity):
    """Runtime charge policy selector."""

    _attr_translation_key = "charge_policy"

    def __init__(self, entry: SmartEVSEDualChargerConfigEntry) -> None:
        """Initialize the selector."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_charge_policy"
        self._attr_options = [policy.value for policy in ChargePolicy]

    @property
    def current_option(self) -> str:
        """Return the selected option."""
        return str(self.coordinator.data.get("charge_policy", ChargePolicy.SMARTEVSE_1_FIRST.value))

    async def async_select_option(self, option: str) -> None:
        """Select a runtime option."""
        await self._entry.runtime_data.controller.async_set_charge_policy(option)
        await self._entry.runtime_data.coordinator._async_refresh_now("select_option")
