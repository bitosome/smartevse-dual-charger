"""Diagnostics support for SmartEVSE Dual Charger."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data

from .const import (
    CONF_EVSE_1_BASE_URL,
    CONF_EVSE_2_BASE_URL,
    CONF_WLED_URL,
)
from .data import SmartEVSEDualChargerConfigEntry

TO_REDACT = {
    CONF_EVSE_1_BASE_URL,
    CONF_EVSE_2_BASE_URL,
    CONF_WLED_URL,
}


async def async_get_config_entry_diagnostics(hass, entry: SmartEVSEDualChargerConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "runtime_state": entry.runtime_data.coordinator.data,
    }

