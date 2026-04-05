"""Constants for the SmartEVSE Dual Charger integration."""

from __future__ import annotations

from enum import StrEnum
import logging

from homeassistant.const import Platform

DOMAIN = "smartevse_dual_charger"
NAME = "SmartEVSE Dual Charger"
VERSION = "0.0.7.1"

LOGGER = logging.getLogger(__package__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
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
DEFAULT_CHARGE_POLICY = "smartevse_1_first"
DEFAULT_PUSH_CURRENTS = True
DEFAULT_PUSH_EV_METER = True
DEFAULT_PUSH_WLED = True
DEFAULT_SMARTEVSE_1_NAME = "Volvo XC40"
DEFAULT_SMARTEVSE_2_NAME = "Volvo EX30"
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
CONF_SMARTEVSE_1_BATTERY_ENTITY = "smartevse_1_battery_entity"
CONF_SMARTEVSE_2_BATTERY_ENTITY = "smartevse_2_battery_entity"
CONF_EV_METER_PUSH_INTERVAL = "ev_meter_push_interval"
CONF_MAINS_L1_ENTITY = "mains_l1_entity"
CONF_MAINS_L2_ENTITY = "mains_l2_entity"
CONF_MAINS_L3_ENTITY = "mains_l3_entity"
CONF_NOTIFY_ON_SCHEDULE_WINDOW = "notify_on_schedule_window"
CONF_PRICE_SENSOR_ENTITY = "price_sensor_entity"
CONF_PUSH_CURRENTS = "push_currents"
CONF_PUSH_EV_METER = "push_ev_meter"
CONF_PUSH_WLED = "push_wled"
CONF_SMARTEVSE_1_NAME = "smartevse_1_name"
CONF_SMARTEVSE_2_NAME = "smartevse_2_name"
CONF_RECREATE_WLED_PRESETS = "recreate_wled_presets"
CONF_SCHEDULE_ENTITY = "schedule_entity"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_WLED_URL = "wled_url"
CONF_WLED_LED_COUNT = "wled_led_count"
CONF_WLED_LED_OFFSET = "wled_led_offset"
CONF_WLED_PRESETS_JSON = "wled_presets_json"

SCHEDULE_NOTIFICATION_ID = f"{DOMAIN}_schedule_disabled"


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
