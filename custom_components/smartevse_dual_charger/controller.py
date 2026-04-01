"""Core controller for SmartEVSE Dual Charger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isclose
from typing import Any
from urllib.parse import urljoin

from aiohttp import ClientError
from homeassistant.components import persistent_notification
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ACTIVE_STATE,
    ATTR_AVAILABLE_CURRENT,
    ATTR_CHARGE_ALLOWED,
    ATTR_CHARGE_REASON,
    ATTR_CONTROLLER_STATE,
    ATTR_EVSE_1_TARGET_CURRENT,
    ATTR_EVSE_2_TARGET_CURRENT,
    ATTR_HOUSE_LOAD,
    ATTR_LAST_CYCLE_REASON,
    ATTR_LAST_METER_PUSH,
    ATTR_LAST_NOTIFICATION,
    ATTR_LAST_WLED_PUSH,
    ATTR_LOW_BUDGET_WINNER,
    ATTR_MAINS_PEAK,
    ATTR_SCHEDULE_WINDOW_ACTIVE,
    ATTR_TIMER_REMAINING,
    ATTR_TIMER_UNTIL,
    COMPLETE_STATES,
    CONF_ACTIVE_MODE,
    CONF_CURRENTS_PUSH_INTERVAL,
    CONF_EVSE_1_BASE_URL,
    CONF_EVSE_1_ERROR_ENTITY,
    CONF_EVSE_1_MODE_ENTITY,
    CONF_EVSE_1_OVERRIDE_ENTITY,
    CONF_EVSE_1_PLUG_ENTITY,
    CONF_EVSE_1_STATE_ENTITY,
    CONF_EVSE_2_BASE_URL,
    CONF_EVSE_2_ERROR_ENTITY,
    CONF_EVSE_2_MODE_ENTITY,
    CONF_EVSE_2_OVERRIDE_ENTITY,
    CONF_EVSE_2_PLUG_ENTITY,
    CONF_EVSE_2_STATE_ENTITY,
    CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY,
    CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY,
    CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY,
    CONF_EV_METER_L1_ENTITY,
    CONF_EV_METER_L2_ENTITY,
    CONF_EV_METER_L3_ENTITY,
    CONF_EV_METER_PUSH_INTERVAL,
    CONF_LOW_BUDGET_POLICY_DEFAULT,
    CONF_MAINS_L1_ENTITY,
    CONF_MAINS_L2_ENTITY,
    CONF_MAINS_L3_ENTITY,
    CONF_NOTIFY_ON_SCHEDULE_WINDOW,
    CONF_OVERRIDE_DEADBAND,
    CONF_PRICE_SENSOR_ENTITY,
    CONF_PUSH_CURRENTS,
    CONF_PUSH_EV_METER,
    CONF_PUSH_WLED,
    CONF_SCHEDULE_ENTITY,
    CONF_TOTAL_CURRENT_LIMIT,
    CONF_UPDATE_INTERVAL,
    CONF_WLED_URL,
    ControllerState,
    DEFAULT_ACTIVE_MODE,
    DEFAULT_ACCEPTABLE_PRICE,
    DEFAULT_BALANCE_PERCENT,
    DEFAULT_FORCE_CHARGE_DURATION_MINUTES,
    DEFAULT_LOW_BUDGET_POLICY,
    DEFAULT_MIN_CURRENT,
    DEFAULT_NOTIFY_ON_SCHEDULE_WINDOW,
    DEFAULT_OVERRIDE_DEADBAND,
    DEFAULT_PUSH_CURRENTS,
    DEFAULT_PUSH_EV_METER,
    DEFAULT_PUSH_WLED,
    DEFAULT_TOTAL_CURRENT_LIMIT,
    DEFAULT_UPDATE_INTERVAL,
    LOGGER,
    LowBudgetPolicy,
    PENDING_STATES,
    SCHEDULE_NOTIFICATION_ID,
    STORAGE_KEY,
    STORAGE_VERSION,
)

MUTABLE_DEFAULTS: dict[str, Any] = {
    "force_charge": False,
    "force_price": False,
    "force_timer": False,
    "schedule_enabled": False,
    "acceptable_price": DEFAULT_ACCEPTABLE_PRICE,
    "balance_percent": DEFAULT_BALANCE_PERCENT,
    "force_charge_duration_minutes": DEFAULT_FORCE_CHARGE_DURATION_MINUTES,
    "low_budget_policy": DEFAULT_LOW_BUDGET_POLICY,
    "timer_until": None,
    "session_has_charged_1": False,
    "session_has_charged_2": False,
    "last_low_budget_winner": None,
    "low_budget_active": False,
    "last_schedule_window_active": False,
    "last_meter_push": None,
    "last_ev_meter_push": None,
    "last_wled_push": None,
    "last_notification": None,
}


@dataclass(slots=True)
class EVSEStatus:
    """Computed EVSE state for one connector."""

    connected: bool
    state: str
    mode: str
    override_current: float
    error: str
    session_has_charged: bool
    active: bool
    pending: bool
    complete: bool


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
        self._last_wled_payload: dict[str, Any] | None = None

    async def async_initialize(self) -> None:
        """Load persisted mutable state."""
        stored = await self._store.async_load() or {}
        self._mutable = {**MUTABLE_DEFAULTS, **stored}
        if "low_budget_policy" not in stored:
            self._mutable["low_budget_policy"] = self._options.get(
                CONF_LOW_BUDGET_POLICY_DEFAULT,
                DEFAULT_LOW_BUDGET_POLICY,
            )

    async def async_set_force_charge(self, value: bool) -> None:
        """Enable or disable force charge."""
        self._mutable["force_charge"] = value
        if value:
            self._mutable["force_price"] = False
            self._mutable["force_timer"] = False
            self._mutable["timer_until"] = None
        await self._async_save_state()

    async def async_set_force_price(self, value: bool) -> None:
        """Enable or disable price-based force charging."""
        self._mutable["force_price"] = value
        if value:
            self._mutable["force_charge"] = False
            self._mutable["force_timer"] = False
            self._mutable["timer_until"] = None
        await self._async_save_state()

    async def async_set_force_timer(self, value: bool) -> None:
        """Enable or disable timer-based force charging."""
        self._mutable["force_timer"] = value
        if value:
            self._mutable["force_charge"] = False
            self._mutable["force_price"] = False
            duration = int(self._mutable["force_charge_duration_minutes"])
            self._mutable["timer_until"] = (dt_util.utcnow() + timedelta(minutes=duration)).isoformat()
        else:
            self._mutable["timer_until"] = None
        await self._async_save_state()

    async def async_set_schedule_enabled(self, value: bool) -> None:
        """Enable or disable the schedule gate."""
        self._mutable["schedule_enabled"] = value
        await self._async_save_state()

    async def async_set_acceptable_price(self, value: float) -> None:
        """Update the acceptable price threshold."""
        self._mutable["acceptable_price"] = round(value, 4)
        await self._async_save_state()

    async def async_set_balance_percent(self, value: float) -> None:
        """Update the balance split percentage."""
        self._mutable["balance_percent"] = int(round(value))
        await self._async_save_state()

    async def async_set_force_charge_duration(self, value: float) -> None:
        """Update timer duration in minutes."""
        self._mutable["force_charge_duration_minutes"] = int(round(value))
        if self._mutable["force_timer"]:
            duration = int(self._mutable["force_charge_duration_minutes"])
            self._mutable["timer_until"] = (dt_util.utcnow() + timedelta(minutes=duration)).isoformat()
        await self._async_save_state()

    async def async_set_low_budget_policy(self, value: str) -> None:
        """Update the low budget policy."""
        self._mutable["low_budget_policy"] = value
        self._mutable["low_budget_active"] = False
        await self._async_save_state()

    async def async_reset_sessions(self) -> None:
        """Reset per-session completion tracking."""
        self._mutable["session_has_charged_1"] = False
        self._mutable["session_has_charged_2"] = False
        self._mutable["low_budget_active"] = False
        await self._async_save_state()

    async def async_run_cycle(self, *, reason: str) -> dict[str, Any]:
        """Run a complete controller cycle and return computed state."""
        now = dt_util.utcnow()
        self._sanitize_mutual_exclusion()

        evse_1 = self._build_evse_status(1)
        evse_2 = self._build_evse_status(2)

        if not evse_1.connected:
            self._mutable["session_has_charged_1"] = False
        elif evse_1.state == ACTIVE_STATE:
            self._mutable["session_has_charged_1"] = True

        if not evse_2.connected:
            self._mutable["session_has_charged_2"] = False
        elif evse_2.state == ACTIVE_STATE:
            self._mutable["session_has_charged_2"] = True

        evse_1 = self._build_evse_status(1)
        evse_2 = self._build_evse_status(2)

        if self._mutable["force_timer"]:
            timer_until = self._timer_until()
            if timer_until is None or now >= timer_until:
                self._mutable["force_timer"] = False
                self._mutable["timer_until"] = None

        if self._mutable["force_timer"] and not evse_1.connected and not evse_2.connected:
            self._mutable["force_timer"] = False
            self._mutable["timer_until"] = None

        price_value = self._state_float(self._entry_data.get(CONF_PRICE_SENSOR_ENTITY))
        schedule_window_active = self._state_on(self._entry_data.get(CONF_SCHEDULE_ENTITY))
        charge_allowed, controller_state, charge_reason = self._determine_charge_allowed(
            price_value=price_value,
            schedule_window_active=schedule_window_active,
        )

        await self._handle_schedule_notification(
            schedule_window_active=schedule_window_active,
            schedule_enabled=bool(self._mutable["schedule_enabled"]),
        )

        mains_peak = max(
            self._state_float(self._entry_data[CONF_MAINS_L1_ENTITY]),
            self._state_float(self._entry_data[CONF_MAINS_L2_ENTITY]),
            self._state_float(self._entry_data[CONF_MAINS_L3_ENTITY]),
        )
        ev_meter_peak = max(
            self._state_float(self._entry_data[CONF_EV_METER_L1_ENTITY]),
            self._state_float(self._entry_data[CONF_EV_METER_L2_ENTITY]),
            self._state_float(self._entry_data[CONF_EV_METER_L3_ENTITY]),
        )
        house_load = max(mains_peak - ev_meter_peak, 0.0)
        available_current = max(self._option_float(CONF_TOTAL_CURRENT_LIMIT, DEFAULT_TOTAL_CURRENT_LIMIT) - house_load, 0.0)

        target_1, target_2, low_budget_winner = self._calculate_targets(
            available_current=available_current,
            charge_allowed=charge_allowed,
            evse_1=evse_1,
            evse_2=evse_2,
        )

        if low_budget_winner:
            controller_state = ControllerState.LOW_BUDGET

        await self._apply_targets(evse_1=evse_1, evse_2=evse_2, target_1=target_1, target_2=target_2)
        await self._maybe_push_meter_data(now=now)
        await self._maybe_push_wled(evse_1=evse_1, evse_2=evse_2)
        await self._async_save_state()

        timer_until = self._timer_until()
        timer_remaining = None
        if timer_until is not None:
            timer_remaining = max(int((timer_until - now).total_seconds()), 0)

        return {
            "force_charge": bool(self._mutable["force_charge"]),
            "force_price": bool(self._mutable["force_price"]),
            "force_timer": bool(self._mutable["force_timer"]),
            "schedule_enabled": bool(self._mutable["schedule_enabled"]),
            "acceptable_price": float(self._mutable["acceptable_price"]),
            "balance_percent": int(self._mutable["balance_percent"]),
            "force_charge_duration_minutes": int(self._mutable["force_charge_duration_minutes"]),
            "low_budget_policy": str(self._mutable["low_budget_policy"]),
            ATTR_CHARGE_ALLOWED: charge_allowed,
            ATTR_CONTROLLER_STATE: controller_state.value,
            ATTR_CHARGE_REASON: charge_reason,
            ATTR_AVAILABLE_CURRENT: round(available_current, 1),
            ATTR_HOUSE_LOAD: round(house_load, 1),
            ATTR_MAINS_PEAK: round(mains_peak, 1),
            ATTR_EVSE_1_TARGET_CURRENT: target_1,
            ATTR_EVSE_2_TARGET_CURRENT: target_2,
            ATTR_SCHEDULE_WINDOW_ACTIVE: schedule_window_active,
            ATTR_LOW_BUDGET_WINNER: low_budget_winner,
            ATTR_LAST_CYCLE_REASON: reason,
            ATTR_TIMER_UNTIL: timer_until,
            ATTR_TIMER_REMAINING: timer_remaining,
            ATTR_LAST_METER_PUSH: self._mutable["last_meter_push"],
            ATTR_LAST_WLED_PUSH: self._mutable["last_wled_push"],
            ATTR_LAST_NOTIFICATION: self._mutable["last_notification"],
            "evse_1_connected": evse_1.connected,
            "evse_2_connected": evse_2.connected,
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

    def _build_evse_status(self, evse_index: int) -> EVSEStatus:
        """Return computed EVSE status."""
        state = self._state_str(self._entry_data[f"evse_{evse_index}_state_entity"])
        connected = self._state_str(self._entry_data[f"evse_{evse_index}_plug_entity"]) == "Connected"
        mode = self._state_str(self._entry_data[f"evse_{evse_index}_mode_entity"])
        override_current = self._state_float(self._entry_data[f"evse_{evse_index}_override_entity"])
        error_entity = self._entry_data.get(f"evse_{evse_index}_error_entity")
        error = self._state_str(error_entity) if error_entity else ""
        session_has_charged = bool(self._mutable[f"session_has_charged_{evse_index}"])
        active = connected and state == ACTIVE_STATE
        pending = connected and not active and (state in PENDING_STATES or (state == "Connected to EV" and not session_has_charged))
        complete = connected and not active and session_has_charged and state in COMPLETE_STATES
        return EVSEStatus(
            connected=connected,
            state=state,
            mode=mode,
            override_current=override_current,
            error=error,
            session_has_charged=session_has_charged,
            active=active,
            pending=pending,
            complete=complete,
        )

    def _determine_charge_allowed(self, *, price_value: float, schedule_window_active: bool) -> tuple[bool, ControllerState, str]:
        """Resolve high-level controller state."""
        acceptable_price = float(self._mutable["acceptable_price"])
        if self._mutable["force_charge"]:
            return True, ControllerState.FORCE, "force_charge"
        if self._mutable["force_timer"] and self._timer_until() is not None:
            return True, ControllerState.TIMER, "force_timer"
        if self._mutable["force_price"] and price_value <= acceptable_price:
            return True, ControllerState.PRICE, "acceptable_price"
        if self._mutable["schedule_enabled"] and schedule_window_active:
            return True, ControllerState.SCHEDULE, "schedule"
        return False, ControllerState.IDLE, "idle"

    def _calculate_targets(
        self,
        *,
        available_current: float,
        charge_allowed: bool,
        evse_1: EVSEStatus,
        evse_2: EVSEStatus,
    ) -> tuple[int, int, str | None]:
        """Calculate current targets in whole amps."""
        if not charge_allowed:
            self._mutable["low_budget_active"] = False
            return 0, 0, None

        min_current = int(round(self._option_float("min_current", DEFAULT_MIN_CURRENT)))
        max_current = int(round(self._option_float(CONF_TOTAL_CURRENT_LIMIT, DEFAULT_TOTAL_CURRENT_LIMIT)))
        available_int = max(int(round(available_current)), 0)

        candidates: list[str] = []
        active_candidates: list[str] = []
        for evse_id, status in (("evse_1", evse_1), ("evse_2", evse_2)):
            if not status.connected:
                continue
            if status.active or status.pending:
                candidates.append(evse_id)
            if status.active:
                active_candidates.append(evse_id)

        if not candidates:
            self._mutable["low_budget_active"] = False
            return 0, 0, None

        if len(candidates) == 1:
            self._mutable["low_budget_active"] = False
            target = self._target_for_single_candidate(available_int, min_current, max_current)
            return (target, 0, None) if candidates[0] == "evse_1" else (0, target, None)

        if available_int < min_current:
            self._mutable["low_budget_active"] = True
            self._mutable["last_low_budget_winner"] = None
            return 0, 0, None

        if available_int < (2 * min_current):
            if LowBudgetPolicy(self._mutable.get("low_budget_policy", DEFAULT_LOW_BUDGET_POLICY)) == LowBudgetPolicy.PAUSE_ALL:
                self._mutable["low_budget_active"] = True
                self._mutable["last_low_budget_winner"] = None
                return 0, 0, None
            winner = self._select_low_budget_winner(
                candidates=candidates,
                active_candidates=active_candidates,
            )
            self._mutable["low_budget_active"] = True
            self._mutable["last_low_budget_winner"] = winner
            target = min(max(available_int, min_current), max_current)
            return (target, 0, winner) if winner == "evse_1" else (0, target, winner)

        self._mutable["low_budget_active"] = False
        remainder = available_int - (2 * min_current)
        ratio = max(0.0, min(float(self._mutable["balance_percent"]) / 100.0, 1.0))
        evse_1_target = min_current + int(round(remainder * ratio))
        evse_2_target = available_int - evse_1_target
        evse_1_target = min(evse_1_target, max_current)
        evse_2_target = min(evse_2_target, max_current)

        if evse_1_target < min_current:
            deficit = min_current - evse_1_target
            evse_1_target = min_current
            evse_2_target = max(evse_2_target - deficit, min_current)
        if evse_2_target < min_current:
            deficit = min_current - evse_2_target
            evse_2_target = min_current
            evse_1_target = max(evse_1_target - deficit, min_current)

        return evse_1_target, evse_2_target, None

    def _target_for_single_candidate(self, available_int: int, min_current: int, max_current: int) -> int:
        """Return the target current for one active/pending EVSE."""
        if available_int < min_current:
            return 0
        return min(max(available_int, min_current), max_current)

    def _select_low_budget_winner(self, *, candidates: list[str], active_candidates: list[str]) -> str:
        """Select which EVSE keeps charging when current is tight."""
        if len(active_candidates) == 1:
            return active_candidates[0]

        policy = LowBudgetPolicy(self._mutable.get("low_budget_policy", DEFAULT_LOW_BUDGET_POLICY))
        if policy == LowBudgetPolicy.PAUSE_ALL:
            return candidates[0]
        if policy == LowBudgetPolicy.EVSE_1_PRIORITY:
            return "evse_1" if "evse_1" in candidates else candidates[0]
        if policy == LowBudgetPolicy.EVSE_2_PRIORITY:
            return "evse_2" if "evse_2" in candidates else candidates[0]

        if self._mutable.get("low_budget_active"):
            previous = self._mutable.get("last_low_budget_winner")
            if previous in candidates:
                return previous

        previous = self._mutable.get("last_low_budget_winner")
        if previous == "evse_1" and "evse_2" in candidates:
            return "evse_2"
        if previous == "evse_2" and "evse_1" in candidates:
            return "evse_1"
        return "evse_1" if "evse_1" in candidates else candidates[0]

    async def _apply_targets(self, *, evse_1: EVSEStatus, evse_2: EVSEStatus, target_1: int, target_2: int) -> None:
        """Apply mode and override changes to both EVSEs."""
        await self._apply_target_for_evse(1, evse_1, target_1)
        await self._apply_target_for_evse(2, evse_2, target_2)

    async def _apply_target_for_evse(self, evse_index: int, status: EVSEStatus, target: int) -> None:
        """Apply mode and override changes to one EVSE."""
        mode_entity = self._entry_data[f"evse_{evse_index}_mode_entity"]
        override_entity = self._entry_data[f"evse_{evse_index}_override_entity"]
        active_mode = self._options.get(CONF_ACTIVE_MODE, DEFAULT_ACTIVE_MODE)
        deadband = float(self._options.get(CONF_OVERRIDE_DEADBAND, DEFAULT_OVERRIDE_DEADBAND))

        if not status.connected or target <= 0:
            if status.mode != "Off":
                await self._async_select_option(mode_entity, "Off")
            if not isclose(status.override_current, 0.0, abs_tol=0.01):
                await self._async_set_number(override_entity, 0.0)
            return

        if status.mode != active_mode:
            await self._async_select_option(mode_entity, active_mode)

        if isclose(status.override_current, 0.0, abs_tol=0.01) or abs(status.override_current - target) >= deadband:
            await self._async_set_number(override_entity, float(target))

    async def _maybe_push_meter_data(self, *, now: datetime) -> None:
        """Push mains and EV meter values to SmartEVSE endpoints when enabled."""
        if not self._options.get(CONF_PUSH_CURRENTS, DEFAULT_PUSH_CURRENTS) and not self._options.get(CONF_PUSH_EV_METER, DEFAULT_PUSH_EV_METER):
            return

        base_urls = [
            self._entry_data.get(CONF_EVSE_1_BASE_URL),
            self._entry_data.get(CONF_EVSE_2_BASE_URL),
        ]
        base_urls = [url for url in base_urls if url]
        if not base_urls:
            return

        if self._options.get(CONF_PUSH_CURRENTS, DEFAULT_PUSH_CURRENTS):
            push_every = int(self._options.get(CONF_CURRENTS_PUSH_INTERVAL, self._options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)))
            last_push = self._parse_datetime(self._mutable.get("last_meter_push"))
            if last_push is None or (now - last_push).total_seconds() >= push_every:
                params = {
                    "L1": self._deciamps(self._state_float(self._entry_data[CONF_MAINS_L1_ENTITY])),
                    "L2": self._deciamps(self._state_float(self._entry_data[CONF_MAINS_L2_ENTITY])),
                    "L3": self._deciamps(self._state_float(self._entry_data[CONF_MAINS_L3_ENTITY])),
                }
                for base_url in base_urls:
                    await self._async_post(urljoin(self._normalize_url(base_url), "currents"), params=params)
                self._mutable["last_meter_push"] = now.isoformat()

        if self._options.get(CONF_PUSH_EV_METER, DEFAULT_PUSH_EV_METER):
            push_every = int(self._options.get(CONF_EV_METER_PUSH_INTERVAL, self._options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL) * 2))
            last_push = self._parse_datetime(self._mutable.get("last_ev_meter_push"))
            if last_push is None or (now - last_push).total_seconds() >= push_every:
                params = {
                    "L1": self._deciamps(self._state_float(self._entry_data[CONF_EV_METER_L1_ENTITY])),
                    "L2": self._deciamps(self._state_float(self._entry_data[CONF_EV_METER_L2_ENTITY])),
                    "L3": self._deciamps(self._state_float(self._entry_data[CONF_EV_METER_L3_ENTITY])),
                    "import_active_power": int(round(self._state_float(self._entry_data[CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY]))),
                    "import_active_energy": int(round(self._state_float(self._entry_data.get(CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY)) * 1000)),
                    "export_active_energy": int(round(self._state_float(self._entry_data.get(CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY)) * 1000)),
                }
                for base_url in base_urls:
                    await self._async_post(urljoin(self._normalize_url(base_url), "ev_meter"), params=params)
                self._mutable["last_ev_meter_push"] = now.isoformat()

    async def _maybe_push_wled(self, *, evse_1: EVSEStatus, evse_2: EVSEStatus) -> None:
        """Update WLED split segments if configured."""
        if not self._options.get(CONF_PUSH_WLED, DEFAULT_PUSH_WLED):
            return
        wled_url = self._entry_data.get(CONF_WLED_URL)
        if not wled_url:
            return

        payload = self._build_wled_payload(evse_1=evse_1, evse_2=evse_2)
        if payload == self._last_wled_payload:
            return

        await self._async_post(self._normalize_wled_url(wled_url), json_payload=payload)
        self._last_wled_payload = payload
        self._mutable["last_wled_push"] = dt_util.utcnow().isoformat()

    def _build_wled_payload(self, *, evse_1: EVSEStatus, evse_2: EVSEStatus) -> dict[str, Any]:
        """Create the WLED payload."""
        def has_error(status: EVSEStatus) -> bool:
            return status.connected and status.error not in {"NONE", "None", "unknown", "unavailable", ""}

        def segment(segment_id: int, start: int, stop: int, status: EVSEStatus) -> dict[str, Any]:
            if not status.connected:
                return {"id": segment_id, "start": start, "stop": stop, "on": False}
            if status.state == "Charging":
                return {"id": segment_id, "start": start, "stop": stop, "on": True, "col": [[0, 255, 0]], "fx": 28, "sx": 100, "ix": 128}
            if status.state == "Ready to Charge":
                return {"id": segment_id, "start": start, "stop": stop, "on": True, "col": [[0, 100, 255]], "fx": 2, "sx": 80, "ix": 128}
            if status.state == "Connected to EV":
                return {"id": segment_id, "start": start, "stop": stop, "on": True, "col": [[255, 160, 0]], "fx": 0}
            if status.state == "Stop Charging":
                return {"id": segment_id, "start": start, "stop": stop, "on": True, "col": [[255, 80, 0]], "fx": 3, "sx": 100, "ix": 128}
            if status.state == "Charging Stopped":
                return {"id": segment_id, "start": start, "stop": stop, "on": True, "col": [[0, 100, 255]], "fx": 2, "sx": 80, "ix": 128}
            return {"id": segment_id, "start": start, "stop": stop, "on": True, "col": [[100, 100, 100]], "fx": 0}

        if not evse_1.connected and not evse_2.connected:
            return {"on": False, "transition": 30}

        if has_error(evse_1) or has_error(evse_2):
            return {
                "on": True,
                "bri": 200,
                "seg": [
                    {"id": 0, "start": 0, "stop": 53, "on": True, "col": [[255, 0, 0]], "fx": 2, "sx": 60, "ix": 200},
                    {"id": 1, "start": 53, "stop": 105, "on": True, "col": [[255, 0, 0]], "fx": 2, "sx": 60, "ix": 200},
                ],
            }

        return {
            "on": True,
            "bri": 128,
            "seg": [
                segment(0, 0, 53, evse_1),
                segment(1, 53, 105, evse_2),
            ],
        }

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

    def _timer_until(self) -> datetime | None:
        """Return the current timer end."""
        return self._parse_datetime(self._mutable.get("timer_until"))

    def _option_float(self, key: str, default: float) -> float:
        """Return a float option."""
        return float(self._options.get(key, default))

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

    def _state_on(self, entity_id: str | None) -> bool:
        """Return whether the entity is on."""
        return self._state_str(entity_id) == "on"

    def _deciamps(self, amps: float) -> int:
        """Convert amps to SmartEVSE API deci-amps."""
        return int(round(amps * 10))

    async def _async_select_option(self, entity_id: str, option: str) -> None:
        """Call select.select_option."""
        await self.hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: entity_id, "option": option},
            blocking=True,
            context=Context(),
        )

    async def _async_set_number(self, entity_id: str, value: float) -> None:
        """Call number.set_value."""
        await self.hass.services.async_call(
            "number",
            "set_value",
            {ATTR_ENTITY_ID: entity_id, "value": value},
            blocking=True,
            context=Context(),
        )

    async def _async_post(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> None:
        """Send a POST request and log failures without crashing the loop."""
        try:
            async with self._session.post(url, params=params, json=json_payload, timeout=5) as response:
                response.raise_for_status()
        except (TimeoutError, ClientError) as err:
            LOGGER.warning("POST %s failed: %s", url, err)

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

    def _normalize_wled_url(self, base_url: str) -> str:
        """Normalize a WLED endpoint URL."""
        normalized = base_url.strip()
        if not normalized.startswith(("http://", "https://")):
            normalized = f"http://{normalized}"
        if normalized.endswith("/json/state"):
            return normalized
        return urljoin(self._normalize_url(normalized), "json/state")
