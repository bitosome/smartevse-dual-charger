"""Select entities for SmartEVSE Dual Charger."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant

from .const import LowBudgetPolicy
from .data import SmartEVSEDualChargerConfigEntry
from .entity import SmartEVSEDualChargerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartEVSEDualChargerConfigEntry,
    async_add_entities,
) -> None:
    """Set up select entities."""
    async_add_entities([LowBudgetPolicySelect(entry)])


class LowBudgetPolicySelect(SmartEVSEDualChargerEntity, SelectEntity):
    """Runtime low-budget policy selector."""

    _attr_translation_key = "low_budget_policy"

    def __init__(self, entry: SmartEVSEDualChargerConfigEntry) -> None:
        """Initialize the selector."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_low_budget_policy"
        self._attr_options = [policy.value for policy in LowBudgetPolicy]

    @property
    def current_option(self) -> str:
        """Return the selected option."""
        return str(self.coordinator.data.get("low_budget_policy", LowBudgetPolicy.ALTERNATE.value))

    async def async_select_option(self, option: str) -> None:
        """Select a runtime option."""
        await self._entry.runtime_data.controller.async_set_low_budget_policy(option)
        await self._entry.runtime_data.coordinator._async_refresh_now("select_option")

