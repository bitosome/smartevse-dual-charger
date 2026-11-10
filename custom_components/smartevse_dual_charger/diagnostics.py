"""Diagnostics support for SmartEVSE Dual Charger."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data

from .const import (
    CONF_SCHEDULE_NOTIFICATION_TARGETS,
    CONF_SMARTEVSE_1_BASE_URL,
    CONF_SMARTEVSE_2_BASE_URL,
    CONF_WLED_URL,
    INTELLIGENT_READY_BY_KEYS,
)
from .data import SmartEVSEDualChargerConfigEntry

TO_REDACT = {
    CONF_SCHEDULE_NOTIFICATION_TARGETS,
    CONF_SMARTEVSE_1_BASE_URL,
    CONF_SMARTEVSE_2_BASE_URL,
    CONF_WLED_URL,
}
REDACTED = "**REDACTED**"
# Values that reveal when the household leaves (departure habits): §9.
_CHARGER_REDACT = ("deadline", "ready_by")
_VEHICLE_REDACT = ("ready_by", "suggested_ready_by", "usually_leaves")
_SESSION_REDACT = ("date", "started_at", "ended_at", "plugged_at", "closed_at")
_COST_TIMESTAMPS = ("started_at", "ended_at")


def _round_to_hour(value: Any) -> Any:
    """ISO timestamp rounded down to the hour (charging_costs records)."""
    if not isinstance(value, str):
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return REDACTED
    return parsed.replace(minute=0, second=0, microsecond=0).isoformat()


def redact_runtime_state(data: Any) -> Any:
    """Copy of coordinator data with Intelligent habits redacted and ledger times rounded."""
    if not isinstance(data, Mapping):
        return data
    result = deepcopy(dict(data))
    for key in INTELLIGENT_READY_BY_KEYS.values():
        if key in result:
            result[key] = REDACTED
    intelligent = result.get("intelligent")
    if isinstance(intelligent, dict):
        for charger in (intelligent.get("chargers") or {}).values():
            if isinstance(charger, dict):
                for key in _CHARGER_REDACT:
                    if charger.get(key) is not None:
                        charger[key] = REDACTED
        for vehicle in (intelligent.get("vehicles") or {}).values():
            if isinstance(vehicle, dict):
                for key in _VEHICLE_REDACT:
                    if vehicle.get(key) is not None:
                        vehicle[key] = REDACTED
        for session in intelligent.get("sessions") or []:
            if isinstance(session, dict):
                for key in _SESSION_REDACT:
                    if session.get(key) is not None:
                        session[key] = REDACTED
    costs = result.get("charging_costs")
    if isinstance(costs, dict):
        for charger in (costs.get("chargers") or {}).values():
            if not isinstance(charger, dict):
                continue
            records = list(charger.get("history") or [])
            if isinstance(charger.get("session"), dict):
                records.append(charger["session"])
            for record in records:
                if isinstance(record, dict):
                    for key in _COST_TIMESTAMPS:
                        if key in record:
                            record[key] = _round_to_hour(record[key])
    return result


async def async_get_config_entry_diagnostics(hass, entry: SmartEVSEDualChargerConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "runtime_state": redact_runtime_state(entry.runtime_data.coordinator.data),
    }
