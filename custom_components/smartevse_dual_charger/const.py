"""Constants for the SmartEVSE Dual Charger integration."""

from __future__ import annotations

from enum import StrEnum
import logging

from homeassistant.const import Platform

DOMAIN = "smartevse_dual_charger"
NAME = "SmartEVSE Dual Charger"
VERSION = "0.1.0"

LOGGER = logging.getLogger(__package__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.BUTTON,
]

STORAGE_VERSION = 1
STORAGE_KEY = DOMAIN

DEFAULT_NAME = NAME
DEFAULT_ACTIVE_MODE = "Normal"
DEFAULT_TOTAL_CURRENT_LIMIT = 16.0
DEFAULT_MIN_CURRENT = 6.0
DEFAULT_UPDATE_INTERVAL = 10
DEFAULT_CURRENTS_PUSH_INTERVAL = 10
DEFAULT_EV_METER_PUSH_INTERVAL = 20
DEFAULT_OVERRIDE_DEADBAND = 2.0
DEFAULT_ACCEPTABLE_PRICE = 0.10
DEFAULT_BALANCE_PERCENT = 50
DEFAULT_FORCE_CHARGE_DURATION_MINUTES = 120
DEFAULT_NOTIFY_ON_SCHEDULE_WINDOW = True
DEFAULT_LOW_BUDGET_POLICY = "alternate"
DEFAULT_PUSH_CURRENTS = True
DEFAULT_PUSH_EV_METER = True
DEFAULT_PUSH_WLED = True

ATTR_AVAILABLE_CURRENT = "available_current"
ATTR_CHARGE_ALLOWED = "charge_allowed"
ATTR_CHARGE_REASON = "charge_reason"
ATTR_CONTROLLER_STATE = "controller_state"
ATTR_EVSE_1_TARGET_CURRENT = "evse_1_target_current"
ATTR_EVSE_2_TARGET_CURRENT = "evse_2_target_current"
ATTR_HOUSE_LOAD = "house_load"
ATTR_LAST_CYCLE_REASON = "last_cycle_reason"
ATTR_LAST_METER_PUSH = "last_meter_push"
ATTR_LAST_NOTIFICATION = "last_notification"
ATTR_LAST_WLED_PUSH = "last_wled_push"
ATTR_LOW_BUDGET_WINNER = "low_budget_winner"
ATTR_MAINS_PEAK = "mains_peak"
ATTR_SCHEDULE_WINDOW_ACTIVE = "schedule_window_active"
ATTR_TIMER_REMAINING = "timer_remaining"
ATTR_TIMER_UNTIL = "timer_until"

CONF_ACTIVE_MODE = "active_mode"
CONF_CURRENTS_PUSH_INTERVAL = "currents_push_interval"
CONF_EVSE_1_BASE_URL = "evse_1_base_url"
CONF_EVSE_1_ERROR_ENTITY = "evse_1_error_entity"
CONF_EVSE_1_MODE_ENTITY = "evse_1_mode_entity"
CONF_EVSE_1_OVERRIDE_ENTITY = "evse_1_override_entity"
CONF_EVSE_1_PLUG_ENTITY = "evse_1_plug_entity"
CONF_EVSE_1_STATE_ENTITY = "evse_1_state_entity"
CONF_EVSE_2_BASE_URL = "evse_2_base_url"
CONF_EVSE_2_ERROR_ENTITY = "evse_2_error_entity"
CONF_EVSE_2_MODE_ENTITY = "evse_2_mode_entity"
CONF_EVSE_2_OVERRIDE_ENTITY = "evse_2_override_entity"
CONF_EVSE_2_PLUG_ENTITY = "evse_2_plug_entity"
CONF_EVSE_2_STATE_ENTITY = "evse_2_state_entity"
CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY = "ev_meter_export_active_energy_entity"
CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY = "ev_meter_import_active_energy_entity"
CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY = "ev_meter_import_active_power_entity"
CONF_EV_METER_L1_ENTITY = "ev_meter_l1_entity"
CONF_EV_METER_L2_ENTITY = "ev_meter_l2_entity"
CONF_EV_METER_L3_ENTITY = "ev_meter_l3_entity"
CONF_EV_METER_PUSH_INTERVAL = "ev_meter_push_interval"
CONF_LOW_BUDGET_POLICY_DEFAULT = "low_budget_policy_default"
CONF_MAINS_L1_ENTITY = "mains_l1_entity"
CONF_MAINS_L2_ENTITY = "mains_l2_entity"
CONF_MAINS_L3_ENTITY = "mains_l3_entity"
CONF_NOTIFY_ON_SCHEDULE_WINDOW = "notify_on_schedule_window"
CONF_OVERRIDE_DEADBAND = "override_deadband"
CONF_PRICE_SENSOR_ENTITY = "price_sensor_entity"
CONF_PUSH_CURRENTS = "push_currents"
CONF_PUSH_EV_METER = "push_ev_meter"
CONF_PUSH_WLED = "push_wled"
CONF_SCHEDULE_ENTITY = "schedule_entity"
CONF_TOTAL_CURRENT_LIMIT = "total_current_limit"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_WLED_URL = "wled_url"

ENTITY_KEYS_BY_ROLE: tuple[str, ...] = (
    CONF_EVSE_1_STATE_ENTITY,
    CONF_EVSE_1_PLUG_ENTITY,
    CONF_EVSE_1_MODE_ENTITY,
    CONF_EVSE_1_OVERRIDE_ENTITY,
    CONF_EVSE_1_ERROR_ENTITY,
    CONF_EVSE_2_STATE_ENTITY,
    CONF_EVSE_2_PLUG_ENTITY,
    CONF_EVSE_2_MODE_ENTITY,
    CONF_EVSE_2_OVERRIDE_ENTITY,
    CONF_EVSE_2_ERROR_ENTITY,
    CONF_MAINS_L1_ENTITY,
    CONF_MAINS_L2_ENTITY,
    CONF_MAINS_L3_ENTITY,
    CONF_EV_METER_L1_ENTITY,
    CONF_EV_METER_L2_ENTITY,
    CONF_EV_METER_L3_ENTITY,
    CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY,
    CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY,
    CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY,
    CONF_PRICE_SENSOR_ENTITY,
    CONF_SCHEDULE_ENTITY,
)

COMPLETE_STATES = {"Connected to EV", "Charging Stopped", "Stop Charging"}
ACTIVE_STATE = "Charging"
PENDING_STATES = {"Ready to Charge"}

SCHEDULE_NOTIFICATION_ID = f"{DOMAIN}_schedule_disabled"


class LowBudgetPolicy(StrEnum):
    """Allocation policy when less than 2 * min current is available."""

    EVSE_1_PRIORITY = "evse_1_priority"
    EVSE_2_PRIORITY = "evse_2_priority"
    ALTERNATE = "alternate"
    PAUSE_ALL = "pause_all"


class ControllerState(StrEnum):
    """High-level controller state."""

    IDLE = "idle"
    SCHEDULE = "schedule"
    PRICE = "price"
    FORCE = "force"
    TIMER = "timer"
    LOW_BUDGET = "low_budget"
    BLOCKED = "blocked"
