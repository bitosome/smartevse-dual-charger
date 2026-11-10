"""Core controller for SmartEVSE Dual Charger."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import math
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
    CONF_VEHICLE_1_BATTERY_ENTITY,
    CONF_VEHICLE_1_CONNECTION_STATUS_ENTITY,
    CONF_VEHICLE_1_NAME,
    CONF_VEHICLE_2_BATTERY_ENTITY,
    CONF_VEHICLE_2_CONNECTION_STATUS_ENTITY,
    CONF_VEHICLE_2_NAME,
    CONF_WLED_URL,
    CONF_WLED_LED_COUNT,
    CONF_WLED_LED_OFFSET,
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
    DEFAULT_VEHICLE_1_NAME,
    DEFAULT_VEHICLE_2_NAME,
    DEFAULT_WLED_LED_COUNT,
    DEFAULT_WLED_LED_OFFSET,
    DEFAULT_MINIMUM_CHARGE,
    DEFAULT_READY_BY,
    ERROR_INTELLIGENT_PLANNER_FAILED,
    INTELLIGENT_ALLOWED_REASONS,
    INTELLIGENT_MINIMUM_KEYS,
    INTELLIGENT_READY_BY_KEYS,
    INTELLIGENT_SETTINGS_KEYS,
    LOGGER,
    REASON_INTELLIGENT_OUTLET_UNAVAILABLE,
    REASON_INTELLIGENT_WAITING,
    SCHEDULE_NOTIFICATION_ID,
    STORAGE_KEY,
    STORAGE_VERSION,
    ChargePolicy,
)
from .charge_planner import floor15
from .charge_model import reached_with_hysteresis
from .charging_cost import ChargingLedger, power_kw, tariff
from .intelligent import (
    IntelligentCharging,
    IntelligentDecision,
    OutletInput,
    OutletPayloadInput,
    PayloadContext,
    PlanInput,
    default_time_zone,
    empty_payload,
    flat_values,
    next_local_occurrence,
    normalize_minimum,
    normalize_ready_by,
    parse_ready_by,
    registry_entities,
    session_deadline,
)
from .storage import StrictStore
from .schedule_reminders import ScheduleReminders
from .naming import active_smartevse_label, configured_vehicle_name, smartevse_name
from .wled import build_flow_card_visuals, build_runtime_payload, normalize_wled_state_url, runtime_state_matches_payload

MUTABLE_DEFAULTS: dict[str, Any] = {
    "force_charge": False,
    "force_price": False,
    "force_timer": False,
    "schedule_enabled": False,
    "schedule_price": False,
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
    "pending_previous_active_smartevse": None,
    "handoff_target": None,
    "handoff_started_at": None,
    "policy_change_pending": False,
    "last_charge_allowed": False,
    "last_active_charge_reason": None,
    "last_schedule_window_active": False,
    "last_meter_push": None,
    "last_ev_meter_push": None,
    "last_wled_push": None,
    "last_notification": None,
    "smartevse_1_active_seen_charging": False,
    "smartevse_1_seen_charging": False,
    "smartevse_1_session_complete": False,
    "smartevse_1_non_charging_since": None,
    "smartevse_1_low_power_since": None,
    "smartevse_1_last_vehicle_charging_state": None,
    "smartevse_1_connected_ev": "unknown",
    "smartevse_1_last_connected": None,
    "smartevse_1_last_plug_connected": None,
    "smartevse_1_last_observed_state": None,
    "smartevse_1_recent_state_changes": [],
    "smartevse_2_active_seen_charging": False,
    "smartevse_2_seen_charging": False,
    "smartevse_2_session_complete": False,
    "smartevse_2_non_charging_since": None,
    "smartevse_2_low_power_since": None,
    "smartevse_2_last_vehicle_charging_state": None,
    "smartevse_2_connected_ev": "unknown",
    "smartevse_2_last_connected": None,
    "smartevse_2_last_plug_connected": None,
    "smartevse_2_last_observed_state": None,
    "smartevse_2_recent_state_changes": [],
    "known_vehicle_1_last_connected": None,
    "known_vehicle_2_last_connected": None,
    # Smart-mode pause guard (see _track_pause_state): last time each charger
    # showed a transient "no power" / communication pause.
    "smartevse_1_last_pause_at": None,
    "smartevse_2_last_pause_at": None,
    # First poll of the current API outage per charger (None while it answers).
    "smartevse_1_unavailable_since": None,
    "smartevse_2_unavailable_since": None,
    # Charging runs per charger (see _track_charging_runs): start of the current
    # polled "Charging" run and the last poll that showed "Charging". Vehicle
    # charging reports only count as mapping evidence when they postdate both.
    "smartevse_1_charging_since": None,
    "smartevse_2_charging_since": None,
    "smartevse_1_last_charging_at": None,
    "smartevse_2_last_charging_at": None,
    # Sequential hand-off (see _apply_modes): the last mode each charger was
    # polled in while reachable, and the outlet whose Off write is settling.
    "smartevse_1_last_observed_mode": None,
    "smartevse_2_last_observed_mode": None,
    "handoff_settle_from": None,
    "handoff_settle_since": None,
    # Intelligent charging settings (§5); persisted, survive restart and unplug.
    "intelligent_enabled": False,
    "intelligent_top_up": False,
    "intelligent_top_up_price": None,  # copied from acceptable_price on first init
    "vehicle_1_ready_by": DEFAULT_READY_BY,
    "vehicle_2_ready_by": DEFAULT_READY_BY,
    "other_vehicle_ready_by": DEFAULT_READY_BY,
    "vehicle_1_minimum_charge": DEFAULT_MINIMUM_CHARGE,
    "vehicle_2_minimum_charge": DEFAULT_MINIMUM_CHARGE,
    "other_vehicle_minimum_charge": DEFAULT_MINIMUM_CHARGE,
    # Previous cycle's final result (not-accepting timer, model attribution).
    "last_controller_state": None,
    "last_charge_reason": None,
    # Last SoC of the current plug session per vehicle (§5).
    "vehicle_1_last_soc": None,
    "vehicle_1_last_soc_at": None,
    "vehicle_2_last_soc": None,
    "vehicle_2_last_soc_at": None,
}

# Per-outlet Intelligent state (§5), cleared only by _clear_intelligent_outlet_state.
INTELLIGENT_OUTLET_DEFAULTS: dict[str, Any] = {
    "intelligent_plugged_at": None,
    "intelligent_deadline": None,
    "intelligent_deadline_vehicle": None,
    "intelligent_overrun": False,
    "intelligent_complete": False,
    "intelligent_complete_at": None,
    "intelligent_retries": 0,
    "intelligent_reached": False,
    "intelligent_not_accepting_until": None,
    "intelligent_probes": 0,
    "intelligent_no_draw_since": None,
    "intelligent_committed_until": None,
}
# Kept when Intelligent is switched on or a vehicle setting changes: the plug
# session identity and its deadline (which follows the §6.2 edit rule).
INTELLIGENT_SESSION_KEYS = frozenset({
    "intelligent_plugged_at",
    "intelligent_deadline",
    "intelligent_deadline_vehicle",
})
for _smartevse_key in ("smartevse_1", "smartevse_2"):
    for _name, _value in INTELLIGENT_OUTLET_DEFAULTS.items():
        MUTABLE_DEFAULTS[f"{_smartevse_key}_{_name}"] = _value

MODE_NAME_TO_ID = {
    "Off": 0,
    "Normal": 1,
    "Solar": 2,
    "Smart": 3,
    "Pause": 4,
}
MODE_ID_TO_NAME = {value: key for key, value in MODE_NAME_TO_ID.items()}
MAPPING_CONNECT_WINDOW = timedelta(minutes=10)
VEHICLE_TELEMETRY_MAX_AGE = timedelta(minutes=10)
# The Volvo cloud reports "charging" 1.5-3.5 min after the charger starts and
# holds it until its next update: a report less than this after the other
# charger last charged may still describe that charger's car.
VEHICLE_CHARGING_REPORT_LAG = timedelta(minutes=4)
# A charging report may predate the first poll of the charger's run by up to
# one poll interval; this much earlier still counts as "after the run began".
CHARGING_RUN_START_TOLERANCE = timedelta(seconds=30)
# A car that stopped with the other charger reports "not charging" within the
# report lag; one still reporting "charging" this long after the other charger
# last charged is charging here, even if its report came soon after the hand-off.
VEHICLE_CHARGING_REPORT_SETTLE = timedelta(minutes=8)
VEHICLE_COMPLETE_CONFIRMATION_GRACE_SECONDS = 30
SMARTEVSE_COMPLETE_GRACE_SECONDS = 180
LOW_EV_POWER_COMPLETE_GRACE_SECONDS = 180
ACTUAL_CHARGING_POWER_THRESHOLD_WATTS = 500
# Two unmapped outlets plugged in closer together than this cannot be told
# apart by "newest plug event"; leave both unknown until charging evidence.
SIMULTANEOUS_PLUG_WINDOW = timedelta(minutes=3)
# SmartEVSE Smart mode pauses (LESS_6A "No Power Available", API meter
# timeouts) followed by the firmware's ~60-90 s ChargeDelay are never a full car.
SMART_PAUSE_STATE_MARKERS = (
    "no power available",
    "communication error",
    "ev meter comm error",
)
SMART_PAUSE_COMPLETION_GUARD_SECONDS = 90
# Sequential hand-off: the incoming charger stays Off until the outgoing one is
# polled Off and the EV meter is below this power, or this long after the Off write.
HANDOFF_SETTLE_POWER_WATTS = 1000
HANDOFF_SETTLE_SECONDS = 30
# Minimum hold when the outgoing charger dropped off the API while drawing.
HANDOFF_UNAVAILABLE_MIN_SECONDS = 60
# Modes in which a charger may be drawing current (the controller itself only
# writes Smart or Off; Normal/Solar/Unknown are treated as drawing for safety).
NON_DRAWING_MODES = {"Off", "Pause"}
# Intelligent charging (§6).
INTELLIGENT_NOT_ACCEPTING_MIN_SECONDS = 300  # >= max(30 x update interval, 300 s)
INTELLIGENT_NOT_ACCEPTING_HOLD = timedelta(minutes=60)
INTELLIGENT_MAX_PROBES = 3
INTELLIGENT_RETRY_AFTER = timedelta(minutes=30)
INTELLIGENT_MAX_RETRIES = 2
INTELLIGENT_RETRY_MARGIN = 1.0  # % below target
INTELLIGENT_LATE_PLUG = timedelta(minutes=60)
INTELLIGENT_STALE_ROLL = timedelta(hours=2)  # a later roll never starts a catch-up
INTELLIGENT_READY_BY_EDIT_KEEP = timedelta(hours=2)
INTELLIGENT_IDENTIFYING_WINDOW = timedelta(minutes=10)
INTELLIGENT_LOG_INTERVAL = timedelta(minutes=10)
VEHICLE_COMPLETE_STATES = {
    "complete",
    "completed",
    "done",
    "fully_charged",
    "fully charged",
}


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
    # Parsed from GET /settings; defaults keep older constructors working.
    max_mains: float | None = None  # settings.current_main (A, MaxMains)
    min_current: float | None = None  # settings.current_min (A, MinCurrent)
    mode_id: int | None = None  # top-level mode_id (0 Off .. 4 Pause)
    raw_state: str = ""  # unmodified evse.state


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
        self._cost_store = StrictStore(hass, 1, f"{STORAGE_KEY}.{entry_data['entry_id']}.charging_costs")
        self._cost_ledger = None
        self._cost_error = None
        # Intelligent charging covers the schedule for reminders (§6.1).
        self._schedule_reminders = ScheduleReminders(
            hass, entry_data["entry_id"], entry_data.get(CONF_SCHEDULE_ENTITY), options,
            lambda: bool(self._mutable.get("schedule_enabled") or self._mutable.get("intelligent_enabled")),
        )
        self._intelligent_log_times: dict[str, datetime] = {}
        try:
            self._intelligent: IntelligentCharging | None = IntelligentCharging(hass, entry_data, options)
        except Exception:  # noqa: BLE001 - Intelligent must never stop the controller
            LOGGER.exception("Unable to set up Intelligent charging")
            self._intelligent = None

    async def async_initialize(self) -> None:
        """Load persisted mutable state."""
        await self._schedule_reminders.async_initialize()
        try:
            self._cost_ledger = ChargingLedger(await self._cost_store.async_load())
        except Exception:
            self._cost_error = "Charging history could not be loaded; existing storage has not been overwritten."
            LOGGER.exception("Unable to load charging cost history")
        stored = await self._store.async_load() or {}
        self._mutable = {**MUTABLE_DEFAULTS, **stored}
        if (
            "schedule_price" not in stored
            and bool(stored.get("schedule_enabled"))
            and bool(stored.get("force_price"))
            and not bool(stored.get("force_charge"))
            and not bool(stored.get("force_timer"))
        ):
            # Before 0.0.8.6 one price switch represented both schedule-price
            # and price-only force plans. Preserve the safe schedule-gated
            # interpretation during migration; users can explicitly re-enable
            # the newly unambiguous Force plan afterward.
            self._mutable["schedule_price"] = True
            self._mutable["force_price"] = False
        for smartevse_key in ("smartevse_1", "smartevse_2"):
            changes_key = f"{smartevse_key}_recent_state_changes"
            if not isinstance(self._mutable.get(changes_key), list):
                self._mutable[changes_key] = []

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
        self._normalize_intelligent_settings()
        self._reset_charge_cycle()
        await self._async_save_state()
        intelligent = getattr(self, "_intelligent", None)
        if intelligent is not None:
            try:
                await intelligent.async_initialize(self.charging_costs())
            except Exception:  # noqa: BLE001 - runs on model defaults
                LOGGER.exception("Unable to initialise Intelligent charging")

    def _normalize_intelligent_settings(self) -> None:
        """Validate stored Intelligent settings (a bad value never breaks an entity)."""
        mutable = self._mutable
        for name in ("intelligent_enabled", "intelligent_top_up"):
            mutable[name] = bool(mutable.get(name))
        for key in INTELLIGENT_SETTINGS_KEYS:
            ready_key = INTELLIGENT_READY_BY_KEYS[key]
            minimum_key = INTELLIGENT_MINIMUM_KEYS[key]
            mutable[ready_key] = normalize_ready_by(mutable.get(ready_key))
            mutable[minimum_key] = normalize_minimum(mutable.get(minimum_key))
        price = mutable.get("intelligent_top_up_price")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
            # First initialisation: start from the existing acceptable price.
            try:
                price = float(mutable.get("acceptable_price", DEFAULT_ACCEPTABLE_PRICE))
            except (TypeError, ValueError):
                price = DEFAULT_ACCEPTABLE_PRICE
            mutable["intelligent_top_up_price"] = round(price, 4)
        for smartevse_key in ("smartevse_1", "smartevse_2"):
            for name, default in INTELLIGENT_OUTLET_DEFAULTS.items():
                mutable_key = f"{smartevse_key}_{name}"
                value = mutable.get(mutable_key, default)
                if isinstance(default, bool):
                    mutable[mutable_key] = bool(value)
                elif isinstance(default, int):
                    mutable[mutable_key] = value if type(value) is int and 0 <= value <= 100 else default
                elif value is not None and self._parse_datetime(value if isinstance(value, str) else None) is None:
                    mutable[mutable_key] = None if name != "intelligent_deadline_vehicle" else (
                        value if value in INTELLIGENT_SETTINGS_KEYS else None)
        for vehicle_key in ("vehicle_1", "vehicle_2"):
            soc = mutable.get(f"{vehicle_key}_last_soc")
            if isinstance(soc, bool) or not isinstance(soc, (int, float)) or not 0 <= soc <= 100:
                mutable[f"{vehicle_key}_last_soc"] = None
                mutable[f"{vehicle_key}_last_soc_at"] = None
        for smartevse_key in ("smartevse_1", "smartevse_2"):
            # A version without Intelligent charging (a rollback) keeps these keys but
            # not up to date: a newer plug event, or an outlet last seen unplugged,
            # means the stored session (deadline, overrun, ...) belongs to an old car.
            plugged_at = self._parse_datetime(mutable.get(f"{smartevse_key}_intelligent_plugged_at"))
            last_plug = self._parse_datetime(mutable.get(f"{smartevse_key}_last_plug_connected"))
            if plugged_at is not None and (
                    mutable.get(f"{smartevse_key}_last_connected") is False
                    or (last_plug is not None and last_plug > plugged_at)):
                self._clear_intelligent_outlet_state(smartevse_key)

    async def async_shutdown(self) -> None:
        """Flush the Intelligent model and stop independent notification delivery."""
        intelligent = getattr(self, "_intelligent", None)
        if intelligent is not None:
            try:
                await intelligent.async_flush()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unable to save the Intelligent charging model on shutdown")
        await self._schedule_reminders.async_shutdown()

    def intelligent_source_entity_ids(self) -> list[str]:
        """Price-list and battery entities whose changes should re-run the controller."""
        intelligent = getattr(self, "_intelligent", None)
        if intelligent is None:
            return []
        try:
            return [entity_id for entity_id in intelligent.source_entity_ids() if isinstance(entity_id, str)]
        except Exception:  # noqa: BLE001
            LOGGER.debug("Unable to derive Intelligent source entities", exc_info=True)
            return []

    async def async_set_force_charge(self, value: bool) -> None:
        """Enable or disable the force-charge plan."""
        self._mutable["force_charge"] = value
        if value:
            self._clear_session_tracking()
            self._reset_charge_cycle(preserve_previous_active=True)
        else:
            # The force switch is the plan activation flag. Its option switches
            # must not keep a force plan running after the user turns it off.
            self._mutable["force_price"] = False
            self._mutable["force_timer"] = False
            self._mutable["timer_until"] = None
            self._reset_charge_cycle(preserve_previous_active=True)
        await self._async_save_state()

    async def async_set_schedule_price(self, value: bool) -> None:
        """Enable or disable the schedule's acceptable-price option."""
        self._mutable["schedule_price"] = value
        self._reset_charge_cycle(preserve_previous_active=True)
        await self._async_save_state()

    async def async_set_force_price(self, value: bool) -> None:
        """Enable or disable the acceptable-price option."""
        self._mutable["force_price"] = value
        if value:
            self._clear_session_tracking()
            self._reset_charge_cycle(preserve_previous_active=True)
        await self._async_save_state()

    async def async_set_force_timer(self, value: bool) -> None:
        """Enable or disable the force-charge timer option."""
        self._mutable["force_timer"] = value
        if value:
            duration = int(self._mutable["force_charge_duration_minutes"])
            self._mutable["timer_until"] = (dt_util.utcnow() + timedelta(minutes=duration)).isoformat()
            self._clear_session_tracking()
            self._reset_charge_cycle(preserve_previous_active=True)
        else:
            self._mutable["timer_until"] = None
            self._reset_charge_cycle(preserve_previous_active=True)
        await self._async_save_state()

    async def async_set_schedule_enabled(self, value: bool) -> None:
        """Enable or disable the schedule gate."""
        self._mutable["schedule_enabled"] = value
        if not value:
            self._reset_charge_cycle(preserve_previous_active=True)
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
        self._reset_charge_cycle(preserve_previous_active=True)
        # A manual policy change should take effect immediately, even mid-cycle,
        # switching to the newly preferred SmartEVSE (see _resolve_active_smartevse).
        self._mutable["policy_change_pending"] = True
        await self._async_save_state()

    async def async_set_duty_cycle_minutes(self, value: float) -> None:
        """Update the duty cycle interval."""
        self._mutable["duty_cycle_minutes"] = max(1, int(round(value)))
        self._reset_charge_cycle(preserve_previous_active=True)
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
        self._reset_charge_cycle(preserve_previous_active=True)
        self._clear_session_tracking()
        for smartevse_key in ("smartevse_1", "smartevse_2"):
            self._clear_intelligent_outlet_state(smartevse_key)
        await self._async_save_state()

    async def async_set_intelligent_enabled(self, value: bool) -> None:
        """Enable or disable Intelligent charging (persists across restart and unplug)."""
        value = bool(value)
        changed = bool(self._mutable.get("intelligent_enabled")) != value
        self._mutable["intelligent_enabled"] = value
        if changed:
            # A fresh start: deadlines, overrun, completion, not-accepting and
            # commitment are recomputed; the plug sessions themselves are kept.
            # The per-vehicle last SoC is not maintained while disabled.
            for smartevse_key in ("smartevse_1", "smartevse_2"):
                self._clear_intelligent_outlet_state(smartevse_key, keep_session=True)
                self._mutable[f"{smartevse_key}_intelligent_deadline"] = None
                self._mutable[f"{smartevse_key}_intelligent_deadline_vehicle"] = None
            for vehicle_key in ("vehicle_1", "vehicle_2"):
                self._mutable[f"{vehicle_key}_last_soc"] = None
                self._mutable[f"{vehicle_key}_last_soc_at"] = None
            self._reset_charge_cycle(preserve_previous_active=True)
        await self._async_save_state()

    async def async_set_intelligent_top_up(self, value: bool) -> None:
        """Enable or disable Intelligent top-up above the minimum when prices are low."""
        self._mutable["intelligent_top_up"] = bool(value)
        await self._async_save_state()

    async def async_set_intelligent_top_up_price(self, value: float) -> None:
        """Update the top-up price threshold (currency/kWh)."""
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Top-up price must be a finite number")
        self._mutable["intelligent_top_up_price"] = round(number, 4)
        await self._async_save_state()

    async def async_set_vehicle_ready_by(self, key: str, value: Any) -> None:
        """Set a ready-by time ("HH:MM" or datetime.time) for vehicle_1, vehicle_2 or other."""
        if key not in INTELLIGENT_SETTINGS_KEYS:
            raise ValueError(f"Unknown Intelligent settings key: {key}")
        parsed = parse_ready_by(value)
        if parsed is None:
            raise ValueError(f"Invalid ready-by time: {value!r}")
        self._mutable[INTELLIGENT_READY_BY_KEYS[key]] = parsed.strftime("%H:%M")
        now = dt_util.utcnow()
        for smartevse_key in self._intelligent_outlets_using(key):
            self._clear_intelligent_settings_state(smartevse_key)
            deadline = self._parse_datetime(self._mutable.get(f"{smartevse_key}_intelligent_deadline"))
            plugged_at = self._parse_datetime(self._mutable.get(f"{smartevse_key}_intelligent_plugged_at"))
            new_deadline = session_deadline(parsed, now, plugged_at, default_time_zone())
            if (deadline is not None and now < deadline <= now + INTELLIGENT_READY_BY_EDIT_KEEP
                    and new_deadline > deadline):
                # Leaving soon: a later time (or one that just passed and would roll to
                # tomorrow) keeps tonight's deadline and applies from the next roll. An
                # earlier time that is still ahead applies now: the car must be ready then.
                continue
            self._mutable[f"{smartevse_key}_intelligent_deadline"] = new_deadline.isoformat()
            self._mutable[f"{smartevse_key}_intelligent_deadline_vehicle"] = key
        await self._async_save_state()

    async def async_set_vehicle_minimum_charge(self, key: str, value: float) -> None:
        """Set the minimum charge (%, 10–100) for vehicle_1, vehicle_2 or other."""
        if key not in INTELLIGENT_SETTINGS_KEYS:
            raise ValueError(f"Unknown Intelligent settings key: {key}")
        self._mutable[INTELLIGENT_MINIMUM_KEYS[key]] = normalize_minimum(value)
        for smartevse_key in self._intelligent_outlets_using(key):
            self._clear_intelligent_settings_state(smartevse_key)
        await self._async_save_state()

    def _intelligent_settings_key(self, smartevse_key: str) -> str:
        """Settings used by an outlet: its mapped vehicle, else the generic "other" (R10)."""
        return self._mapped_vehicle_key(smartevse_key) or "other"

    def _intelligent_outlets_using(self, key: str) -> list[str]:
        """Connected outlets whose plan uses the given settings key."""
        return [
            smartevse_key
            for smartevse_key in ("smartevse_1", "smartevse_2")
            if self._mutable.get(f"{smartevse_key}_last_connected")
            and self._intelligent_settings_key(smartevse_key) == key
        ]

    def _clear_intelligent_settings_state(self, smartevse_key: str) -> None:
        """A ready-by/minimum edit re-plans the outlet but keeps its plug session,
        deadline (§6.2 edit rule) and overrun: overrun clears only when the minimum
        is reached, on unplug or when Intelligent is switched off and on (§6.2), so a
        car still below its (new) minimum after its ready-by keeps catching up."""
        overrun = bool(self._mutable.get(f"{smartevse_key}_intelligent_overrun"))
        self._clear_intelligent_outlet_state(smartevse_key, keep_session=True)
        self._mutable[f"{smartevse_key}_intelligent_overrun"] = overrun

    def _clear_intelligent_outlet_state(self, smartevse_key: str, *, keep_session: bool = False) -> None:
        """Clear one outlet's Intelligent state (§5).

        Called only on an observed unplug, async_reset_sessions, the Intelligent
        switch and (keep_session=True) a ready-by/minimum edit for the outlet
        using that setting; never from _clear_session_tracking/_clear_force_modes.
        """
        if not keep_session:
            vehicle_key = self._mutable.get(f"{smartevse_key}_intelligent_deadline_vehicle")
            if vehicle_key in {"vehicle_1", "vehicle_2"}:
                self._mutable[f"{vehicle_key}_last_soc"] = None
                self._mutable[f"{vehicle_key}_last_soc_at"] = None
            self._mutable[f"{smartevse_key}_last_pause_at"] = None
        for name, default in INTELLIGENT_OUTLET_DEFAULTS.items():
            if keep_session and name in INTELLIGENT_SESSION_KEYS:
                continue
            self._mutable[f"{smartevse_key}_{name}"] = default

    async def async_run_cycle(self, *, reason: str) -> dict[str, Any]:
        """Run a complete controller cycle and return computed state."""
        now = dt_util.utcnow()
        self._sanitize_mutual_exclusion()

        smartevse_1, smartevse_2 = await asyncio.gather(
            self._async_fetch_status("smartevse_1", self._entry_data[CONF_SMARTEVSE_1_BASE_URL]),
            self._async_fetch_status("smartevse_2", self._entry_data[CONF_SMARTEVSE_2_BASE_URL]),
        )
        previous_any_connected = any(
            bool(self._mutable.get(f"{smartevse_key}_last_connected"))
            for smartevse_key in ("smartevse_1", "smartevse_2")
        )
        self._track_state_transitions(smartevse_1, now=now)
        self._track_state_transitions(smartevse_2, now=now)
        self._track_pause_state(smartevse_1, now=now)
        self._track_pause_state(smartevse_2, now=now)
        self._track_unavailable(smartevse_1, now=now)
        self._track_unavailable(smartevse_2, now=now)
        self._update_connected_vehicle_mapping(
            now=now,
            smartevse_1=smartevse_1,
            smartevse_2=smartevse_2,
        )
        ev_meter_power = self._state_float_or_none(self._entry_data.get(CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY))
        self._update_session_tracking(smartevse_1, now=now, ev_meter_power=ev_meter_power)
        self._update_session_tracking(smartevse_2, now=now, ev_meter_power=ev_meter_power)
        self._track_intelligent_plug_sessions(now=now, smartevse_1=smartevse_1, smartevse_2=smartevse_2)

        await self._observe_charging_costs(smartevse_1, smartevse_2, now=dt_util.utcnow())
        charging_costs = self.charging_costs()

        if self._mutable["force_timer"]:
            timer_until = self._timer_until()
            if timer_until is None or now >= timer_until:
                self._mutable["force_charge"] = False
                self._mutable["force_timer"] = False
                # A timer + price plan is one combined force-charge request.
                # When its timer expires, the accompanying price gate must not
                # continue charging indefinitely as a standalone price plan.
                self._mutable["force_price"] = False
                self._mutable["timer_until"] = None
                self._reset_charge_cycle()

        if (
            smartevse_1.available
            and smartevse_2.available
            and not smartevse_1.connected
            and not smartevse_2.connected
            and previous_any_connected
        ):
            self._clear_force_modes()
            self._mutable["charge_policy"] = self._configured_charge_policy()
            self._reset_charge_cycle()

        price_value = self._state_float_or_none(self._entry_data.get(CONF_PRICE_SENSOR_ENTITY))
        schedule_window_active = self._state_on(self._entry_data.get(CONF_SCHEDULE_ENTITY))
        statuses = {
            "smartevse_1": smartevse_1,
            "smartevse_2": smartevse_2,
        }
        # Precedence without the plan tells whether Intelligent governs this
        # cycle (Force and the legacy direct modes come first, §6.1).
        _pre_allowed, pre_state, pre_reason = self._determine_charge_allowed(
            price_value=price_value,
            schedule_window_active=schedule_window_active,
        )
        intelligent_governing = pre_state == ControllerState.INTELLIGENT
        intelligent_decision = await self._async_evaluate_intelligent(
            now=now,
            statuses=statuses,
            pre_state=pre_state,
            pre_reason=pre_reason,
            ledger=charging_costs,
        )
        charge_allowed, controller_state, charge_reason = self._determine_charge_allowed(
            price_value=price_value,
            schedule_window_active=schedule_window_active,
            intelligent=intelligent_decision,
        )

        # Intelligent charging covers the schedule for notifications (§6.1).
        await self._handle_schedule_notification(
            schedule_window_active=schedule_window_active,
            schedule_enabled=bool(self._mutable["schedule_enabled"] or self._mutable.get("intelligent_enabled")),
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

        oscillation_reason = self._detect_active_smartevse_oscillation(
            now=now,
            charge_allowed=charge_allowed,
            statuses=statuses,
        )
        if oscillation_reason is not None:
            controller_error = oscillation_reason
            self._clear_session_tracking()
            self._reset_charge_cycle()

        previous_charge_allowed = bool(self._mutable.get("last_charge_allowed"))
        if charge_allowed and not previous_charge_allowed:
            # Intelligent starts a block after every gap; wiping completion there
            # would re-plan full cars at each block start (§6.3).
            if controller_state != ControllerState.INTELLIGENT:
                self._clear_session_tracking()
            self._reset_charge_cycle()
        self._mutable["last_charge_allowed"] = charge_allowed

        previous_active_smartevse = str(
            self._mutable.get("pending_previous_active_smartevse")
            or self._mutable.get("active_smartevse")
            or ""
        )
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
            intelligent_kwargs: dict[str, Any] = {}
            if controller_state == ControllerState.INTELLIGENT and intelligent_decision is not None:
                intelligent_kwargs = {
                    "intelligent_outlet": intelligent_decision.outlet,
                    "intelligent_block_start": intelligent_decision.block_start,
                }
            active_smartevse, active_smartevse_since, duty_cycle_remaining, blocked_reason = self._resolve_active_smartevse(
                now=now,
                smartevse_1=smartevse_1,
                smartevse_2=smartevse_2,
                **intelligent_kwargs,
            )
            if active_smartevse == "none":
                # Every "no outlet" result clears the active outlet, so the
                # controller's own Off write is never later read as a finished
                # session of a still-selected charger.
                self._activate_smartevse(None, None)
                controller_state = ControllerState.BLOCKED
                charge_reason = blocked_reason
                controller_error = self._controller_error_for_reason(blocked_reason)
                if intelligent_kwargs:
                    charge_allowed = False

        self._mutable["pending_previous_active_smartevse"] = None

        await self._apply_modes(
            smartevse_1=smartevse_1,
            smartevse_2=smartevse_2,
            active_smartevse=active_smartevse,
            charge_allowed=charge_allowed,
            now=now,
            ev_meter_power=ev_meter_power,
        )
        if controller_error is None and intelligent_governing and intelligent_decision is not None:
            # Warning level only: prices missing, planner fallback, model, car timer.
            controller_error = intelligent_decision.controller_error
        if controller_error is None:
            controller_error = self._vehicle_mapping_warning(statuses)
        if controller_error is None:
            controller_error = self._vehicle_data_warning()
        self._mutable["last_controller_state"] = controller_state.value
        self._mutable["last_charge_reason"] = charge_reason
        intelligent_payload, intelligent_flat = self._intelligent_payload(
            now=now,
            governing=intelligent_governing,
            charge_allowed=charge_allowed,
            controller_state=controller_state,
            charge_reason=charge_reason,
            active_smartevse=active_smartevse,
        )
        await self._maybe_push_wled(smartevse_1=smartevse_1, smartevse_2=smartevse_2)
        await self._async_save_state()

        timer_until = self._timer_until()
        timer_remaining = 0
        if timer_until is not None:
            timer_remaining = max(int((timer_until - now).total_seconds()), 0)

        smartevse_1_name = smartevse_name("smartevse_1")
        smartevse_2_name = smartevse_name("smartevse_2")
        smartevse_1_connected_ev = self._connected_ev_label("smartevse_1")
        smartevse_2_connected_ev = self._connected_ev_label("smartevse_2")
        smartevse_1_battery = self._connected_ev_battery("smartevse_1")
        smartevse_2_battery = self._connected_ev_battery("smartevse_2")
        smartevse_1_vehicle_charging_state = self._connected_ev_charging_state_display("smartevse_1")
        smartevse_2_vehicle_charging_state = self._connected_ev_charging_state_display("smartevse_2")

        return {
            "charging_costs": charging_costs,
            "force_charge": bool(self._mutable["force_charge"]),
            "force_price": bool(self._mutable["force_price"]),
            "force_timer": bool(self._mutable["force_timer"]),
            "schedule_enabled": bool(self._mutable["schedule_enabled"]),
            "schedule_price": bool(self._mutable["schedule_price"]),
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
            ATTR_ACTIVE_SMARTEVSE: active_smartevse_label(active_smartevse),
            "active_smartevse_raw": active_smartevse,
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
            "wled_visuals": build_flow_card_visuals(),
            "smartevse_1_name": smartevse_1_name,
            "smartevse_1_connected_ev": smartevse_1_connected_ev,
            "smartevse_1_connected_ev_raw": self._mapped_vehicle_key("smartevse_1") or "unknown",
            "smartevse_1_battery": smartevse_1_battery,
            "smartevse_1_vehicle_charging_state": smartevse_1_vehicle_charging_state,
            "smartevse_1_available": smartevse_1.available,
            "smartevse_1_state": smartevse_1.state,
            "smartevse_1_plug_state": smartevse_1.plug_state,
            "smartevse_1_mode": smartevse_1.mode,
            "smartevse_1_charge_current": round(smartevse_1.charge_current, 1),
            "smartevse_1_max_current": round(smartevse_1.max_current, 1),
            "smartevse_1_override_current": round(smartevse_1.override_current, 1),
            "smartevse_1_error": smartevse_1.error,
            "smartevse_1_session_complete": bool(self._mutable["smartevse_1_session_complete"]),
            "smartevse_2_name": smartevse_2_name,
            "smartevse_2_connected_ev": smartevse_2_connected_ev,
            "smartevse_2_connected_ev_raw": self._mapped_vehicle_key("smartevse_2") or "unknown",
            "smartevse_2_battery": smartevse_2_battery,
            "smartevse_2_vehicle_charging_state": smartevse_2_vehicle_charging_state,
            "smartevse_2_available": smartevse_2.available,
            "smartevse_2_state": smartevse_2.state,
            "smartevse_2_plug_state": smartevse_2.plug_state,
            "smartevse_2_mode": smartevse_2.mode,
            "smartevse_2_charge_current": round(smartevse_2.charge_current, 1),
            "smartevse_2_max_current": round(smartevse_2.max_current, 1),
            "smartevse_2_override_current": round(smartevse_2.override_current, 1),
            "smartevse_2_error": smartevse_2.error,
            "smartevse_2_session_complete": bool(self._mutable["smartevse_2_session_complete"]),
            # Intelligent charging settings and results (entity data keys, §4/§10).
            "intelligent_enabled": bool(self._mutable.get("intelligent_enabled")),
            "intelligent_top_up": bool(self._mutable.get("intelligent_top_up")),
            "intelligent_top_up_price": self._intelligent_top_up_price_value(),
            "vehicle_1_ready_by": normalize_ready_by(self._mutable.get("vehicle_1_ready_by")),
            "vehicle_2_ready_by": normalize_ready_by(self._mutable.get("vehicle_2_ready_by")),
            "other_vehicle_ready_by": normalize_ready_by(self._mutable.get("other_vehicle_ready_by")),
            "vehicle_1_minimum_charge": normalize_minimum(self._mutable.get("vehicle_1_minimum_charge")),
            "vehicle_2_minimum_charge": normalize_minimum(self._mutable.get("vehicle_2_minimum_charge")),
            "other_vehicle_minimum_charge": normalize_minimum(self._mutable.get("other_vehicle_minimum_charge")),
            "intelligent": intelligent_payload,
            **intelligent_flat,
        }

    def _sanitize_mutual_exclusion(self) -> None:
        """Normalize impossible timer state loaded from older storage."""
        if not self._mutable.get("force_timer"):
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

    def _configured_vehicle_name(self, vehicle_key: str) -> str:
        """Return the configured name for one known vehicle."""
        values = {
            **self._entry_data,
            **self._options,
            CONF_VEHICLE_1_NAME: self._options.get(
                CONF_VEHICLE_1_NAME,
                self._entry_data.get(CONF_VEHICLE_1_NAME, DEFAULT_VEHICLE_1_NAME),
            ),
            CONF_VEHICLE_2_NAME: self._options.get(
                CONF_VEHICLE_2_NAME,
                self._entry_data.get(CONF_VEHICLE_2_NAME, DEFAULT_VEHICLE_2_NAME),
            ),
        }
        return configured_vehicle_name(values, vehicle_key)

    def _configured_battery_entity(self, vehicle_key: str) -> str | None:
        """Return the configured EV battery entity for one known vehicle."""
        config_key = (
            CONF_VEHICLE_1_BATTERY_ENTITY
            if vehicle_key == "vehicle_1"
            else CONF_VEHICLE_2_BATTERY_ENTITY
        )
        value = self._options.get(config_key, self._entry_data.get(config_key))
        return str(value).strip() or None

    def _configured_connection_status_entity(self, vehicle_key: str) -> str | None:
        """Return the configured EV connection-status entity for one known vehicle."""
        config_key = (
            CONF_VEHICLE_1_CONNECTION_STATUS_ENTITY
            if vehicle_key == "vehicle_1"
            else CONF_VEHICLE_2_CONNECTION_STATUS_ENTITY
        )
        value = self._options.get(config_key, self._entry_data.get(config_key))
        return str(value).strip() or None

    def _derived_vehicle_charging_status_entity(self, vehicle_key: str) -> str | None:
        """Derive an EV charging-status entity from the configured connection-status sensor."""
        connection_entity = self._configured_connection_status_entity(vehicle_key)
        if not connection_entity:
            return None
        if "charging_connection_status" not in connection_entity:
            return None
        return connection_entity.replace("charging_connection_status", "charging_status")

    def _vehicle_connection_changed_to_connected(
        self,
        previous: bool | None,
        current: bool | None,
    ) -> bool:
        """Return whether a vehicle connection sensor just changed to connected."""
        return previous is not None and previous is False and current is True

    def _smartevse_connection_changed_to_connected(
        self,
        previous: bool | None,
        current: bool,
    ) -> bool:
        """Return whether a SmartEVSE plug just changed to connected."""
        return previous is not None and previous is False and current is True

    def _mapped_vehicle_key(self, smartevse_key: str) -> str | None:
        """Return the mapped known-vehicle key for one SmartEVSE."""
        value = str(self._mutable.get(f"{smartevse_key}_connected_ev") or "").strip()
        if value in {"vehicle_1", "vehicle_2"}:
            return value
        return None

    def _connected_ev_label(self, smartevse_key: str) -> str:
        """Return the mapped EV label for one SmartEVSE."""
        vehicle_key = self._mapped_vehicle_key(smartevse_key)
        if not vehicle_key:
            return "unknown"
        return self._configured_vehicle_name(vehicle_key)

    def _connected_ev_battery(self, smartevse_key: str) -> str | None:
        """Return the mapped EV battery display for one SmartEVSE."""
        vehicle_key = self._mapped_vehicle_key(smartevse_key)
        if not vehicle_key:
            return None
        return self._state_display(self._configured_battery_entity(vehicle_key))

    def _connected_ev_charging_state_display(self, smartevse_key: str) -> str | None:
        """Return the mapped EV charging state for one SmartEVSE."""
        vehicle_key = self._mapped_vehicle_key(smartevse_key)
        if not vehicle_key:
            return None
        state = self._vehicle_charging_state(vehicle_key)
        return state or None

    def _vehicle_connection_state(
        self,
        vehicle_key: str,
        *,
        now: datetime | None = None,
        require_fresh: bool = False,
    ) -> bool | None:
        """Return whether the configured vehicle reports a connected cable."""
        entity_id = self._configured_connection_status_entity(vehicle_key)
        if require_fresh and not self._entity_state_fresh(entity_id, now=now):
            return None
        state = self._state_str(entity_id).strip().lower()
        if not state:
            return None
        if state in {"connected", "on", "true", "plugged_in", "plugged in"}:
            return True
        if state in {"disconnected", "off", "false", "unplugged", "unplugged_from_vehicle"}:
            return False
        return None

    def _vehicle_charging_state(
        self,
        vehicle_key: str,
        *,
        now: datetime | None = None,
        require_fresh: bool = False,
    ) -> str:
        """Return the normalized charging state for one configured vehicle."""
        entity_id = self._derived_vehicle_charging_status_entity(vehicle_key)
        if require_fresh and not self._entity_state_fresh(entity_id, now=now):
            return ""
        return self._state_str(entity_id).strip().lower()

    def _vehicle_reports_charging(self, vehicle_key: str, *, now: datetime | None = None) -> bool:
        """Return whether the configured vehicle reports active charging."""
        return self._vehicle_charging_state(vehicle_key, now=now, require_fresh=True) == "charging"

    def _plug_connected_recently(self, smartevse_key: str, *, now: datetime) -> bool:
        """Return whether a SmartEVSE plug-connect event is recent enough for vehicle correlation."""
        connected_at = self._parse_datetime(self._mutable.get(f"{smartevse_key}_last_plug_connected"))
        if connected_at is None:
            return False
        return now - connected_at <= MAPPING_CONNECT_WINDOW

    def _recent_state_change_key(self, smartevse_key: str) -> str:
        """Return the mutable key used for recent state-change timestamps."""
        return f"{smartevse_key}_recent_state_changes"

    def _clear_state_change_tracking(self, smartevse_key: str) -> None:
        """Clear transient state-change tracking for one SmartEVSE."""
        self._mutable[f"{smartevse_key}_last_observed_state"] = None
        self._mutable[self._recent_state_change_key(smartevse_key)] = []

    def _trim_recent_state_changes(self, smartevse_key: str, *, now: datetime) -> list[str]:
        """Trim state-change timestamps to the rolling oscillation window."""
        changes_key = self._recent_state_change_key(smartevse_key)
        window_started_at = now - timedelta(minutes=3)
        timestamps = [
            timestamp
            for timestamp in self._mutable.get(changes_key, [])
            if (parsed := self._parse_datetime(timestamp)) is not None and parsed >= window_started_at
        ]
        self._mutable[changes_key] = timestamps
        return timestamps

    def _track_state_transitions(self, status: SmartEVSEStatus, *, now: datetime) -> None:
        """Track recent SmartEVSE charging-state transitions for oscillation detection."""
        if not status.available or not status.connected:
            self._clear_state_change_tracking(status.key)
            return

        relevant_states = {"Connected to EV", "Charging", "Charging Stopped", "Stop Charging", "Ready to Charge"}
        last_state_key = f"{status.key}_last_observed_state"
        previous_state = str(self._mutable.get(last_state_key) or "")
        current_state = status.state
        timestamps = self._trim_recent_state_changes(status.key, now=now)

        if previous_state and previous_state != current_state:
            if previous_state in relevant_states and current_state in relevant_states:
                timestamps.append(now.isoformat())
                self._mutable[self._recent_state_change_key(status.key)] = timestamps

        self._mutable[last_state_key] = current_state

    def _detect_active_smartevse_oscillation(
        self,
        *,
        now: datetime,
        charge_allowed: bool,
        statuses: dict[str, SmartEVSEStatus],
    ) -> str | None:
        """Return an oscillation reason when the active SmartEVSE is flapping excessively."""
        if not charge_allowed or self._mutable.get("handoff_target"):
            return None

        active_smartevse = str(self._mutable.get("active_smartevse") or "")
        status = statuses.get(active_smartevse)
        if status is None or not status.available or not status.connected:
            return None

        timestamps = self._trim_recent_state_changes(active_smartevse, now=now)
        if len(timestamps) < 8:
            return None

        self._clear_state_change_tracking(active_smartevse)
        return f"{active_smartevse}_state_oscillation"

    def _vehicle_data_warning(self) -> str | None:
        """Return a non-blocking warning when mapped vehicle telemetry is unavailable."""
        now = dt_util.utcnow()
        for smartevse_key in ("smartevse_1", "smartevse_2"):
            vehicle_key = self._mapped_vehicle_key(smartevse_key)
            if vehicle_key is None:
                continue

            connection_entity = self._configured_connection_status_entity(vehicle_key)
            charging_entity = self._derived_vehicle_charging_status_entity(vehicle_key)
            connection_state = self._state_str(connection_entity).strip().lower()
            charging_state = self._state_str(charging_entity).strip().lower()

            if connection_state in {"unavailable", "unknown"} or charging_state in {"unavailable", "unknown"}:
                return f"{smartevse_key}_vehicle_data_unavailable"
            if (
                not self._entity_state_fresh(connection_entity, now=now)
                or not self._entity_state_fresh(charging_entity, now=now)
            ):
                return f"{smartevse_key}_vehicle_data_stale"
        return None

    def _set_connected_vehicle(self, smartevse_key: str, vehicle_key: str | None) -> bool:
        """Assign one known vehicle to one SmartEVSE."""
        normalized = vehicle_key if vehicle_key in {"vehicle_1", "vehicle_2"} else "unknown"
        changed = self._mutable.get(f"{smartevse_key}_connected_ev") != normalized
        if normalized != "unknown":
            other_smartevse = self._other_smartevse(smartevse_key)
            if self._mutable.get(f"{other_smartevse}_connected_ev") == normalized:
                changed = True
                self._mutable[f"{other_smartevse}_connected_ev"] = "unknown"
        self._mutable[f"{smartevse_key}_connected_ev"] = normalized
        return changed

    def _update_connected_vehicle_mapping(
        self,
        *,
        now: datetime,
        smartevse_1: SmartEVSEStatus,
        smartevse_2: SmartEVSEStatus,
    ) -> None:
        """Map known EV identities to SmartEVSE connectors by correlating connect events."""
        statuses = {
            "smartevse_1": smartevse_1,
            "smartevse_2": smartevse_2,
        }
        self._track_charging_runs(statuses, now=now)
        vehicle_connected = {
            "vehicle_1": self._vehicle_connection_state("vehicle_1", now=now, require_fresh=True),
            "vehicle_2": self._vehicle_connection_state("vehicle_2", now=now, require_fresh=True),
        }
        vehicle_last_keys = {
            "vehicle_1": "known_vehicle_1_last_connected",
            "vehicle_2": "known_vehicle_2_last_connected",
        }
        connected_smartevse = [
            smartevse_key
            for smartevse_key, status in statuses.items()
            if status.available and status.connected
        ]

        for smartevse_key, status in statuses.items():
            if not status.available:
                # An API blip is not an unplug: keep last_connected, the plug
                # time and the mapping untouched until the charger answers again.
                # An unplug is only "available and not connected", and a plug
                # event only an observed available disconnected→connected change.
                continue
            last_connected_key = f"{smartevse_key}_last_connected"
            previous_connected = self._mutable.get(last_connected_key)
            current_connected = bool(status.connected)

            if self._smartevse_connection_changed_to_connected(previous_connected, current_connected):
                self._mutable[f"{smartevse_key}_last_plug_connected"] = now.isoformat()
            elif not current_connected:
                self._mutable[f"{smartevse_key}_last_plug_connected"] = None
                self._set_connected_vehicle(smartevse_key, None)

            self._mutable[last_connected_key] = current_connected

        for vehicle_key, current_connected in vehicle_connected.items():
            previous_connected = self._mutable.get(vehicle_last_keys[vehicle_key])
            if current_connected is False:
                for smartevse_key in ("smartevse_1", "smartevse_2"):
                    if self._mapped_vehicle_key(smartevse_key) == vehicle_key:
                        self._set_connected_vehicle(smartevse_key, None)
            self._mutable[vehicle_last_keys[vehicle_key]] = current_connected

            if not self._vehicle_connection_changed_to_connected(previous_connected, current_connected):
                continue

            candidate_smartevse = [
                smartevse_key
                for smartevse_key, status in statuses.items()
                if status.available and status.connected and self._mapped_vehicle_key(smartevse_key) is None
            ]

            recent_candidates: list[tuple[datetime, str]] = []
            for smartevse_key in candidate_smartevse:
                connected_at = self._parse_datetime(self._mutable.get(f"{smartevse_key}_last_plug_connected"))
                if connected_at is not None and self._plug_connected_recently(smartevse_key, now=now):
                    recent_candidates.append((connected_at, smartevse_key))
            if recent_candidates:
                recent_candidates.sort(key=lambda item: item[0], reverse=True)
                newest_at, newest_smartevse = recent_candidates[0]
                if (
                    len(recent_candidates) == 1
                    or newest_at - recent_candidates[1][0] >= SIMULTANEOUS_PLUG_WINDOW
                ):
                    self._set_connected_vehicle(newest_smartevse, vehicle_key)
                    continue
                # Both unmapped outlets were plugged in less than 3 min apart and
                # the vehicle cloud lags by minutes, so "newest" is a guess that
                # can swap the cars for the whole night. Leave both unknown until
                # charging evidence (or a later unambiguous event) identifies them.
                continue

            if len(candidate_smartevse) == 1 and len(connected_smartevse) == 1:
                self._set_connected_vehicle(candidate_smartevse[0], vehicle_key)
                continue

        unmapped_connected_smartevse = [
            smartevse_key
            for smartevse_key in connected_smartevse
            if self._mapped_vehicle_key(smartevse_key) is None
        ]
        unmapped_connected_vehicles = [
            vehicle_key
            for vehicle_key, connected in vehicle_connected.items()
            if connected is True
            and vehicle_key
            not in {
                mapped
                for mapped in (
                    self._mapped_vehicle_key("smartevse_1"),
                    self._mapped_vehicle_key("smartevse_2"),
                )
                if mapped is not None
            }
        ]
        existing_mappings = [
            self._mapped_vehicle_key(smartevse_key)
            for smartevse_key in connected_smartevse
            if self._mapped_vehicle_key(smartevse_key) is not None
        ]
        if (
            len(unmapped_connected_smartevse) == 1
            and len(unmapped_connected_vehicles) == 1
            and (len(connected_smartevse) == 1 or bool(existing_mappings))
        ):
            self._set_connected_vehicle(unmapped_connected_smartevse[0], unmapped_connected_vehicles[0])

        self._correct_connected_vehicle_mapping_from_charging(
            now=now,
            statuses=statuses,
            vehicle_connected=vehicle_connected,
        )

    def _track_charging_runs(self, statuses: dict[str, SmartEVSEStatus], *, now: datetime) -> None:
        """Remember when each charger's current polled "Charging" run began and when
        it was last seen charging. An API blip changes neither; a Smart-mode pause
        ("No Power Available") does not end the run."""
        for smartevse_key, status in statuses.items():
            if not status.available:
                continue
            since_key = f"{smartevse_key}_charging_since"
            if status.connected and status.state == "Charging":
                if self._parse_datetime(self._mutable.get(since_key)) is None:
                    self._mutable[since_key] = now.isoformat()
                self._mutable[f"{smartevse_key}_last_charging_at"] = now.isoformat()
            elif not (status.connected and self._status_shows_smart_pause(status)):
                self._mutable[since_key] = None

    def _vehicle_charging_report_at(self, vehicle_key: str) -> datetime | None:
        """When the vehicle's charging-status sensor last changed state."""
        entity_id = self._derived_vehicle_charging_status_entity(vehicle_key)
        state = self.hass.states.get(entity_id) if entity_id else None
        changed = getattr(state, "last_changed", None) if state is not None else None
        return dt_util.as_utc(changed) if isinstance(changed, datetime) else None

    def _charging_report_is_evidence(self, vehicle_key: str, smartevse_key: str, now: datetime) -> bool:
        """A vehicle's "charging" report identifies the charging outlet only when it
        changed after that outlet's current run began and clearly after the other
        outlet last charged (or it still says "charging" well after that). The
        vehicle cloud reports minutes late and holds the state, so at a hand-off the
        car that was just switched Off is still the freshest "charging" car while
        the incoming charger already charges."""
        changed = self._vehicle_charging_report_at(vehicle_key)
        if changed is None:
            return False
        run_since = self._parse_datetime(self._mutable.get(f"{smartevse_key}_charging_since"))
        if run_since is None or changed < run_since - CHARGING_RUN_START_TOLERANCE:
            return False
        other_last = self._parse_datetime(
            self._mutable.get(f"{self._other_smartevse(smartevse_key)}_last_charging_at"))
        return (other_last is None or changed >= other_last + VEHICLE_CHARGING_REPORT_LAG
                or now >= other_last + VEHICLE_CHARGING_REPORT_SETTLE)

    def _correct_connected_vehicle_mapping_from_charging(
        self,
        *,
        now: datetime,
        statuses: dict[str, SmartEVSEStatus],
        vehicle_connected: dict[str, bool | None],
    ) -> None:
        """Correct vehicle mapping when one SmartEVSE and one known vehicle are charging."""
        charging_smartevse = [
            smartevse_key
            for smartevse_key, status in statuses.items()
            if status.available and status.connected and status.state == "Charging"
        ]
        if len(charging_smartevse) != 1:
            return
        smartevse_key = charging_smartevse[0]
        # A fresh "charging" report is the evidence; the connection sensor only
        # has to not contradict it. Volvo writes a state only on change, so its
        # "connected" is stale 10 min after plug-in — a stale connected (or an
        # unknown state) is accepted, a known "disconnected" is not. The report
        # must also postdate this charging run and the other outlet's last
        # charge (see _charging_report_is_evidence).
        # (vehicle_connected holds the fresh-only states and is kept in the
        # signature for callers; the last known state is read here instead.)
        charging_vehicles = [
            vehicle_key
            for vehicle_key in vehicle_connected
            if self._vehicle_connection_state(vehicle_key) is not False
            and self._vehicle_reports_charging(vehicle_key, now=now)
            and self._charging_report_is_evidence(vehicle_key, smartevse_key, now)
        ]
        if len(charging_vehicles) != 1:
            return
        vehicle_key = charging_vehicles[0]
        if self._mapped_vehicle_key(smartevse_key) == vehicle_key:
            return

        mapping_before = {key: self._mapped_vehicle_key(key) for key in statuses}
        self._set_connected_vehicle(smartevse_key, vehicle_key)
        self._map_remaining_vehicle_by_elimination(statuses)
        # Clear session tracking only where the outlet's vehicle actually
        # changed; an outlet whose car stayed the same keeps its completion.
        for key, before in mapping_before.items():
            if self._mapped_vehicle_key(key) != before:
                self._clear_smartevse_session_tracking(key)

    def _map_remaining_vehicle_by_elimination(self, statuses: dict[str, SmartEVSEStatus]) -> None:
        """After a charging-evidence correction, map the one remaining car to the one remaining outlet."""
        unmapped_connected_smartevse = [
            smartevse_key
            for smartevse_key, status in statuses.items()
            if status.available and status.connected and self._mapped_vehicle_key(smartevse_key) is None
        ]
        mapped_vehicles = {
            mapped
            for mapped in (self._mapped_vehicle_key(smartevse_key) for smartevse_key in statuses)
            if mapped is not None
        }
        # The last known connection state (fresh or not) must say connected.
        remaining_connected_vehicles = [
            vehicle_key
            for vehicle_key in ("vehicle_1", "vehicle_2")
            if vehicle_key not in mapped_vehicles and self._vehicle_connection_state(vehicle_key) is True
        ]
        if len(unmapped_connected_smartevse) == 1 and len(remaining_connected_vehicles) == 1:
            self._set_connected_vehicle(unmapped_connected_smartevse[0], remaining_connected_vehicles[0])

    def _vehicle_mapping_warning(self, statuses: dict[str, SmartEVSEStatus]) -> str | None:
        """Return a warning when multiple connected vehicles cannot be mapped safely."""
        connected_smartevse = [
            smartevse_key
            for smartevse_key, status in statuses.items()
            if status.available and status.connected
        ]
        connected_vehicle_count = sum(
            1
            for vehicle_key in ("vehicle_1", "vehicle_2")
            if self._vehicle_connection_state(vehicle_key, require_fresh=True) is True
        )
        if len(connected_smartevse) < 2 or connected_vehicle_count < 2:
            return None
        if all(self._mapped_vehicle_key(smartevse_key) is not None for smartevse_key in connected_smartevse):
            return None
        return "vehicle_mapping_ambiguous"

    # -- Intelligent charging (§5, §6) ----------------------------------------------

    def _log_intelligent_exception(self, kind: str, message: str, now: datetime) -> None:
        """Log an Intelligent failure at most every 10 min per kind (debug otherwise)."""
        log_times = getattr(self, "_intelligent_log_times", None)
        if log_times is None:
            log_times = self._intelligent_log_times = {}
        last = log_times.get(kind)
        if last is None or now - last >= INTELLIGENT_LOG_INTERVAL or now < last:
            log_times[kind] = now
            LOGGER.exception(message)
        else:
            LOGGER.debug(message, exc_info=True)

    def _outlet_effectively_connected(self, smartevse_key: str, status: SmartEVSEStatus) -> bool:
        """Connected as polled; while the API is unavailable the last poll is kept (§3.2)."""
        if status.available:
            return bool(status.connected)
        return bool(self._mutable.get(f"{smartevse_key}_last_connected"))

    def _track_intelligent_plug_sessions(
        self,
        *,
        now: datetime,
        smartevse_1: SmartEVSEStatus,
        smartevse_2: SmartEVSEStatus,
    ) -> None:
        """Remember each plug session's start in every mode (observed plug-connect or
        the first connected observation); cleared only on an observed unplug."""
        for status in (smartevse_1, smartevse_2):
            if not status.available or not status.connected:
                continue
            key = f"{status.key}_intelligent_plugged_at"
            if self._parse_datetime(self._mutable.get(key)) is None:
                observed = self._parse_datetime(self._mutable.get(f"{status.key}_last_plug_connected"))
                self._mutable[key] = (observed if observed is not None and observed <= now else now).isoformat()

    def _intelligent_settings(self) -> dict[str, dict[str, Any]]:
        return {
            key: {
                "ready_by": normalize_ready_by(self._mutable.get(INTELLIGENT_READY_BY_KEYS[key])),
                "minimum": normalize_minimum(self._mutable.get(INTELLIGENT_MINIMUM_KEYS[key])),
            }
            for key in INTELLIGENT_SETTINGS_KEYS
        }

    def _intelligent_last_socs(self) -> dict[str, tuple[float | None, datetime | None]]:
        result = {}
        for vehicle_key in ("vehicle_1", "vehicle_2"):
            soc = self._mutable.get(f"{vehicle_key}_last_soc")
            soc = float(soc) if isinstance(soc, (int, float)) and not isinstance(soc, bool) else None
            result[vehicle_key] = (soc, self._parse_datetime(self._mutable.get(f"{vehicle_key}_last_soc_at")))
        return result

    def _intelligent_top_up_price_value(self) -> float | None:
        price = self._mutable.get("intelligent_top_up_price")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
            return None
        return float(price)

    def _intelligent_not_accepting_active(self, smartevse_key: str, now: datetime) -> bool:
        if int(self._mutable.get(f"{smartevse_key}_intelligent_probes") or 0) >= INTELLIGENT_MAX_PROBES:
            return True
        until = self._parse_datetime(self._mutable.get(f"{smartevse_key}_intelligent_not_accepting_until"))
        return until is not None and until > now

    def _intelligent_model_state(self, pre_state: ControllerState, pre_reason: str) -> tuple[str, str | None]:
        """Controller state/reason the model attributes this cycle's energy to."""
        if pre_state != ControllerState.INTELLIGENT:
            return pre_state.value, pre_reason
        previous_reason = self._mutable.get("last_charge_reason")
        if (self._mutable.get("last_controller_state") == ControllerState.INTELLIGENT.value
                and isinstance(previous_reason, str) and previous_reason.startswith("intelligent_")):
            return ControllerState.INTELLIGENT.value, previous_reason
        return ControllerState.INTELLIGENT.value, "intelligent_charging"

    def _intelligent_outlet_inputs(self, statuses: dict[str, SmartEVSEStatus]) -> dict[str, OutletInput]:
        inputs = {}
        for smartevse_key, status in statuses.items():
            vehicle_key = self._mapped_vehicle_key(smartevse_key)
            inputs[smartevse_key] = OutletInput(
                key=smartevse_key,
                available=status.available,
                connected=self._outlet_effectively_connected(smartevse_key, status),
                charging=status.available and status.connected and status.state == "Charging",
                mode=status.mode,
                vehicle=vehicle_key,
                offered_current=status.charge_current if status.available else None,
                max_current=status.max_current if status.available and status.max_current > 0 else None,
                max_mains=status.max_mains,
                min_current=status.min_current,
                plugged_at=self._parse_datetime(self._mutable.get(f"{smartevse_key}_intelligent_plugged_at")),
                deadline=self._parse_datetime(self._mutable.get(f"{smartevse_key}_intelligent_deadline")),
                reached=bool(self._mutable.get(f"{smartevse_key}_intelligent_reached")),
                vehicle_charging_state=self._vehicle_charging_state(vehicle_key) if vehicle_key else "",
                last_charging_at=self._parse_datetime(self._mutable.get(f"{smartevse_key}_last_charging_at")),
            )
        return inputs

    async def _async_evaluate_intelligent(
        self,
        *,
        now: datetime,
        statuses: dict[str, SmartEVSEStatus],
        pre_state: ControllerState,
        pre_reason: str,
        ledger: Any,
    ) -> IntelligentDecision | None:
        """Observe (every mode), update per-outlet state and plan (while enabled).

        Fully isolated: any failure gives a non-charging decision with the
        warning ``intelligent_planner_failed``; the control loop carries on.
        """
        intelligent = getattr(self, "_intelligent", None)
        enabled = bool(self._mutable.get("intelligent_enabled"))
        if intelligent is None:
            return None
        try:
            if enabled:
                self._update_intelligent_not_accepting(now=now, statuses=statuses)
            model_state, model_reason = self._intelligent_model_state(pre_state, pre_reason)
            observed = intelligent.observe(
                now,
                outlets=self._intelligent_outlet_inputs(statuses),
                settings=self._intelligent_settings(),
                last_socs=self._intelligent_last_socs(),
                ledger=ledger,
                controller_state=model_state,
                charge_reason=model_reason,
            )
            decision: IntelligentDecision | None = None
            if observed.swap is not None and enabled:
                # §8.11: the facts of this cycle used the old mapping; keep the
                # previous decision for one cycle and re-plan with the new one.
                # Only while Intelligent plans: Schedule/Force keep the mapping
                # rules of the controller alone (0.0.8.x behaviour).
                self._swap_vehicle_mapping(*observed.swap)
                intelligent.discard_sessions(observed.swap)
                decision = intelligent.last_decision or IntelligentDecision(
                    allowed=False, reason=REASON_INTELLIGENT_WAITING)
            elif enabled:
                self._update_intelligent_outlets(now=now, statuses=statuses, facts=observed.facts)
                decision = intelligent.plan(
                    now,
                    self._intelligent_plan_inputs(now=now, statuses=statuses, facts=observed.facts),
                    commitment=self._intelligent_commitment(now),
                    top_up_price=self._intelligent_top_up_price_value() if self._mutable.get("intelligent_top_up") else None,
                    seed=self._intelligent_seed_decision(now, statuses) if intelligent.last_decision is None else None,
                )
            else:
                intelligent.clear_plan()
        except Exception:  # noqa: BLE001 - Intelligent never breaks the control loop
            self._log_intelligent_exception("evaluate", "Intelligent charging evaluation failed", now)
            decision = (IntelligentDecision(allowed=False, reason=REASON_INTELLIGENT_WAITING,
                                            controller_error=ERROR_INTELLIGENT_PLANNER_FAILED)
                        if enabled else None)
        try:
            await intelligent.async_save_if_due(now)
        except Exception:  # noqa: BLE001
            self._log_intelligent_exception("save", "Unable to save the Intelligent charging model", now)
        # While Force overrides Intelligent the decision is only published, not
        # applied: _determine_charge_allowed returns the Force result first.
        return decision

    def _update_intelligent_not_accepting(self, *, now: datetime, statuses: dict[str, SmartEVSEStatus]) -> None:
        """§6.4 no-draw timer, from the previous cycle's persisted result."""
        threshold = max(30 * self.get_update_interval(), INTELLIGENT_NOT_ACCEPTING_MIN_SECONDS)
        previous_allowed = bool(self._mutable.get("last_charge_allowed"))
        previous_intelligent = self._mutable.get("last_controller_state") == ControllerState.INTELLIGENT.value
        active = self._mutable.get("active_smartevse")
        for smartevse_key, status in statuses.items():
            since_key = f"{smartevse_key}_intelligent_no_draw_since"
            if not status.available:
                continue  # an API blip neither advances nor resets the timer
            if not status.connected or status.state == "Charging":
                self._mutable[since_key] = None
                continue
            vehicle_key = self._mapped_vehicle_key(smartevse_key)
            car_timer = vehicle_key is not None and self._vehicle_charging_state(vehicle_key) == "scheduled"
            holds = (
                previous_allowed
                and previous_intelligent
                and active == smartevse_key
                and status.mode == "Smart"
                and not self._status_blocks_completion_detection(status, now=now)
                and not car_timer
            )
            if not holds:
                self._mutable[since_key] = None
                continue
            since = self._parse_datetime(self._mutable.get(since_key))
            if since is None or since > now:
                self._mutable[since_key] = now.isoformat()
                continue
            if (now - since).total_seconds() >= threshold:
                self._mutable[since_key] = None
                self._mark_intelligent_not_accepting(smartevse_key, now=now)

    def _mark_intelligent_not_accepting(self, smartevse_key: str, *, now: datetime) -> None:
        """The car did not draw for the whole probe: hold it off for an hour, or for the session."""
        intelligent = getattr(self, "_intelligent", None)
        facts = intelligent.facts(smartevse_key) if intelligent is not None else None
        self._mutable[f"{smartevse_key}_intelligent_committed_until"] = None
        if facts is not None and facts.soc is not None and facts.soc >= facts.target:
            # A car showing its minimum (e.g. an unverified reading probed with one
            # short charge) that does not draw is full, not refusing: it has reached
            # its minimum for this session (planned again only below target - 2).
            self._mutable[f"{smartevse_key}_intelligent_reached"] = True
            return
        probes = int(self._mutable.get(f"{smartevse_key}_intelligent_probes") or 0) + 1
        if facts is None or facts.soc is None:
            probes = INTELLIGENT_MAX_PROBES  # unknown SoC: done for the session
        self._mutable[f"{smartevse_key}_intelligent_probes"] = min(probes, INTELLIGENT_MAX_PROBES)
        self._mutable[f"{smartevse_key}_intelligent_not_accepting_until"] = (
            (now + INTELLIGENT_NOT_ACCEPTING_HOLD).isoformat() if probes < INTELLIGENT_MAX_PROBES else None)

    def _update_intelligent_outlets(self, *, now: datetime, statuses: dict[str, SmartEVSEStatus], facts: dict) -> None:
        """Deadlines, rolls and overrun (§6.2), reached hysteresis (§8.4), completion
        (§6.3), not-accepting expiry and commitment release (§6.5)."""
        tz = default_time_zone()
        settings = self._intelligent_settings()
        for smartevse_key, status in statuses.items():
            car = facts.get(smartevse_key)
            if car is None or not self._outlet_effectively_connected(smartevse_key, status):
                continue
            prefix = f"{smartevse_key}_intelligent_"
            settings_key = car.settings_key
            ready_by = settings[settings_key]["ready_by"]
            plugged_at = self._parse_datetime(self._mutable.get(f"{prefix}plugged_at"))
            deadline = self._parse_datetime(self._mutable.get(f"{prefix}deadline"))
            stored_key = self._mutable.get(f"{prefix}deadline_vehicle")
            remapped = stored_key is not None and stored_key != settings_key
            if stored_key != settings_key:
                if remapped:
                    # The mapped vehicle changed: its deadline and any refusal belong
                    # to the other car (§6.2, §6.4). The plug session (and with it a
                    # catch-up already running) continues: overrun is re-evaluated
                    # against this vehicle's ready-by below, never just dropped.
                    self._mutable[f"{prefix}not_accepting_until"] = None
                    self._mutable[f"{prefix}probes"] = 0
                    self._mutable[f"{prefix}no_draw_since"] = None
                    self._mutable[f"{prefix}reached"] = False
                deadline = None
            if deadline is None:
                deadline = session_deadline(ready_by, now, plugged_at, tz)
                self._mutable[f"{prefix}deadline"] = deadline.isoformat()
                self._mutable[f"{prefix}deadline_vehicle"] = settings_key
                if remapped and plugged_at is not None:
                    # The first ready-by of this vehicle after the plug-in has passed:
                    # the car is late under its own settings too (same rule as a roll).
                    first = next_local_occurrence(ready_by, plugged_at, tz)
                    below = (car.soc < car.target) if car.soc is not None else not self._mutable.get(
                        f"{prefix}complete")
                    if first <= now and below and plugged_at <= first - INTELLIGENT_LATE_PLUG:
                        self._mutable[f"{prefix}overrun"] = True
            if now >= deadline:
                below = (car.soc < car.target) if car.soc is not None else not self._mutable.get(f"{prefix}complete")
                began_early = plugged_at is not None and plugged_at <= deadline - INTELLIGENT_LATE_PLUG
                # A deadline that passed long before this roll (Home Assistant down, or
                # a version without Intelligent running meanwhile) is history: plan for
                # the next ready-by instead of catching up at any price now.
                recent = now - deadline <= INTELLIGENT_STALE_ROLL
                if below and began_early and recent:
                    self._mutable[f"{prefix}overrun"] = True
                self._mutable[f"{prefix}deadline"] = next_local_occurrence(ready_by, now, tz).isoformat()

            was_reached = bool(self._mutable.get(f"{prefix}reached"))
            reached = was_reached
            if car.soc is not None and car.soc_source != "unverified":
                reached = reached_with_hysteresis(car.soc, car.target, was_reached)
                if reached and not was_reached and car.vehicle is not None:
                    previous = self._mutable.get(f"{car.vehicle}_last_soc")
                    rose = isinstance(previous, (int, float)) and car.soc > float(previous)
                    charging_here = car.charging or self._mutable.get("active_smartevse") == smartevse_key
                    if rose and not charging_here:
                        # §8.11: never "reached" from a rise seen while this outlet was not charging.
                        reached = False
            self._mutable[f"{prefix}reached"] = reached
            if reached:
                self._mutable[f"{prefix}overrun"] = False
                if not was_reached:
                    # Released when the minimum is reached (§6.5); a top-up block
                    # that follows is committed again like any other block.
                    self._mutable[f"{prefix}committed_until"] = None

            if status.available and self._session_complete(smartevse_key):
                if not self._mutable.get(f"{prefix}complete"):
                    self._mutable[f"{prefix}complete"] = True
                    self._mutable[f"{prefix}complete_at"] = now.isoformat()
                self._mutable[f"{prefix}committed_until"] = None

            until = self._parse_datetime(self._mutable.get(f"{prefix}not_accepting_until"))
            if until is not None and until <= now:
                self._mutable[f"{prefix}not_accepting_until"] = None
            if (self._intelligent_not_accepting_active(smartevse_key, now)
                    or self._unavailable_too_long(smartevse_key, status, now)):
                self._mutable[f"{prefix}committed_until"] = None

            if car.vehicle is not None and car.live_soc is not None:
                at = car.live_changed_at or now
                if self._mutable.get(f"{car.vehicle}_last_soc") != car.live_soc:
                    self._mutable[f"{car.vehicle}_last_soc"] = car.live_soc
                    self._mutable[f"{car.vehicle}_last_soc_at"] = at.isoformat()
        for vehicle_key in ("vehicle_1", "vehicle_2"):
            if self._vehicle_connection_state(vehicle_key, now=now, require_fresh=True) is False:
                self._mutable[f"{vehicle_key}_last_soc"] = None
                self._mutable[f"{vehicle_key}_last_soc_at"] = None

    def _intelligent_retry_allowed(self, smartevse_key: str, car: Any, now: datetime) -> bool:
        """§6.3: re-plan a completed car with known SoC clearly below its target."""
        complete_at = self._parse_datetime(self._mutable.get(f"{smartevse_key}_intelligent_complete_at"))
        retries = int(self._mutable.get(f"{smartevse_key}_intelligent_retries") or 0)
        return (
            car is not None
            and car.soc is not None
            and car.soc < car.target - INTELLIGENT_RETRY_MARGIN
            and complete_at is not None
            and now - complete_at >= INTELLIGENT_RETRY_AFTER
            and retries < INTELLIGENT_MAX_RETRIES
        )

    def _intelligent_identifying(self, smartevse_key: str, car: Any, now: datetime) -> bool:
        """§6.9: right after a plug-connect while the outlet is still unmapped."""
        if car is None or car.vehicle is not None:
            return False
        plugged_at = self._parse_datetime(self._mutable.get(f"{smartevse_key}_intelligent_plugged_at"))
        return plugged_at is not None and timedelta(0) <= now - plugged_at < INTELLIGENT_IDENTIFYING_WINDOW

    def _intelligent_plan_inputs(self, *, now: datetime, statuses: dict[str, SmartEVSEStatus],
                                 facts: dict) -> dict[str, PlanInput]:
        try:
            policy = ChargePolicy(self._mutable["charge_policy"])
        except ValueError:
            policy = ChargePolicy(DEFAULT_CHARGE_POLICY)
        only = {
            ChargePolicy.SMARTEVSE_1_ONLY: "smartevse_1",
            ChargePolicy.SMARTEVSE_2_ONLY: "smartevse_2",
        }.get(policy)
        preferred = only or self._preferred_smartevse(policy)
        inputs = {}
        for smartevse_key, status in statuses.items():
            car = facts.get(smartevse_key)
            if car is None or not self._outlet_effectively_connected(smartevse_key, status):
                continue
            prefix = f"{smartevse_key}_intelligent_"
            deadline = self._parse_datetime(self._mutable.get(f"{prefix}deadline"))
            if deadline is None:
                continue
            complete = bool(self._mutable.get(f"{prefix}complete"))
            not_accepting = self._intelligent_not_accepting_active(smartevse_key, now)
            done = (
                (complete and not self._intelligent_retry_allowed(smartevse_key, car, now))
                or not_accepting
            )
            # A reached car is not "done": it has no minimum need but may still be
            # topped up (§7.5 step 10); the planner keeps the two apart.
            reached = bool(self._mutable.get(f"{prefix}reached"))
            polled_live = status.available and status.connected
            # The outlet kept in Smart since last cycle is running even while the
            # firmware pauses it ("No Power Available") or ramps up after a start:
            # an Off/Smart pair would clear the firmware's back-off (§3.4).
            running = polled_live and (
                status.state == "Charging"
                or (self._mutable.get("active_smartevse") == smartevse_key and status.mode == "Smart")
            )
            inputs[smartevse_key] = PlanInput(
                outlet=smartevse_key,
                deadline=deadline,
                overrun=bool(self._mutable.get(f"{prefix}overrun")),
                done=done,
                reached=reached,
                not_accepting=not_accepting,
                excluded=only is not None and smartevse_key != only,
                priority=0 if smartevse_key == preferred else 1,
                drawing=running,
                identifying=self._intelligent_identifying(smartevse_key, car, now),
                unavailable=self._unavailable_too_long(smartevse_key, status, now),
            )
        return inputs

    def _intelligent_seed_decision(self, now: datetime,
                                   statuses: dict[str, SmartEVSEStatus]) -> IntelligentDecision | None:
        """The last Intelligent decision as persisted before a restart or reload: the
        outlet committed for this unit, still polled in Smart, while the last cycle
        let Intelligent charge. None when nothing was charging."""
        if (self._mutable.get("last_controller_state") != ControllerState.INTELLIGENT.value
                or not self._mutable.get("last_charge_allowed")
                or self._mutable.get("last_charge_reason") not in INTELLIGENT_ALLOWED_REASONS):
            return None
        unit = floor15(now)
        for smartevse_key, status in statuses.items():
            until = self._parse_datetime(self._mutable.get(f"{smartevse_key}_intelligent_committed_until"))
            if (until is not None and until > unit and until > now and status.available
                    and status.connected and status.mode == "Smart"):
                since = self._parse_datetime(self._mutable.get("active_smartevse_since"))
                return IntelligentDecision(allowed=True, reason=str(self._mutable["last_charge_reason"]),
                                           outlet=smartevse_key, block_start=since)
        return None

    def _intelligent_commitment(self, now: datetime) -> tuple[str, datetime] | None:
        """The outlet committed for the current 15-min unit, if any (§6.5)."""
        candidates = []
        for smartevse_key in ("smartevse_1", "smartevse_2"):
            until = self._parse_datetime(self._mutable.get(f"{smartevse_key}_intelligent_committed_until"))
            if until is not None and until > now:
                candidates.append((self._mutable.get("active_smartevse") != smartevse_key, smartevse_key, until))
        if not candidates:
            return None
        _, smartevse_key, until = min(candidates)
        return smartevse_key, until

    def _swap_vehicle_mapping(self, first: str, second: str) -> None:
        """§8.11 cross-check: exchange the two outlets' vehicles, clear only their session
        tracking and void their not-accepting state."""
        first_vehicle = self._mapped_vehicle_key(first)
        second_vehicle = self._mapped_vehicle_key(second)
        self._mutable[f"{first}_connected_ev"] = second_vehicle or "unknown"
        self._mutable[f"{second}_connected_ev"] = first_vehicle or "unknown"
        for smartevse_key in (first, second):
            self._clear_smartevse_session_tracking(smartevse_key)
            prefix = f"{smartevse_key}_intelligent_"
            self._mutable[f"{prefix}not_accepting_until"] = None
            self._mutable[f"{prefix}probes"] = 0
            self._mutable[f"{prefix}no_draw_since"] = None
            self._mutable[f"{prefix}reached"] = False
        LOGGER.info("Swapped the vehicle mapping of %s and %s after a battery-level cross-check", first, second)

    def _intelligent_payload(
        self,
        *,
        now: datetime,
        governing: bool,
        charge_allowed: bool,
        controller_state: ControllerState,
        charge_reason: str,
        active_smartevse: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The ``intelligent`` payload and flat sensor values (never raises)."""
        enabled = bool(self._mutable.get("intelligent_enabled"))
        settings = self._intelligent_settings()
        top_up_enabled = bool(self._mutable.get("intelligent_top_up"))
        top_up_price = self._intelligent_top_up_price_value()
        intelligent = getattr(self, "_intelligent", None)
        overridden = enabled and not governing
        if intelligent is not None:
            try:
                outlets = {}
                for smartevse_key in ("smartevse_1", "smartevse_2"):
                    prefix = f"{smartevse_key}_intelligent_"
                    car = intelligent.facts(smartevse_key)
                    plan_input = intelligent.plan_input(smartevse_key)
                    outlets[smartevse_key] = OutletPayloadInput(
                        deadline=self._parse_datetime(self._mutable.get(f"{prefix}deadline")) if enabled else None,
                        overrun=enabled and bool(self._mutable.get(f"{prefix}overrun")),
                        not_accepting=enabled and self._intelligent_not_accepting_active(smartevse_key, now),
                        identifying=enabled and self._intelligent_identifying(smartevse_key, car, now),
                        excluded=bool(plan_input.excluded) if plan_input is not None else False,
                    )
                active = active_smartevse if active_smartevse in {"smartevse_1", "smartevse_2"} else None
                context = PayloadContext(
                    enabled=enabled,
                    overridden=overridden,
                    governing=governing,
                    charge_allowed=bool(charge_allowed),
                    controller_state=controller_state.value,
                    charge_reason=charge_reason,
                    active_outlet=active,
                    active_since=self._parse_datetime(self._mutable.get("active_smartevse_since")) if active else None,
                    settings=settings,
                    top_up_enabled=top_up_enabled,
                    top_up_price=top_up_price,
                    outlets=outlets,
                )
                return intelligent.build_payload(now, context)
            except Exception:  # noqa: BLE001
                self._log_intelligent_exception("payload", "Unable to build the Intelligent charging payload", now)
        # The card finds the Intelligent controls through these ids; without them it
        # would hide the feature while Intelligent still governs charging.
        entities = None
        if intelligent is not None:
            try:
                entities = intelligent.entities()
            except Exception:  # noqa: BLE001
                entities = None
        if entities is None:
            entities = registry_entities(self.hass, str(self._entry_data.get("entry_id") or ""))
        payload = empty_payload(
            enabled=enabled,
            overridden=overridden,
            settings=settings,
            vehicle_names={key: self._configured_vehicle_name(key) for key in ("vehicle_1", "vehicle_2")},
            top_up_enabled=top_up_enabled,
            top_up_price=top_up_price,
            entities=entities,
        )
        if intelligent is not None and enabled:
            payload["planner_error"] = "payload_failed"
        return payload, flat_values(payload)

    def _reset_charge_cycle(self, *, preserve_previous_active: bool = False) -> None:
        """Clear the active SmartEVSE so the next cycle restarts from policy."""
        if preserve_previous_active and self._mutable.get("active_smartevse") in {"smartevse_1", "smartevse_2"}:
            self._mutable["pending_previous_active_smartevse"] = self._mutable["active_smartevse"]
        elif not preserve_previous_active:
            self._mutable["pending_previous_active_smartevse"] = None
        self._mutable["active_smartevse"] = None
        self._mutable["active_smartevse_since"] = None
        self._mutable["handoff_target"] = None
        self._mutable["handoff_started_at"] = None
        self._clear_active_attempt_tracking()

    def _clear_active_attempt_tracking(self) -> None:
        """Forget active-turn charging state without clearing whole plug sessions."""
        for smartevse_key in ("smartevse_1", "smartevse_2"):
            self._mutable[f"{smartevse_key}_active_seen_charging"] = False
            self._mutable[f"{smartevse_key}_non_charging_since"] = None
            self._mutable[f"{smartevse_key}_low_power_since"] = None

    def _activate_smartevse(self, smartevse_key: str | None, active_since: datetime | None) -> None:
        """Persist the active SmartEVSE and reset tracking when a new active turn starts."""
        normalized = smartevse_key if smartevse_key in {"smartevse_1", "smartevse_2"} else None
        previous = self._mutable.get("active_smartevse")
        if normalized is None and previous is not None:
            self._clear_active_attempt_tracking()
        elif normalized is not None and previous != normalized:
            self._mutable[f"{normalized}_active_seen_charging"] = False
            self._mutable[f"{normalized}_non_charging_since"] = None
        self._mutable["active_smartevse"] = normalized
        self._mutable["active_smartevse_since"] = active_since.isoformat() if active_since else None

    def _start_handoff(self, smartevse_key: str, *, now: datetime) -> None:
        """Persist a pending handoff target so rotation remains sticky."""
        self._mutable["handoff_target"] = smartevse_key
        self._mutable["handoff_started_at"] = now.isoformat()

    def _clear_handoff(self) -> None:
        """Clear any pending handoff target."""
        self._mutable["handoff_target"] = None
        self._mutable["handoff_started_at"] = None

    def _clear_session_tracking(self) -> None:
        """Forget per-connector session completion state."""
        for smartevse_key in ("smartevse_1", "smartevse_2"):
            self._clear_smartevse_session_tracking(smartevse_key)

    def _clear_smartevse_session_tracking(self, smartevse_key: str) -> None:
        """Forget completion state for one SmartEVSE."""
        self._mutable[f"{smartevse_key}_active_seen_charging"] = False
        self._mutable[f"{smartevse_key}_seen_charging"] = False
        self._mutable[f"{smartevse_key}_session_complete"] = False
        self._mutable[f"{smartevse_key}_non_charging_since"] = None
        self._mutable[f"{smartevse_key}_low_power_since"] = None
        self._mutable[f"{smartevse_key}_last_vehicle_charging_state"] = None

    def _update_session_tracking(
        self,
        status: SmartEVSEStatus,
        *,
        now: datetime,
        ev_meter_power: float | None,
    ) -> None:
        """Track whether a connected EV has already completed one charge session."""
        seen_key = f"{status.key}_seen_charging"
        active_seen_key = f"{status.key}_active_seen_charging"
        complete_key = f"{status.key}_session_complete"
        non_charging_key = f"{status.key}_non_charging_since"
        low_power_key = f"{status.key}_low_power_since"
        last_vehicle_state_key = f"{status.key}_last_vehicle_charging_state"

        if not status.available:
            return

        if not status.connected:
            self._mutable[active_seen_key] = False
            self._mutable[seen_key] = False
            self._mutable[complete_key] = False
            self._mutable[non_charging_key] = None
            self._mutable[low_power_key] = None
            self._mutable[last_vehicle_state_key] = None
            # An observed unplug (available and not connected) ends the
            # Intelligent plug session; an API blip returned above.
            self._clear_intelligent_outlet_state(status.key)
            return

        mapped_vehicle_key = self._mapped_vehicle_key(status.key)
        vehicle_charging_state = (
            self._vehicle_charging_state(mapped_vehicle_key) if mapped_vehicle_key is not None else ""
        )
        has_vehicle_charging_state = bool(vehicle_charging_state)
        fresh_vehicle_charging_state = (
            self._vehicle_charging_state(mapped_vehicle_key, now=now, require_fresh=True)
            if mapped_vehicle_key is not None
            else ""
        )
        vehicle_reports_complete = fresh_vehicle_charging_state in VEHICLE_COMPLETE_STATES
        is_actively_charging = status.state == "Charging"
        is_selected = self._mutable.get("active_smartevse") == status.key
        active_non_charging_state = (
            status.state in {"Connected to EV", "Ready to Charge", "Stop Charging"}
            or status.state.startswith("Charging Stopped")
        )

        if is_actively_charging:
            if is_selected:
                self._mutable[active_seen_key] = True
                low_power_since = self._parse_datetime(self._mutable.get(low_power_key))
                if self._has_actual_ev_power(ev_meter_power):
                    self._mutable[low_power_key] = None
                elif self._has_low_ev_power_while_charging(status, ev_meter_power):
                    if low_power_since is None:
                        self._mutable[low_power_key] = now.isoformat()
                    elif int((now - low_power_since).total_seconds()) >= LOW_EV_POWER_COMPLETE_GRACE_SECONDS:
                        self._mutable[seen_key] = True
                        self._mutable[complete_key] = True
                        self._mutable[non_charging_key] = now.isoformat()
                        if mapped_vehicle_key is not None and has_vehicle_charging_state:
                            self._mutable[last_vehicle_state_key] = vehicle_charging_state or None
                        return
                else:
                    self._mutable[low_power_key] = None
            else:
                self._mutable[low_power_key] = None
            self._mutable[seen_key] = True
            self._mutable[complete_key] = False
            self._mutable[non_charging_key] = None
            self._mutable[last_vehicle_state_key] = vehicle_charging_state or None
            return

        if self._mutable.get(active_seen_key) and is_selected and active_non_charging_state:
            if not vehicle_reports_complete and self._status_blocks_completion_detection(status, now=now):
                self._mutable[non_charging_key] = None
                if mapped_vehicle_key is not None and has_vehicle_charging_state:
                    self._mutable[last_vehicle_state_key] = vehicle_charging_state or None
                return
            non_charging_since = self._parse_datetime(self._mutable.get(non_charging_key))
            if non_charging_since is None:
                self._mutable[non_charging_key] = now.isoformat()
                if mapped_vehicle_key is not None and has_vehicle_charging_state:
                    self._mutable[last_vehicle_state_key] = vehicle_charging_state or None
                return
            if vehicle_reports_complete:
                grace_seconds = max(self.get_update_interval() * 2, VEHICLE_COMPLETE_CONFIRMATION_GRACE_SECONDS)
            else:
                grace_seconds = max(self.get_update_interval() * 12, SMARTEVSE_COMPLETE_GRACE_SECONDS)
            if int((now - non_charging_since).total_seconds()) >= grace_seconds:
                self._mutable[complete_key] = True
            if mapped_vehicle_key is not None and has_vehicle_charging_state:
                self._mutable[last_vehicle_state_key] = vehicle_charging_state or None
            return

        if mapped_vehicle_key is not None and has_vehicle_charging_state:
            self._mutable[last_vehicle_state_key] = vehicle_charging_state or None

    def _has_actual_ev_power(self, ev_meter_power: float | None) -> bool:
        """Return whether the EV meter shows meaningful charging power."""
        return ev_meter_power is not None and abs(ev_meter_power) >= ACTUAL_CHARGING_POWER_THRESHOLD_WATTS

    def _has_low_ev_power_while_charging(self, status: SmartEVSEStatus, ev_meter_power: float | None) -> bool:
        """Return whether SmartEVSE reports charging while the local EV meter shows no real load."""
        return (
            ev_meter_power is not None
            and abs(ev_meter_power) < ACTUAL_CHARGING_POWER_THRESHOLD_WATTS
            and status.charge_current >= 6.0
        )

    def _status_shows_smart_pause(self, status: SmartEVSEStatus) -> bool:
        """Return whether the charger shows a transient Smart-mode pause (not a full car)."""
        states = (str(status.raw_state or "").lower(), str(status.state or "").lower())
        return any(marker in state for state in states for marker in SMART_PAUSE_STATE_MARKERS)

    def _track_pause_state(self, status: SmartEVSEStatus, *, now: datetime) -> None:
        """Remember when a charger last showed a Smart-mode pause (every mode, every cycle)."""
        if status.available and self._status_shows_smart_pause(status):
            self._mutable[f"{status.key}_last_pause_at"] = now.isoformat()

    def _track_unavailable(self, status: SmartEVSEStatus, *, now: datetime) -> None:
        """Remember the first unavailable poll of the current API outage (every mode)."""
        key = f"{status.key}_unavailable_since"
        if status.available:
            self._mutable[key] = None
            return
        since = self._parse_datetime(self._mutable.get(key))
        if since is None or since > now:
            self._mutable[key] = now.isoformat()

    def _unavailable_too_long(self, smartevse_key: str, status: SmartEVSEStatus, now: datetime) -> bool:
        """An outlet off the API for longer than the hand-off hold: it cannot charge now.

        Intelligent leaves such an outlet out of planning (its plug session,
        deadline and overrun are kept) so the other car gets the time; it is
        planned again as soon as the charger answers.
        """
        if status.available:
            return False
        since = self._parse_datetime(self._mutable.get(f"{smartevse_key}_unavailable_since"))
        if since is None:
            return False
        hold = max(2 * self.get_update_interval(), HANDOFF_UNAVAILABLE_MIN_SECONDS)
        return (now - since).total_seconds() >= hold

    def _status_blocks_completion_detection(
        self,
        status: SmartEVSEStatus,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return whether a transient charger condition should not be treated as charge completion."""
        error = str(status.error or "").strip().lower()
        state = str(status.state or "").strip().lower()
        if error and error not in {"none", "unknown", "unavailable"}:
            return True
        if self._status_shows_smart_pause(status):
            return True
        if "comm error" in state or "communication error" in state or "no power available" in state:
            return True
        # After a "No Power Available" pause the firmware shows a plain
        # "Charging Stopped" (ChargeDelay, ~60-90 s) before it resumes on its
        # own; keep that window from starting the completion grace timer.
        last_pause_at = self._parse_datetime(self._mutable.get(f"{status.key}_last_pause_at"))
        if last_pause_at is None:
            return False
        elapsed = (dt_util.as_utc(now or dt_util.utcnow()) - last_pause_at).total_seconds()
        return 0 <= elapsed <= SMART_PAUSE_COMPLETION_GUARD_SECONDS

    def _status_reports_charging(self, status: SmartEVSEStatus) -> bool:
        """Return whether the SmartEVSE itself reports active charging."""
        return status.state == "Charging"

    def _session_complete(self, smartevse_key: str) -> bool:
        """Return whether the current plugged session has already completed."""
        return bool(self._mutable.get(f"{smartevse_key}_session_complete"))

    def _determine_charge_allowed(
        self,
        *,
        price_value: float | None,
        schedule_window_active: bool,
        intelligent: IntelligentDecision | None = None,
    ) -> tuple[bool, ControllerState, str]:
        """Resolve high-level controller state.

        Precedence: Force (all variants) -> legacy direct timer/price ->
        Intelligent (when enabled; ``intelligent`` is this cycle's plan
        decision) -> Schedule -> idle.
        """
        acceptable_price = float(self._mutable["acceptable_price"])
        if self._mutable["force_charge"]:
            if self._mutable["force_timer"] and self._timer_until() is not None:
                if self._mutable["force_price"]:
                    if not self._entry_data.get(CONF_PRICE_SENSOR_ENTITY):
                        return False, ControllerState.IDLE, "price_sensor_unavailable"
                    if price_value is None:
                        return False, ControllerState.IDLE, "price_sensor_unavailable"
                    if price_value > acceptable_price:
                        return False, ControllerState.IDLE, "waiting_for_acceptable_price"
                    return True, ControllerState.TIMER, "force_timer_acceptable_price"
                return True, ControllerState.TIMER, "force_timer"
            if self._mutable["force_price"]:
                if not self._entry_data.get(CONF_PRICE_SENSOR_ENTITY):
                    return False, ControllerState.IDLE, "price_sensor_unavailable"
                if price_value is None:
                    return False, ControllerState.IDLE, "price_sensor_unavailable"
                if price_value <= acceptable_price:
                    return True, ControllerState.PRICE, "acceptable_price"
                return False, ControllerState.IDLE, "waiting_for_acceptable_price"
            return True, ControllerState.FORCE, "force_charge"

        # Keep accepting the pre-0.0.8.6 direct timer/price switch behavior for
        # automations that do not yet use Force charge as the activation flag.
        if self._mutable["force_timer"] and self._timer_until() is not None:
            if self._mutable["force_price"]:
                if not self._entry_data.get(CONF_PRICE_SENSOR_ENTITY):
                    return False, ControllerState.IDLE, "price_sensor_unavailable"
                if price_value is None:
                    return False, ControllerState.IDLE, "price_sensor_unavailable"
                if price_value > acceptable_price:
                    return False, ControllerState.IDLE, "waiting_for_acceptable_price"
                return True, ControllerState.TIMER, "force_timer_acceptable_price"
            return True, ControllerState.TIMER, "force_timer"
        if self._mutable["force_price"]:
            if not self._entry_data.get(CONF_PRICE_SENSOR_ENTITY):
                return False, ControllerState.IDLE, "price_sensor_unavailable"
            if price_value is None:
                return False, ControllerState.IDLE, "price_sensor_unavailable"
            if price_value <= acceptable_price:
                return True, ControllerState.PRICE, "acceptable_price"
            return False, ControllerState.IDLE, "waiting_for_acceptable_price"
        if self._mutable.get("intelligent_enabled"):
            if intelligent is None:
                return False, ControllerState.INTELLIGENT, REASON_INTELLIGENT_WAITING
            allowed = bool(intelligent.allowed and intelligent.outlet)
            reason = intelligent.reason if allowed or not intelligent.allowed else REASON_INTELLIGENT_WAITING
            return allowed, ControllerState.INTELLIGENT, reason
        if self._mutable["schedule_enabled"]:
            if not self._entry_data.get(CONF_SCHEDULE_ENTITY):
                return False, ControllerState.IDLE, "schedule_entity_unavailable"
            if not schedule_window_active:
                return False, ControllerState.IDLE, "waiting_for_schedule_window"
            if self._mutable["schedule_price"]:
                if not self._entry_data.get(CONF_PRICE_SENSOR_ENTITY):
                    return False, ControllerState.IDLE, "price_sensor_unavailable"
                if price_value is None:
                    return False, ControllerState.IDLE, "price_sensor_unavailable"
                if price_value > acceptable_price:
                    return False, ControllerState.IDLE, "waiting_for_acceptable_price"
                return True, ControllerState.SCHEDULE, "schedule_acceptable_price"
            return True, ControllerState.SCHEDULE, "schedule"
        return False, ControllerState.IDLE, "idle"

    def _resolve_active_smartevse(
        self,
        *,
        now: datetime,
        smartevse_1: SmartEVSEStatus,
        smartevse_2: SmartEVSEStatus,
        intelligent_outlet: str | None = None,
        intelligent_block_start: datetime | None = None,
    ) -> tuple[str, str | None, int, str]:
        """Return the active SmartEVSE, start time, remaining time, and any block reason.

        ``intelligent_outlet`` is the planned outlet when the gate state is
        INTELLIGENT; the plan then replaces policy order and duty-cycle rotation.
        """
        statuses = {
            "smartevse_1": smartevse_1,
            "smartevse_2": smartevse_2,
        }
        if intelligent_outlet is not None:
            return self._resolve_intelligent_outlet(
                now=now,
                statuses=statuses,
                outlet=intelligent_outlet,
                block_start=intelligent_block_start,
            )
        policy = ChargePolicy(self._mutable["charge_policy"])
        policy_change_pending = bool(self._mutable.get("policy_change_pending"))
        if policy_change_pending and policy in (
            ChargePolicy.SMARTEVSE_1_ONLY,
            ChargePolicy.SMARTEVSE_2_ONLY,
        ):
            # Only-policies switch to the fixed charger directly below, so the
            # pending change is already satisfied — consume the flag now.
            self._mutable["policy_change_pending"] = False
            policy_change_pending = False
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
            self._activate_smartevse(only_smartevse, None)
            self._clear_handoff()
            return only_smartevse, None, 0, ""

        preferred_smartevse = self._preferred_smartevse(policy)
        active_smartevse = str(self._mutable.get("active_smartevse") or "")
        active_smartevse_since = self._parse_datetime(self._mutable.get("active_smartevse_since"))
        handoff_target = str(self._mutable.get("handoff_target") or "")
        handoff_started_at = self._parse_datetime(self._mutable.get("handoff_started_at"))
        interval_seconds = max(1, int(self._mutable["duty_cycle_minutes"])) * 60
        # The sequential hand-off in _apply_modes can keep the incoming charger
        # Off for up to HANDOFF_SETTLE_SECONDS while the outgoing one settles.
        handoff_grace_seconds = max(self.get_update_interval() * 12, 120) + HANDOFF_SETTLE_SECONDS
        observed_charging = [
            smartevse_key
            for smartevse_key in eligible_smartevse
            if self._status_reports_charging(statuses[smartevse_key])
        ]

        if handoff_target and handoff_target not in eligible_smartevse:
            self._clear_handoff()
            handoff_target = ""
            handoff_started_at = None
        elif handoff_target and handoff_started_at is None:
            self._clear_handoff()
            handoff_target = ""

        if policy_change_pending:
            # Both chargers are eligible, so a pending policy change can finally be
            # applied. Consume the flag only here (not on the earlier waiting/
            # offline returns) so a change made while a charger was offline or only
            # one EV was plugged in still takes effect once both are ready — and so
            # a stale flag can never keep forcing the preferred EV after a normal
            # rotation.
            self._mutable["policy_change_pending"] = False
            if (
                not handoff_target
                and preferred_smartevse in eligible_smartevse
                and preferred_smartevse not in observed_charging
            ):
                # Switch to the newly preferred SmartEVSE right away via a handoff
                # so the preferred charger takes over (staying sticky through the
                # grace window) instead of the controller re-adopting the EV that
                # is still winding down.
                self._start_handoff(preferred_smartevse, now=now)
                handoff_target = preferred_smartevse
                handoff_started_at = now

        if handoff_target and handoff_started_at is not None:
            handoff_elapsed = int((now - handoff_started_at).total_seconds())
            if handoff_target in observed_charging or handoff_elapsed < handoff_grace_seconds:
                self._activate_smartevse(handoff_target, handoff_started_at)
                if handoff_target in observed_charging:
                    self._clear_handoff()
                duty_cycle_remaining = max(
                    interval_seconds - int((now - handoff_started_at).total_seconds()),
                    0,
                )
                return handoff_target, handoff_started_at.isoformat(), duty_cycle_remaining, ""
            failed_handoff_target = handoff_target
            self._clear_handoff()
            fallback_smartevse = self._other_smartevse(failed_handoff_target)
            if fallback_smartevse in eligible_smartevse:
                active_smartevse_since = now
                self._start_handoff(fallback_smartevse, now=now)
                self._activate_smartevse(fallback_smartevse, active_smartevse_since)
                return fallback_smartevse, active_smartevse_since.isoformat(), interval_seconds, ""

        if len(observed_charging) == 1 and active_smartevse != observed_charging[0]:
            active_smartevse = observed_charging[0]
            active_smartevse_since = now

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
                self._start_handoff(next_smartevse, now=now)

        duty_cycle_remaining = max(
            interval_seconds - int((now - active_smartevse_since).total_seconds()),
            0,
        )

        self._activate_smartevse(active_smartevse, active_smartevse_since)
        return active_smartevse, active_smartevse_since.isoformat(), duty_cycle_remaining, ""

    def _resolve_intelligent_outlet(
        self,
        *,
        now: datetime,
        statuses: dict[str, SmartEVSEStatus],
        outlet: str,
        block_start: datetime | None,
    ) -> tuple[str, str | None, int, str]:
        """Intelligent branch (§6.6): activate the planned outlet or report why not."""
        if not any(status.available for status in statuses.values()):
            self._reset_charge_cycle()
            return "none", None, 0, "smartevse_api_unavailable"
        status = statuses.get(outlet)
        if status is None or not status.available or not status.connected:
            self._activate_smartevse(None, None)
            return "none", None, 0, REASON_INTELLIGENT_OUTLET_UNAVAILABLE
        since = self._parse_datetime(self._mutable.get("active_smartevse_since"))
        if self._mutable.get("active_smartevse") != outlet or since is None:
            # The planned block start, not now, so the recorded attribute
            # does not change every cycle.
            since = dt_util.as_utc(block_start) if block_start is not None else now.replace(second=0, microsecond=0)
            since = min(since, now)
        if self._mutable.get(f"{outlet}_intelligent_complete"):
            # The planner only plans a completed outlet as a retry (§6.3).
            self._clear_smartevse_session_tracking(outlet)
            self._mutable[f"{outlet}_intelligent_complete"] = False
            self._mutable[f"{outlet}_intelligent_complete_at"] = None
            self._mutable[f"{outlet}_intelligent_retries"] = int(
                self._mutable.get(f"{outlet}_intelligent_retries") or 0) + 1
        self._activate_smartevse(outlet, since)
        self._clear_handoff()
        self._mutable["policy_change_pending"] = False
        # Commitment (§6.5): renewed every cycle the outlet is activated or kept.
        self._mutable[f"{outlet}_intelligent_committed_until"] = (floor15(now) + timedelta(minutes=15)).isoformat()
        self._mutable[f"{self._other_smartevse(outlet)}_intelligent_committed_until"] = None
        return outlet, since.isoformat(), 0, ""

    def _resolve_only_policy(
        self,
        selected_smartevse: str,
        selected_status: SmartEVSEStatus,
    ) -> tuple[str, str | None, int, str]:
        """Resolve a fixed SmartEVSE policy."""
        if not selected_status.available:
            self._activate_smartevse(None, None)
            return "none", None, 0, f"{selected_smartevse}_api_unavailable"
        if not selected_status.connected:
            self._activate_smartevse(None, None)
            return "none", None, 0, f"{selected_smartevse}_only_waiting_for_selected_ev"
        if self._session_complete(selected_smartevse):
            self._activate_smartevse(None, None)
            return "none", None, 0, f"{selected_smartevse}_only_selected_ev_already_complete"
        self._activate_smartevse(selected_smartevse, None)
        return selected_smartevse, None, 0, ""

    async def _apply_modes(
        self,
        *,
        smartevse_1: SmartEVSEStatus,
        smartevse_2: SmartEVSEStatus,
        active_smartevse: str,
        charge_allowed: bool,
        now: datetime | None = None,
        ev_meter_power: float | None = None,
    ) -> None:
        """Apply the desired SmartEVSE modes for this cycle.

        Hand-offs are sequential. SmartEVSE firmware balances on the mains
        reading, so when both modes change at once the incoming charger still
        sees the outgoing car's ~15 A, pauses with "No Power Available" and
        loses ~79 s. The Off write to the outgoing charger is therefore awaited
        first, and the incoming one is not switched to Smart until the outgoing
        one has settled (see _handoff_holds_incoming). A mode is only written
        when it differs from the polled mode (setMode clears the firmware's
        back-off timers).
        """
        now = now or dt_util.utcnow()
        statuses = {"smartevse_1": smartevse_1, "smartevse_2": smartevse_2}
        for smartevse_key, status in statuses.items():
            if status.available:
                self._mutable[f"{smartevse_key}_last_observed_mode"] = status.mode

        desired_smartevse = active_smartevse if charge_allowed else "none"
        incoming = desired_smartevse if desired_smartevse in statuses else None
        if incoming is None:
            # Nothing is enabled, so both Off writes may run together.
            await asyncio.gather(
                *(self._apply_mode_for_smartevse(status, desired_mode="Off") for status in statuses.values())
            )
            for status in statuses.values():
                self._note_handoff_off_write(status, now=now)
            return

        if self._mutable.get("handoff_settle_from") not in {None, self._other_smartevse(incoming)}:
            # Switching back to the outlet that was settling (or stale state):
            # there is nothing to wait for.
            self._clear_handoff_settle()

        outgoing_status = statuses[self._other_smartevse(incoming)]
        # Awaited before anything is written to the incoming charger. If this
        # POST fails the outgoing charger is still polled in a drawing mode next
        # cycle, so the incoming one is not enabled (see _handoff_holds_incoming).
        await self._apply_mode_for_smartevse(outgoing_status, desired_mode="Off")
        self._note_handoff_off_write(outgoing_status, now=now)
        hold_incoming = self._handoff_holds_incoming(outgoing_status, now=now, ev_meter_power=ev_meter_power)
        incoming_status = statuses[incoming]
        incoming_mode = self._desired_mode(incoming, desired_smartevse)
        if hold_incoming and incoming_status.mode != incoming_mode:
            # Never send Smart while the outgoing charger settles. An incoming
            # charger that is already in Smart (e.g. released after the
            # unavailable-outgoing timeout) is left alone rather than restarted.
            incoming_mode = "Off"
        await self._apply_mode_for_smartevse(incoming_status, desired_mode=incoming_mode)

    def _mode_may_draw(self, mode: str | None) -> bool:
        """Return whether a charger in this (polled) mode may be drawing current."""
        return bool(mode) and mode not in NON_DRAWING_MODES and mode != "Unavailable"

    def _clear_handoff_settle(self) -> None:
        """Forget the sequential hand-off settle state."""
        self._mutable["handoff_settle_from"] = None
        self._mutable["handoff_settle_since"] = None

    def _note_handoff_off_write(self, status: SmartEVSEStatus, *, now: datetime) -> None:
        """(Re)start the settle window when an Off write went to a charger that may draw.

        A failed POST may still have reached the charger, so the attempt time
        counts as the Off write; a charger that ignored it is polled in a
        drawing mode again next cycle and gets a new attempt and window.
        """
        if not status.available or not self._mode_may_draw(status.mode):
            return
        self._mutable["handoff_settle_from"] = status.key
        self._mutable["handoff_settle_since"] = now.isoformat()

    def _handoff_holds_incoming(
        self,
        outgoing: SmartEVSEStatus,
        *,
        now: datetime,
        ev_meter_power: float | None,
    ) -> bool:
        """Return whether the incoming charger must stay Off while the outgoing one settles."""
        key = outgoing.key
        settling = self._mutable.get("handoff_settle_from") == key
        settle_since = self._parse_datetime(self._mutable.get("handoff_settle_since"))
        if settling and (settle_since is None or settle_since > now):
            settle_since = now
            self._mutable["handoff_settle_since"] = now.isoformat()

        if outgoing.available:
            if self._mode_may_draw(outgoing.mode):
                # Not yet observed Off: this cycle's Off write was just awaited
                # (or failed, in which case the incoming is not enabled either).
                return True
            observed_mode = outgoing.mode
        else:
            observed_mode = self._mutable.get(f"{key}_last_observed_mode")
            if self._mode_may_draw(observed_mode):
                # The outgoing charger dropped off the API while it may still be
                # drawing and cannot be switched Off: wait a bounded time.
                if not settling:
                    self._mutable["handoff_settle_from"] = key
                    self._mutable["handoff_settle_since"] = now.isoformat()
                    settle_since = now
                hold_seconds = max(2 * self.get_update_interval(), HANDOFF_UNAVAILABLE_MIN_SECONDS)
                return (now - settle_since).total_seconds() < hold_seconds

        if not settling or self._mode_may_draw(observed_mode):
            return False
        # Observed Off: wait until the car has stopped drawing (EV meter) or
        # HANDOFF_SETTLE_SECONDS after the Off write, whichever comes first.
        power_settled = ev_meter_power is not None and ev_meter_power < HANDOFF_SETTLE_POWER_WATTS
        if power_settled or (now - settle_since).total_seconds() >= HANDOFF_SETTLE_SECONDS:
            self._clear_handoff_settle()
            return False
        return True

    def _desired_mode(self, smartevse_key: str, active_smartevse: str) -> str:
        """Return the desired mode for the given SmartEVSE."""
        return "Smart" if smartevse_key == active_smartevse else "Off"

    async def _apply_mode_for_smartevse(self, status: SmartEVSEStatus, *, desired_mode: str) -> bool:
        """Apply the desired mode and clear any override current.

        Return True when no write was needed or the write succeeded, False when
        the charger is unavailable or the POST failed.
        """
        if not status.available:
            return False

        params: dict[str, Any] = {}
        if status.mode != desired_mode:
            params["mode"] = MODE_NAME_TO_ID[desired_mode]
        if status.override_current > 0:
            params["disable_override_current"] = 1
        if not params:
            return True

        return bool(
            await self._async_post(
                urljoin(self._normalize_url(status.base_url), "settings"),
                params=params,
            )
        )

    def charging_costs(self):
        """Return observational accounting independently of charge-control errors."""
        try:
            data = self._cost_ledger.snapshot() if self._cost_ledger else {"version": 1, "chargers": {}}
        except Exception:
            # Accounting must never interrupt the existing charger-control loop.
            self._cost_error = "Charging history could not be read. Check integration logs."
            LOGGER.exception("Unable to read charging cost history")
            data = {"version": 1, "chargers": {}}
        data["error"] = self._cost_error
        data["price_entity"] = self._entry_data.get(CONF_PRICE_SENSOR_ENTITY)
        data["power_entity"] = self._entry_data.get(CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY)
        return data

    async def _observe_charging_costs(self, *statuses, now):
        if self._cost_ledger is None:
            return
        try:
            power_id = self._entry_data.get(CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY)
            price_id = self._entry_data.get(CONF_PRICE_SENSOR_ENTITY)
            power = power_kw(self.hass.states.get(power_id)) if power_id else None
            price, currency = tariff(self.hass.states.get(price_id)) if price_id else (None, None)
            self._cost_ledger.observe(now, {s.key: {
                "available": s.available, "connected": s.connected,
                "charging": s.state == "Charging", "complete": self._session_complete(s.key),
                "vehicle": self._connected_ev_label(s.key),
            } for s in statuses}, power, price, currency, [power_id, price_id])
            await self._cost_store.async_save(self._cost_ledger.data)
            self._cost_error = None
        except Exception:
            self._cost_error = "Charging history could not be updated. Recorded costs may be incomplete."
            LOGGER.exception("Unable to update charging cost history")

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
        if not isinstance(evse, dict):
            evse = {}
        if not isinstance(settings, dict):
            settings = {}
        connected = bool(payload.get("car_connected") or evse.get("connected"))
        plug_state = "Connected" if connected else "Disconnected"
        raw_state = "" if evse.get("state") is None else str(evse.get("state"))
        state = str(evse.get("state") or ("Connected to EV" if connected else "Disconnected"))
        if not connected:
            state = "Disconnected"

        return SmartEVSEStatus(
            key=key,
            base_url=base_url,
            available=True,
            connected=connected,
            plug_state=plug_state,
            state=state,
            mode=self._normalize_mode(payload.get("mode"), payload.get("mode_id")),
            charge_current=self._deciamp_to_amp(settings.get("charge_current")),
            max_current=self._to_float(settings.get("current_max") or settings.get("current_max_circuit")),
            override_current=self._deciamp_to_amp(settings.get("override_current")),
            error=str(evse.get("error") or "None"),
            # current_main / current_min are whole amps (not deci-amps).
            max_mains=self._positive_float_or_none(settings.get("current_main")),
            min_current=self._positive_float_or_none(settings.get("current_min")),
            mode_id=self._int_or_none(payload.get("mode_id")),
            raw_state=raw_state,
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

        state_url = normalize_wled_state_url(wled_url)
        payload = self._build_wled_payload(smartevse_1=smartevse_1, smartevse_2=smartevse_2)
        current_state = await self._async_get_json(state_url)
        if current_state is not None and runtime_state_matches_payload(current_state, payload):
            return

        if await self._async_post(state_url, json_payload=payload):
            self._mutable["last_wled_push"] = dt_util.utcnow().isoformat()

    def _build_wled_payload(
        self,
        *,
        smartevse_1: SmartEVSEStatus,
        smartevse_2: SmartEVSEStatus,
    ) -> dict[str, Any]:
        """Create the WLED payload."""
        return build_runtime_payload(
            smartevse_1=smartevse_1,
            smartevse_2=smartevse_2,
            led_count=int(self._entry_data.get(CONF_WLED_LED_COUNT, DEFAULT_WLED_LED_COUNT)),
            led_offset=int(self._entry_data.get(CONF_WLED_LED_OFFSET, DEFAULT_WLED_LED_OFFSET)),
        )

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

        if not notify_enabled or not schedule_window_active or schedule_enabled:
            persistent_notification.async_dismiss(self.hass, SCHEDULE_NOTIFICATION_ID)

        self._mutable["last_schedule_window_active"] = schedule_window_active
        self._schedule_reminders.async_check()

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

    def _entity_state_fresh(self, entity_id: str | None, *, now: datetime | None = None) -> bool:
        """Return whether an entity has usable recent state."""
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        if state is None or state.state in {"unknown", "unavailable", None}:
            return False
        reference = dt_util.as_utc(now or dt_util.utcnow())
        last_seen = dt_util.as_utc(getattr(state, "last_reported", None) or state.last_updated)
        if last_seen > reference:
            return False
        return reference - last_seen <= VEHICLE_TELEMETRY_MAX_AGE

    def _state_display(self, entity_id: str | None) -> str | None:
        """Read an entity state as a display string with unit if available."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in {"unknown", "unavailable", None}:
            return None
        raw = str(state.state).strip()
        if not raw:
            return None
        unit = str(state.attributes.get("unit_of_measurement", "")).strip()
        if not unit or raw.endswith(unit):
            return raw
        return f"{raw}{unit}" if unit in {"%", "°C"} else f"{raw} {unit}"

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

    def _positive_float_or_none(self, value: Any) -> float | None:
        """Convert a JSON value to a finite positive float, or None when missing or invalid."""
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number <= 0:
            return None
        return number

    def _int_or_none(self, value: Any) -> int | None:
        """Convert a JSON value to an int, or None when missing or not a whole number."""
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number != int(number):
            return None
        return int(number)

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
