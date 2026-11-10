"""Intelligent charging inside the controller (spec §5, §6).

Controllers are built with __new__ (no network): charger I/O is mocked, entity
states are native homeassistant.core.State objects, and the real Intelligent
adapter, planner and model run against a fake store. Times are Europe/Tallinn.
"""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, time, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from homeassistant.core import CoreState, HomeAssistant, State
from homeassistant.helpers import frame
from homeassistant.util import dt as dt_util

import custom_components.smartevse_dual_charger as integration
from custom_components.smartevse_dual_charger import controller as controller_module
from custom_components.smartevse_dual_charger import intelligent as intelligent_module
from custom_components.smartevse_dual_charger.charging_cost import ChargingLedger
from custom_components.smartevse_dual_charger.const import ControllerState
from custom_components.smartevse_dual_charger.controller import (
    INTELLIGENT_OUTLET_DEFAULTS,
    MUTABLE_DEFAULTS,
    SmartEVSEDualChargerController,
    SmartEVSEStatus,
)
from custom_components.smartevse_dual_charger.coordinator import SmartEVSEDualChargerCoordinator
from custom_components.smartevse_dual_charger.intelligent import (
    IntelligentCharging,
    IntelligentDecision,
    ObserveResult,
)

TZ = ZoneInfo("Europe/Tallinn")
URL_1 = "http://evse1.invalid"
URL_2 = "http://evse2.invalid"
PRICE_ENTITY = "sensor.real_electricity_price_current_price"
XC40_BATTERY = "sensor.volvo_xc40_battery"
EX30_BATTERY = "sensor.volvo_ex30_battery"
XC40_CONNECTION = "sensor.volvo_xc40_charging_connection_status"
XC40_CHARGING = "sensor.volvo_xc40_charging_status"
EX30_CONNECTION = "sensor.volvo_ex30_charging_connection_status"
EX30_CHARGING = "sensor.volvo_ex30_charging_status"
PAUSE = "Charging Stopped - No Power Available"


def L(month, day, hour, minute=0, second=0, year=2026):
    """Tallinn wall-clock time as an aware UTC datetime."""
    return datetime(year, month, day, hour, minute, second, tzinfo=TZ).astimezone(UTC)


def price_list(day, cheap_hours):
    """One Real Electricity Price day list (01:00 -> 01:00 local): 0.40 except cheap hours 0.20."""
    items = []
    for offset in range(24):
        start = datetime(2026, 11, day, 1, tzinfo=TZ) + timedelta(hours=offset)
        price = 0.20 if start.hour in cheap_hours else 0.40
        items.append({"start_time": start.isoformat(), "end_time": (start + timedelta(hours=1)).isoformat(),
                      "nord_pool_price": None, "actual_price": price, "tariff": "off_peak",
                      "is_holiday": False, "is_weekend": False})
    return items


def put(states, entity_id, value, at=None, **attributes):
    at = at or L(11, 9, 17)
    states[entity_id] = State(entity_id, str(value), attributes, last_changed=at, last_reported=at, last_updated=at)


def base_states():
    states = {}
    put(states, PRICE_ENTITY, 0.40, unit_of_measurement="€/kWh")
    put(states, "sensor.real_electricity_price_hourly_prices_yesterday", 0.3,
        hourly_prices=price_list(8, {1, 2, 3}), unit_of_measurement="€/kWh")
    put(states, "sensor.real_electricity_price_hourly_prices_today", 0.3,
        hourly_prices=price_list(9, {1, 2, 3}), unit_of_measurement="€/kWh")
    put(states, "sensor.real_electricity_price_hourly_prices_tomorrow", 0.3,
        hourly_prices=price_list(10, {1, 2, 3}), unit_of_measurement="€/kWh")
    for phase in "abc":
        put(states, f"sensor.mains_{phase}", 3.0, unit_of_measurement="A")
    for phase in "123":
        put(states, f"sensor.ev_l{phase}", 0.0, unit_of_measurement="A")
    put(states, "sensor.ev_power_total", 3, unit_of_measurement="W")
    put(states, XC40_BATTERY, 53.0, at=L(11, 9, 17, 54), unit_of_measurement="%")
    put(states, "sensor.volvo_xc40_battery_capacity", 78.182, unit_of_measurement="kWh")
    put(states, "sensor.volvo_xc40_target_battery_charge_level", 90, unit_of_measurement="%")
    put(states, XC40_CONNECTION, "connected", at=L(11, 9, 17, 57))
    put(states, XC40_CHARGING, "idle", at=L(11, 9, 17, 57))
    put(states, EX30_BATTERY, 68.0, at=L(11, 9, 17, 54), unit_of_measurement="%")
    put(states, "sensor.volvo_ex30_battery_capacity", 69.0, unit_of_measurement="kWh")
    put(states, "sensor.volvo_ex30_target_battery_charge_level", 100, unit_of_measurement="%")
    put(states, EX30_CONNECTION, "disconnected", at=L(11, 9, 12))
    put(states, EX30_CHARGING, "idle", at=L(11, 9, 12))
    return states


ENTRY = {
    "entry_id": "test",
    "smartevse_1_base_url": URL_1, "smartevse_2_base_url": URL_2,
    "price_sensor_entity": PRICE_ENTITY, "schedule_entity": "schedule.charge_schedule",
    "mains_l1_entity": "sensor.mains_a", "mains_l2_entity": "sensor.mains_b", "mains_l3_entity": "sensor.mains_c",
    "ev_meter_l1_entity": "sensor.ev_l1", "ev_meter_l2_entity": "sensor.ev_l2", "ev_meter_l3_entity": "sensor.ev_l3",
    "ev_meter_import_active_power_entity": "sensor.ev_power_total",
    "vehicle_1_battery_entity": XC40_BATTERY, "vehicle_2_battery_entity": EX30_BATTERY,
    "vehicle_1_connection_status_entity": XC40_CONNECTION,
    "vehicle_2_connection_status_entity": EX30_CONNECTION,
}


def fake_store():
    return SimpleNamespace(async_load=AsyncMock(return_value=None), async_save=AsyncMock(),
                           make_read_only=MagicMock())


def make_controller(states=None, *, adapter=True, **mutable):
    states = base_states() if states is None else states
    controller = SmartEVSEDualChargerController.__new__(SmartEVSEDualChargerController)
    controller.hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    controller._entry_data = dict(ENTRY)
    controller._options = {"push_currents": False, "push_wled": False}
    controller._mutable = {**deepcopy(MUTABLE_DEFAULTS), **mutable}
    controller._endpoint_failures = {}
    controller._store = SimpleNamespace(async_save=AsyncMock())
    controller._cost_ledger = ChargingLedger()
    controller._cost_store = SimpleNamespace(async_save=AsyncMock())
    controller._cost_error = None
    controller._schedule_reminders = MagicMock()
    controller._intelligent = (IntelligentCharging(controller.hass, controller._entry_data, controller._options,
                                                   store=fake_store()) if adapter else None)
    controller._normalize_intelligent_settings()
    controller._async_post = AsyncMock(return_value=True)
    controller.states = states
    return controller


def status(key, *, connected=True, state=None, mode="Off", available=True, raw_state=None):
    if not available:
        return SmartEVSEStatus(key=key, base_url=URL_1 if key == "smartevse_1" else URL_2, available=False,
                               connected=False, plug_state="Unavailable", state="Unavailable", mode="Unavailable",
                               charge_current=0.0, max_current=0.0, override_current=0.0, error="API unavailable")
    state = state or ("Connected to EV" if connected else "Disconnected")
    return SmartEVSEStatus(key=key, base_url=URL_1 if key == "smartevse_1" else URL_2, available=True,
                           connected=connected, plug_state="Connected" if connected else "Disconnected", state=state,
                           mode=mode, charge_current=16.0, max_current=16.0, override_current=0.0, error="None",
                           max_mains=16.0, min_current=6.0, mode_id={"Off": 0, "Smart": 3}.get(mode),
                           raw_state=state if raw_state is None else raw_state)


class Harness:
    """Drive full controller cycles with polled statuses and a patched clock."""

    def __init__(self, controller, **statuses):
        self.c = controller
        self.statuses = {"smartevse_1": status("smartevse_1", connected=False),
                         "smartevse_2": status("smartevse_2", connected=False), **statuses}

        async def fetch(key, url):
            return self.statuses[key]

        controller._async_fetch_status = fetch

    async def cycle(self, now):
        with patch.object(dt_util, "utcnow", return_value=now), \
                patch.object(dt_util, "DEFAULT_TIME_ZONE", TZ), \
                patch.object(controller_module.persistent_notification, "async_create") as create, \
                patch.object(controller_module.persistent_notification, "async_dismiss"):
            data = await self.c.async_run_cycle(reason="test")
        self.create = create
        return data

    def posts(self):
        return [(call.args[0], call.kwargs.get("params")) for call in self.c._async_post.await_args_list]


def facts(**values):
    """Minimal CarFacts stand-in for the controller's per-outlet update."""
    defaults = {"settings_key": "vehicle_1", "vehicle": "vehicle_1", "soc": 53.0, "soc_source": "vehicle",
                "target": 80.0, "charging": False, "live_soc": None, "live_changed_at": None}
    return SimpleNamespace(**{**defaults, **values})


class TzTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.object(dt_util, "DEFAULT_TIME_ZONE", TZ)
        patcher.start()
        self.addCleanup(patcher.stop)


class PrecedenceTests(unittest.TestCase):
    def gate(self, controller, decision=None, *, price=0.30, window=True):
        return controller._determine_charge_allowed(price_value=price, schedule_window_active=window,
                                                    intelligent=decision)

    def test_force_then_legacy_then_intelligent_then_schedule(self):
        charging = IntelligentDecision(allowed=True, reason="intelligent_charging", outlet="smartevse_1",
                                       block_start=L(11, 10, 1))
        c = make_controller(adapter=False, intelligent_enabled=True, schedule_enabled=True)
        self.assertEqual(self.gate(c, charging), (True, ControllerState.INTELLIGENT, "intelligent_charging"))
        c._mutable["force_charge"] = True
        self.assertEqual(self.gate(c, charging), (True, ControllerState.FORCE, "force_charge"))
        c._mutable.update(force_charge=False, force_price=True, acceptable_price=0.10)
        self.assertEqual(self.gate(c, charging), (False, ControllerState.IDLE, "waiting_for_acceptable_price"))
        c._mutable["force_price"] = False
        c._mutable["intelligent_enabled"] = False
        self.assertEqual(self.gate(c, charging), (True, ControllerState.SCHEDULE, "schedule"))

    def test_intelligent_waits_without_a_decision_or_outlet(self):
        c = make_controller(adapter=False, intelligent_enabled=True, schedule_enabled=True)
        self.assertEqual(self.gate(c), (False, ControllerState.INTELLIGENT, "intelligent_waiting_for_planned_slot"))
        broken = IntelligentDecision(allowed=True, reason="intelligent_charging", outlet=None)
        self.assertEqual(self.gate(c, broken),
                         (False, ControllerState.INTELLIGENT, "intelligent_waiting_for_planned_slot"))
        waiting = IntelligentDecision(allowed=False, reason="intelligent_targets_reached")
        self.assertEqual(self.gate(c, waiting), (False, ControllerState.INTELLIGENT, "intelligent_targets_reached"))

    def test_schedule_reminders_treat_intelligent_as_covering_the_schedule(self):
        hass = MagicMock()
        with patch.object(controller_module, "async_get_clientsession"), \
                patch.object(controller_module, "Store"), patch.object(controller_module, "StrictStore"), \
                patch.object(controller_module, "IntelligentCharging"), \
                patch.object(controller_module, "ScheduleReminders") as reminders:
            controller = SmartEVSEDualChargerController(hass, dict(ENTRY), {})
        schedule_covered = reminders.call_args.args[4]
        controller._mutable = {"schedule_enabled": False, "intelligent_enabled": False}
        self.assertFalse(schedule_covered())
        controller._mutable["intelligent_enabled"] = True
        self.assertTrue(schedule_covered())


class DeadlineTests(TzTestCase):
    def controller(self, **mutable):
        c = make_controller(**mutable)
        c._mutable.update(intelligent_enabled=True, smartevse_1_last_connected=True,
                          smartevse_1_connected_ev="vehicle_1")
        return c

    def update(self, c, now, car, *, state=None):
        c._update_intelligent_outlets(now=now, statuses={"smartevse_1": status("smartevse_1", state=state)},
                                      facts={"smartevse_1": car})

    def test_deadline_is_the_next_local_ready_by_after_plug_in(self):
        c = self.controller(smartevse_1_intelligent_plugged_at=L(11, 9, 17, 56).isoformat())
        self.update(c, L(11, 9, 17, 57), facts())
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline"], L(11, 10, 7).isoformat())
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline_vehicle"], "vehicle_1")

    def test_late_plug_in_is_planned_for_the_next_occurrence_without_overrun(self):
        c = self.controller(smartevse_1_intelligent_plugged_at=L(11, 10, 6, 20).isoformat())
        self.update(c, L(11, 10, 6, 21), facts(soc=40.0))
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline"], L(11, 11, 7).isoformat())
        self.update(c, L(11, 10, 7, 0, 5), facts(soc=45.0))
        self.assertFalse(c._mutable["smartevse_1_intelligent_overrun"])
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline"], L(11, 11, 7).isoformat())

    def test_roll_sets_overrun_only_below_the_minimum_after_an_early_plug_in(self):
        c = self.controller(smartevse_1_intelligent_plugged_at=L(11, 9, 22).isoformat())
        self.update(c, L(11, 9, 22, 1), facts(soc=40.0))
        self.update(c, L(11, 10, 7, 0, 5), facts(soc=70.0))
        self.assertTrue(c._mutable["smartevse_1_intelligent_overrun"])
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline"], L(11, 11, 7).isoformat())
        # Overrun clears once the minimum is reached.
        self.update(c, L(11, 10, 8), facts(soc=80.0, charging=True))
        self.assertFalse(c._mutable["smartevse_1_intelligent_overrun"])
        self.assertTrue(c._mutable["smartevse_1_intelligent_reached"])

        reached = self.controller(smartevse_1_intelligent_plugged_at=L(11, 9, 22).isoformat())
        self.update(reached, L(11, 9, 22, 1), facts(soc=85.0))
        self.update(reached, L(11, 10, 7, 0, 5), facts(soc=85.0))
        self.assertFalse(reached._mutable["smartevse_1_intelligent_overrun"])

    def test_unknown_soc_overruns_unless_the_session_completed(self):
        c = self.controller(smartevse_1_intelligent_plugged_at=L(11, 9, 22).isoformat())
        self.update(c, L(11, 9, 22, 1), facts(soc=None, soc_source="unknown"))
        self.update(c, L(11, 10, 7, 0, 5), facts(soc=None, soc_source="unknown"))
        self.assertTrue(c._mutable["smartevse_1_intelligent_overrun"])
        done = self.controller(smartevse_1_intelligent_plugged_at=L(11, 9, 22).isoformat(),
                               smartevse_1_intelligent_complete=True)
        self.update(done, L(11, 9, 22, 1), facts(soc=None, soc_source="unknown"))
        self.update(done, L(11, 10, 7, 0, 5), facts(soc=None, soc_source="unknown"))
        self.assertFalse(done._mutable["smartevse_1_intelligent_overrun"])

    def test_roll_keeps_local_time_across_spring_forward(self):
        c = self.controller(smartevse_1_intelligent_plugged_at=L(3, 26, 22, year=2027).isoformat())
        self.update(c, L(3, 26, 22, 1, year=2027), facts())
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline"], L(3, 27, 7, year=2027).isoformat())
        self.update(c, L(3, 27, 7, 0, 10, year=2027), facts(soc=85.0))
        rolled = datetime.fromisoformat(c._mutable["smartevse_1_intelligent_deadline"])
        self.assertEqual(rolled.astimezone(TZ).replace(tzinfo=None), datetime(2027, 3, 28, 7, 0))
        self.assertEqual(rolled, datetime(2027, 3, 28, 4, 0, tzinfo=UTC))  # EEST, not 08:00 local

    def test_mapping_change_recomputes_the_deadline_and_voids_refusal(self):
        c = self.controller(smartevse_1_intelligent_plugged_at=L(11, 9, 22).isoformat(),
                            vehicle_2_ready_by="06:00")
        c._mutable["smartevse_1_connected_ev"] = "unknown"
        self.update(c, L(11, 9, 22, 1), facts(settings_key="other", vehicle=None, soc=None, soc_source="unknown"))
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline_vehicle"], "other")
        c._mutable.update(smartevse_1_intelligent_probes=2,
                          smartevse_1_intelligent_not_accepting_until=L(11, 9, 23).isoformat())
        self.update(c, L(11, 9, 22, 5), facts(settings_key="vehicle_2", vehicle="vehicle_2"))
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline"], L(11, 10, 6).isoformat())
        self.assertEqual(c._mutable["smartevse_1_intelligent_probes"], 0)
        self.assertIsNone(c._mutable["smartevse_1_intelligent_not_accepting_until"])

    def test_reached_is_never_set_from_a_rise_seen_while_the_outlet_was_not_charging(self):
        c = self.controller(smartevse_1_intelligent_plugged_at=L(11, 9, 22).isoformat(),
                            vehicle_1_last_soc=78.0)
        self.update(c, L(11, 10, 1), facts(soc=81.0, live_soc=81.0, charging=False))
        self.assertFalse(c._mutable["smartevse_1_intelligent_reached"])
        self.assertEqual(c._mutable["vehicle_1_last_soc"], 81.0)
        self.update(c, L(11, 10, 1, 1), facts(soc=81.0, live_soc=81.0, charging=False))
        self.assertTrue(c._mutable["smartevse_1_intelligent_reached"])  # no rise: a real reading
        # Hysteresis: plan again only below target - 2.
        self.update(c, L(11, 10, 2), facts(soc=78.5, live_soc=78.5))
        self.assertTrue(c._mutable["smartevse_1_intelligent_reached"])
        self.update(c, L(11, 10, 3), facts(soc=77.5, live_soc=77.5))
        self.assertFalse(c._mutable["smartevse_1_intelligent_reached"])


class SettingsTests(TzTestCase):
    def controller(self):
        c = make_controller(intelligent_enabled=True, smartevse_1_last_connected=True,
                            smartevse_1_connected_ev="vehicle_1", smartevse_2_last_connected=True)
        for key, deadline in (("smartevse_1", L(11, 10, 7)), ("smartevse_2", L(11, 10, 7))):
            c._mutable.update({
                f"{key}_intelligent_plugged_at": L(11, 9, 22).isoformat(),
                f"{key}_intelligent_deadline": deadline.isoformat(),
                f"{key}_intelligent_deadline_vehicle": "vehicle_1" if key == "smartevse_1" else "other",
                f"{key}_intelligent_overrun": True,
                f"{key}_intelligent_complete": True,
                f"{key}_intelligent_probes": 1,
            })
        return c

    async def set_ready_by(self, c, key, value, now):
        with patch.object(dt_util, "utcnow", return_value=now):
            await c.async_set_vehicle_ready_by(key, value)

    async def test_ready_by_edit_within_two_hours_keeps_tonights_deadline(self):
        # A time that just passed (05:00 at 05:30) would roll to tomorrow: tonight's 07:00
        # stays. (Before review round 3 this used 06:00, an earlier time still ahead,
        # which must apply now — see the next test.)
        c = self.controller()
        await self.set_ready_by(c, "vehicle_1", "05:00", L(11, 10, 5, 30))
        self.assertEqual(c._mutable["vehicle_1_ready_by"], "05:00")
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline"], L(11, 10, 7).isoformat())
        # §6.2: overrun clears only when the minimum is reached, on unplug or off/on.
        self.assertTrue(c._mutable["smartevse_1_intelligent_overrun"])
        self.assertFalse(c._mutable["smartevse_1_intelligent_complete"])
        self.assertEqual(c._mutable["smartevse_1_intelligent_probes"], 0)
        self.assertEqual(c._mutable["smartevse_1_intelligent_plugged_at"], L(11, 9, 22).isoformat())
        # The other outlet (unidentified car, "other" settings) is untouched.
        self.assertTrue(c._mutable["smartevse_2_intelligent_overrun"])
        c._store.async_save.assert_awaited()

    async def test_earlier_ready_by_within_two_hours_applies_tonight(self):
        # Review round 3: at 05:10 the owner must leave at 06:00 instead of 07:00.
        c = self.controller()
        await self.set_ready_by(c, "vehicle_1", "06:00", L(11, 10, 5, 10))
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline"], L(11, 10, 6).isoformat())
        # A later time within the 2 h keeps tonight's deadline (applies from the next roll).
        c = self.controller()
        await self.set_ready_by(c, "vehicle_1", "08:00", L(11, 10, 5, 10))
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline"], L(11, 10, 7).isoformat())

    async def test_ready_by_edit_further_away_recomputes_the_deadline(self):
        c = self.controller()
        await self.set_ready_by(c, "vehicle_1", time(6, 15, 30), L(11, 9, 22, 30))
        self.assertEqual(c._mutable["vehicle_1_ready_by"], "06:15")
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline"], L(11, 10, 6, 15).isoformat())

    async def test_other_vehicle_settings_apply_to_the_unmapped_outlet(self):
        c = self.controller()
        await c.async_set_vehicle_minimum_charge("other", 64.6)
        self.assertEqual(c._mutable["other_vehicle_minimum_charge"], 65)
        self.assertFalse(c._mutable["smartevse_2_intelligent_complete"])
        self.assertEqual(c._mutable["smartevse_2_intelligent_probes"], 0)
        self.assertTrue(c._mutable["smartevse_2_intelligent_overrun"])  # §6.2: kept
        self.assertEqual(c._mutable["smartevse_2_intelligent_deadline"], L(11, 10, 7).isoformat())
        self.assertTrue(c._mutable["smartevse_1_intelligent_complete"])  # the XC40's outlet is untouched
        await c.async_set_vehicle_minimum_charge("vehicle_2", 3)
        self.assertEqual(c._mutable["vehicle_2_minimum_charge"], 10)

    async def test_invalid_keys_and_times_are_rejected(self):
        c = self.controller()
        with self.assertRaises(ValueError):
            await c.async_set_vehicle_ready_by("vehicle_3", "07:00")
        with self.assertRaises(ValueError):
            await c.async_set_vehicle_ready_by("vehicle_1", "25:61")
        with self.assertRaises(ValueError):
            await c.async_set_vehicle_minimum_charge("garage", 80)
        # The top-up switch and price are gone since 0.0.10.0 (extra charging is automatic).
        self.assertFalse(hasattr(c, "async_set_intelligent_top_up"))
        self.assertFalse(hasattr(c, "async_set_intelligent_top_up_price"))

    async def test_switch_setter_saves_and_resets(self):
        c = self.controller()
        c._mutable.update(smartevse_1_intelligent_committed_until=L(11, 10, 1, 15).isoformat(),
                          active_smartevse="smartevse_1", vehicle_1_last_soc=40.0)
        await c.async_set_intelligent_enabled(True)  # already on: nothing is reset
        self.assertTrue(c._mutable["smartevse_1_intelligent_overrun"])
        self.assertEqual(c._mutable["active_smartevse"], "smartevse_1")
        await c.async_set_intelligent_enabled(False)
        self.assertFalse(c._mutable["intelligent_enabled"])
        await c.async_set_intelligent_enabled(True)
        self.assertIsNone(c._mutable["vehicle_1_last_soc"])
        self.assertIsNone(c._mutable["smartevse_1_intelligent_deadline"])  # recomputed next cycle
        self.assertFalse(c._mutable["smartevse_1_intelligent_overrun"])
        self.assertIsNone(c._mutable["smartevse_1_intelligent_committed_until"])
        self.assertEqual(c._mutable["smartevse_1_intelligent_plugged_at"], L(11, 9, 22).isoformat())
        self.assertIsNone(c._mutable["active_smartevse"])
        self.assertEqual(c._store.async_save.await_count, 3)

    async def test_initialisation_normalises_stored_settings(self):
        c = make_controller()
        stored = {"vehicle_1_ready_by": "7:0", "vehicle_2_ready_by": "", "other_vehicle_ready_by": None,
                  "vehicle_1_minimum_charge": 150, "vehicle_2_minimum_charge": "x", "acceptable_price": 0.16,
                  "intelligent_enabled": 1, "smartevse_1_intelligent_retries": "two",
                  "smartevse_1_intelligent_deadline": "garbage", "vehicle_1_last_soc": 120}
        c._store = SimpleNamespace(async_load=AsyncMock(return_value=stored), async_save=AsyncMock())
        c._cost_store = SimpleNamespace(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
        c._schedule_reminders = SimpleNamespace(async_initialize=AsyncMock())
        await c.async_initialize()
        m = c._mutable
        self.assertEqual((m["vehicle_1_ready_by"], m["vehicle_2_ready_by"], m["other_vehicle_ready_by"]),
                         ("07:00", "07:00", "07:00"))
        self.assertEqual((m["vehicle_1_minimum_charge"], m["vehicle_2_minimum_charge"]), (100, 80))
        self.assertEqual(m["intelligent_top_up_price"], 0.16)  # copied from acceptable_price the first time
        self.assertIs(m["intelligent_enabled"], True)
        self.assertEqual(m["smartevse_1_intelligent_retries"], 0)
        self.assertIsNone(m["smartevse_1_intelligent_deadline"])
        self.assertIsNone(m["vehicle_1_last_soc"])
        c._intelligent._store.async_load.assert_awaited_once()


class ClearStateTests(TzTestCase):
    def loaded(self):
        c = make_controller()
        for key in ("smartevse_1", "smartevse_2"):
            c._mutable.update({
                f"{key}_intelligent_plugged_at": L(11, 9, 22).isoformat(),
                f"{key}_intelligent_deadline": L(11, 10, 7).isoformat(),
                f"{key}_intelligent_deadline_vehicle": "vehicle_1" if key == "smartevse_1" else "vehicle_2",
                f"{key}_intelligent_overrun": True,
                f"{key}_intelligent_complete": True,
                f"{key}_intelligent_retries": 1,
                f"{key}_intelligent_committed_until": L(11, 10, 1, 15).isoformat(),
                f"{key}_last_pause_at": L(11, 10, 1).isoformat(),
            })
        c._mutable.update(vehicle_1_last_soc=60.0, vehicle_1_last_soc_at=L(11, 10, 1).isoformat())
        return c

    def test_intelligent_state_survives_session_force_and_cycle_resets(self):
        c = self.loaded()
        before = {k: v for k, v in c._mutable.items() if "_intelligent_" in k}
        c._clear_session_tracking()
        c._clear_force_modes()
        c._reset_charge_cycle()
        c._clear_smartevse_session_tracking("smartevse_1")
        self.assertEqual({k: v for k, v in c._mutable.items() if "_intelligent_" in k}, before)

    def test_observed_unplug_clears_but_an_api_blip_does_not(self):
        c = self.loaded()
        c._update_session_tracking(status("smartevse_1", available=False), now=L(11, 10, 2), ev_meter_power=None)
        self.assertTrue(c._mutable["smartevse_1_intelligent_overrun"])
        self.assertEqual(c._mutable["vehicle_1_last_soc"], 60.0)
        c._update_session_tracking(status("smartevse_1", connected=False), now=L(11, 10, 2), ev_meter_power=None)
        for name, default in INTELLIGENT_OUTLET_DEFAULTS.items():
            self.assertEqual(c._mutable[f"smartevse_1_{name}"], default, name)
        self.assertIsNone(c._mutable["smartevse_1_last_pause_at"])
        self.assertIsNone(c._mutable["vehicle_1_last_soc"])  # current plug session only
        self.assertTrue(c._mutable["smartevse_2_intelligent_overrun"])

    async def test_reset_sessions_clears_both_outlets(self):
        c = self.loaded()
        await c.async_reset_sessions()
        for key in ("smartevse_1", "smartevse_2"):
            for name, default in INTELLIGENT_OUTLET_DEFAULTS.items():
                self.assertEqual(c._mutable[f"{key}_{name}"], default)

    def test_settings_edit_keeps_the_session_and_deadline(self):
        c = self.loaded()
        c._clear_intelligent_outlet_state("smartevse_1", keep_session=True)
        self.assertEqual(c._mutable["smartevse_1_intelligent_plugged_at"], L(11, 9, 22).isoformat())
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline"], L(11, 10, 7).isoformat())
        self.assertFalse(c._mutable["smartevse_1_intelligent_overrun"])
        self.assertEqual(c._mutable["smartevse_1_intelligent_retries"], 0)
        self.assertEqual(c._mutable["vehicle_1_last_soc"], 60.0)


class CompletionTests(TzTestCase):
    def controller(self, **mutable):
        c = make_controller(intelligent_enabled=True, smartevse_1_last_connected=True,
                            smartevse_1_connected_ev="vehicle_1", **mutable)
        c._mutable.update(smartevse_1_intelligent_deadline=L(11, 10, 7).isoformat(),
                          smartevse_1_intelligent_deadline_vehicle="vehicle_1",
                          smartevse_1_intelligent_plugged_at=L(11, 9, 22).isoformat())
        return c

    def plan_inputs(self, c, now, car):
        return c._intelligent_plan_inputs(now=now, statuses={"smartevse_1": status("smartevse_1")},
                                          facts={"smartevse_1": car})

    def test_session_complete_while_enabled_marks_intelligent_complete(self):
        c = self.controller(smartevse_1_session_complete=True)
        c._update_intelligent_outlets(now=L(11, 10, 1), statuses={"smartevse_1": status("smartevse_1")},
                                      facts={"smartevse_1": facts(soc=None, soc_source="unknown")})
        self.assertTrue(c._mutable["smartevse_1_intelligent_complete"])
        self.assertEqual(c._mutable["smartevse_1_intelligent_complete_at"], L(11, 10, 1).isoformat())
        self.assertTrue(self.plan_inputs(c, L(11, 10, 2), facts(soc=None, soc_source="unknown"))["smartevse_1"].done)

    def test_retry_rule(self):
        c = self.controller(smartevse_1_intelligent_complete=True,
                            smartevse_1_intelligent_complete_at=L(11, 10, 1).isoformat())
        low = facts(soc=60.0)
        self.assertTrue(self.plan_inputs(c, L(11, 10, 1, 20), low)["smartevse_1"].done)  # < 30 min
        self.assertFalse(self.plan_inputs(c, L(11, 10, 1, 31), low)["smartevse_1"].done)
        self.assertTrue(self.plan_inputs(c, L(11, 10, 1, 31), facts(soc=79.5))["smartevse_1"].done)  # >= target - 1
        self.assertTrue(self.plan_inputs(c, L(11, 10, 1, 31), facts(soc=None, soc_source="unknown"))
                        ["smartevse_1"].done)
        c._mutable["smartevse_1_intelligent_retries"] = 2
        self.assertTrue(self.plan_inputs(c, L(11, 10, 1, 31), low)["smartevse_1"].done)

    def test_activating_a_retry_clears_only_that_outlets_tracking_and_counts_it(self):
        c = self.controller(smartevse_1_intelligent_complete=True, smartevse_1_session_complete=True,
                            smartevse_2_session_complete=True,
                            smartevse_1_intelligent_complete_at=L(11, 10, 1).isoformat())
        now = L(11, 10, 1, 31)
        result = c._resolve_active_smartevse(now=now, smartevse_1=status("smartevse_1"),
                                             smartevse_2=status("smartevse_2"), intelligent_outlet="smartevse_1",
                                             intelligent_block_start=L(11, 10, 1, 30))
        self.assertEqual(result, ("smartevse_1", L(11, 10, 1, 30).isoformat(), 0, ""))
        self.assertFalse(c._mutable["smartevse_1_session_complete"])
        self.assertTrue(c._mutable["smartevse_2_session_complete"])
        self.assertFalse(c._mutable["smartevse_1_intelligent_complete"])
        self.assertEqual(c._mutable["smartevse_1_intelligent_retries"], 1)

    async def test_force_and_policy_setters_do_not_clear_intelligent_complete(self):
        c = self.controller(smartevse_1_intelligent_complete=True, smartevse_1_session_complete=True)
        await c.async_set_force_charge(True)
        await c.async_set_charge_policy("smartevse_2_first")
        self.assertFalse(c._mutable["smartevse_1_session_complete"])
        self.assertTrue(c._mutable["smartevse_1_intelligent_complete"])

    async def test_gate_opening_in_intelligent_keeps_completion_but_schedule_clears_it(self):
        # SmartEVSE 2 holds a full car; SmartEVSE 1's first planned block opens the gate.
        c = make_controller(intelligent_enabled=True, smartevse_2_session_complete=True,
                            smartevse_1_last_connected=True, smartevse_2_last_connected=True,
                            smartevse_1_connected_ev="vehicle_1")
        harness = Harness(c, smartevse_1=status("smartevse_1"), smartevse_2=status("smartevse_2"))
        await harness.cycle(L(11, 9, 22))
        data = await harness.cycle(L(11, 10, 1, 0, 30))
        self.assertTrue(data["charge_allowed"])
        self.assertEqual(data["controller_state"], "intelligent")
        self.assertTrue(c._mutable["smartevse_2_session_complete"])

        s = make_controller(schedule_enabled=True, smartevse_2_session_complete=True,
                            smartevse_1_last_connected=True, smartevse_2_last_connected=True)
        put(s.states, "schedule.charge_schedule", "on")
        harness = Harness(s, smartevse_1=status("smartevse_1"), smartevse_2=status("smartevse_2"))
        data = await harness.cycle(L(11, 10, 1))
        self.assertEqual(data["controller_state"], "schedule")
        self.assertFalse(s._mutable["smartevse_2_session_complete"])


class NotAcceptingTests(TzTestCase):
    def controller(self, *, soc=50.0, target=80.0, **mutable):
        values = {"intelligent_enabled": True, "last_charge_allowed": True, "last_controller_state": "intelligent",
                  "active_smartevse": "smartevse_1", "smartevse_1_connected_ev": "vehicle_1",
                  "smartevse_1_intelligent_committed_until": L(11, 10, 1, 15).isoformat(), **mutable}
        c = make_controller(**values)
        c._intelligent._facts = {"smartevse_1": SimpleNamespace(soc=soc, target=target)}
        return c

    def step(self, c, now, **kwargs):
        smart = status("smartevse_1", mode="Smart", **kwargs)
        c._update_intelligent_not_accepting(now=now, statuses={"smartevse_1": smart,
                                                               "smartevse_2": status("smartevse_2", connected=False)})

    def test_five_minutes_without_drawing_holds_the_car_off_for_an_hour(self):
        c = self.controller()
        start = L(11, 10, 1)
        self.step(c, start)
        self.assertEqual(c._mutable["smartevse_1_intelligent_no_draw_since"], start.isoformat())
        self.step(c, start + timedelta(seconds=290))
        self.assertIsNone(c._mutable["smartevse_1_intelligent_not_accepting_until"])
        self.step(c, start + timedelta(seconds=300))
        self.assertEqual(c._mutable["smartevse_1_intelligent_not_accepting_until"],
                         (start + timedelta(seconds=300) + timedelta(minutes=60)).isoformat())
        self.assertEqual(c._mutable["smartevse_1_intelligent_probes"], 1)
        self.assertIsNone(c._mutable["smartevse_1_intelligent_committed_until"])
        self.assertTrue(c._intelligent_not_accepting_active("smartevse_1", start + timedelta(minutes=30)))
        self.assertFalse(c._intelligent_not_accepting_active("smartevse_1", start + timedelta(minutes=70)))

    def test_threshold_scales_with_a_slow_update_interval(self):
        c = self.controller(update_interval=20)
        start = L(11, 10, 1)
        self.step(c, start)
        self.step(c, start + timedelta(seconds=300))
        self.assertIsNone(c._mutable["smartevse_1_intelligent_not_accepting_until"])
        self.step(c, start + timedelta(seconds=600))
        self.assertIsNotNone(c._mutable["smartevse_1_intelligent_not_accepting_until"])

    def test_pause_car_timer_force_and_charging_never_count(self):
        start = L(11, 10, 1)
        cases = {
            "pause": ({}, {"state": PAUSE}),
            "force": ({"last_controller_state": "force"}, {}),
            "gate_closed": ({"last_charge_allowed": False}, {}),
            "other_outlet": ({"active_smartevse": "smartevse_2"}, {}),
            "charging": ({}, {"state": "Charging"}),
        }
        for name, (mutable, kwargs) in cases.items():
            with self.subTest(name):
                c = self.controller(**mutable)
                self.step(c, start, **kwargs)
                self.step(c, start + timedelta(seconds=400), **kwargs)
                self.assertIsNone(c._mutable["smartevse_1_intelligent_no_draw_since"])
                self.assertEqual(c._mutable["smartevse_1_intelligent_probes"], 0)
        c = self.controller()
        put(c.states, XC40_CHARGING, "scheduled", at=start)
        self.step(c, start)
        self.step(c, start + timedelta(seconds=400))
        self.assertEqual(c._mutable["smartevse_1_intelligent_probes"], 0)
        # The firmware's plain "Charging Stopped" for up to 90 s after a pause does not count.
        c = self.controller(smartevse_1_last_pause_at=start.isoformat())
        self.step(c, start + timedelta(seconds=60), state="Charging Stopped")
        self.assertIsNone(c._mutable["smartevse_1_intelligent_no_draw_since"])
        self.step(c, start + timedelta(seconds=100), state="Charging Stopped")
        self.assertEqual(c._mutable["smartevse_1_intelligent_no_draw_since"],
                         (start + timedelta(seconds=100)).isoformat())

    def test_unknown_or_reached_soc_and_three_probes_end_the_session(self):
        start = L(11, 10, 1)
        c = self.controller(soc=None)
        self.step(c, start)
        self.step(c, start + timedelta(seconds=300))
        self.assertEqual(c._mutable["smartevse_1_intelligent_probes"], 3)
        self.assertIsNone(c._mutable["smartevse_1_intelligent_not_accepting_until"])
        self.assertTrue(c._intelligent_not_accepting_active("smartevse_1", start + timedelta(hours=5)))
        # A car at (or above) its minimum that does not draw is full, not refusing:
        # reached for the session, never published as "not accepting".
        for soc in (80.0, 85.0):
            c = self.controller(soc=soc)
            self.step(c, start)
            self.step(c, start + timedelta(seconds=300))
            self.assertTrue(c._mutable["smartevse_1_intelligent_reached"])
            self.assertEqual(c._mutable["smartevse_1_intelligent_probes"], 0)
            self.assertIsNone(c._mutable["smartevse_1_intelligent_committed_until"])
            self.assertFalse(c._intelligent_not_accepting_active("smartevse_1", start + timedelta(minutes=6)))
        c = self.controller(smartevse_1_intelligent_probes=2)
        self.step(c, start)
        self.step(c, start + timedelta(seconds=300))
        self.assertEqual(c._mutable["smartevse_1_intelligent_probes"], 3)
        self.assertTrue(c._intelligent_not_accepting_active("smartevse_1", start + timedelta(hours=5)))

    def test_not_accepting_car_is_done_for_the_planner(self):
        c = self.controller(smartevse_1_last_connected=True,
                            smartevse_1_intelligent_deadline=L(11, 10, 7).isoformat(),
                            smartevse_1_intelligent_not_accepting_until=L(11, 10, 2).isoformat())
        inputs = c._intelligent_plan_inputs(now=L(11, 10, 1, 30), statuses={"smartevse_1": status("smartevse_1")},
                                            facts={"smartevse_1": facts(soc=50.0)})
        self.assertTrue(inputs["smartevse_1"].done)
        inputs = c._intelligent_plan_inputs(now=L(11, 10, 2, 1), statuses={"smartevse_1": status("smartevse_1")},
                                            facts={"smartevse_1": facts(soc=50.0)})
        self.assertFalse(inputs["smartevse_1"].done)


class ResolutionTests(TzTestCase):
    def resolve(self, c, now, outlet, *, s1=None, s2=None, block_start=None):
        return c._resolve_active_smartevse(now=now, smartevse_1=s1 or status("smartevse_1"),
                                           smartevse_2=s2 or status("smartevse_2"), intelligent_outlet=outlet,
                                           intelligent_block_start=block_start)

    def test_planned_outlet_is_activated_with_the_block_start_and_committed(self):
        c = make_controller(policy_change_pending=True, handoff_target="smartevse_2",
                            handoff_started_at=L(11, 10, 0, 59).isoformat(),
                            smartevse_2_intelligent_committed_until=L(11, 10, 1).isoformat())
        result = self.resolve(c, L(11, 10, 1, 0, 40), "smartevse_1", block_start=L(11, 10, 1))
        self.assertEqual(result, ("smartevse_1", L(11, 10, 1).isoformat(), 0, ""))
        self.assertEqual(c._mutable["active_smartevse"], "smartevse_1")
        self.assertFalse(c._mutable["policy_change_pending"])
        self.assertIsNone(c._mutable["handoff_target"])
        self.assertEqual(c._mutable["smartevse_1_intelligent_committed_until"], L(11, 10, 1, 15).isoformat())
        self.assertIsNone(c._mutable["smartevse_2_intelligent_committed_until"])
        self.assertEqual(c._intelligent_commitment(L(11, 10, 1, 5)), ("smartevse_1", L(11, 10, 1, 15)))
        # Kept: the start does not move, the commitment renews per unit.
        result = self.resolve(c, L(11, 10, 1, 16), "smartevse_1", block_start=L(11, 10, 1, 16))
        self.assertEqual(result[1], L(11, 10, 1).isoformat())
        self.assertEqual(c._mutable["smartevse_1_intelligent_committed_until"], L(11, 10, 1, 30).isoformat())
        self.assertIsNone(c._intelligent_commitment(L(11, 10, 1, 31)))

    def test_unavailable_or_unplugged_planned_outlet_clears_the_active_outlet(self):
        for s1 in (status("smartevse_1", available=False), status("smartevse_1", connected=False)):
            c = make_controller(active_smartevse="smartevse_1", active_smartevse_since=L(11, 10, 1).isoformat())
            result = self.resolve(c, L(11, 10, 1, 5), "smartevse_1", s1=s1)
            self.assertEqual(result, ("none", None, 0, "intelligent_planned_outlet_unavailable"))
            self.assertIsNone(c._mutable["active_smartevse"])
        c = make_controller(active_smartevse="smartevse_1")
        result = self.resolve(c, L(11, 10, 1, 5), "smartevse_1", s1=status("smartevse_1", available=False),
                              s2=status("smartevse_2", available=False))
        self.assertEqual(result, ("none", None, 0, "smartevse_api_unavailable"))

    def test_commitment_is_released_when_the_target_is_reached_or_the_session_completes(self):
        for car, mutable in ((facts(soc=80.0, charging=True), {}),
                             (facts(soc=60.0), {"smartevse_1_session_complete": True})):
            c = make_controller(intelligent_enabled=True, smartevse_1_last_connected=True,
                                smartevse_1_connected_ev="vehicle_1",
                                smartevse_1_intelligent_deadline=L(11, 10, 7).isoformat(),
                                smartevse_1_intelligent_deadline_vehicle="vehicle_1",
                                smartevse_1_intelligent_committed_until=L(11, 10, 1, 15).isoformat(), **mutable)
            c._update_intelligent_outlets(now=L(11, 10, 1, 5), statuses={"smartevse_1": status("smartevse_1")},
                                          facts={"smartevse_1": car})
            self.assertIsNone(c._mutable["smartevse_1_intelligent_committed_until"])

    def test_only_policy_excludes_the_other_outlet_and_first_policy_sets_priority(self):
        c = make_controller(intelligent_enabled=True, charge_policy="smartevse_2_first",
                            smartevse_1_last_connected=True, smartevse_2_last_connected=True)
        for key in ("smartevse_1", "smartevse_2"):
            c._mutable[f"{key}_intelligent_deadline"] = L(11, 10, 7).isoformat()
        statuses = {"smartevse_1": status("smartevse_1"), "smartevse_2": status("smartevse_2")}
        cars = {"smartevse_1": facts(), "smartevse_2": facts(settings_key="vehicle_2", vehicle="vehicle_2")}
        inputs = c._intelligent_plan_inputs(now=L(11, 9, 22), statuses=statuses, facts=cars)
        self.assertEqual((inputs["smartevse_1"].priority, inputs["smartevse_2"].priority), (1, 0))
        self.assertFalse(inputs["smartevse_1"].excluded)
        c._mutable["charge_policy"] = "smartevse_1_only"
        inputs = c._intelligent_plan_inputs(now=L(11, 9, 22), statuses=statuses, facts=cars)
        self.assertEqual((inputs["smartevse_1"].excluded, inputs["smartevse_2"].excluded), (False, True))


class CycleTests(TzTestCase):
    async def plugged_xc40(self, **mutable):
        c = make_controller(intelligent_enabled=True, **mutable)
        harness = Harness(c)
        await harness.cycle(L(11, 9, 17, 55))
        harness.statuses["smartevse_1"] = status("smartevse_1")
        put(c.states, XC40_CONNECTION, "disconnected", at=L(11, 9, 17, 50))
        await harness.cycle(L(11, 9, 17, 56))
        put(c.states, XC40_CONNECTION, "connected", at=L(11, 9, 17, 57))
        await harness.cycle(L(11, 9, 17, 57))
        self.assertEqual(c._mutable["smartevse_1_connected_ev"], "vehicle_1")
        return c, harness

    async def test_cycle_publishes_settings_payload_and_flat_sensor_values(self):
        c, harness = await self.plugged_xc40()
        data = await harness.cycle(L(11, 9, 18))
        for key, value in {"intelligent_enabled": True, "vehicle_1_ready_by": "07:00",
                           "vehicle_2_ready_by": "07:00", "other_vehicle_ready_by": "07:00",
                           "vehicle_1_minimum_charge": 80, "vehicle_2_minimum_charge": 80,
                           "other_vehicle_minimum_charge": 80, "intelligent_status": "planned",
                           "intelligent_currency": "EUR"}.items():
            self.assertEqual(data[key], value, key)
        self.assertEqual(data["controller_state"], "intelligent")
        self.assertEqual(data["charge_reason"], "intelligent_waiting_for_planned_slot")
        self.assertFalse(data["charge_allowed"])
        self.assertIsInstance(data["intelligent_planned_cost"], float)
        self.assertIsNone(data["controller_error"])
        next_charge = data["intelligent_next_charge"]
        self.assertIsInstance(next_charge, datetime)
        self.assertIsNotNone(next_charge.tzinfo)
        self.assertEqual(next_charge, L(11, 10, 1))  # the cheap 01:00-04:00 window
        charger = data["intelligent"]["chargers"]["smartevse_1"]
        self.assertEqual((charger["vehicle"], charger["soc"], charger["deadline"]),
                         ("vehicle_1", 53.0, L(11, 10, 7).isoformat()))
        self.assertIsNone(data["intelligent"]["chargers"]["smartevse_2"])
        self.assertEqual(c._mutable["smartevse_1_intelligent_plugged_at"], L(11, 9, 17, 56).isoformat())

    async def test_planned_block_starts_charging_with_a_stable_reason_and_start(self):
        c, harness = await self.plugged_xc40()
        c._async_post.reset_mock()
        data = await harness.cycle(L(11, 10, 1, 0, 20))
        self.assertTrue(data["charge_allowed"])
        self.assertEqual((data["charge_reason"], data["active_smartevse_raw"]), ("intelligent_charging", "smartevse_1"))
        self.assertEqual(data["active_smartevse_since"], L(11, 10, 1).isoformat())
        self.assertEqual(data["duty_cycle_remaining"], 0)
        self.assertEqual(harness.posts(), [(f"{URL_1}/settings", {"mode": 3})])
        self.assertEqual(data["intelligent_status"], "charging")
        harness.statuses["smartevse_1"] = status("smartevse_1", state="Charging", mode="Smart")
        put(c.states, "sensor.ev_power_total", 10300, unit_of_measurement="W")
        c._async_post.reset_mock()
        previous = None
        for step in range(1, 8):
            data = await harness.cycle(L(11, 10, 1, 0, 20) + timedelta(seconds=10 * step))
            self.assertEqual(data["charge_reason"], "intelligent_charging")
            self.assertEqual(data["active_smartevse_since"], L(11, 10, 1).isoformat())
            # The 17:54 reading is old once the car charges, so the SoC becomes
            # "estimated" once; after that nothing in the payload moves every cycle.
            if step >= 3:
                self.assertEqual(data["intelligent"], previous)
            previous = data["intelligent"]
        self.assertEqual(data["intelligent"]["chargers"]["smartevse_1"]["soc_source"], "estimated")
        c._async_post.assert_not_awaited()  # never re-POST Smart to a charger already in Smart
        self.assertEqual(c._mutable["smartevse_1_intelligent_committed_until"], L(11, 10, 1, 15).isoformat())

    async def test_charging_continues_through_unit_boundaries(self):
        # The last minute of a unit is too short to plan; the active car must not be
        # switched Off and back to Smart at every 15-minute boundary.
        c, harness = await self.plugged_xc40()
        start = L(11, 10, 1, 0, 20)
        data = await harness.cycle(start)
        self.assertTrue(data["charge_allowed"])
        harness.statuses["smartevse_1"] = status("smartevse_1", state="Charging", mode="Smart")
        put(c.states, "sensor.ev_power_total", 10300, unit_of_measurement="W")
        c._async_post.reset_mock()
        for step in range(1, 190):  # 01:00:30 .. 01:31:50 every 10 s, two boundaries
            now = start + timedelta(seconds=10 * step)
            data = await harness.cycle(now)
            self.assertTrue(data["charge_allowed"], now.isoformat())
            self.assertEqual(data["active_smartevse_since"], L(11, 10, 1).isoformat())
        c._async_post.assert_not_awaited()

    async def test_switch_to_the_other_car_turns_the_outgoing_charger_off_first(self):
        # XC40 reached 80 % on SmartEVSE 1 (still polled Smart and charging); the EX30
        # on SmartEVSE 2 must start now to make 06:00.
        c = make_controller(intelligent_enabled=True, vehicle_2_ready_by="06:00", vehicle_2_minimum_charge=80,
                            smartevse_1_connected_ev="vehicle_1", smartevse_2_connected_ev="vehicle_2",
                            smartevse_1_last_connected=True, smartevse_2_last_connected=True,
                            last_charge_allowed=True, last_controller_state="intelligent",
                            last_charge_reason="intelligent_charging", last_active_charge_reason="intelligent_charging",
                            active_smartevse="smartevse_1", active_smartevse_since=L(11, 10, 0).isoformat(),
                            smartevse_1_intelligent_plugged_at=L(11, 9, 20).isoformat(),
                            smartevse_2_intelligent_plugged_at=L(11, 9, 20).isoformat())
        put(c.states, XC40_BATTERY, 80.0, at=L(11, 10, 2, 59), unit_of_measurement="%")
        put(c.states, EX30_BATTERY, 50.0, at=L(11, 9, 20, 5), unit_of_measurement="%")
        put(c.states, EX30_CONNECTION, "connected", at=L(11, 9, 20, 1))
        put(c.states, "sensor.ev_power_total", 10300, unit_of_measurement="W")
        harness = Harness(c, smartevse_1=status("smartevse_1", state="Charging", mode="Smart"),
                          smartevse_2=status("smartevse_2"))
        data = await harness.cycle(L(11, 10, 3, 0, 5))
        self.assertEqual((data["charge_reason"], data["active_smartevse_raw"]), ("intelligent_charging", "smartevse_2"))
        # Off to the outgoing charger was awaited; the incoming one stays Off while it settles.
        self.assertEqual(harness.posts(), [(f"{URL_1}/settings", {"mode": 0})])
        self.assertEqual(c._mutable["handoff_settle_from"], "smartevse_1")

    async def test_identifying_window_after_a_plug_in_of_an_unknown_car(self):
        c = make_controller(intelligent_enabled=True)
        harness = Harness(c)
        await harness.cycle(L(11, 10, 1, 0))
        harness.statuses["smartevse_2"] = status("smartevse_2")
        data = await harness.cycle(L(11, 10, 1, 1))
        self.assertEqual(data["charge_reason"], "intelligent_identifying_vehicle")
        self.assertFalse(data["charge_allowed"])
        self.assertEqual(data["intelligent_status"], "identifying")
        charger = data["intelligent"]["chargers"]["smartevse_2"]
        self.assertEqual((charger["status"], charger["settings_key"]), ("identifying", "other"))
        self.assertIsNone(charger["session_cost"])
        # Still unmapped after 10 min: plan with the "other" settings (unknown SoC, budget).
        data = await harness.cycle(L(11, 10, 1, 16))
        self.assertEqual(data["charge_reason"], "intelligent_charging")
        self.assertEqual(data["intelligent"]["chargers"]["smartevse_2"]["status"], "charging")

    async def test_force_overrides_and_intelligent_resumes(self):
        c, harness = await self.plugged_xc40()
        c._mutable["force_charge"] = True
        data = await harness.cycle(L(11, 9, 18))
        self.assertEqual((data["controller_state"], data["charge_reason"]), ("force", "force_charge"))
        self.assertEqual(data["intelligent_status"], "overridden")
        self.assertEqual(data["intelligent"]["overridden_by"], "force")
        self.assertIsNotNone(data["intelligent"]["chargers"]["smartevse_1"])  # plan still published
        c._mutable["force_charge"] = False
        data = await harness.cycle(L(11, 9, 18, 0, 10))
        self.assertEqual(data["controller_state"], "intelligent")

    async def test_disabled_intelligent_still_learns_and_publishes_disabled(self):
        c = make_controller(schedule_enabled=True)
        put(c.states, "schedule.charge_schedule", "on")
        observe = MagicMock(wraps=c._intelligent.observe)
        c._intelligent.observe = observe
        harness = Harness(c, smartevse_1=status("smartevse_1"))
        data = await harness.cycle(L(11, 10, 1))
        self.assertEqual(data["controller_state"], "schedule")
        self.assertEqual(data["intelligent_status"], "disabled")
        self.assertEqual(observe.call_args.kwargs["controller_state"], "schedule")
        self.assertEqual(data["intelligent"]["chargers"], {"smartevse_1": None, "smartevse_2": None})

    async def test_notification_is_not_raised_when_intelligent_covers_the_schedule(self):
        c = make_controller(intelligent_enabled=True, schedule_enabled=False)
        put(c.states, "schedule.charge_schedule", "on")
        harness = Harness(c)
        await harness.cycle(L(11, 10, 0, 0, 5))
        harness.create.assert_not_called()
        c2 = make_controller(intelligent_enabled=False, schedule_enabled=False)
        put(c2.states, "schedule.charge_schedule", "on")
        harness2 = Harness(c2)
        await harness2.cycle(L(11, 10, 0, 0, 5))
        harness2.create.assert_called_once()

    async def test_planned_outlet_unavailable_blocks_with_a_stable_reason(self):
        c, harness = await self.plugged_xc40()
        harness.statuses["smartevse_1"] = status("smartevse_1", available=False)
        data = await harness.cycle(L(11, 10, 1, 0, 20))
        self.assertEqual(data["charge_reason"], "intelligent_planned_outlet_unavailable")
        self.assertFalse(data["charge_allowed"])
        self.assertEqual(data["active_smartevse_raw"], "none")
        self.assertIsNone(c._mutable["active_smartevse"])
        # The blip did not end the plug session.
        self.assertEqual(c._mutable["smartevse_1_intelligent_plugged_at"], L(11, 9, 17, 56).isoformat())
        self.assertEqual(c._mutable["smartevse_1_connected_ev"], "vehicle_1")

    async def test_cross_check_swap_exchanges_only_the_two_outlets(self):
        c = make_controller(intelligent_enabled=True, smartevse_1_connected_ev="vehicle_2",
                            smartevse_2_connected_ev="vehicle_1", smartevse_1_session_complete=True,
                            smartevse_2_intelligent_probes=2, smartevse_1_last_connected=True,
                            smartevse_2_last_connected=True)
        real_observe = c._intelligent.observe

        def observe(now, **kwargs):
            result = real_observe(now, **kwargs)
            return ObserveResult(facts=result.facts, swap=("smartevse_1", "smartevse_2"))

        c._intelligent.observe = observe
        c._intelligent.discard_sessions = MagicMock()
        harness = Harness(c, smartevse_1=status("smartevse_1"), smartevse_2=status("smartevse_2", state="Charging",
                                                                                     mode="Smart"))
        with self.assertLogs("custom_components.smartevse_dual_charger", level="INFO"):
            await harness.cycle(L(11, 10, 2))
        self.assertEqual((c._mutable["smartevse_1_connected_ev"], c._mutable["smartevse_2_connected_ev"]),
                         ("vehicle_1", "vehicle_2"))
        self.assertFalse(c._mutable["smartevse_1_session_complete"])
        self.assertEqual(c._mutable["smartevse_2_intelligent_probes"], 0)
        c._intelligent.discard_sessions.assert_called_once_with(("smartevse_1", "smartevse_2"))

    async def test_adapter_failures_never_break_the_control_loop(self):
        c, harness = await self.plugged_xc40()
        c._intelligent.observe = MagicMock(side_effect=RuntimeError("adapter bug"))
        with self.assertLogs("custom_components.smartevse_dual_charger", level="ERROR"):
            data = await harness.cycle(L(11, 10, 1, 0, 20))
        self.assertEqual(data["controller_state"], "intelligent")
        self.assertFalse(data["charge_allowed"])  # never "charge now at any price"
        self.assertEqual(data["controller_error"], "intelligent_planner_failed")
        c._intelligent.build_payload = MagicMock(side_effect=RuntimeError("payload bug"))
        with self.assertLogs("custom_components.smartevse_dual_charger", level="DEBUG"):
            data = await harness.cycle(L(11, 10, 1, 0, 30))
        self.assertEqual(data["intelligent"]["planner_error"], "payload_failed")
        self.assertEqual(data["intelligent"]["status"], "no_ev")
        self.assertIn("intelligent_next_charge", data)

    async def test_controller_without_adapter_still_runs(self):
        c = make_controller(adapter=False, intelligent_enabled=True)
        harness = Harness(c, smartevse_1=status("smartevse_1"))
        data = await harness.cycle(L(11, 10, 1))
        self.assertEqual((data["controller_state"], data["charge_reason"]),
                         ("intelligent", "intelligent_waiting_for_planned_slot"))
        self.assertEqual(data["intelligent"]["status"], "no_ev")
        self.assertEqual(c.intelligent_source_entity_ids(), [])

    async def test_shutdown_flushes_the_model_before_reminders(self):
        c = make_controller()
        order = []
        c._intelligent.async_flush = AsyncMock(side_effect=lambda: order.append("flush"))
        c._schedule_reminders = SimpleNamespace(async_shutdown=AsyncMock(side_effect=lambda: order.append("reminders")))
        await c.async_shutdown()
        self.assertEqual(order, ["flush", "reminders"])
        c._intelligent.async_flush = AsyncMock(side_effect=OSError("disk"))
        with self.assertLogs("custom_components.smartevse_dual_charger", level="ERROR"):
            await c.async_shutdown()

    def test_source_entity_ids_from_the_adapter(self):
        c = make_controller()
        self.assertEqual(c.intelligent_source_entity_ids(), [
            "sensor.real_electricity_price_hourly_prices_yesterday",
            "sensor.real_electricity_price_hourly_prices_today",
            "sensor.real_electricity_price_hourly_prices_tomorrow",
            XC40_BATTERY, EX30_BATTERY,
        ])
        c._intelligent.source_entity_ids = MagicMock(side_effect=RuntimeError("registry"))
        self.assertEqual(c.intelligent_source_entity_ids(), [])


class ReviewRegressionTests(TzTestCase):
    """Second design review: outages, overrun edits, pauses, deadlines, fallbacks."""

    def two_cars(self, *, ex30_soc=20.0):
        c = make_controller(intelligent_enabled=True, smartevse_1_connected_ev="vehicle_1",
                            smartevse_2_connected_ev="vehicle_2", smartevse_1_last_connected=True,
                            smartevse_2_last_connected=True,
                            smartevse_1_intelligent_plugged_at=L(11, 9, 20).isoformat(),
                            smartevse_2_intelligent_plugged_at=L(11, 9, 20).isoformat())
        put(c.states, XC40_BATTERY, 53.0, at=L(11, 9, 20, 5), unit_of_measurement="%")
        put(c.states, EX30_BATTERY, ex30_soc, at=L(11, 9, 20, 5), unit_of_measurement="%")
        put(c.states, EX30_CONNECTION, "connected", at=L(11, 9, 20, 1))
        return c, Harness(c, smartevse_1=status("smartevse_1"), smartevse_2=status("smartevse_2"))

    async def test_unreachable_charger_gives_the_other_car_the_night(self):
        c, harness = self.two_cars()
        data = await harness.cycle(L(11, 10, 0, 58))
        # Both cars do not fit: R11 keeps the XC40 (priority) on time, the EX30 is at risk.
        self.assertEqual(data["intelligent"]["chargers"]["smartevse_2"]["status"], "at_risk")
        plugged, deadline = (c._mutable["smartevse_1_intelligent_plugged_at"],
                             c._mutable["smartevse_1_intelligent_deadline"])
        harness.statuses["smartevse_1"] = status("smartevse_1", available=False)
        data = await harness.cycle(L(11, 10, 0, 58, 10))
        # A blip shorter than the hand-off hold changes nothing.
        self.assertFalse(c._intelligent.plan_input("smartevse_1").unavailable)
        self.assertEqual(data["intelligent"]["chargers"]["smartevse_2"]["status"], "at_risk")
        data = await harness.cycle(L(11, 10, 0, 59, 30))
        self.assertTrue(c._intelligent.plan_input("smartevse_1").unavailable)
        self.assertEqual(data["charge_reason"], "intelligent_planned_outlet_unavailable")
        data = await harness.cycle(L(11, 10, 1))
        ex30 = data["intelligent"]["chargers"]["smartevse_2"]
        self.assertEqual((data["charge_reason"], data["active_smartevse_raw"]), ("intelligent_charging", "smartevse_2"))
        self.assertEqual((ex30["status"], ex30["projected_soc"]), ("charging", 80.0))
        xc40 = data["intelligent"]["chargers"]["smartevse_1"]
        self.assertEqual((xc40["status"], xc40["blocks"]), ("planned", []))
        self.assertIsNone(c._mutable["smartevse_1_intelligent_committed_until"])
        # The XC40's plug session and deadline are kept: planned again once the charger answers.
        self.assertEqual((c._mutable["smartevse_1_intelligent_plugged_at"],
                          c._mutable["smartevse_1_intelligent_deadline"]), (plugged, deadline))
        self.assertEqual(c._mutable["smartevse_1_connected_ev"], "vehicle_1")
        harness.statuses["smartevse_1"] = status("smartevse_1")
        data = await harness.cycle(L(11, 10, 1, 0, 10))
        self.assertFalse(c._intelligent.plan_input("smartevse_1").unavailable)
        self.assertIsNone(c._mutable["smartevse_1_unavailable_since"])
        self.assertEqual(data["intelligent"]["chargers"]["smartevse_1"]["projected_soc"], 80.0)

    async def test_unreachable_charger_publishes_no_plan_numbers_for_its_car(self):
        # Review round 3: the unreachable car's done-car plan (no blocks, current SoC,
        # cost so far) read as "57 % -> 80 % by 7:00 · ≈ €3.02" and shrank the totals.
        c, harness = self.two_cars()
        await harness.cycle(L(11, 10, 0, 58))
        harness.statuses["smartevse_1"] = status("smartevse_1", available=False)
        await harness.cycle(L(11, 10, 0, 58, 10))
        data = await harness.cycle(L(11, 10, 0, 59, 30))
        self.assertTrue(c._intelligent.plan_input("smartevse_1").unavailable)
        xc40 = data["intelligent"]["chargers"]["smartevse_1"]
        self.assertEqual((xc40["status"], xc40["projected_soc"], xc40["session_cost"], xc40["saving"]),
                         ("planned", None, None, None))
        self.assertIsNone(data["intelligent"]["totals"]["cost"])
        self.assertIsNone(data["intelligent"]["totals"]["saving"])
        self.assertIsNone(data["intelligent_planned_cost"])
        self.assertIsNotNone(data["intelligent"]["chargers"]["smartevse_2"]["session_cost"])

    async def test_editing_the_minimum_during_overrun_keeps_catching_up(self):
        c = make_controller(intelligent_enabled=True, smartevse_1_connected_ev="vehicle_1",
                            smartevse_1_last_connected=True,
                            smartevse_1_intelligent_plugged_at=L(11, 9, 22).isoformat())
        put(c.states, XC40_BATTERY, 60.0, at=L(11, 10, 6, 50), unit_of_measurement="%")
        harness = Harness(c, smartevse_1=status("smartevse_1", state="Charging", mode="Smart"))
        await harness.cycle(L(11, 10, 6, 59, 50))
        data = await harness.cycle(L(11, 10, 7))
        self.assertTrue(c._mutable["smartevse_1_intelligent_overrun"])
        self.assertEqual(data["charge_reason"], "intelligent_catch_up")
        c._async_post.reset_mock()
        with patch.object(dt_util, "utcnow", return_value=L(11, 10, 7, 0, 5)):
            await c.async_set_vehicle_minimum_charge("vehicle_1", 67)  # still above the 60 %
        data = await harness.cycle(L(11, 10, 7, 0, 10))
        self.assertTrue(c._mutable["smartevse_1_intelligent_overrun"])
        self.assertEqual((data["charge_reason"], data["active_smartevse_raw"]), ("intelligent_catch_up", "smartevse_1"))
        self.assertEqual(data["intelligent"]["chargers"]["smartevse_1"]["status"], "catch_up")
        c._async_post.assert_not_awaited()  # no Off write
        # Reaching the (new) minimum still ends the overrun.
        put(c.states, XC40_BATTERY, 67.0, at=L(11, 10, 8), unit_of_measurement="%")
        await harness.cycle(L(11, 10, 8, 0, 10))
        self.assertFalse(c._mutable["smartevse_1_intelligent_overrun"])

    async def test_identifying_the_car_during_catch_up_keeps_catching_up(self):
        # Review round 3: plugged in at 04:30 while the Volvo cloud is down, the car runs as
        # "Unidentified vehicle" and rolls into catch-up at 07:00. Identified as the XC40
        # (also ready by 07:00) at 07:20, it is the same car, still late and below 80 %.
        c = make_controller(intelligent_enabled=True, smartevse_1_last_connected=True,
                            smartevse_1_intelligent_plugged_at=L(11, 10, 4, 30).isoformat())
        put(c.states, XC40_BATTERY, 54.0, at=L(11, 10, 7, 10), unit_of_measurement="%")
        put(c.states, "sensor.ev_power_total", 10300, unit_of_measurement="W")
        harness = Harness(c, smartevse_1=status("smartevse_1", state="Charging", mode="Smart"))
        await harness.cycle(L(11, 10, 6, 59, 50))
        data = await harness.cycle(L(11, 10, 7, 0, 0))
        self.assertTrue(c._mutable["smartevse_1_intelligent_overrun"])
        self.assertEqual(data["charge_reason"], "intelligent_catch_up")
        c._mutable["smartevse_1_connected_ev"] = "vehicle_1"  # the cloud is back: identified
        c._async_post.reset_mock()
        for second in range(0, 600, 10):
            data = await harness.cycle(L(11, 10, 7, 20, 0) + timedelta(seconds=second))
            self.assertTrue(c._mutable["smartevse_1_intelligent_overrun"], second)
            self.assertEqual((data["charge_reason"], data["active_smartevse_raw"]),
                             ("intelligent_catch_up", "smartevse_1"), second)
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline_vehicle"], "vehicle_1")
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline"], L(11, 11, 7).isoformat())
        c._async_post.assert_not_awaited()

    async def test_reached_car_keeps_charging_in_cheap_known_hours(self):
        # XC40 plugged in at its 80 % minimum (car limit 90 %). The minimum is a floor:
        # above it the car keeps charging in hours at or below the learned cheap price
        # (0.29 here; 01:00-04:00 cost 0.20). No user switch or price since 0.0.10.0.
        c = make_controller(intelligent_enabled=True)
        c._intelligent.extra_price = MagicMock(return_value=0.29)
        put(c.states, XC40_BATTERY, 80.0, at=L(11, 9, 17, 54), unit_of_measurement="%")
        harness = Harness(c)
        await harness.cycle(L(11, 9, 17, 55))
        harness.statuses["smartevse_1"] = status("smartevse_1")
        put(c.states, XC40_CONNECTION, "disconnected", at=L(11, 9, 17, 50))
        await harness.cycle(L(11, 9, 17, 56))
        put(c.states, XC40_CONNECTION, "connected", at=L(11, 9, 17, 57))
        data = await harness.cycle(L(11, 9, 18))
        self.assertTrue(c._mutable["smartevse_1_intelligent_reached"])
        charger = data["intelligent"]["chargers"]["smartevse_1"]
        self.assertEqual(charger["status"], "reached")
        self.assertTrue(charger["top_up"])
        self.assertEqual(charger["extra_soc"], 90.0)  # up to the car's own limit
        self.assertEqual(data["intelligent"]["top_up"], {"enabled": True, "price": 0.29, "source": "learned"})
        self.assertTrue(all(block["top_up"] for block in charger["blocks"]))
        self.assertEqual(charger["blocks"][0]["start"], L(11, 10, 1).isoformat())
        c._async_post.reset_mock()
        data = await harness.cycle(L(11, 10, 1, 0, 20))
        self.assertEqual((data["charge_allowed"], data["charge_reason"], data["active_smartevse_raw"]),
                         (True, "intelligent_top_up", "smartevse_1"))
        self.assertEqual(harness.posts(), [(f"{URL_1}/settings", {"mode": 3})])
        # The top-up block is committed for its unit like any other block.
        harness.statuses["smartevse_1"] = status("smartevse_1", state="Charging", mode="Smart")
        await harness.cycle(L(11, 10, 1, 0, 30))
        self.assertEqual(c._mutable["smartevse_1_intelligent_committed_until"], L(11, 10, 1, 15).isoformat())

    async def test_schedule_mode_never_applies_a_cross_check_swap(self):
        # Review round 3: the §8.11 swap only acts while Intelligent plans; Schedule keeps
        # the 0.0.8.x mapping rules (no swap, no discarded learning).
        c = make_controller(schedule_enabled=True, smartevse_1_connected_ev="vehicle_2",
                            smartevse_2_connected_ev="vehicle_1",
                            smartevse_1_last_connected=True, smartevse_2_last_connected=True)
        put(c.states, "schedule.charge_schedule", "on")
        real_observe = c._intelligent.observe

        def observe(now, **kwargs):
            result = real_observe(now, **kwargs)
            return ObserveResult(facts=result.facts, swap=("smartevse_1", "smartevse_2"))

        c._intelligent.observe = observe
        c._intelligent.discard_sessions = MagicMock()
        harness = Harness(c, smartevse_1=status("smartevse_1"),
                          smartevse_2=status("smartevse_2", state="Charging", mode="Smart"))
        await harness.cycle(L(11, 10, 2))
        self.assertEqual((c._mutable["smartevse_1_connected_ev"], c._mutable["smartevse_2_connected_ev"]),
                         ("vehicle_2", "vehicle_1"))
        c._intelligent.discard_sessions.assert_not_called()

    async def test_late_car_charges_into_its_deadline_without_an_off_write(self):
        c = make_controller(intelligent_enabled=True, smartevse_1_connected_ev="vehicle_1",
                            smartevse_1_last_connected=True, vehicle_1_ready_by="03:00",
                            smartevse_1_intelligent_plugged_at=L(11, 9, 23).isoformat())
        put(c.states, XC40_BATTERY, 30.0, at=L(11, 10, 2, 45), unit_of_measurement="%")
        harness = Harness(c, smartevse_1=status("smartevse_1"))
        data = await harness.cycle(L(11, 10, 2, 50))
        self.assertEqual(data["charge_reason"], "intelligent_catch_up")  # at risk, charging now
        harness.statuses["smartevse_1"] = status("smartevse_1", state="Charging", mode="Smart")
        put(c.states, "sensor.ev_power_total", 10300, unit_of_measurement="W")
        c._async_post.reset_mock()
        moment = L(11, 10, 2, 50, 10)
        while moment <= L(11, 10, 3, 0, 30):
            data = await harness.cycle(moment)
            self.assertTrue(data["charge_allowed"], moment.isoformat())
            self.assertEqual(data["charge_reason"], "intelligent_catch_up", moment.isoformat())
            moment += timedelta(seconds=10)
        c._async_post.assert_not_awaited()
        self.assertTrue(c._mutable["smartevse_1_intelligent_overrun"])  # rolled at 03:00

    def test_paused_active_charger_counts_as_running_for_the_planner(self):
        c = make_controller(intelligent_enabled=True, active_smartevse="smartevse_1", smartevse_1_last_connected=True,
                            smartevse_1_intelligent_deadline=L(11, 10, 7).isoformat())
        cars = {"smartevse_1": facts(), "smartevse_2": facts(settings_key="vehicle_2", vehicle="vehicle_2")}
        c._mutable.update(smartevse_2_last_connected=True, smartevse_2_intelligent_deadline=L(11, 10, 7).isoformat())

        def inputs(**s1):
            statuses = {"smartevse_1": status("smartevse_1", **s1), "smartevse_2": status("smartevse_2", mode="Smart")}
            return c._intelligent_plan_inputs(now=L(11, 10, 2, 30), statuses=statuses, facts=cars)

        # "No Power Available" in Smart at a unit boundary (commitment just lapsed) or
        # the ~90 s ramp after the Smart write: still the running car.
        self.assertTrue(inputs(mode="Smart", state=PAUSE)["smartevse_1"].drawing)
        self.assertTrue(inputs(mode="Smart")["smartevse_1"].drawing)
        self.assertFalse(inputs(mode="Off")["smartevse_1"].drawing)
        self.assertFalse(inputs(mode="Smart", available=False)["smartevse_1"].drawing)
        self.assertFalse(inputs(mode="Smart")["smartevse_2"].drawing)  # Smart but not the active outlet
        self.assertTrue(inputs(mode="Off")["smartevse_2"].drawing is False)

    async def test_paused_charger_is_not_switched_off_at_a_unit_boundary(self):
        c, harness = await CycleTests.plugged_xc40(self)
        await harness.cycle(L(11, 10, 1, 0, 20))
        harness.statuses["smartevse_1"] = status("smartevse_1", state="Charging", mode="Smart")
        put(c.states, "sensor.ev_power_total", 10300, unit_of_measurement="W")
        moment = L(11, 10, 1, 0, 30)
        while moment < L(11, 10, 1, 14, 50):
            await harness.cycle(moment)
            moment += timedelta(seconds=10)
        c._async_post.reset_mock()
        harness.statuses["smartevse_1"] = status("smartevse_1", state=PAUSE, mode="Smart")
        put(c.states, "sensor.ev_power_total", 0, unit_of_measurement="W")
        while moment <= L(11, 10, 1, 16):
            data = await harness.cycle(moment)
            self.assertTrue(data["charge_allowed"], moment.isoformat())
            self.assertTrue(c._intelligent.plan_input("smartevse_1").drawing)
            moment += timedelta(seconds=10)
        c._async_post.assert_not_awaited()

    async def test_reupgrade_after_a_rollback_does_not_revive_the_old_plug_session(self):
        # Review round 3: 0.0.8.11 keeps but never maintains the Intelligent keys. The car
        # was unplugged and plugged in again meanwhile (0.0.8.11 recorded the new plug);
        # re-installing 0.0.9.0 must not roll the 2-day-old deadline into a catch-up.
        stale = dict(intelligent_enabled=True, smartevse_1_connected_ev="vehicle_1", smartevse_1_last_connected=True,
                     smartevse_1_intelligent_plugged_at=L(11, 9, 20).isoformat(),
                     smartevse_1_intelligent_deadline=L(11, 10, 7).isoformat(),
                     smartevse_1_intelligent_deadline_vehicle="vehicle_1")
        c = make_controller(**stale, smartevse_1_last_plug_connected=L(11, 11, 18).isoformat())
        self.assertIsNone(c._mutable["smartevse_1_intelligent_deadline"])
        put(c.states, XC40_BATTERY, 50.0, at=L(11, 11, 18, 20), unit_of_measurement="%")
        data = await Harness(c, smartevse_1=status("smartevse_1")).cycle(L(11, 11, 18, 30))
        self.assertFalse(c._mutable["smartevse_1_intelligent_overrun"])
        self.assertNotEqual(data["charge_reason"], "intelligent_catch_up")
        self.assertEqual(c._mutable["smartevse_1_intelligent_plugged_at"], L(11, 11, 18).isoformat())
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline"], L(11, 12, 7).isoformat())
        # Same plug session but its deadline passed days ago: rolled without a catch-up.
        c = make_controller(**stale, smartevse_1_last_plug_connected=L(11, 9, 20).isoformat())
        put(c.states, XC40_BATTERY, 50.0, at=L(11, 11, 18, 20), unit_of_measurement="%")
        data = await Harness(c, smartevse_1=status("smartevse_1")).cycle(L(11, 11, 18, 30))
        self.assertFalse(c._mutable["smartevse_1_intelligent_overrun"])
        self.assertNotEqual(data["charge_reason"], "intelligent_catch_up")
        self.assertEqual(c._mutable["smartevse_1_intelligent_deadline"], L(11, 12, 7).isoformat())

    async def test_restart_in_the_last_minute_of_a_unit_keeps_charging(self):
        # Review round 3: a restart/reload whose first cycle lands 40 s before a boundary
        # lost the in-memory bridge and wrote Off, then Smart again ~9 min later.
        c, harness = await CycleTests.plugged_xc40(self)
        await harness.cycle(L(11, 10, 1, 0, 20))
        harness.statuses["smartevse_1"] = status("smartevse_1", state="Charging", mode="Smart")
        put(c.states, "sensor.ev_power_total", 10300, unit_of_measurement="W")
        moment = L(11, 10, 1, 0, 30)
        while moment < L(11, 10, 1, 13, 0):
            await harness.cycle(moment)
            moment += timedelta(seconds=10)
        # Restart: a fresh adapter (no previous decision) and the reset done by async_initialize.
        c._intelligent = IntelligentCharging(c.hass, c._entry_data, c._options, store=fake_store())
        c._reset_charge_cycle()
        c._async_post.reset_mock()
        moment = L(11, 10, 1, 14, 20)
        while moment <= L(11, 10, 1, 16):
            data = await harness.cycle(moment)
            self.assertTrue(data["charge_allowed"], moment.isoformat())
            self.assertEqual(data["active_smartevse_raw"], "smartevse_1", moment.isoformat())
            moment += timedelta(seconds=10)
        c._async_post.assert_not_awaited()

    async def test_fallback_payload_keeps_the_entity_ids(self):
        c, harness = await CycleTests.plugged_xc40(self)
        ids = intelligent_module.build_entities(lambda domain, key: f"{domain}.{key}")
        c._intelligent.entities = MagicMock(return_value=ids)
        c._intelligent.build_payload = MagicMock(side_effect=RuntimeError("payload bug"))
        with self.assertLogs("custom_components.smartevse_dual_charger", level="DEBUG"):
            data = await harness.cycle(L(11, 10, 1, 0, 30))
        self.assertEqual(data["intelligent"]["planner_error"], "payload_failed")
        self.assertEqual(data["intelligent"]["entities"], ids)
        self.assertEqual(data["intelligent"]["entities"]["intelligent_charge"], "switch.intelligent_charge")
        # Without an adapter the controller looks the ids up in the registry itself.
        c = make_controller(adapter=False, intelligent_enabled=True)
        with patch.object(controller_module, "registry_entities", return_value=ids) as lookup:
            data = await Harness(c).cycle(L(11, 10, 1))
        lookup.assert_called_with(c.hass, "test")
        self.assertEqual(data["intelligent"]["entities"], ids)


class SetupRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_rebuilds_the_payload_once_the_platforms_registered_their_entities(self):
        order = []
        coordinator = MagicMock()
        coordinator.async_config_entry_first_refresh = AsyncMock(side_effect=lambda: order.append("first_refresh"))

        async def refresh(reason):
            order.append(f"refresh:{reason}")

        coordinator._async_refresh_now = refresh
        controller = MagicMock(async_initialize=AsyncMock())
        hass = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock(side_effect=lambda *_: order.append("platforms"))
        entry = MagicMock(data={}, options={}, entry_id="test")
        tasks = []
        entry.async_create_task = MagicMock(side_effect=lambda _hass, coro, name: tasks.append(coro))
        with patch.object(integration, "SmartEVSEDualChargerController", return_value=controller), \
                patch.object(integration, "SmartEVSEDualChargerCoordinator", return_value=coordinator):
            self.assertTrue(await integration.async_setup_entry(hass, entry))
        self.assertEqual(order, ["first_refresh", "platforms"])
        self.assertEqual(len(tasks), 1)
        await tasks[0]
        self.assertEqual(order, ["first_refresh", "platforms", "refresh:platforms_ready"])


class CoordinatorListenerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        self.hass.state = CoreState.running
        frame.async_setup(self.hass)
        self.addAsyncCleanup(self.hass.async_stop)

    def coordinator(self, controller):
        controller.get_update_interval.return_value = 10
        controller.async_run_cycle = AsyncMock(return_value={"state": "idle"})
        controller.async_shutdown = AsyncMock()
        coordinator = SmartEVSEDualChargerCoordinator(
            self.hass, controller, entry_data={"price_sensor_entity": PRICE_ENTITY, "schedule_entity": "schedule.x"},
            options={"push_currents": False, "push_ev_meter": False})
        self.addAsyncCleanup(coordinator.async_shutdown)
        return coordinator

    async def test_listener_includes_intelligent_source_entities(self):
        controller = MagicMock()
        controller.intelligent_source_entity_ids.return_value = [
            "sensor.real_electricity_price_hourly_prices_tomorrow", XC40_BATTERY, None, PRICE_ENTITY]
        self.coordinator(controller)
        self.hass.states.async_set(XC40_BATTERY, "54")
        await self.hass.async_block_till_done()
        self.hass.states.async_set("sensor.real_electricity_price_hourly_prices_tomorrow", "unknown",
                                   {"hourly_prices": []})
        await self.hass.async_block_till_done()
        reasons = [call.kwargs.get("reason") for call in controller.async_run_cycle.await_args_list]
        self.assertEqual(reasons.count("state_change"), 2)

    async def test_magicmock_or_failing_controller_keeps_the_existing_listener(self):
        for controller in (MagicMock(), MagicMock(**{"intelligent_source_entity_ids.side_effect": RuntimeError("x")})):
            self.coordinator(controller)
            self.hass.states.async_set(PRICE_ENTITY, "0.31")
            await self.hass.async_block_till_done()
            reasons = [call.kwargs.get("reason") for call in controller.async_run_cycle.await_args_list]
            self.assertIn("state_change", reasons)
            self.hass.states.async_set(PRICE_ENTITY, "0.32")
            await self.hass.async_block_till_done()


if __name__ == "__main__":
    unittest.main()
