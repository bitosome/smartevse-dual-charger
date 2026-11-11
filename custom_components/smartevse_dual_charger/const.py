"""Constants for the SmartEVSE Dual Charger integration."""

from __future__ import annotations

from enum import StrEnum
import logging

from homeassistant.const import Platform

DOMAIN = "smartevse_dual_charger"
NAME = "SmartEVSE Dual Charger"
VERSION = "0.0.12.0"

LOGGER = logging.getLogger(__package__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.TIME,
]

STORAGE_VERSION = 1
STORAGE_KEY = DOMAIN

DEFAULT_NAME = NAME
DEFAULT_UPDATE_INTERVAL = 10
DEFAULT_CURRENTS_PUSH_INTERVAL = 10
DEFAULT_EV_METER_PUSH_INTERVAL = 10
DEFAULT_ACCEPTABLE_PRICE = 0.10
DEFAULT_FORCE_CHARGE_DURATION_MINUTES = 120
DEFAULT_DUTY_CYCLE_MINUTES = 60
DEFAULT_NOTIFY_ON_SCHEDULE_WINDOW = True
DEFAULT_SCHEDULE_REMINDER_MINUTES = 60
DEFAULT_CHARGE_POLICY = "smartevse_1_first"
DEFAULT_PUSH_CURRENTS = True
DEFAULT_PUSH_EV_METER = True
DEFAULT_PUSH_WLED = True
DEFAULT_VEHICLE_1_NAME = "Volvo XC40"
DEFAULT_VEHICLE_2_NAME = "Volvo EX30"
SMARTEVSE_1_NAME = "SmartEVSE 1"
SMARTEVSE_2_NAME = "SmartEVSE 2"
DEFAULT_WLED_LED_COUNT = 105
DEFAULT_WLED_LED_OFFSET = 11

ATTR_ACTIVE_SMARTEVSE = "active_smartevse"
ATTR_ACTIVE_SMARTEVSE_SINCE = "active_smartevse_since"
ATTR_CHARGE_ALLOWED = "charge_allowed"
ATTR_CHARGE_REASON = "charge_reason"
ATTR_CONTROLLER_ERROR = "controller_error"
ATTR_CONTROLLER_STATE = "controller_state"
ATTR_DUTY_CYCLE_REMAINING = "duty_cycle_remaining"
ATTR_LAST_CYCLE_REASON = "last_cycle_reason"
ATTR_LAST_EV_METER_PUSH = "last_ev_meter_push"
ATTR_LAST_METER_PUSH = "last_meter_push"
ATTR_LAST_NOTIFICATION = "last_notification"
ATTR_LAST_WLED_PUSH = "last_wled_push"
ATTR_MAINS_PEAK = "mains_peak"
ATTR_SCHEDULE_WINDOW_ACTIVE = "schedule_window_active"
ATTR_TIMER_REMAINING = "timer_remaining"
ATTR_TIMER_UNTIL = "timer_until"

CONF_CHARGE_POLICY_DEFAULT = "charge_policy_default"
CONF_CURRENTS_PUSH_INTERVAL = "currents_push_interval"
CONF_DUTY_CYCLE_MINUTES = "duty_cycle_minutes"
CONF_SMARTEVSE_1_BASE_URL = "smartevse_1_base_url"
CONF_SMARTEVSE_2_BASE_URL = "smartevse_2_base_url"
CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY = "ev_meter_export_active_energy_entity"
CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY = "ev_meter_import_active_energy_entity"
CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY = "ev_meter_import_active_power_entity"
CONF_EV_METER_L1_ENTITY = "ev_meter_l1_entity"
CONF_EV_METER_L2_ENTITY = "ev_meter_l2_entity"
CONF_EV_METER_L3_ENTITY = "ev_meter_l3_entity"
CONF_VEHICLE_1_BATTERY_ENTITY = "vehicle_1_battery_entity"
CONF_VEHICLE_1_CONNECTION_STATUS_ENTITY = "vehicle_1_connection_status_entity"
CONF_VEHICLE_2_BATTERY_ENTITY = "vehicle_2_battery_entity"
CONF_VEHICLE_2_CONNECTION_STATUS_ENTITY = "vehicle_2_connection_status_entity"
CONF_EV_METER_PUSH_INTERVAL = "ev_meter_push_interval"
CONF_MAINS_L1_ENTITY = "mains_l1_entity"
CONF_MAINS_L2_ENTITY = "mains_l2_entity"
CONF_MAINS_L3_ENTITY = "mains_l3_entity"
CONF_NOTIFY_ON_SCHEDULE_WINDOW = "notify_on_schedule_window"
CONF_SCHEDULE_NOTIFICATION_SERVICE = "schedule_notification_service"
CONF_SCHEDULE_NOTIFICATION_TARGETS = "schedule_notification_targets"
CONF_SCHEDULE_REMINDER_MINUTES = "schedule_reminder_minutes"
CONF_PRICE_SENSOR_ENTITY = "price_sensor_entity"
CONF_PUSH_CURRENTS = "push_currents"
CONF_PUSH_EV_METER = "push_ev_meter"
CONF_PUSH_WLED = "push_wled"
CONF_VEHICLE_1_NAME = "vehicle_1_name"
CONF_VEHICLE_2_NAME = "vehicle_2_name"
CONF_RECREATE_WLED_PRESETS = "recreate_wled_presets"
CONF_SCHEDULE_ENTITY = "schedule_entity"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_WLED_URL = "wled_url"
CONF_WLED_LED_COUNT = "wled_led_count"
CONF_WLED_LED_OFFSET = "wled_led_offset"
CONF_WLED_PRESETS_JSON = "wled_presets_json"
# Optional usable battery capacity per vehicle (kWh); 0 = automatic (Volvo
# capacity sensor, else the model default).
CONF_VEHICLE_1_BATTERY_CAPACITY_KWH = "vehicle_1_battery_capacity_kwh"
CONF_VEHICLE_2_BATTERY_CAPACITY_KWH = "vehicle_2_battery_capacity_kwh"
DEFAULT_BATTERY_CAPACITY_KWH = 0

SCHEDULE_NOTIFICATION_ID = f"{DOMAIN}_schedule_disabled"

# --- Intelligent charging ---------------------------------------------------
INTELLIGENT_STORAGE_KEY = f"{STORAGE_KEY}.{{entry_id}}.intelligent"
PRICE_INTEGRATION_DOMAIN = "real_electricity_price"
VEHICLE_INTEGRATION_DOMAIN = "volvo"
DEFAULT_READY_BY = "07:00"
DEFAULT_MINIMUM_CHARGE = 80
MINIMUM_CHARGE_MIN = 10
MINIMUM_CHARGE_MAX = 100
OTHER_VEHICLE_NAME = "Unidentified vehicle"
# Settings keys: a detected vehicle uses its own, an unmapped outlet "other".
INTELLIGENT_SETTINGS_KEYS = ("vehicle_1", "vehicle_2", "other")
INTELLIGENT_READY_BY_KEYS = {
    "vehicle_1": "vehicle_1_ready_by",
    "vehicle_2": "vehicle_2_ready_by",
    "other": "other_vehicle_ready_by",
}
INTELLIGENT_MINIMUM_KEYS = {
    "vehicle_1": "vehicle_1_minimum_charge",
    "vehicle_2": "vehicle_2_minimum_charge",
    "other": "other_vehicle_minimum_charge",
}

# Overall status (ENUM sensor options, in this order).
INTELLIGENT_STATUS_DISABLED = "disabled"
INTELLIGENT_STATUS_OVERRIDDEN = "overridden"
INTELLIGENT_STATUS_NO_EV = "no_ev"
INTELLIGENT_STATUS_IDENTIFYING = "identifying"
INTELLIGENT_STATUS_PLANNED = "planned"
INTELLIGENT_STATUS_CHARGING = "charging"
INTELLIGENT_STATUS_REACHED = "reached"
INTELLIGENT_STATUS_AT_RISK = "at_risk"
INTELLIGENT_STATUS_NOT_ACCEPTING = "not_accepting"
INTELLIGENT_STATUS_NO_PRICE_DATA = "no_price_data"
INTELLIGENT_STATUSES = (
    INTELLIGENT_STATUS_DISABLED,
    INTELLIGENT_STATUS_OVERRIDDEN,
    INTELLIGENT_STATUS_NO_EV,
    INTELLIGENT_STATUS_IDENTIFYING,
    INTELLIGENT_STATUS_PLANNED,
    INTELLIGENT_STATUS_CHARGING,
    INTELLIGENT_STATUS_REACHED,
    INTELLIGENT_STATUS_AT_RISK,
    INTELLIGENT_STATUS_NOT_ACCEPTING,
    INTELLIGENT_STATUS_NO_PRICE_DATA,
)

# Stable charge reasons (never change every cycle).
REASON_INTELLIGENT_CHARGING = "intelligent_charging"
REASON_INTELLIGENT_TOP_UP = "intelligent_top_up"
REASON_INTELLIGENT_CATCH_UP = "intelligent_catch_up"
REASON_INTELLIGENT_NO_PRICE_DATA = "intelligent_no_price_data"
REASON_INTELLIGENT_WAITING = "intelligent_waiting_for_planned_slot"
REASON_INTELLIGENT_TARGETS_REACHED = "intelligent_targets_reached"
REASON_INTELLIGENT_NO_EV = "intelligent_waiting_for_connected_ev"
REASON_INTELLIGENT_IDENTIFYING = "intelligent_identifying_vehicle"
REASON_INTELLIGENT_OUTLET_UNAVAILABLE = "intelligent_planned_outlet_unavailable"
# Nothing left to plan although a car is below its minimum: it does not draw
# (§6.4), or its session completed below the target (retries used, unknown SoC).
REASON_INTELLIGENT_NOT_ACCEPTING = "intelligent_vehicle_not_accepting"
REASON_INTELLIGENT_CHARGING_COMPLETE = "intelligent_charging_complete"
# The only connected car is on the outlet a "SmartEVSE N only" policy excludes
# (the controller's existing reason for that policy, N = the selected outlet).
REASON_ONLY_WAITING_FOR_SELECTED_EV = "{smartevse_key}_only_waiting_for_selected_ev"
INTELLIGENT_ALLOWED_REASONS = frozenset({
    REASON_INTELLIGENT_CHARGING,
    REASON_INTELLIGENT_TOP_UP,
    REASON_INTELLIGENT_CATCH_UP,
    REASON_INTELLIGENT_NO_PRICE_DATA,
})

# Warning-level controller_error values (non-blocking).
ERROR_INTELLIGENT_PRICE_DATA_UNAVAILABLE = "intelligent_price_data_unavailable"
ERROR_INTELLIGENT_PLANNER_FAILED = "intelligent_planner_failed"
ERROR_INTELLIGENT_MODEL_UNAVAILABLE = "intelligent_model_unavailable"
ERROR_VEHICLE_TIMER_BLOCKS_CHARGING = "{smartevse_key}_vehicle_timer_blocks_charging"


class ChargePolicy(StrEnum):
    """Which SmartEVSE should start charging first."""

    SMARTEVSE_1_FIRST = "smartevse_1_first"
    SMARTEVSE_2_FIRST = "smartevse_2_first"
    SMARTEVSE_1_ONLY = "smartevse_1_only"
    SMARTEVSE_2_ONLY = "smartevse_2_only"


class ControllerState(StrEnum):
    """High-level controller state."""

    IDLE = "idle"
    SCHEDULE = "schedule"
    PRICE = "price"
    FORCE = "force"
    TIMER = "timer"
    BLOCKED = "blocked"
    INTELLIGENT = "intelligent"
