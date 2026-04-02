"""Core controller for SmartEVSE Dual Charger."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin

from aiohttp import ClientError
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_ACTIVE_SMARTEVSE,
    ATTR_ACTIVE_SMARTEVSE_SINCE,
    ATTR_CHARGE_ALLOWED,
    ATTR_CHARGE_REASON,
    ATTR_CONTROLLER_ERROR,
    ATTR_CONTROLLER_STATE,
    ATTR_DUTY_CYCLE_REMAINING,
    ATTR_LAST_CYCLE_REASON,
    ATTR_LAST_EV_METER_PUSH,
    ATTR_LAST_METER_PUSH,
    ATTR_LAST_NOTIFICATION,
    ATTR_LAST_WLED_PUSH,
    ATTR_MAINS_PEAK,
    ATTR_SCHEDULE_WINDOW_ACTIVE,
    ATTR_TIMER_REMAINING,
    ATTR_TIMER_UNTIL,
    CONF_CHARGE_POLICY_DEFAULT,
    CONF_CURRENTS_PUSH_INTERVAL,
    CONF_DUTY_CYCLE_MINUTES,
    CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY,
    CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY,
    CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY,
    CONF_EV_METER_L1_ENTITY,
    CONF_EV_METER_L2_ENTITY,
    CONF_EV_METER_L3_ENTITY,
    CONF_EV_METER_PUSH_INTERVAL,
    CONF_MAINS_L1_ENTITY,
    CONF_MAINS_L2_ENTITY,
    CONF_MAINS_L3_ENTITY,
    CONF_NOTIFY_ON_SCHEDULE_WINDOW,
    CONF_PRICE_SENSOR_ENTITY,
    CONF_PUSH_CURRENTS,
    CONF_PUSH_EV_METER,
    CONF_PUSH_WLED,
    CONF_SCHEDULE_ENTITY,
    CONF_SMARTEVSE_1_BASE_URL,
    CONF_SMARTEVSE_2_BASE_URL,
    CONF_UPDATE_INTERVAL,
    CONF_WLED_URL,
    ControllerState,
    DEFAULT_ACCEPTABLE_PRICE,
    DEFAULT_CHARGE_POLICY,
    DEFAULT_CURRENTS_PUSH_INTERVAL,
    DEFAULT_DUTY_CYCLE_MINUTES,
    DEFAULT_EV_METER_PUSH_INTERVAL,
    DEFAULT_FORCE_CHARGE_DURATION_MINUTES,
    DEFAULT_NOTIFY_ON_SCHEDULE_WINDOW,
    DEFAULT_PUSH_CURRENTS,
    DEFAULT_PUSH_EV_METER,
    DEFAULT_PUSH_WLED,
    DEFAULT_UPDATE_INTERVAL,
    LOGGER,
    SCHEDULE_NOTIFICATION_ID,
    STORAGE_KEY,
    STORAGE_VERSION,
    ChargePolicy,
)
from .wled import build_runtime_payload, normalize_wled_state_url

MUTABLE_DEFAULTS: dict[str, Any] = {
    "force_charge": False,
    "force_price": False,
    "force_timer": False,
    "schedule_enabled": False,
    "acceptable_price": DEFAULT_ACCEPTABLE_PRICE,
    "force_charge_duration_minutes": DEFAULT_FORCE_CHARGE_DURATION_MINUTES,
    "charge_policy": DEFAULT_CHARGE_POLICY,
    "duty_cycle_minutes": DEFAULT_DUTY_CYCLE_MINUTES,
    "update_interval": DEFAULT_UPDATE_INTERVAL,
    "currents_push_interval": DEFAULT_CURRENTS_PUSH_INTERVAL,
    "ev_meter_push_interval": DEFAULT_EV_METER_PUSH_INTERVAL,
    "timer_until": None,
    "active_smartevse": None,
    "active_smartevse_since": None,
    "last_charge_allowed": False,
    "last_active_charge_reason": None,
    "last_schedule_window_active": False,
    "last_meter_push": None,
    "last_ev_meter_push": None,
    "last_wled_push": None,
    "last_notification": None,
    "smartevse_1_seen_charging": False,
    "smartevse_1_session_complete": False,
    "smartevse_2_seen_charging": False,
    "smartevse_2_session_complete": False,
}

MODE_NAME_TO_ID = {
    "Off": 0,
    "Normal": 1,
    "Solar": 2,
    "Smart": 3,
    "Pause": 4,
}
MODE_ID_TO_NAME = {value: key for key, value in MODE_NAME_TO_ID.items()}


@dataclass(slots=True)
class SmartEVSEStatus:
    """Status snapshot for one SmartEVSE."""

    key: str
    base_url: str
    available: bool
    connected: bool
    plug_state: str
    state: str
    mode: str
    charge_current: float
    max_current: float
    override_current: float
    error: str


class SmartEVSEDualChargerController:
    """Controller for SmartEVSE dual charger orchestration."""

    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any], options: dict[str, Any]) -> None:
        """Initialize the controller."""
        self.hass = hass
        self._entry_data = entry_data
        self._options = options
        self._session = async_get_clientsession(hass)
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY}.{entry_data['entry_id']}",
        )
        self._mutable: dict[str, Any] = {}
        self._endpoint_failures: dict[str, str] = {}
        self._last_wled_payload: dict[str, Any] | None = None

    async def async_initialize(self) -> None:
        """Load persisted mutable state."""
        stored = await self._store.async_load() or {}
        self._mutable = {**MUTABLE_DEFAULTS, **stored}

        try:
            self._mutable["charge_policy"] = ChargePolicy(self._mutable["charge_policy"]).value
        except ValueError:
            self._mutable["charge_policy"] = self._options.get(
                CONF_CHARGE_POLICY_DEFAULT,
                DEFAULT_CHARGE_POLICY,
            )
        if "duty_cycle_minutes" not in stored:
            self._mutable["duty_cycle_minutes"] = self._options.get(
                CONF_DUTY_CYCLE_MINUTES,
                DEFAULT_DUTY_CYCLE_MINUTES,
            )
        self._mutable["duty_cycle_minutes"] = max(1, int(self._mutable["duty_cycle_minutes"]))
        if "update_interval" not in stored:
            self._mutable["update_interval"] = self._options.get(
                CONF_UPDATE_INTERVAL,
                DEFAULT_UPDATE_INTERVAL,
            )
        self._mutable["update_interval"] = max(1, int(self._mutable["update_interval"]))
        if "currents_push_interval" not in stored:
            self._mutable["currents_push_interval"] = self._options.get(
                CONF_CURRENTS_PUSH_INTERVAL,
                DEFAULT_CURRENTS_PUSH_INTERVAL,
            )
        self._mutable["currents_push_interval"] = max(1, int(self._mutable["currents_push_interval"]))
        if "ev_meter_push_interval" not in stored:
            self._mutable["ev_meter_push_interval"] = self._options.get(
                CONF_EV_METER_PUSH_INTERVAL,
                DEFAULT_EV_METER_PUSH_INTERVAL,
            )
        self._mutable["ev_meter_push_interval"] = max(1, int(self._mutable["ev_meter_push_interval"]))

        if CONF_CHARGE_POLICY_DEFAULT in self._options:
            self._mutable["charge_policy"] = ChargePolicy(self._options[CONF_CHARGE_POLICY_DEFAULT]).value
        if CONF_DUTY_CYCLE_MINUTES in self._options:
            self._mutable["duty_cycle_minutes"] = max(1, int(self._options[CONF_DUTY_CYCLE_MINUTES]))
        if CONF_UPDATE_INTERVAL in self._options:
            self._mutable["update_interval"] = max(1, int(self._options[CONF_UPDATE_INTERVAL]))
        if CONF_CURRENTS_PUSH_INTERVAL in self._options:
            self._mutable["currents_push_interval"] = max(1, int(self._options[CONF_CURRENTS_PUSH_INTERVAL]))
        if CONF_EV_METER_PUSH_INTERVAL in self._options:
            self._mutable["ev_meter_push_interval"] = max(1, int(self._options[CONF_EV_METER_PUSH_INTERVAL]))

        # Timer charge should not survive a Home Assistant restart.
        self._mutable["force_timer"] = False
        self._mutable["timer_until"] = None
        self._mutable["charge_policy"] = self._configured_charge_policy()
        self._reset_charge_cycle()
        await self._async_save_state()

    async def async_set_force_charge(self, value: bool) -> None:
        """Enable or disable force charge."""
        self._mutable["force_charge"] = value
        if value:
            self._mutable["force_price"] = False
            self._mutable["force_timer"] = False
            self._mutable["timer_until"] = None
            self._clear_session_tracking()
            self._reset_charge_cycle()
        await self._async_save_state()

    async def async_set_force_price(self, value: bool) -> None:
        """Enable or disable price-based force charging."""
        self._mutable["force_price"] = value
        if value:
            self._mutable["force_charge"] = False
            self._mutable["force_timer"] = False
            self._mutable["timer_until"] = None
            self._clear_session_tracking()
            self._reset_charge_cycle()
        await self._async_save_state()

    async def async_set_force_timer(self, value: bool) -> None:
        """Enable or disable timer-based force charging."""
        self._mutable["force_timer"] = value
        if value:
            self._mutable["force_charge"] = False
            self._mutable["force_price"] = False
            duration = int(self._mutable["force_charge_duration_minutes"])
            self._mutable["timer_until"] = (dt_util.utcnow() + timedelta(minutes=duration)).isoformat()
            self._clear_session_tracking()
            self._reset_charge_cycle()
        else:
            self._mutable["timer_until"] = None
            self._reset_charge_cycle()
        await self._async_save_state()

    async def async_set_schedule_enabled(self, value: bool) -> None:
        """Enable or disable the schedule gate."""
        self._mutable["schedule_enabled"] = value
        if not value:
            self._reset_charge_cycle()
        await self._async_save_state()

    async def async_set_acceptable_price(self, value: float) -> None:
        """Update the acceptable price threshold."""
        self._mutable["acceptable_price"] = round(value, 4)
        await self._async_save_state()

    async def async_set_force_charge_duration(self, value: float) -> None:
        """Update timer duration in minutes."""
        self._mutable["force_charge_duration_minutes"] = int(round(value))
        if self._mutable["force_timer"]:
            duration = int(self._mutable["force_charge_duration_minutes"])
            self._mutable["timer_until"] = (dt_util.utcnow() + timedelta(minutes=duration)).isoformat()
        await self._async_save_state()

    async def async_set_charge_policy(self, value: str) -> None:
        """Update the runtime charge policy."""
        self._mutable["charge_policy"] = ChargePolicy(value).value
        self._clear_session_tracking()
        self._reset_charge_cycle()
        await self._async_save_state()

    async def async_set_duty_cycle_minutes(self, value: float) -> None:
        """Update the duty cycle interval."""
        self._mutable["duty_cycle_minutes"] = max(1, int(round(value)))
        self._reset_charge_cycle()
        await self._async_save_state()

    async def async_set_update_interval(self, value: float) -> None:
        """Update the controller refresh interval."""
        self._mutable["update_interval"] = max(1, int(round(value)))
        await self._async_save_state()

    async def async_set_currents_push_interval(self, value: float) -> None:
        """Update the mains current push interval."""
        self._mutable["currents_push_interval"] = max(1, int(round(value)))
        await self._async_save_state()

    async def async_set_ev_meter_push_interval(self, value: float) -> None:
        """Update the EV meter push interval."""
        self._mutable["ev_meter_push_interval"] = max(1, int(round(value)))
        await self._async_save_state()

    async def async_reset_sessions(self) -> None:
        """Reset the active SmartEVSE and start a new cycle."""
        self._reset_charge_cycle()
        self._clear_session_tracking()
        await self._async_save_state()

    async def async_run_cycle(self, *, reason: str) -> dict[str, Any]:
        """Run a complete controller cycle and return computed state."""
        now = dt_util.utcnow()
        self._sanitize_mutual_exclusion()

        smartevse_1, smartevse_2 = await asyncio.gather(
            self._async_fetch_status("smartevse_1", self._entry_data[CONF_SMARTEVSE_1_BASE_URL]),
            self._async_fetch_status("smartevse_2", self._entry_data[CONF_SMARTEVSE_2_BASE_URL]),
        )
        self._update_session_tracking(smartevse_1)
        self._update_session_tracking(smartevse_2)

        if self._mutable["force_timer"]:
            timer_until = self._timer_until()
            if timer_until is None or now >= timer_until:
                self._mutable["force_timer"] = False
                self._mutable["timer_until"] = None
                self._reset_charge_cycle()

        if (
            smartevse_1.available
            and smartevse_2.available
            and not smartevse_1.connected
            and not smartevse_2.connected
        ):
            self._clear_force_modes()
            self._mutable["charge_policy"] = self._configured_charge_policy()
            self._reset_charge_cycle()

        price_value = self._state_float_or_none(self._entry_data.get(CONF_PRICE_SENSOR_ENTITY))
        schedule_window_active = self._state_on(self._entry_data.get(CONF_SCHEDULE_ENTITY))
        charge_allowed, controller_state, charge_reason = self._determine_charge_allowed(
            price_value=price_value,
            schedule_window_active=schedule_window_active,
        )

        await self._handle_schedule_notification(
            schedule_window_active=schedule_window_active,
            schedule_enabled=bool(self._mutable["schedule_enabled"]),
        )

        mains_currents = self._phase_currents_or_none(
            (
                self._entry_data[CONF_MAINS_L1_ENTITY],
                self._entry_data[CONF_MAINS_L2_ENTITY],
                self._entry_data[CONF_MAINS_L3_ENTITY],
            )
        )
        mains_peak = max(mains_currents) if mains_currents is not None else None
        controller_error = self._controller_error_for_reason(charge_reason)

        if self._options.get(CONF_PUSH_CURRENTS, DEFAULT_PUSH_CURRENTS) and mains_currents is None:
            charge_allowed = False
            controller_state = ControllerState.BLOCKED
            charge_reason = "mains_data_unavailable"
            controller_error = charge_reason

        previous_charge_allowed = bool(self._mutable.get("last_charge_allowed"))
        if charge_allowed and not previous_charge_allowed:
            self._clear_session_tracking()
            self._reset_charge_cycle()
        self._mutable["last_charge_allowed"] = charge_allowed

        previous_active_smartevse = str(self._mutable.get("active_smartevse") or "")
        active_smartevse = "none"
        duty_cycle_remaining = 0
        active_smartevse_since = None

        if not charge_allowed:
            self._reset_charge_cycle()
            self._mutable["last_active_charge_reason"] = None
        else:
            if self._mutable.get("last_active_charge_reason") != charge_reason:
                self._reset_charge_cycle()
            self._mutable["last_active_charge_reason"] = charge_reason
            active_smartevse, active_smartevse_since, duty_cycle_remaining, blocked_reason = self._resolve_active_smartevse(
                now=now,
                smartevse_1=smartevse_1,
                smartevse_2=smartevse_2,
            )
            if active_smartevse == "none":
                controller_state = ControllerState.BLOCKED
                charge_reason = blocked_reason
                controller_error = self._controller_error_for_reason(blocked_reason)

        if (
            previous_active_smartevse in {"smartevse_1", "smartevse_2"}
            and active_smartevse in {"smartevse_1", "smartevse_2"}
            and previous_active_smartevse != active_smartevse
        ):
            self._clear_smartevse_session_tracking(previous_active_smartevse)

        await self._apply_modes(
            smartevse_1=smartevse_1,
            smartevse_2=smartevse_2,
            active_smartevse=active_smartevse,
            charge_allowed=charge_allowed,
        )
        await self._maybe_push_wled(smartevse_1=smartevse_1, smartevse_2=smartevse_2)
        await self._async_save_state()

        timer_until = self._timer_until()
        timer_remaining = 0
        if timer_until is not None:
            timer_remaining = max(int((timer_until - now).total_seconds()), 0)

        return {
            "force_charge": bool(self._mutable["force_charge"]),
            "force_price": bool(self._mutable["force_price"]),
            "force_timer": bool(self._mutable["force_timer"]),
            "schedule_enabled": bool(self._mutable["schedule_enabled"]),
            "acceptable_price": float(self._mutable["acceptable_price"]),
            "force_charge_duration_minutes": int(self._mutable["force_charge_duration_minutes"]),
            "charge_policy": str(self._mutable["charge_policy"]),
            "duty_cycle_minutes": int(self._mutable["duty_cycle_minutes"]),
            "update_interval": int(self._mutable["update_interval"]),
            "currents_push_interval": int(self._mutable["currents_push_interval"]),
            "ev_meter_push_interval": int(self._mutable["ev_meter_push_interval"]),
            ATTR_CHARGE_ALLOWED: charge_allowed,
            ATTR_CONTROLLER_ERROR: controller_error,
            ATTR_CONTROLLER_STATE: controller_state.value,
            ATTR_CHARGE_REASON: charge_reason,
            ATTR_MAINS_PEAK: None if mains_peak is None else round(mains_peak, 1),
            ATTR_ACTIVE_SMARTEVSE: active_smartevse,
            ATTR_ACTIVE_SMARTEVSE_SINCE: active_smartevse_since,
            ATTR_DUTY_CYCLE_REMAINING: duty_cycle_remaining,
            ATTR_SCHEDULE_WINDOW_ACTIVE: schedule_window_active,
            ATTR_LAST_CYCLE_REASON: reason,
            ATTR_TIMER_UNTIL: timer_until,
            ATTR_TIMER_REMAINING: timer_remaining,
            ATTR_LAST_METER_PUSH: self._mutable["last_meter_push"],
            ATTR_LAST_EV_METER_PUSH: self._mutable["last_ev_meter_push"],
            ATTR_LAST_WLED_PUSH: self._mutable["last_wled_push"],
            ATTR_LAST_NOTIFICATION: self._mutable["last_notification"],
            "smartevse_1_available": smartevse_1.available,
            "smartevse_1_state": smartevse_1.state,
            "smartevse_1_plug_state": smartevse_1.plug_state,
            "smartevse_1_mode": smartevse_1.mode,
            "smartevse_1_charge_current": round(smartevse_1.charge_current, 1),
            "smartevse_1_max_current": round(smartevse_1.max_current, 1),
            "smartevse_1_override_current": round(smartevse_1.override_current, 1),
            "smartevse_1_error": smartevse_1.error,
            "smartevse_1_session_complete": bool(self._mutable["smartevse_1_session_complete"]),
            "smartevse_2_available": smartevse_2.available,
            "smartevse_2_state": smartevse_2.state,
            "smartevse_2_plug_state": smartevse_2.plug_state,
            "smartevse_2_mode": smartevse_2.mode,
            "smartevse_2_charge_current": round(smartevse_2.charge_current, 1),
            "smartevse_2_max_current": round(smartevse_2.max_current, 1),
            "smartevse_2_override_current": round(smartevse_2.override_current, 1),
            "smartevse_2_error": smartevse_2.error,
            "smartevse_2_session_complete": bool(self._mutable["smartevse_2_session_complete"]),
        }

    def _sanitize_mutual_exclusion(self) -> None:
        """Enforce one active force mode."""
        active_force_modes = [
            key
            for key in ("force_charge", "force_price", "force_timer")
            if self._mutable.get(key)
        ]
        if len(active_force_modes) <= 1:
            return
        first = active_force_modes[0]
        for key in active_force_modes[1:]:
            self._mutable[key] = False
        if first != "force_timer":
            self._mutable["timer_until"] = None

    def _clear_force_modes(self) -> None:
        """Clear all force-mode toggles."""
        self._mutable["force_charge"] = False
        self._mutable["force_price"] = False
        self._mutable["force_timer"] = False
        self._mutable["timer_until"] = None

    def _configured_charge_policy(self) -> str:
        """Return the configured default charge policy."""
        value = self._options.get(CONF_CHARGE_POLICY_DEFAULT, DEFAULT_CHARGE_POLICY)
        try:
            return ChargePolicy(value).value
        except ValueError:
            return DEFAULT_CHARGE_POLICY

    def _reset_charge_cycle(self) -> None:
        """Clear the active SmartEVSE so the next cycle restarts from policy."""
        self._mutable["active_smartevse"] = None
        self._mutable["active_smartevse_since"] = None

    def _clear_session_tracking(self) -> None:
        """Forget per-connector session completion state."""
        for smartevse_key in ("smartevse_1", "smartevse_2"):
            self._clear_smartevse_session_tracking(smartevse_key)

    def _clear_smartevse_session_tracking(self, smartevse_key: str) -> None:
        """Forget completion state for one SmartEVSE."""
        self._mutable[f"{smartevse_key}_seen_charging"] = False
        self._mutable[f"{smartevse_key}_session_complete"] = False

    def _update_session_tracking(self, status: SmartEVSEStatus) -> None:
        """Track whether a connected EV has already completed one charge session."""
        seen_key = f"{status.key}_seen_charging"
        complete_key = f"{status.key}_session_complete"

        if not status.available:
            return

        if not status.connected:
            self._mutable[seen_key] = False
            self._mutable[complete_key] = False
            return

        if status.state == "Charging":
            self._mutable[seen_key] = True
            self._mutable[complete_key] = False
            return

        if self._mutable.get(seen_key) and status.state == "Charging Stopped":
            self._mutable[complete_key] = True

    def _session_complete(self, smartevse_key: str) -> bool:
        """Return whether the current plugged session has already completed."""
        return bool(self._mutable.get(f"{smartevse_key}_session_complete"))

    def _determine_charge_allowed(
        self,
        *,
        price_value: float | None,
        schedule_window_active: bool,
    ) -> tuple[bool, ControllerState, str]:
        """Resolve high-level controller state."""
        acceptable_price = float(self._mutable["acceptable_price"])
        if self._mutable["force_charge"]:
            return True, ControllerState.FORCE, "force_charge"
        if self._mutable["force_timer"] and self._timer_until() is not None:
            return True, ControllerState.TIMER, "force_timer"
        if self._mutable["force_price"]:
            if not self._entry_data.get(CONF_PRICE_SENSOR_ENTITY):
                return False, ControllerState.IDLE, "price_sensor_unavailable"
            if price_value is None:
                return False, ControllerState.IDLE, "price_sensor_unavailable"
            if price_value <= acceptable_price:
                return True, ControllerState.PRICE, "acceptable_price"
            return False, ControllerState.IDLE, "waiting_for_acceptable_price"
        if self._mutable["schedule_enabled"]:
            if not self._entry_data.get(CONF_SCHEDULE_ENTITY):
                return False, ControllerState.IDLE, "schedule_entity_unavailable"
            if schedule_window_active:
                return True, ControllerState.SCHEDULE, "schedule"
            return False, ControllerState.IDLE, "waiting_for_schedule_window"
        return False, ControllerState.IDLE, "idle"

    def _resolve_active_smartevse(
        self,
        *,
        now: datetime,
        smartevse_1: SmartEVSEStatus,
        smartevse_2: SmartEVSEStatus,
    ) -> tuple[str, str | None, int, str]:
        """Return the active SmartEVSE, start time, remaining time, and any block reason."""
        statuses = {
            "smartevse_1": smartevse_1,
            "smartevse_2": smartevse_2,
        }
        policy = ChargePolicy(self._mutable["charge_policy"])
        available_smartevse = [smartevse_key for smartevse_key, status in statuses.items() if status.available]
        connected_smartevse = [
            smartevse_key for smartevse_key, status in statuses.items() if status.available and status.connected
        ]
        eligible_smartevse = [
            smartevse_key for smartevse_key in connected_smartevse if not self._session_complete(smartevse_key)
        ]

        if not available_smartevse:
            self._reset_charge_cycle()
            return "none", None, 0, "smartevse_api_unavailable"

        if policy == ChargePolicy.SMARTEVSE_1_ONLY:
            return self._resolve_only_policy("smartevse_1", smartevse_1)
        if policy == ChargePolicy.SMARTEVSE_2_ONLY:
            return self._resolve_only_policy("smartevse_2", smartevse_2)

        if not connected_smartevse:
            self._reset_charge_cycle()
            return "none", None, 0, "waiting_for_connected_ev"

        if not eligible_smartevse:
            self._reset_charge_cycle()
            return "none", None, 0, "all_connected_evs_complete"

        if len(eligible_smartevse) == 1:
            only_smartevse = eligible_smartevse[0]
            self._mutable["active_smartevse"] = only_smartevse
            self._mutable["active_smartevse_since"] = None
            return only_smartevse, None, 0, ""

        preferred_smartevse = self._preferred_smartevse(policy)
        active_smartevse = str(self._mutable.get("active_smartevse") or "")
        active_smartevse_since = self._parse_datetime(self._mutable.get("active_smartevse_since"))
        interval_seconds = max(1, int(self._mutable["duty_cycle_minutes"])) * 60

        if active_smartevse not in eligible_smartevse:
            active_smartevse = (
                preferred_smartevse if preferred_smartevse in eligible_smartevse else eligible_smartevse[0]
            )
            active_smartevse_since = now
        elif active_smartevse_since is None:
            if preferred_smartevse in eligible_smartevse and active_smartevse != preferred_smartevse:
                active_smartevse = preferred_smartevse
            active_smartevse_since = now
        else:
            elapsed = int((now - active_smartevse_since).total_seconds())
            next_smartevse = self._other_smartevse(active_smartevse)
            if elapsed >= interval_seconds and next_smartevse in eligible_smartevse:
                active_smartevse = next_smartevse
                active_smartevse_since = now

        duty_cycle_remaining = max(
            interval_seconds - int((now - active_smartevse_since).total_seconds()),
            0,
        )

        self._mutable["active_smartevse"] = active_smartevse
        self._mutable["active_smartevse_since"] = active_smartevse_since.isoformat()
        return active_smartevse, active_smartevse_since.isoformat(), duty_cycle_remaining, ""

    def _resolve_only_policy(
        self,
        selected_smartevse: str,
        selected_status: SmartEVSEStatus,
    ) -> tuple[str, str | None, int, str]:
        """Resolve a fixed SmartEVSE policy."""
        self._mutable["active_smartevse"] = None
        self._mutable["active_smartevse_since"] = None

        if not selected_status.available:
            return "none", None, 0, f"{selected_smartevse}_api_unavailable"
        if not selected_status.connected:
            return "none", None, 0, f"{selected_smartevse}_only_waiting_for_selected_ev"
        if self._session_complete(selected_smartevse):
            return "none", None, 0, f"{selected_smartevse}_only_selected_ev_already_complete"
        return selected_smartevse, None, 0, ""

    async def _apply_modes(
        self,
        *,
        smartevse_1: SmartEVSEStatus,
        smartevse_2: SmartEVSEStatus,
        active_smartevse: str,
        charge_allowed: bool,
    ) -> None:
        """Apply the desired SmartEVSE modes for this cycle."""
        desired_smartevse = active_smartevse if charge_allowed else "none"
        await asyncio.gather(
            self._apply_mode_for_smartevse(
                smartevse_1, desired_mode=self._desired_mode("smartevse_1", desired_smartevse)
            ),
            self._apply_mode_for_smartevse(
                smartevse_2, desired_mode=self._desired_mode("smartevse_2", desired_smartevse)
            ),
        )

    def _desired_mode(self, smartevse_key: str, active_smartevse: str) -> str:
        """Return the desired mode for the given SmartEVSE."""
        return "Smart" if smartevse_key == active_smartevse else "Off"

    async def _apply_mode_for_smartevse(self, status: SmartEVSEStatus, *, desired_mode: str) -> None:
        """Apply the desired mode and clear any override current."""
        if not status.available:
            return

        params: dict[str, Any] = {}
        if status.mode != desired_mode:
            params["mode"] = MODE_NAME_TO_ID[desired_mode]
        if status.override_current > 0:
            params["disable_override_current"] = 1
        if not params:
            return

        await self._async_post(
            urljoin(self._normalize_url(status.base_url), "settings"),
            params=params,
        )

    async def _async_fetch_status(self, key: str, base_url: str) -> SmartEVSEStatus:
        """Fetch status from one SmartEVSE."""
        payload = await self._async_get_json(urljoin(self._normalize_url(base_url), "settings"))
        if payload is None:
            return SmartEVSEStatus(
                key=key,
                base_url=base_url,
                available=False,
                connected=False,
                plug_state="Unavailable",
                state="Unavailable",
                mode="Unavailable",
                charge_current=0.0,
                max_current=0.0,
                override_current=0.0,
                error="API unavailable",
            )

        evse = payload.get("evse") or {}
        settings = payload.get("settings") or {}
        connected = bool(payload.get("car_connected") or evse.get("connected"))
        plug_state = "Connected" if connected else "Disconnected"

        return SmartEVSEStatus(
            key=key,
            base_url=base_url,
            available=True,
            connected=connected,
            plug_state=plug_state,
            state=str(evse.get("state") or ("Connected to EV" if connected else "Ready to Charge")),
            mode=self._normalize_mode(payload.get("mode"), payload.get("mode_id")),
            charge_current=self._deciamp_to_amp(settings.get("charge_current")),
            max_current=self._to_float(settings.get("current_max") or settings.get("current_max_circuit")),
            override_current=self._deciamp_to_amp(settings.get("override_current")),
            error=str(evse.get("error") or "None"),
        )

    async def async_push_currents(self) -> None:
        """Push mains currents to both SmartEVSE devices."""
        if not self._options.get(CONF_PUSH_CURRENTS, DEFAULT_PUSH_CURRENTS):
            return
        base_urls = self._configured_base_urls()
        if not base_urls:
            return

        mains_currents = self._phase_currents_or_none(
            (
                self._entry_data[CONF_MAINS_L1_ENTITY],
                self._entry_data[CONF_MAINS_L2_ENTITY],
                self._entry_data[CONF_MAINS_L3_ENTITY],
            )
        )
        if mains_currents is None:
            return

        params = {
            "L1": self._deciamps(mains_currents[0]),
            "L2": self._deciamps(mains_currents[1]),
            "L3": self._deciamps(mains_currents[2]),
        }
        results = await asyncio.gather(
            *(self._async_post(urljoin(self._normalize_url(base_url), "currents"), params=params) for base_url in base_urls)
        )
        if results and all(results):
            self._mutable["last_meter_push"] = dt_util.utcnow().isoformat()

    async def async_push_ev_meter(self) -> None:
        """Push EV meter data to both SmartEVSE devices."""
        if not self._options.get(CONF_PUSH_EV_METER, DEFAULT_PUSH_EV_METER):
            return
        base_urls = self._configured_base_urls()
        if not base_urls:
            return

        params = {
            "L1": self._deciamps(self._state_float(self._entry_data[CONF_EV_METER_L1_ENTITY])),
            "L2": self._deciamps(self._state_float(self._entry_data[CONF_EV_METER_L2_ENTITY])),
            "L3": self._deciamps(self._state_float(self._entry_data[CONF_EV_METER_L3_ENTITY])),
            "import_active_power": int(round(self._state_float(self._entry_data[CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY]))),
            "import_active_energy": int(round(self._state_float(self._entry_data.get(CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY)) * 1000)),
            "export_active_energy": int(round(self._state_float(self._entry_data.get(CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY)) * 1000)),
        }
        results = await asyncio.gather(
            *(self._async_post(urljoin(self._normalize_url(base_url), "ev_meter"), params=params) for base_url in base_urls)
        )
        if results and all(results):
            self._mutable["last_ev_meter_push"] = dt_util.utcnow().isoformat()

    def _configured_base_urls(self) -> list[str]:
        """Return configured SmartEVSE base URLs."""
        return [
            url
            for url in (
                self._entry_data.get(CONF_SMARTEVSE_1_BASE_URL),
                self._entry_data.get(CONF_SMARTEVSE_2_BASE_URL),
            )
            if url
        ]

    async def _maybe_push_wled(self, *, smartevse_1: SmartEVSEStatus, smartevse_2: SmartEVSEStatus) -> None:
        """Update WLED split segments if configured."""
        if not self._options.get(CONF_PUSH_WLED, DEFAULT_PUSH_WLED):
            return
        wled_url = self._entry_data.get(CONF_WLED_URL)
        if not wled_url:
            return

        payload = self._build_wled_payload(smartevse_1=smartevse_1, smartevse_2=smartevse_2)
        if payload == self._last_wled_payload:
            return

        if await self._async_post(normalize_wled_state_url(wled_url), json_payload=payload):
            self._last_wled_payload = payload
            self._mutable["last_wled_push"] = dt_util.utcnow().isoformat()

    def _build_wled_payload(
        self,
        *,
        smartevse_1: SmartEVSEStatus,
        smartevse_2: SmartEVSEStatus,
    ) -> dict[str, Any]:
        """Create the WLED payload."""
        return build_runtime_payload(smartevse_1=smartevse_1, smartevse_2=smartevse_2)

    async def _handle_schedule_notification(self, *, schedule_window_active: bool, schedule_enabled: bool) -> None:
        """Create or dismiss the schedule disabled notification."""
        previous = bool(self._mutable.get("last_schedule_window_active"))
        notify_enabled = self._options.get(CONF_NOTIFY_ON_SCHEDULE_WINDOW, DEFAULT_NOTIFY_ON_SCHEDULE_WINDOW)

        if notify_enabled and schedule_window_active and not previous and not schedule_enabled:
            persistent_notification.async_create(
                self.hass,
                "Charging schedule window started but the schedule gate is disabled.",
                title="EV Charging Schedule",
                notification_id=SCHEDULE_NOTIFICATION_ID,
            )
            self._mutable["last_notification"] = dt_util.utcnow().isoformat()

        if not schedule_window_active or schedule_enabled:
            persistent_notification.async_dismiss(self.hass, SCHEDULE_NOTIFICATION_ID)

        self._mutable["last_schedule_window_active"] = schedule_window_active

    def _preferred_smartevse(self, policy: ChargePolicy) -> str:
        """Return the preferred SmartEVSE for alternating policies."""
        if policy == ChargePolicy.SMARTEVSE_2_FIRST:
            return "smartevse_2"
        return "smartevse_1"

    def _other_smartevse(self, smartevse_key: str) -> str:
        """Return the other SmartEVSE."""
        return "smartevse_2" if smartevse_key == "smartevse_1" else "smartevse_1"

    def _timer_until(self) -> datetime | None:
        """Return the current timer end."""
        return self._parse_datetime(self._mutable.get("timer_until"))

    def _state_str(self, entity_id: str | None, default: str = "") -> str:
        """Read an entity state as a string."""
        if not entity_id:
            return default
        state = self.hass.states.get(entity_id)
        if state is None or state.state in {"unknown", "unavailable", None}:
            return default
        return str(state.state)

    def _state_float(self, entity_id: str | None, default: float = 0.0) -> float:
        """Read an entity state as a float."""
        if not entity_id:
            return default
        state = self.hass.states.get(entity_id)
        if state is None:
            return default
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return default

    def _state_float_or_none(self, entity_id: str | None) -> float | None:
        """Read an entity state as a float or return None when unavailable."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in {"unknown", "unavailable", None}:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _phase_currents_or_none(self, entity_ids: tuple[str, str, str]) -> tuple[float, float, float] | None:
        """Return a 3-phase tuple or None if any phase is unavailable."""
        values = tuple(self._state_float_or_none(entity_id) for entity_id in entity_ids)
        if any(value is None for value in values):
            return None
        return values[0], values[1], values[2]

    def _controller_error_for_reason(self, reason: str | None) -> str | None:
        """Return the exposed controller error string for actionable failures."""
        if not reason:
            return None
        if reason in {
            "mains_data_unavailable",
            "price_sensor_unavailable",
            "schedule_entity_unavailable",
            "smartevse_api_unavailable",
        }:
            return reason
        if reason.endswith("_api_unavailable"):
            return reason
        return None

    def _state_on(self, entity_id: str | None) -> bool:
        """Return whether the entity is on."""
        return self._state_str(entity_id) == "on"

    def get_update_interval(self) -> int:
        """Return the controller refresh interval in seconds."""
        return max(1, int(self._mutable["update_interval"]))

    def get_currents_push_interval(self) -> int:
        """Return the mains current push interval in seconds."""
        return max(1, int(self._mutable["currents_push_interval"]))

    def get_ev_meter_push_interval(self) -> int:
        """Return the EV meter push interval in seconds."""
        return max(1, int(self._mutable["ev_meter_push_interval"]))

    def _to_float(self, value: Any, default: float = 0.0) -> float:
        """Convert an arbitrary JSON value to float."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _deciamp_to_amp(self, value: Any) -> float:
        """Convert SmartEVSE deci-amps into amps."""
        return self._to_float(value) / 10.0

    def _deciamps(self, amps: float) -> int:
        """Convert amps to SmartEVSE API deci-amps."""
        return int(round(amps * 10))

    def _normalize_mode(self, mode: Any, mode_id: Any) -> str:
        """Normalize REST mode names into title case."""
        if isinstance(mode, str) and mode:
            lookup = mode.strip().upper()
            for name in MODE_NAME_TO_ID:
                if name.upper() == lookup:
                    return name
        try:
            return MODE_ID_TO_NAME[int(mode_id)]
        except (TypeError, ValueError, KeyError):
            return "Unknown"

    async def _async_get_json(self, url: str) -> dict[str, Any] | None:
        """Fetch JSON and suppress repetitive endpoint failures."""
        endpoint_key = f"GET {url}"
        try:
            async with self._session.get(url, timeout=5) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except (TimeoutError, ClientError, ValueError) as err:
            self._log_endpoint_failure(endpoint_key, err)
            return None

        self._clear_endpoint_failure(endpoint_key)
        return payload if isinstance(payload, dict) else None

    async def _async_post(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> bool:
        """Send a POST request and log failures without crashing the loop."""
        endpoint_key = f"POST {url}"
        request_kwargs: dict[str, Any] = {"params": params, "timeout": 5}
        if json_payload is None:
            request_kwargs["data"] = ""
        else:
            request_kwargs["json"] = json_payload
        try:
            async with self._session.post(url, **request_kwargs) as response:
                response.raise_for_status()
        except (TimeoutError, ClientError) as err:
            self._log_endpoint_failure(endpoint_key, err)
            return False

        self._clear_endpoint_failure(endpoint_key)
        return True

    def _log_endpoint_failure(self, endpoint_key: str, err: Exception) -> None:
        """Log only on endpoint failure state transitions."""
        message = str(err)
        if self._endpoint_failures.get(endpoint_key) == message:
            return
        self._endpoint_failures[endpoint_key] = message
        LOGGER.warning("%s failed: %s", endpoint_key, message)

    def _clear_endpoint_failure(self, endpoint_key: str) -> None:
        """Clear endpoint failure tracking after recovery."""
        if endpoint_key in self._endpoint_failures:
            LOGGER.info("%s recovered", endpoint_key)
            self._endpoint_failures.pop(endpoint_key, None)

    async def _async_save_state(self) -> None:
        """Persist mutable controller state."""
        await self._store.async_save(self._mutable)

    def _parse_datetime(self, value: str | None) -> datetime | None:
        """Parse an ISO datetime string."""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _normalize_url(self, base_url: str) -> str:
        """Normalize a device base URL."""
        normalized = base_url.strip()
        if not normalized.startswith(("http://", "https://")):
            normalized = f"http://{normalized}"
        if not normalized.endswith("/"):
            normalized = f"{normalized}/"
        return normalized
