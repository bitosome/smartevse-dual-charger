"""SmartEVSE Dual Charger integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.loader import async_get_loaded_integration

from .const import (
    CONF_CHARGE_POLICY_DEFAULT,
    CONF_CURRENTS_PUSH_INTERVAL,
    CONF_DUTY_CYCLE_MINUTES,
    CONF_EV_METER_PUSH_INTERVAL,
    CONF_NOTIFY_ON_SCHEDULE_WINDOW,
    CONF_PUSH_CURRENTS,
    CONF_PUSH_EV_METER,
    CONF_PUSH_WLED,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    LOGGER,
    PLATFORMS,
)
from .controller import SmartEVSEDualChargerController
from .coordinator import SmartEVSEDualChargerCoordinator
from .data import SmartEVSEDualChargerData, SmartEVSEDualChargerConfigEntry

OPTION_KEYS = {
    CONF_CHARGE_POLICY_DEFAULT,
    CONF_CURRENTS_PUSH_INTERVAL,
    CONF_DUTY_CYCLE_MINUTES,
    CONF_EV_METER_PUSH_INTERVAL,
    CONF_NOTIFY_ON_SCHEDULE_WINDOW,
    CONF_PUSH_CURRENTS,
    CONF_PUSH_EV_METER,
    CONF_PUSH_WLED,
    CONF_UPDATE_INTERVAL,
}


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration domain."""
    def _target_entries(entry_id: str | None) -> list[SmartEVSEDualChargerConfigEntry]:
        entries = hass.config_entries.async_entries(DOMAIN)
        if entry_id:
            entry = next((candidate for candidate in entries if candidate.entry_id == entry_id), None)
            if entry is None:
                raise ServiceValidationError(f"Unknown {DOMAIN} entry_id: {entry_id}")
            if not hasattr(entry, "runtime_data"):
                raise ServiceValidationError(f"{DOMAIN} entry {entry_id} is not loaded")
            return [entry]

        loaded_entries = [entry for entry in entries if hasattr(entry, "runtime_data")]
        if not loaded_entries:
            raise ServiceValidationError(f"No loaded {DOMAIN} entries are available")
        return loaded_entries

    async def async_refresh(call: ServiceCall) -> None:
        entry_id = call.data.get("entry_id")
        for entry in _target_entries(entry_id):
            await entry.runtime_data.coordinator._async_refresh_now("service_refresh")

    async def async_reset_sessions(call: ServiceCall) -> None:
        entry_id = call.data.get("entry_id")
        for entry in _target_entries(entry_id):
            await entry.runtime_data.controller.async_reset_sessions()
            await entry.runtime_data.coordinator._async_refresh_now("service_reset_sessions")

    hass.services.async_register(DOMAIN, "refresh", async_refresh)
    hass.services.async_register(DOMAIN, "reset_sessions", async_reset_sessions)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SmartEVSEDualChargerConfigEntry) -> bool:
    """Set up SmartEVSE Dual Charger from a config entry."""
    entry_data = {**entry.data, "entry_id": entry.entry_id}
    options = {
        key: entry.data[key]
        for key in OPTION_KEYS
        if key in entry.data
    }
    options.update(entry.options)
    controller = SmartEVSEDualChargerController(hass, entry_data, options)
    await controller.async_initialize()
    coordinator = SmartEVSEDualChargerCoordinator(
        hass,
        controller,
        entry_data=entry_data,
        options=options,
    )
    entry.runtime_data = SmartEVSEDualChargerData(
        controller=controller,
        coordinator=coordinator,
        integration=async_get_loaded_integration(hass, entry.domain),
    )
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    LOGGER.debug("Set up SmartEVSE Dual Charger entry %s", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and hasattr(entry, "runtime_data"):
        await entry.runtime_data.coordinator.async_shutdown()
    return unload_ok
