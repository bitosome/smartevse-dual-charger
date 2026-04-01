"""SmartEVSE Dual Charger integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.loader import async_get_loaded_integration

from .const import DOMAIN, LOGGER, PLATFORMS
from .controller import SmartEVSEDualChargerController
from .coordinator import SmartEVSEDualChargerCoordinator
from .data import SmartEVSEDualChargerData, SmartEVSEDualChargerConfigEntry


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration domain."""
    async def async_refresh(call: ServiceCall) -> None:
        entry_id = call.data.get("entry_id")
        entries = hass.config_entries.async_entries(DOMAIN)
        for entry in entries:
            if entry_id and entry.entry_id != entry_id:
                continue
            await entry.runtime_data.coordinator._async_refresh_now("service_refresh")

    async def async_reset_sessions(call: ServiceCall) -> None:
        entry_id = call.data.get("entry_id")
        entries = hass.config_entries.async_entries(DOMAIN)
        for entry in entries:
            if entry_id and entry.entry_id != entry_id:
                continue
            await entry.runtime_data.controller.async_reset_sessions()
            await entry.runtime_data.coordinator._async_refresh_now("service_reset_sessions")

    hass.services.async_register(DOMAIN, "refresh", async_refresh)
    hass.services.async_register(DOMAIN, "reset_sessions", async_reset_sessions)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SmartEVSEDualChargerConfigEntry) -> bool:
    """Set up SmartEVSE Dual Charger from a config entry."""
    entry_data = {**entry.data, "entry_id": entry.entry_id}
    options = dict(entry.options)
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
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    LOGGER.debug("Set up SmartEVSE Dual Charger entry %s", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        remaining_entries = hass.config_entries.async_entries(DOMAIN)
        if not any(other.entry_id != entry.entry_id for other in remaining_entries):
            hass.services.async_remove(DOMAIN, "refresh")
            hass.services.async_remove(DOMAIN, "reset_sessions")
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload an entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
