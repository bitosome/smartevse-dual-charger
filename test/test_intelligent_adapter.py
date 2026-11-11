"""Intelligent charging HA adapter (intelligent.py): entity derivation, prices, facts, payload, store.

Native homeassistant.core.State objects and a SimpleNamespace hass are used for
state reads; registry derivation is checked on a bare HomeAssistant, a loaded
entity registry and a fake registry. Prices follow production (Real Electricity
Price v1.6.1, Nov 2026): three hourly lists that each run 01:00 -> 01:00 local,
so the local 00:00-01:00 hour sits in the previous day's list.
"""

from copy import deepcopy
from datetime import UTC, datetime, time, timedelta
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import CoreState, HomeAssistant, State
from homeassistant.helpers import area_registry, device_registry, entity_registry, floor_registry, label_registry
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.file import WriteError

from custom_components.smartevse_dual_charger import charge_model as cm
from custom_components.smartevse_dual_charger import charge_planner as cp
from custom_components.smartevse_dual_charger import intelligent as intel
from custom_components.smartevse_dual_charger.charging_cost import ChargingLedger, price_unit, tariff
from custom_components.smartevse_dual_charger.diagnostics import REDACTED, redact_runtime_state
from custom_components.smartevse_dual_charger.intelligent import (
    IntelligentCharging,
    OutletInput,
    OutletPayloadInput,
    PayloadContext,
    PlanInput,
)
from custom_components.smartevse_dual_charger.storage import ModelStore

TZ = ZoneInfo("Europe/Tallinn")
PRICE_ENTITY = "sensor.real_electricity_price_current_price"
LIST_IDS = {day: f"sensor.real_electricity_price_hourly_prices_{day}" for day in ("yesterday", "today", "tomorrow")}
XC40_BATTERY = "sensor.volvo_xc40_battery"
EX30_BATTERY = "sensor.volvo_ex30_battery"


def L(month, day, hour, minute=0, second=0):
    """Tallinn wall-clock time as an aware UTC datetime."""
    return datetime(2026, month, day, hour, minute, second, tzinfo=TZ).astimezone(UTC)


# Live 2026-11-09/10 consumer prices (EUR/kWh) from production-live.md / price-data.md.
# Hours marked "synthetic" were not recorded and only fill the list.
NOV_09 = {  # list "today" (2026-11-09 01:00 -> 2026-11-10 01:00)
    1: 0.286858, 2: 0.273004, 3: 0.29675,
    4: 0.28, 5: 0.29, 6: 0.33, 7: 0.40, 8: 0.45, 9: 0.42, 10: 0.38, 11: 0.35,  # synthetic
    12: 0.33, 13: 0.32, 14: 0.31, 15: 0.33, 16: 0.37, 17: 0.42,  # synthetic
    18: 0.4434, 19: 0.3909, 20: 0.3574, 21: 0.2418, 22: 0.2843, 23: 0.2396,
}
NOV_09_MIDNIGHT = 0.3105  # 2026-11-10 00:00 local, last entry of the 11-09 list
NOV_10 = {1: 0.2994, 2: 0.2780, 3: 0.2635, 4: 0.2983, 5: 0.2827, 6: 0.3575, 22: 0.3137, 23: 0.3534}
PEAK_10 = [0.4300, 0.6950, 0.6200, 0.5400, 0.5000, 0.4700, 0.4600, 0.4700, 0.5000, 0.5600, 0.6400,
           0.6500, 0.5600, 0.4480, 0.3544]  # 11-10 07..21, synthesised to the stated min/max/avg
for _offset, _price in enumerate(PEAK_10):
    NOV_10[7 + _offset] = _price
NOV_10_MIDNIGHT = 0.3052  # 2026-11-11 00:00 local
NOV_09_FROM_YESTERDAY = 0.248981  # 2026-11-09 00:00 local, in the 11-08 list


def entry(local_start, price, *, weekend=False):
    end = local_start + timedelta(hours=1)
    return {"start_time": local_start.isoformat(), "end_time": end.isoformat(), "nord_pool_price": None,
            "actual_price": price, "tariff": "off_peak", "is_holiday": False, "is_weekend": weekend}


def day_list(day, prices, midnight, *, weekend=False):
    items = [entry(datetime(2026, 11, day, hour, tzinfo=TZ), price, weekend=weekend)
             for hour, price in sorted(prices.items())]
    items.append(entry(datetime(2026, 11, day + 1, 0, tzinfo=TZ), midnight, weekend=weekend))
    return items


def yesterday_list():
    prices = {hour: 0.25 for hour in range(1, 24)}  # synthetic 11-08 (a Sunday)
    return day_list(8, prices, NOV_09_FROM_YESTERDAY, weekend=True)


def placeholder_list(day):
    """Tomorrow before publication: real times, null prices, state unknown."""
    return [entry(datetime(2026, 11, day, hour, tzinfo=TZ), None) for hour in range(1, 24)]


def put(states, entity_id, value, at=None, **attributes):
    at = at or L(11, 9, 17)
    states[entity_id] = State(entity_id, str(value), attributes, last_changed=at, last_reported=at, last_updated=at)


def price_states(states, *, tomorrow=True, unit="€/kWh"):
    put(states, PRICE_ENTITY, 0.4434, unit_of_measurement="€/kWh")
    put(states, LIST_IDS["yesterday"], 0.249, hourly_prices=yesterday_list(), unit_of_measurement=unit)
    put(states, LIST_IDS["today"], 0.3159, hourly_prices=day_list(9, NOV_09, NOV_09_MIDNIGHT), unit_of_measurement=unit)
    if tomorrow:
        put(states, LIST_IDS["tomorrow"], 0.4437, hourly_prices=day_list(10, NOV_10, NOV_10_MIDNIGHT),
            unit_of_measurement=unit)
    else:
        put(states, LIST_IDS["tomorrow"], "unknown", hourly_prices=placeholder_list(10), unit_of_measurement=unit)


ENTRY = {
    "entry_id": "test_entry",
    "price_sensor_entity": PRICE_ENTITY,
    "mains_l1_entity": "sensor.mains_a", "mains_l2_entity": "sensor.mains_b", "mains_l3_entity": "sensor.mains_c",
    "ev_meter_l1_entity": "sensor.ev_l1", "ev_meter_l2_entity": "sensor.ev_l2", "ev_meter_l3_entity": "sensor.ev_l3",
    "ev_meter_import_active_power_entity": "sensor.ev_power_total",
    "vehicle_1_battery_entity": XC40_BATTERY, "vehicle_2_battery_entity": EX30_BATTERY,
}
SETTINGS = {
    "vehicle_1": {"ready_by": "07:00", "minimum": 80},
    "vehicle_2": {"ready_by": "06:00", "minimum": 70},
    "other": {"ready_by": "06:30", "minimum": 60},
}


def base_states():
    states = {}
    price_states(states)
    for phase in "abc":
        put(states, f"sensor.mains_{phase}", 3.0, unit_of_measurement="A")
    for phase in "123":
        put(states, f"sensor.ev_l{phase}", 0.0, unit_of_measurement="A")
    put(states, "sensor.ev_power_total", 3, unit_of_measurement="W")
    put(states, XC40_BATTERY, 53.0, at=L(11, 9, 17, 54), unit_of_measurement="%")
    put(states, "sensor.volvo_xc40_battery_capacity", 78.182, unit_of_measurement="kWh")
    put(states, "sensor.volvo_xc40_target_battery_charge_level", 90, unit_of_measurement="%")
    put(states, EX30_BATTERY, 68.0, at=L(11, 9, 17, 54), unit_of_measurement="%")
    put(states, "sensor.volvo_ex30_battery_capacity", 69.0, unit_of_measurement="kWh")
    put(states, "sensor.volvo_ex30_target_battery_charge_level", 100, unit_of_measurement="%")
    return states


def fake_store(loaded=None):
    return SimpleNamespace(async_load=AsyncMock(return_value=loaded), async_save=AsyncMock(),
                           make_read_only=MagicMock())


def make_adapter(states, *, options=None, store=None, entry_data=None):
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    return IntelligentCharging(hass, entry_data or ENTRY, options or {}, store=store or fake_store())


def outlet(key, *, connected=True, charging=False, vehicle=None, plugged_at=None, deadline=None, mode="Off",
           available=True, vehicle_charging_state="", last_charging_at=None):
    extra = {} if last_charging_at is None else {"last_charging_at": last_charging_at}
    return OutletInput(key=key, available=available, connected=connected, charging=charging, mode=mode,
                       vehicle=vehicle, offered_current=16.0, max_current=16.0, max_mains=16.0, min_current=6.0,
                       plugged_at=plugged_at, deadline=deadline, vehicle_charging_state=vehicle_charging_state,
                       **extra)


def context(*, enabled=True, overridden=False, governing=True, allowed=False, reason=None, active=None,
            since=None, outlets=None, top_up=False):
    return PayloadContext(enabled=enabled, overridden=overridden, governing=governing, charge_allowed=allowed,
                          controller_state="intelligent", charge_reason=reason, active_outlet=active,
                          active_since=since, settings=SETTINGS, top_up_enabled=top_up,
                          outlets=outlets or {})


class TzMixin:
    def setUp(self):
        patcher = patch.object(dt_util, "DEFAULT_TIME_ZONE", TZ)
        patcher.start()
        self.addCleanup(patcher.stop)


class PriceUnitTests(unittest.TestCase):
    def test_price_unit_is_split_out_and_tariff_keeps_its_behaviour(self):
        self.assertEqual(price_unit("€/kWh"), (1, "EUR"))
        self.assertEqual(price_unit("EUR / MWh"), (0.001, "EUR"))
        self.assertEqual(price_unit("SEK/Wh"), (1000, "SEK"))
        self.assertEqual(price_unit("c/kWh"), (None, None))
        self.assertEqual(price_unit(None), (None, None))
        state = State("sensor.p", "0.25", {"unit_of_measurement": "€/kWh"})
        self.assertEqual(tariff(state), (0.25, "EUR"))
        self.assertEqual(tariff(State("sensor.p", "250", {"unit_of_measurement": "EUR/MWh"})), (0.25, "EUR"))
        self.assertEqual(tariff(State("sensor.p", "unknown", {"unit_of_measurement": "€/kWh"})), (None, None))
        self.assertEqual(tariff(State("sensor.p", "0.25", {"unit_of_measurement": "kWh"})), (None, None))


class PriceMergeTests(TzMixin, unittest.TestCase):
    def merged(self, states):
        entries, currency = intel.merge_price_lists([states.get(LIST_IDS[d]) for d in ("yesterday", "today", "tomorrow")])
        return {e["start"]: e["price"] for e in cm.normalize_price_entries(entries)}, currency

    def test_three_lists_merge_by_timestamp_including_midnight_hour_of_previous_list(self):
        states = {}
        price_states(states)
        merged, currency = self.merged(states)
        self.assertEqual(currency, "EUR")
        # 00:00-01:00 local of 11-09 only exists in the 11-08 ("yesterday") list.
        self.assertAlmostEqual(merged[L(11, 9, 0)], NOV_09_FROM_YESTERDAY)
        # 00:00-01:00 local of 11-10 is the last entry of the 11-09 ("today") list.
        self.assertAlmostEqual(merged[L(11, 10, 0)], NOV_09_MIDNIGHT)
        self.assertAlmostEqual(merged[L(11, 9, 21)], 0.2418)
        self.assertAlmostEqual(merged[L(11, 10, 3)], 0.2635)
        self.assertAlmostEqual(merged[L(11, 11, 0)], NOV_10_MIDNIGHT)
        # Every local hour from 11-08 01:00 to 11-11 01:00 exactly once.
        self.assertEqual(len(merged), 72)

    def test_list_unit_is_normalised_to_currency_per_kwh(self):
        states = {}
        price_states(states, unit="EUR/MWh")
        for day in ("yesterday", "today", "tomorrow"):
            items = deepcopy(states[LIST_IDS[day]].attributes["hourly_prices"])
            for item in items:
                item["actual_price"] = None if item["actual_price"] is None else item["actual_price"] * 1000
            put(states, LIST_IDS[day], "x", hourly_prices=items, unit_of_measurement="EUR/MWh")
        merged, currency = self.merged(states)
        self.assertEqual(currency, "EUR")
        self.assertAlmostEqual(merged[L(11, 9, 21)], 0.2418)

    def test_unknown_tomorrow_keeps_unit_but_gives_no_prices(self):
        states = {}
        price_states(states, tomorrow=False)
        merged, currency = self.merged(states)
        self.assertEqual(currency, "EUR")
        self.assertIsNone(merged[L(11, 10, 3)])  # placeholder hour stays unpriced, never 0
        self.assertAlmostEqual(merged[L(11, 10, 0)], NOV_09_MIDNIGHT)

    def test_unsupported_unit_or_missing_attributes_are_ignored(self):
        states = {}
        price_states(states, unit="c/kWh")
        merged, currency = self.merged(states)
        self.assertEqual(merged, {})
        self.assertIsNone(currency)
        self.assertEqual(intel.merge_price_lists([None, State("sensor.x", "unavailable", {})]), ([], None))


class HelperTests(TzMixin, unittest.TestCase):
    def test_ready_by_and_minimum_normalisation(self):
        self.assertEqual(intel.normalize_ready_by("7:0"), "07:00")
        self.assertEqual(intel.normalize_ready_by("06:45:30"), "06:45")
        self.assertEqual(intel.normalize_ready_by(time(5, 15, 59)), "05:15")
        for bad in ("", None, "25:00", "x", 7, datetime(2026, 1, 1)):
            self.assertEqual(intel.normalize_ready_by(bad), "07:00")
        self.assertEqual(intel.normalize_minimum(79.6), 80)
        self.assertEqual(intel.normalize_minimum(5), 10)
        self.assertEqual(intel.normalize_minimum(150), 100)
        for bad in (None, "x", float("nan"), True):
            self.assertEqual(intel.normalize_minimum(bad), 80)

    def test_next_local_occurrence_is_local_wall_clock_across_dst(self):
        # Spring forward 2027-03-28 03:00 EET -> 04:00 EEST: 07:00 stays 07:00 local.
        before = datetime(2027, 3, 27, 8, 0, tzinfo=TZ)
        deadline = intel.next_local_occurrence("07:00", before)
        self.assertEqual(deadline.astimezone(TZ).replace(tzinfo=None), datetime(2027, 3, 28, 7, 0))
        self.assertEqual(deadline, datetime(2027, 3, 28, 4, 0, tzinfo=UTC))
        # A nonexistent 03:30 moves forward by the gap to 04:30 local.
        missing = intel.next_local_occurrence("03:30", datetime(2027, 3, 28, 1, 0, tzinfo=TZ))
        self.assertEqual(missing.astimezone(TZ).replace(tzinfo=None), datetime(2027, 3, 28, 4, 30))
        # Fall back 2026-10-25 04:00 EEST -> 03:00 EET: 03:30 is the first occurrence (fold=0).
        ambiguous = intel.next_local_occurrence("03:30", datetime(2026, 10, 25, 1, 0, tzinfo=TZ))
        self.assertEqual(ambiguous, datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
        # 07:00 after the fall-back night is 05:00 UTC (EET), not 04:00.
        self.assertEqual(intel.next_local_occurrence("07:00", datetime(2026, 10, 24, 12, 0, tzinfo=TZ)),
                         datetime(2026, 10, 25, 5, 0, tzinfo=UTC))
        # Strictly after: exactly at ready-by gives tomorrow.
        self.assertEqual(intel.next_local_occurrence("07:00", L(11, 10, 7)), L(11, 11, 7))

    def test_session_deadline_plans_a_late_plug_in_for_the_next_occurrence(self):
        self.assertEqual(intel.session_deadline("07:00", L(11, 10, 5, 30), L(11, 10, 5, 30)), L(11, 10, 7))
        self.assertEqual(intel.session_deadline("07:00", L(11, 10, 6, 5), L(11, 10, 6, 5)), L(11, 11, 7))
        # Intelligent turned on at 06:30 for a car plugged in at 22:00: tonight's deadline stays.
        self.assertEqual(intel.session_deadline("07:00", L(11, 10, 6, 30), L(11, 9, 22)), L(11, 10, 7))

    def test_iso_minute_and_money(self):
        self.assertEqual(intel.iso_minute(L(11, 9, 21, 0, 29)), "2026-11-09T19:00:00+00:00")
        self.assertEqual(intel.iso_minute(L(11, 9, 21, 0, 31)), "2026-11-09T19:01:00+00:00")
        self.assertIsNone(intel.iso_minute(None))
        self.assertEqual(intel.money(1.234), 1.23)
        self.assertEqual(intel.money(-0.001), 0.0)
        self.assertIsNone(intel.money(float("inf")))


class RegistryDerivationTests(TzMixin, unittest.IsolatedAsyncioTestCase):
    async def test_bare_home_assistant_without_registries_never_raises(self):
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            hass.state = CoreState.running
            try:
                adapter = IntelligentCharging(hass, ENTRY, {}, store=fake_store())
                self.assertEqual(adapter.price_list_entity_ids(), list(LIST_IDS.values()))
                self.assertEqual(adapter.vehicle_entity("vehicle_1", "battery_capacity"),
                                 "sensor.volvo_xc40_battery_capacity")
                self.assertEqual(adapter.vehicle_entity("vehicle_2", "target_battery_charge_level"),
                                 "sensor.volvo_ex30_target_battery_charge_level")
                self.assertEqual(adapter.source_entity_ids(), [*LIST_IDS.values(), XC40_BATTERY, EX30_BATTERY])
                entities = adapter.entities()
                self.assertIsNone(entities["intelligent_charge"])
                self.assertEqual(entities["vehicle_1"], {"ready_by": None, "minimum_charge": None})
            finally:
                await hass.async_stop(force=True)

    async def test_retired_entities_are_removed_from_the_registry(self):
        # 0.0.10.0: the top-up switch and price are gone; upgrading must not leave them
        # behind as "unavailable" entities.
        import custom_components.smartevse_dual_charger as integration
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            hass.state = CoreState.running
            device_registry.async_setup(hass)
            for registry in (floor_registry, area_registry, label_registry, device_registry, entity_registry):
                await registry.async_load(hass)
            try:
                reg = entity_registry.async_get(hass)
                old_switch = reg.async_get_or_create("switch", "smartevse_dual_charger", "test_entry_intelligent_top_up")
                old_number = reg.async_get_or_create("number", "smartevse_dual_charger",
                                                     "test_entry_intelligent_top_up_price")
                kept = reg.async_get_or_create("switch", "smartevse_dual_charger", "test_entry_intelligent_charge")
                other = reg.async_get_or_create("switch", "smartevse_dual_charger", "other_entry_intelligent_top_up")
                integration._remove_retired_entities(hass, SimpleNamespace(entry_id="test_entry"))
                self.assertIsNone(reg.async_get(old_switch.entity_id))
                self.assertIsNone(reg.async_get(old_number.entity_id))
                self.assertIsNotNone(reg.async_get(kept.entity_id))
                self.assertIsNotNone(reg.async_get(other.entity_id))  # another entry's entity
                integration._remove_retired_entities(hass, SimpleNamespace(entry_id="test_entry"))  # idempotent
            finally:
                await hass.async_stop(force=True)

    async def test_loaded_registry_resolves_renamed_volvo_and_own_entities_and_skips_disabled(self):
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            hass.state = CoreState.running
            device_registry.async_setup(hass)
            for registry in (floor_registry, area_registry, label_registry, device_registry, entity_registry):
                await registry.async_load(hass)
            try:
                reg = entity_registry.async_get(hass)
                battery = reg.async_get_or_create("sensor", "volvo", "YV1VIN_battery_charge_level",
                                                  suggested_object_id="car_one_battery")
                reg.async_get_or_create("sensor", "volvo", "YV1VIN_battery_capacity",
                                        suggested_object_id="renamed_capacity")
                reg.async_get_or_create("sensor", "volvo", "YV1VIN_target_battery_charge_level",
                                        suggested_object_id="renamed_target",
                                        disabled_by=RegistryEntryDisabler.USER)
                switch = reg.async_get_or_create("switch", "smartevse_dual_charger", "test_entry_intelligent_charge",
                                                 suggested_object_id="my_intelligent")
                reg.async_get_or_create("time", "smartevse_dual_charger", "test_entry_vehicle_1_ready_by",
                                        suggested_object_id="xc40_ready", disabled_by=RegistryEntryDisabler.USER)
                reg.async_get_or_create("time", "smartevse_dual_charger", "test_entry_other_vehicle_ready_by",
                                        suggested_object_id="other_ready")
                adapter = IntelligentCharging(hass, {**ENTRY, "vehicle_1_battery_entity": battery.entity_id}, {},
                                              store=fake_store())
                self.assertEqual(adapter.vehicle_entity("vehicle_1", "battery_capacity"), "sensor.renamed_capacity")
                # A disabled registry entry is not used, and a registry-known Volvo battery
                # is never second-guessed by its entity_id suffix.
                self.assertIsNone(adapter.vehicle_entity("vehicle_1", "target_battery_charge_level"))
                entities = adapter.entities()
                self.assertEqual(entities["intelligent_charge"], switch.entity_id)
                self.assertIsNone(entities["vehicle_1"]["ready_by"])  # disabled -> None
                self.assertEqual(entities["other"]["ready_by"], "time.other_ready")
                self.assertNotIn("minimum_charge", entities["other"])  # no minimum for an unidentified car
                self.assertIsNone(entities["status"])
                # The price entity is not in the registry: string substitution fallback.
                self.assertEqual(adapter.price_list_entity_ids(), list(LIST_IDS.values()))
            finally:
                await hass.async_stop(force=True)

    def test_price_lists_come_from_the_price_config_entry_first(self):
        def registry_entry(entity_id, unique_id, *, platform="real_electricity_price", disabled=None,
                           domain="sensor"):
            return SimpleNamespace(entity_id=entity_id, unique_id=unique_id, platform=platform,
                                   disabled_by=disabled, domain=domain, config_entry_id="rep")
        entries = [
            registry_entry("sensor.price_now", "rep_real_electricity_price_current_price"),
            registry_entry("sensor.prices_d0", "rep_real_electricity_price_hourly_prices_today"),
            registry_entry("sensor.prices_d1", "rep_real_electricity_price_hourly_prices_tomorrow"),
            registry_entry("sensor.prices_dm1", "rep_real_electricity_price_hourly_prices_yesterday"),
            registry_entry("sensor.prices_old", "rep_real_electricity_price_hourly_prices_today_v0",
                           disabled="user"),
            registry_entry("sensor.other_platform", "x_hourly_prices_today", platform="nordpool"),
        ]
        fake = SimpleNamespace(async_get=lambda entity_id: entries[0] if entity_id == "sensor.price_now" else None)
        adapter = make_adapter({}, entry_data={**ENTRY, "price_sensor_entity": "sensor.price_now"})
        with patch.object(intel.er, "async_get", return_value=fake), \
                patch.object(intel.er, "async_entries_for_config_entry", return_value=entries):
            self.assertEqual(adapter.price_list_entity_ids(),
                             ["sensor.prices_dm1", "sensor.prices_d0", "sensor.prices_d1"])
        # A renamed price entity without registry entry and without "current_price" in its id.
        self.assertEqual(adapter.price_list_entity_ids(), [])

    def test_build_entities_shape(self):
        seen = []

        def lookup(domain, key):
            seen.append((domain, key))
            return f"{domain}.{key}"

        entities = intel.build_entities(lookup)
        self.assertEqual(entities["vehicle_2"], {"ready_by": "time.vehicle_2_ready_by",
                                                 "minimum_charge": "number.vehicle_2_minimum_charge"})
        self.assertEqual(entities["next_charge"], "sensor.intelligent_next_charge")
        self.assertEqual(len(seen), 10)
        self.assertEqual(len(set(key for _, key in seen)), 10)


class FactsTests(TzMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.states = base_states()
        self.adapter = make_adapter(self.states)
        await self.adapter.async_initialize(None)
        self.now = L(11, 9, 18, 0)

    def observe(self, outlets, now=None, **kwargs):
        return self.adapter.observe(now or self.now, outlets={o.key: o for o in outlets}, settings=SETTINGS,
                                    controller_state="intelligent", charge_reason="intelligent_charging", **kwargs)

    def test_mapped_vehicle_uses_its_settings_and_car_limit(self):
        result = self.observe([outlet("smartevse_1", vehicle="vehicle_1", plugged_at=L(11, 9, 17, 56)),
                               outlet("smartevse_2", connected=False)])
        facts = result.facts["smartevse_1"]
        self.assertNotIn("smartevse_2", result.facts)
        self.assertEqual((facts.settings_key, facts.minimum, facts.ready_by), ("vehicle_1", 80, "07:00"))
        self.assertEqual((facts.soc, facts.soc_source), (53.0, "vehicle"))
        self.assertEqual((facts.car_limit, facts.target, facts.limited_by_car), (90.0, 80.0, False))
        self.assertEqual(facts.capacity_kwh, 78.182)
        self.assertEqual(facts.kwh_per_pct_source, "capacity")
        self.assertAlmostEqual(facts.kwh_per_pct, 0.8209, places=3)

    def test_unmapped_vehicle_uses_other_vehicle_settings(self):
        facts = self.observe([outlet("smartevse_2", vehicle=None, plugged_at=L(11, 9, 17, 56))]).facts["smartevse_2"]
        self.assertEqual((facts.settings_key, facts.name, facts.minimum, facts.ready_by),
                         ("other", "Unidentified vehicle", 60, "06:30"))
        self.assertEqual((facts.soc, facts.soc_source, facts.car_limit), (None, "unknown", None))
        self.assertEqual(facts.budget_kwh, 30.0)  # unknown-SoC budget default

    def test_session_cap_reaches_the_planner_inputs(self):
        # Review round 4: a car that drew clearly less than offered in this plug session
        # is planned at the median current it took (the only path from the model's
        # session evidence to the planner).
        cars = [outlet("smartevse_2", vehicle="vehicle_2", plugged_at=L(11, 9, 17, 56))]
        before = self.observe(cars).facts["smartevse_2"]
        session = self.adapter.model._sessions["smartevse_2"]
        session["power_windows"] = [(self.now, "vehicle_2", 8.0, True), (self.now, "vehicle_2", 8.2, True)]
        after = self.observe(cars, now=self.now + timedelta(seconds=10)).facts["smartevse_2"]
        kpa = self.adapter.model.kw_per_phase_amp().value
        self.assertGreater(before.cap_kw, after.cap_kw)
        self.assertAlmostEqual(after.cap_kw, round(3 * kpa * 8.1, 3), places=3)
        self.assertAlmostEqual(after.cap_low_kw, round(after.cap_kw * 0.95, 3), places=3)

    def test_car_limit_below_minimum_limits_the_target(self):
        put(self.states, "sensor.volvo_ex30_target_battery_charge_level", 65, unit_of_measurement="%")
        facts = self.observe([outlet("smartevse_2", vehicle="vehicle_2", plugged_at=L(11, 9, 17, 56))]).facts["smartevse_2"]
        self.assertEqual((facts.minimum, facts.car_limit, facts.target, facts.limited_by_car), (70, 65.0, 65.0, True))

    def test_capacity_option_overrides_the_volvo_sensor(self):
        adapter = make_adapter(self.states, options={"vehicle_1_battery_capacity_kwh": 72.0})
        facts = adapter.observe(self.now, outlets={"smartevse_1": outlet("smartevse_1", vehicle="vehicle_1")},
                                settings=SETTINGS).facts["smartevse_1"]
        self.assertEqual(facts.capacity_kwh, 72.0)
        self.assertAlmostEqual(facts.kwh_per_pct, 0.756, places=3)

    def test_unavailable_outlet_keeps_its_car_and_vehicle_timer_is_reported(self):
        facts = self.observe([outlet("smartevse_1", vehicle="vehicle_1", available=False,
                                     vehicle_charging_state="scheduled")]).facts["smartevse_1"]
        self.assertFalse(facts.available)
        self.assertTrue(facts.car_timer)

    def test_cross_check_requests_a_swap_after_ten_minutes_and_two_points(self):
        # Mapping says XC40 on SmartEVSE 1 (Off) while SmartEVSE 2 is the only one charging,
        # yet the XC40's SoC rises: the cars are swapped.
        put(self.states, "sensor.ev_power_total", 10300, unit_of_measurement="W")
        start = L(11, 10, 1, 0)
        result = None
        for minute in range(0, 12):
            now = start + timedelta(minutes=minute)
            soc = 53.0 + (2.0 if minute >= 11 else 1.0 if minute >= 6 else 0.0)
            put(self.states, XC40_BATTERY, soc, at=now, unit_of_measurement="%")
            result = self.observe([
                outlet("smartevse_1", vehicle="vehicle_1", mode="Off"),
                outlet("smartevse_2", vehicle="vehicle_2", charging=True, mode="Smart"),
            ], now=now)
            if minute < 11:
                self.assertIsNone(result.swap)
        self.assertEqual(result.swap, ("smartevse_1", "smartevse_2"))

    def run_cross_check(self, *, xc40_socs, ex30_socs=None, se1_last_charging=None, minutes=21):
        """XC40 mapped to SmartEVSE 1 (Off), EX30 mapped to SmartEVSE 2 (charging, 10.3 kW)."""
        put(self.states, "sensor.ev_power_total", 10300, unit_of_measurement="W")
        start = L(11, 10, 1, 0)
        swaps = []
        for minute in range(0, minutes):
            now = start + timedelta(minutes=minute)
            put(self.states, XC40_BATTERY, xc40_socs(minute), at=now, unit_of_measurement="%")
            if ex30_socs is not None:
                put(self.states, EX30_BATTERY, ex30_socs(minute), at=now, unit_of_measurement="%")
            result = self.observe([
                outlet("smartevse_1", vehicle="vehicle_1", mode="Off", last_charging_at=se1_last_charging),
                outlet("smartevse_2", vehicle="vehicle_2", charging=True, mode="Smart"),
            ], now=now)
            if result.swap is not None:
                swaps.append(minute)
        return swaps

    def test_cross_check_ignores_a_lagging_rise_after_a_hand_off(self):
        # Review round 3: the XC40 just finished on SmartEVSE 1 at 01:00; its Volvo
        # reading trails the battery and keeps rising while SmartEVSE 2 charges the
        # EX30. The mapping is right and must never be swapped.
        lagging = lambda minute: 88.0 + min(minute, 10) * 0.3  # noqa: E731 - +3 points
        flat = lambda minute: 50.0  # noqa: E731
        # The adapter itself saw SmartEVSE 1 charge until 00:59:50 ...
        self.observe([outlet("smartevse_1", vehicle="vehicle_1", charging=True, mode="Smart"),
                      outlet("smartevse_2", vehicle="vehicle_2")], now=L(11, 10, 0, 59, 50))
        self.assertEqual(self.run_cross_check(xc40_socs=lagging, ex30_socs=flat), [])
        # ... or only the controller's persisted last-charging time says so (restart).
        self.adapter = make_adapter(self.states)
        self.assertEqual(self.run_cross_check(xc40_socs=lagging, ex30_socs=flat,
                                              se1_last_charging=L(11, 10, 0, 59, 50)), [])

    def test_cross_check_needs_the_charging_outlets_car_to_stay_flat(self):
        # The EX30 mapped to the charging outlet rises as well: it is the car charging
        # there, so the XC40's rise is not evidence (no own charge for an hour either).
        rising = lambda minute: 53.0 + minute * 0.25  # noqa: E731
        self.assertEqual(self.run_cross_check(xc40_socs=rising, ex30_socs=lambda minute: 50.0 + minute * 0.25), [])

    def test_cross_check_ignores_a_jump_the_metered_energy_cannot_explain(self):
        # A stalled XC40 reading catches up by 6 points at once (4.5 kWh) while only
        # ~1.9 kWh were metered in the window: not this charge.
        self.assertEqual(self.run_cross_check(xc40_socs=lambda minute: 53.0 if minute < 11 else 59.0,
                                              ex30_socs=lambda minute: 50.0, minutes=13), [])

    def test_cross_check_resets_when_the_power_drops(self):
        start = L(11, 10, 1, 0)
        for minute in range(0, 12):
            now = start + timedelta(minutes=minute)
            put(self.states, "sensor.ev_power_total", 10300 if minute != 5 else 2000, unit_of_measurement="W")
            put(self.states, XC40_BATTERY, 53.0 + minute * 0.3, at=now, unit_of_measurement="%")
            result = self.observe([
                outlet("smartevse_1", vehicle="vehicle_1", mode="Off"),
                outlet("smartevse_2", vehicle="vehicle_2", charging=True, mode="Smart"),
            ], now=now)
            self.assertIsNone(result.swap)


class PlanTests(TzMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.states = base_states()
        self.adapter = make_adapter(self.states)
        await self.adapter.async_initialize(None)
        self.now = L(11, 9, 18, 0)
        self.deadline = L(11, 10, 7)

    def plan_xc40(self, now=None, **plan_kwargs):
        now = now or self.now
        self.adapter.observe(now, outlets={"smartevse_1": outlet("smartevse_1", vehicle="vehicle_1",
                                                                  plugged_at=L(11, 9, 17, 56))},
                             settings=SETTINGS, controller_state="intelligent")
        return self.adapter.plan(now, {"smartevse_1": PlanInput("smartevse_1", self.deadline, **plan_kwargs)})

    def test_xc40_waits_for_the_cheapest_hours_with_real_prices(self):
        decision = self.plan_xc40()
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "intelligent_waiting_for_planned_slot")
        self.assertIsNone(decision.controller_error)
        plan = self.adapter._plan
        self.assertFalse(plan.no_price_mode)
        self.assertEqual(plan.known_until, L(11, 11, 1))
        self.assertEqual(self.adapter._known_until, L(11, 11, 1))
        blocks = plan.car("smartevse_1").blocks
        self.assertEqual(blocks[0].start, L(11, 9, 21))  # 0.2418 is the cheapest hour tonight
        self.assertTrue(all(block.end <= self.deadline for block in blocks))

    def test_waits_for_tomorrows_prices_and_replans_when_they_arrive(self):
        # Round 5 (owner): at 11:00 tomorrow's prices are not published. The plan avoids
        # the known hours before 15:00, puts nothing into forecast hours the known hours
        # do not need, and is rebuilt as soon as the prices arrive.
        price_states(self.states, tomorrow=False)
        self.deadline = L(11, 10, 7)
        now = L(11, 9, 11)
        self.plan_xc40(now=now)
        self.assertEqual(self.adapter._prices_expected_at, L(11, 9, 15))
        plan = self.adapter._plan
        segments = plan.segments
        self.assertTrue(segments)
        self.assertTrue(all(seg.start >= L(11, 9, 15) for seg in segments))
        self.assertTrue(all(seg.price_kind == "known" for seg in segments))
        # Published at 14:40: the next cycle plans with them and stops waiting.
        price_states(self.states, tomorrow=True)
        later = L(11, 9, 14, 40)
        self.plan_xc40(now=later)
        self.assertIsNone(self.adapter._prices_expected_at)
        self.assertEqual(self.adapter._known_until, L(11, 11, 1))
        replanned = self.adapter._plan.car("smartevse_1").blocks
        self.assertEqual(replanned[0].start, L(11, 9, 21))  # tonight's cheapest hours
        self.assertNotEqual([(b.start, b.end) for b in replanned],
                            [(b.start, b.end) for b in plan.car("smartevse_1").blocks])

    def test_charging_above_the_minimum_waits_for_tomorrows_prices(self):
        # 0.0.11.0: XC40 at 85 % (minimum 80, car limit 90) at 11:00, ready-by tomorrow
        # 07:00 and tomorrow's list not published. Its minimum is in, so nothing has to
        # happen now: the charging above it waits for the night's prices.
        price_states(self.states, tomorrow=False)
        self.deadline = L(11, 10, 7)
        put(self.states, XC40_BATTERY, 85.0, at=L(11, 9, 10, 58), unit_of_measurement="%")
        now = L(11, 9, 11)
        self.adapter.observe(now, outlets={"smartevse_1": outlet("smartevse_1", vehicle="vehicle_1",
                                                                  plugged_at=L(11, 9, 10, 56))},
                             settings=SETTINGS, controller_state="intelligent")
        inputs = {"smartevse_1": PlanInput("smartevse_1", self.deadline)}
        decision = self.adapter.plan(now, inputs)
        self.assertEqual((decision.allowed, decision.reason), (False, "intelligent_targets_reached"))
        self.assertIsNone(self.adapter._prices_expected_at)  # nobody needs a minimum
        self.assertEqual(self.adapter._extra_waits_until, L(11, 9, 15))
        self.assertEqual(self.adapter._plan.segments, ())
        payload, _ = self.adapter.build_payload(now, context(outlets={
            "smartevse_1": OutletPayloadInput(deadline=self.deadline)}))
        charger = payload["chargers"]["smartevse_1"]
        self.assertEqual((charger["status"], charger["extra_soc"], charger["extra_waits_until"]),
                         ("reached", None, "2026-11-09T13:00:00+00:00"))
        self.assertEqual(payload["top_up"]["waits_until"], "2026-11-09T13:00:00+00:00")
        # Published: the cheapest hour of the night takes it.
        price_states(self.states, tomorrow=True)
        later = L(11, 9, 14, 40)
        self.adapter.observe(later, outlets={"smartevse_1": outlet("smartevse_1", vehicle="vehicle_1",
                                                                    plugged_at=L(11, 9, 10, 56))},
                             settings=SETTINGS, controller_state="intelligent")
        self.adapter.plan(later, inputs)
        self.assertIsNone(self.adapter._extra_waits_until)
        car = self.adapter._plan.car("smartevse_1")
        self.assertEqual((car.extra_soc, [block.top_up for block in car.blocks]), (90.0, [True]))
        self.assertEqual(car.blocks[0].start, L(11, 9, 23))  # 0.2396, the cheapest hour
        # Late prices (15:01, still missing): the known hours are used, never a forecast.
        price_states(self.states, tomorrow=False)
        late = L(11, 9, 15, 1)
        self.adapter.observe(late, outlets={"smartevse_1": outlet("smartevse_1", vehicle="vehicle_1",
                                                                   plugged_at=L(11, 9, 10, 56))},
                             settings=SETTINGS, controller_state="intelligent")
        self.adapter.plan(late, inputs)
        self.assertIsNone(self.adapter._extra_waits_until)
        segments = self.adapter._plan.segments
        self.assertTrue(segments)
        self.assertTrue(all(seg.top_up and seg.price_kind == "known" for seg in segments))

    def test_only_cars_that_need_charge_make_the_plan_wait_for_prices(self):
        # Review 6: a car sitting at its minimum with a ready-by tomorrow made the other
        # car, whose own hours all have prices, avoid its cheapest hours before 15:00.
        price_states(self.states, tomorrow=False)
        now = L(11, 9, 9)

        def request(outlet, soc, target, deadline, **extra):
            fields = dict(outlet=outlet, vehicle="vehicle_1", settings_key="vehicle_1", name="x", soc=soc,
                          soc_source="vehicle", target_soc=target, car_limit=100.0, deadline=deadline,
                          kwh_per_pct=0.75, kwh_per_pct_conservative=0.8, cap_kw=10.7, cap_low_kw=10.2,
                          budget_kwh=None, done=False, overrun=False, drawing=False, priority=0)
            fields.update(extra)
            return intel.cp.CarRequest(**fields)

        self.plan_xc40(now=now)  # reads the price lists
        needs_tonight = request("smartevse_1", 40.0, 80.0, L(11, 9, 18))
        needs_tomorrow = request("smartevse_2", 40.0, 80.0, L(11, 10, 7))
        idle = request("smartevse_2", 85.0, 80.0, L(11, 10, 7), reached=True)
        expected = self.adapter._expected_prices
        self.assertEqual(expected(now, [needs_tomorrow]), L(11, 9, 15))
        self.assertIsNone(expected(now, [needs_tonight]))            # its window is fully priced
        self.assertIsNone(expected(now, [needs_tonight, idle]))      # the idle car does not count
        self.assertIsNone(expected(now, [needs_tonight, needs_tomorrow]))  # nobody waits
        self.assertIsNone(expected(now, [idle]))
        self.assertIsNone(expected(now, [request("smartevse_2", 40.0, 80.0, L(11, 10, 7), excluded=True)]))
        self.assertIsNone(expected(L(11, 9, 15, 1), [needs_tomorrow]))  # late: no waiting

    def test_the_midnight_hour_belongs_to_the_previous_days_list(self):
        # Day-ahead lists run 01:00 -> 01:00. A missing 00:00-01:00 hour is not in the
        # list published this afternoon, so the plan must not wait for it.
        price_states(self.states, tomorrow=True)
        self.plan_xc40(now=L(11, 9, 10))
        slots = self.adapter._price_slots
        hour = L(11, 10, 0)
        self.adapter._price_slots = [intel.cp.PriceSlot(s.start, s.end, None, "unknown") if s.start == hour else s
                                     for s in slots]
        car = intel.cp.CarRequest(outlet="smartevse_1", vehicle="vehicle_1", settings_key="vehicle_1", name="x",
                                  soc=40.0, soc_source="vehicle", target_soc=80.0, car_limit=100.0,
                                  deadline=L(11, 10, 7), kwh_per_pct=0.75, kwh_per_pct_conservative=0.8,
                                  cap_kw=10.7, cap_low_kw=10.2, budget_kwh=None, done=False, overrun=False,
                                  drawing=False, priority=0)
        self.assertIsNone(self.adapter._expected_prices(L(11, 9, 10), [car]))

    def test_late_prices_are_not_waited_for(self):
        # After 15:00 the missing prices are late: no waiting, unpriced hours last.
        price_states(self.states, tomorrow=False)
        self.plan_xc40(now=L(11, 9, 18))
        self.assertIsNone(self.adapter._prices_expected_at)
        self.assertTrue(all(seg.price_kind == "known" for seg in self.adapter._plan.segments
                            if seg.start < L(11, 10, 1)))

    def test_decision_at_a_planned_block_names_the_outlet_and_block_start(self):
        decision = self.plan_xc40(now=L(11, 9, 21, 0, 10))
        self.assertTrue(decision.allowed)
        self.assertEqual((decision.reason, decision.outlet), ("intelligent_charging", "smartevse_1"))
        self.assertEqual(decision.block_start, L(11, 9, 21))

    def test_last_good_prices_are_used_when_the_live_lists_disappear(self):
        self.plan_xc40()
        for entity_id in LIST_IDS.values():
            put(self.states, entity_id, "unavailable")
        decision = self.plan_xc40(now=self.now + timedelta(seconds=10))
        self.assertFalse(self.adapter._plan.no_price_mode)
        self.assertIsNone(decision.controller_error)
        self.assertEqual(self.adapter._plan.car("smartevse_1").blocks[0].start, L(11, 9, 21))

    def test_no_prices_at_all_gives_the_no_price_plan_and_a_warning_after_the_startup_grace(self):
        for entity_id in LIST_IDS.values():
            put(self.states, entity_id, "unavailable")
        decision = self.plan_xc40()
        self.assertTrue(self.adapter._plan.no_price_mode)
        self.assertFalse(decision.allowed)  # as late as possible, never "charge now"
        self.assertIsNone(decision.controller_error)  # first 10 min after start: grace
        decision = self.plan_xc40(now=self.now + timedelta(minutes=11))
        self.assertEqual(decision.controller_error, "intelligent_price_data_unavailable")

    def test_planner_exception_falls_back_to_the_no_price_plan(self):
        real = cp.build_plan
        calls = []

        def flaky(now, prices, powers, cars, config=cp.PlannerConfig(), commitment=None):
            calls.append(config)
            if not config.no_price_mode:
                raise RuntimeError("boom")
            return real(now, prices, powers, cars, config, commitment)

        with patch.object(intel.cp, "build_plan", side_effect=flaky), \
                self.assertLogs("custom_components.smartevse_dual_charger", level="ERROR"):
            decision = self.plan_xc40()
        self.assertEqual([c.no_price_mode for c in calls], [False, True])
        self.assertTrue(self.adapter._plan.no_price_mode)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.controller_error, "intelligent_planner_failed")
        self.assertIn("planner_failed", self.adapter.planner_error)

    def test_both_plans_failing_use_the_latest_start_emergency_rule(self):
        with patch.object(intel.cp, "build_plan", side_effect=RuntimeError("boom")), \
                self.assertLogs("custom_components.smartevse_dual_charger", level="ERROR"):
            early = self.plan_xc40()
            # 22.2 kWh at 10.7 kW, +10 % and +10 min: the latest start is about 04:33.
            late = self.plan_xc40(now=L(11, 10, 4, 40))
        self.assertFalse(early.allowed)
        self.assertEqual(early.controller_error, "intelligent_planner_failed")
        self.assertTrue(late.allowed)
        self.assertEqual((late.outlet, late.reason), ("smartevse_1", "intelligent_no_price_data"))

    def test_unverified_soc_at_target_gets_one_short_probe(self):
        # The SoC last changed hours before the plug-in: it cannot prove "reached".
        put(self.states, XC40_BATTERY, 85.0, at=L(11, 9, 9), unit_of_measurement="%")
        self.plan_xc40()
        facts = self.adapter.facts("smartevse_1")
        self.assertEqual(facts.soc_source, "unverified")
        car = self.adapter._plan.car("smartevse_1")
        minutes = sum((b.end - b.start).total_seconds() / 60 for b in car.blocks)
        self.assertGreater(minutes, 0)
        self.assertLessEqual(minutes, 15)
        # A verified SoC at the target needs nothing for its minimum: what is planned
        # is the charging above it, up to the car's own limit (90 %).
        put(self.states, XC40_BATTERY, 85.0, at=L(11, 9, 17, 58), unit_of_measurement="%")
        self.plan_xc40(now=self.now + timedelta(seconds=10))
        car = self.adapter._plan.car("smartevse_1")
        self.assertEqual((car.status, car.need_kwh, car.extra_soc), ("reached", 0.0, 90.0))
        self.assertTrue(car.blocks)
        self.assertTrue(all(block.top_up for block in car.blocks))
        # At the car's limit nothing is left.
        put(self.states, XC40_BATTERY, 90.0, at=L(11, 9, 17, 59), unit_of_measurement="%")
        self.plan_xc40(now=self.now + timedelta(seconds=20))
        self.assertEqual(self.adapter._plan.car("smartevse_1").blocks, ())

    def test_identifying_car_waits_unless_it_must_start_now(self):
        now = L(11, 9, 21, 1)
        self.adapter.observe(now, outlets={"smartevse_2": outlet("smartevse_2", plugged_at=L(11, 9, 20, 58))},
                             settings=SETTINGS)
        decision = self.adapter.plan(now, {"smartevse_2": PlanInput("smartevse_2", L(11, 10, 6, 30),
                                                                    identifying=True)})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "intelligent_identifying_vehicle")
        # A deadline that leaves no slack: a unit must start now even while identifying.
        decision = self.adapter.plan(now, {"smartevse_2": PlanInput("smartevse_2", L(11, 9, 23, 45),
                                                                    identifying=True)})
        self.assertTrue(decision.allowed)

    def test_minimum_reached_only_when_every_car_has_it(self):
        # Review round 3: a 53 % car that does not draw (done through not-accepting) or a
        # session completed below its target is not "Minimum charge reached".
        refusing = self.plan_xc40(done=True, not_accepting=True)
        self.assertEqual((refusing.allowed, refusing.reason), (False, "intelligent_vehicle_not_accepting"))
        complete = self.plan_xc40(done=True)
        self.assertEqual(complete.reason, "intelligent_charging_complete")
        put(self.states, XC40_BATTERY, 80.0, at=L(11, 9, 17, 58), unit_of_measurement="%")
        # At its minimum the car goes on to its own limit in the cheapest hours...
        reached = self.plan_xc40(now=self.now + timedelta(seconds=10), reached=True)
        self.assertEqual((reached.allowed, reached.reason), (False, "intelligent_waiting_for_planned_slot"))
        self.assertTrue(self.adapter._plan.car("smartevse_1").top_up)
        # ...and only at that limit is everything reached.
        put(self.states, XC40_BATTERY, 90.0, at=L(11, 9, 17, 59), unit_of_measurement="%")
        full = self.plan_xc40(now=self.now + timedelta(seconds=20), reached=True)
        self.assertEqual(full.reason, "intelligent_targets_reached")

    async def test_only_connected_car_excluded_by_policy_waits_for_the_selected_car(self):
        # Review round 3: "SmartEVSE 1 only" with only the EX30 on SmartEVSE 2: no price
        # warning, no "no_price_data" status, and the policy reason (not "no vehicle").
        now = L(11, 9, 18, 10) + timedelta(minutes=11)  # past the startup grace
        self.adapter.observe(L(11, 9, 18, 10), outlets={"smartevse_2": outlet("smartevse_2", vehicle="vehicle_2")},
                             settings=SETTINGS)
        self.adapter.observe(now, outlets={"smartevse_2": outlet("smartevse_2", vehicle="vehicle_2")},
                             settings=SETTINGS)
        decision = self.adapter.plan(now, {"smartevse_2": PlanInput("smartevse_2", L(11, 10, 6), excluded=True)})
        self.assertFalse(self.adapter._plan.no_price_mode)
        self.assertIsNone(decision.controller_error)
        self.assertEqual((decision.allowed, decision.reason),
                         (False, "smartevse_1_only_waiting_for_selected_ev"))
        payload, flat = self.adapter.build_payload(now, context(
            reason=decision.reason, outlets={"smartevse_2": OutletPayloadInput(deadline=L(11, 10, 6), excluded=True)}))
        self.assertEqual(payload["status"], "no_ev")
        self.assertEqual(payload["chargers"]["smartevse_2"]["status"], "excluded")

    def test_vehicle_timer_on_the_planned_car_is_a_warning(self):
        now = L(11, 9, 21, 0, 10)
        self.adapter.observe(now, outlets={"smartevse_1": outlet("smartevse_1", vehicle="vehicle_1",
                                                                  vehicle_charging_state="scheduled")},
                             settings=SETTINGS)
        decision = self.adapter.plan(now, {"smartevse_1": PlanInput("smartevse_1", self.deadline)})
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.controller_error, "smartevse_1_vehicle_timer_blocks_charging")


EXPECTED_TOP_KEYS = {
    "enabled", "status", "reason", "overridden_by", "connected_count", "currency", "known_until", "prices_expected_at",
    "forecast_used", "baseline_kind", "model_error", "planner_error", "totals", "next_charge", "order",
    "chargers", "vehicles", "household", "slots", "sessions", "top_up", "entities",
}
EXPECTED_CHARGER_KEYS = {
    "vehicle", "vehicle_name", "settings_key", "soc", "soc_source", "minimum", "target", "car_limit",
    "limited_by_car", "ready_by", "deadline", "status", "feasible", "estimated", "projected_soc", "need_kwh",
    "blocks", "next_start", "session_cost", "saving", "includes_forecast", "partial", "overrun",
    "not_accepting", "car_timer", "queue_after", "top_up", "top_up_cost", "extra_soc", "extra_waits_until",
    "extra_done", "planned_kwh", "planned_cost", "extra_price_limit",
}
EXPECTED_VEHICLE_KEYS = {"name", "ready_by", "minimum", "suggested_ready_by", "usually_leaves",
                         "learned_sessions", "model"}
EXPECTED_SLOT_KEYS = {"start", "end", "price", "kind", "smartevse_1_minutes", "smartevse_2_minutes", "expected_kw"}
EXPECTED_SESSION_KEYS = {"baseline_kind", "ledger_id", "outlet", "vehicle_name", "closed", "energy_kwh", "cost", "baseline_cost",
                         "saving", "mixed", "partial"}
EXPECTED_ENTITY_KEYS = {"intelligent_charge", "vehicle_1",
                        "vehicle_2", "other", "status", "planned_cost", "planned_saving", "next_charge"}


class PayloadTests(TzMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.states = base_states()
        self.adapter = make_adapter(self.states)
        await self.adapter.async_initialize(None)
        self.now = L(11, 9, 18, 0)
        self.deadlines = {"smartevse_1": L(11, 10, 7), "smartevse_2": L(11, 10, 6)}

    def cycle(self, now, *, xc40=53.0, allowed=False, reason="intelligent_waiting_for_planned_slot", active=None,
              since=None, overridden=False, first="smartevse_1"):
        put(self.states, XC40_BATTERY, xc40, at=L(11, 9, 17, 54), unit_of_measurement="%")
        outlets = {
            "smartevse_1": outlet("smartevse_1", vehicle="vehicle_1", plugged_at=L(11, 9, 17, 56),
                                  deadline=self.deadlines["smartevse_1"]),
            "smartevse_2": outlet("smartevse_2", vehicle="vehicle_2", plugged_at=L(11, 9, 17, 58),
                                  deadline=self.deadlines["smartevse_2"]),
        }
        self.adapter.observe(now, outlets=outlets, settings=SETTINGS, controller_state="intelligent")
        decision = self.adapter.plan(now, {key: PlanInput(key, self.deadlines[key], priority=0 if key == first
                                                          else 1) for key in outlets})
        payload_outlets = {key: OutletPayloadInput(deadline=self.deadlines[key]) for key in outlets}
        return decision, self.adapter.build_payload(now, context(allowed=allowed, reason=reason, active=active,
                                                                 since=since, overridden=overridden,
                                                                 governing=not overridden, outlets=payload_outlets))

    def test_payload_shape_is_exactly_the_contract(self):
        _, (payload, flat) = self.cycle(self.now)
        self.assertEqual(set(payload), EXPECTED_TOP_KEYS)
        self.assertEqual(set(payload["totals"]), {"cost", "saving", "energy_kwh", "includes_forecast", "partial"})
        self.assertEqual(set(payload["chargers"]), {"smartevse_1", "smartevse_2"})
        for charger in payload["chargers"].values():
            self.assertEqual(set(charger), EXPECTED_CHARGER_KEYS)
            for block in charger["blocks"]:
                self.assertEqual(set(block), {"start", "end", "top_up"})
        self.assertEqual(set(payload["vehicles"]), {"vehicle_1", "vehicle_2", "other"})
        self.assertEqual(set(payload["vehicles"]["vehicle_1"]), EXPECTED_VEHICLE_KEYS)
        self.assertEqual(set(payload["vehicles"]["vehicle_1"]["model"]), {"kwh_per_pct", "kwh_per_pct_source", "cap_kw"})
        self.assertEqual(payload["vehicles"]["other"], {"name": "Unidentified vehicle", "ready_by": "06:30",
                                                        "minimum": 60})
        self.assertEqual(set(payload["household"]), {"learned_days", "night_expected_kw"})
        self.assertTrue(payload["slots"])
        self.assertLessEqual(len(payload["slots"]), 36)
        for slot in payload["slots"]:
            self.assertEqual(set(slot), EXPECTED_SLOT_KEYS)
        self.assertEqual(set(payload["top_up"]), {"enabled", "price", "source", "waits_until"})
        self.assertEqual(set(payload["entities"]), EXPECTED_ENTITY_KEYS)
        self.assertEqual(set(payload["entities"]["other"]), {"ready_by"})
        self.assertEqual(set(flat), {"intelligent_status", "intelligent_planned_cost", "intelligent_planned_saving",
                                     "intelligent_currency", "intelligent_next_charge"})

    def test_payload_values_for_two_cars_waiting_for_their_slots(self):
        _, (payload, flat) = self.cycle(self.now)
        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["reason"], "intelligent_waiting_for_planned_slot")
        self.assertEqual((payload["currency"], payload["baseline_kind"], payload["connected_count"]),
                         ("EUR", "plug_in", 2))
        self.assertEqual(payload["known_until"], "2026-11-10T23:00:00+00:00")  # 2026-11-11 01:00 local
        xc40, ex30 = payload["chargers"]["smartevse_1"], payload["chargers"]["smartevse_2"]
        self.assertEqual((xc40["vehicle_name"], xc40["ready_by"], xc40["deadline"]),
                         ("Volvo XC40", "07:00", "2026-11-10T05:00:00+00:00"))
        self.assertEqual((ex30["vehicle_name"], ex30["minimum"], ex30["target"], ex30["car_limit"]),
                         ("Volvo EX30", 70, 70.0, 100.0))
        self.assertEqual(sorted(payload["order"]), ["smartevse_1", "smartevse_2"])
        second = payload["order"][1]
        self.assertEqual(payload["chargers"][second]["queue_after"], payload["order"][0])
        self.assertIsNone(payload["chargers"][payload["order"][0]]["queue_after"])
        for charger in (xc40, ex30):
            self.assertIn(charger["status"], {"planned", "tight"})
            self.assertTrue(charger["feasible"])
            self.assertIsNotNone(charger["session_cost"])
            self.assertEqual(charger["session_cost"], round(charger["session_cost"], 2))
            # Known prices from plug-in: the planned saving vs charging at plug-in is published.
            self.assertIsNotNone(charger["saving"])
        # The baseline charges one car after the other from plug-in, so the second car's
        # share can be small or negative; what the plan saves is the sum.
        self.assertGreater(payload["totals"]["saving"], 0)
        self.assertEqual(payload["totals"]["cost"], round(xc40["session_cost"] + ex30["session_cost"], 2))
        self.assertEqual(payload["totals"]["saving"], round(xc40["saving"] + ex30["saving"], 2))
        self.assertEqual(flat["intelligent_planned_cost"], payload["totals"]["cost"])
        self.assertEqual(flat["intelligent_currency"], "EUR")
        self.assertEqual(flat["intelligent_status"], "planned")
        self.assertIsNotNone(flat["intelligent_next_charge"].tzinfo)
        self.assertEqual(flat["intelligent_next_charge"], datetime.fromisoformat(payload["next_charge"]))
        for charger in (xc40, ex30):
            for block in charger["blocks"]:
                self.assertEqual(datetime.fromisoformat(block["start"]).second, 0)
        minutes = sum(slot["smartevse_1_minutes"] + slot["smartevse_2_minutes"] for slot in payload["slots"])
        self.assertGreater(minutes, 0)

    def test_two_identical_cycles_give_identical_payloads_and_a_soc_change_updates(self):
        _, (first, first_flat) = self.cycle(self.now)
        _, (second, second_flat) = self.cycle(self.now + timedelta(seconds=10))
        self.assertEqual(first, second)
        self.assertEqual(first_flat, second_flat)
        _, (third, _) = self.cycle(self.now + timedelta(seconds=20), xc40=60.0)
        self.assertNotEqual(first, third)
        self.assertEqual(third["chargers"]["smartevse_1"]["soc"], 60.0)

    def test_running_block_keeps_its_start_while_charging(self):
        now = L(11, 9, 21, 0, 20)
        decision, _ = self.cycle(now)
        outlet_key = decision.outlet
        self.assertIsNotNone(outlet_key)
        since = L(11, 9, 21)
        payloads = []
        for step in range(0, 6):
            moment = now + timedelta(minutes=step)
            _, (payload, flat) = self.cycle(moment, allowed=True, reason="intelligent_charging", active=outlet_key,
                                            since=since)
            payloads.append(payload)
            self.assertEqual(payload["status"], "charging")
            self.assertEqual(payload["chargers"][outlet_key]["status"], "charging")
            self.assertEqual(payload["chargers"][outlet_key]["blocks"][0]["start"], "2026-11-09T19:00:00+00:00")
        self.assertTrue(all(p == payloads[0] for p in payloads))

    def test_overridden_by_force(self):
        _, (payload, flat) = self.cycle(self.now, overridden=True)
        self.assertEqual((payload["status"], payload["overridden_by"], payload["reason"]),
                         ("overridden", "force", None))
        self.assertEqual(flat["intelligent_status"], "overridden")

    def test_disabled_payload_keeps_settings_and_entities(self):
        payload, flat = self.adapter.build_payload(self.now, context(enabled=False, governing=False))
        self.assertEqual(set(payload), EXPECTED_TOP_KEYS)
        self.assertEqual(payload["status"], "disabled")
        self.assertEqual(payload["chargers"], {"smartevse_1": None, "smartevse_2": None})
        self.assertEqual(payload["vehicles"]["vehicle_2"]["ready_by"], "06:00")
        self.assertEqual(flat["intelligent_status"], "disabled")
        self.assertIsNone(flat["intelligent_planned_cost"])
        self.assertIsNone(flat["intelligent_next_charge"])

    def test_identifying_and_not_accepting_statuses_hide_money(self):
        outlets = {"smartevse_2": outlet("smartevse_2", plugged_at=L(11, 9, 17, 58))}
        self.adapter.observe(self.now, outlets=outlets, settings=SETTINGS)
        self.adapter.plan(self.now, {"smartevse_2": PlanInput("smartevse_2", L(11, 10, 6, 30), identifying=True)})
        payload, flat = self.adapter.build_payload(self.now, context(
            reason="intelligent_identifying_vehicle",
            outlets={"smartevse_2": OutletPayloadInput(deadline=L(11, 10, 6, 30), identifying=True)}))
        charger = payload["chargers"]["smartevse_2"]
        self.assertEqual((charger["status"], charger["settings_key"], charger["vehicle"]),
                         ("identifying", "other", None))
        self.assertIsNone(charger["session_cost"])
        self.assertIsNone(charger["saving"])
        self.assertEqual(payload["status"], "identifying")
        self.assertIsNone(flat["intelligent_planned_cost"])
        payload, _ = self.adapter.build_payload(self.now + timedelta(seconds=10), context(
            outlets={"smartevse_2": OutletPayloadInput(deadline=L(11, 10, 6, 30), not_accepting=True)}))
        self.assertEqual(payload["chargers"]["smartevse_2"]["status"], "not_accepting")
        self.assertTrue(payload["chargers"]["smartevse_2"]["not_accepting"])
        self.assertEqual(payload["status"], "not_accepting")

    def test_realised_sessions_are_published_without_timestamps(self):
        ledger = ChargingLedger()
        sample = {"smartevse_1": {"available": True, "connected": True, "charging": True, "complete": False,
                                  "vehicle": "Volvo XC40"},
                  "smartevse_2": {"available": True, "connected": False, "charging": False, "complete": False}}
        start = L(11, 9, 21, 0, 5)
        for step in range(0, 12):
            ledger.observe(start + timedelta(seconds=10 * step), sample, 10.0, 0.2418, "EUR", ["p", "q"])
            self.adapter.observe(start + timedelta(seconds=10 * step),
                                 outlets={"smartevse_1": outlet("smartevse_1", vehicle="vehicle_1", charging=True,
                                                                mode="Smart", plugged_at=L(11, 9, 17, 56),
                                                                deadline=L(11, 10, 7))},
                                 settings=SETTINGS, ledger=ledger.snapshot(), controller_state="intelligent",
                                 charge_reason="intelligent_charging")
        payload, _ = self.adapter.build_payload(start + timedelta(minutes=2), context())
        self.assertEqual(len(payload["sessions"]), 1)
        session = payload["sessions"][0]
        self.assertEqual(set(session), EXPECTED_SESSION_KEYS)
        self.assertEqual((session["outlet"], session["vehicle_name"], session["closed"]),
                         ("smartevse_1", "Volvo XC40", False))
        self.assertGreater(session["energy_kwh"], 0)


    def test_windows_without_energy_are_not_published(self):
        # Review: the deadline roll of a car that stays plugged in closes the charging
        # window and opens an empty one with the same ledger id; once it is unplugged the
        # empty window is closed too and must not shadow the saving of the night.
        night = {"ledger_id": "7bf4b0", "outlet": "smartevse_1", "vehicle": "vehicle_1",
                 "vehicle_name": "Volvo XC40", "date": "2026-11-09", "closed": True, "energy_kwh": 21.67,
                 "cost": 4.12, "baseline_cost": 7.49, "saving": 3.37, "mixed": False, "partial": False}
        empty = {**night, "energy_kwh": 0.0, "cost": 0.0, "baseline_cost": None, "saving": None,
                 "date": "2026-11-10"}
        open_empty = {**empty, "closed": False, "ledger_id": "3cc125"}
        self.adapter.model.realised_sessions = MagicMock(return_value=[open_empty, empty, night])
        payload, _ = self.adapter.build_payload(self.now, context())
        self.assertEqual(payload["sessions"], [{
            "ledger_id": "7bf4b0", "outlet": "smartevse_1", "vehicle_name": "Volvo XC40", "closed": True,
            "baseline_kind": "plug_in",
            "energy_kwh": 21.67, "cost": 4.12, "baseline_cost": 7.49, "saving": 3.37, "mixed": False,
            "partial": False}])

    def test_reached_car_after_its_ready_by_roll_has_no_planned_cost(self):
        # Charged overnight to 80 %, still plugged in after 07:00: the new deadline window
        # has no energy and nothing is planned, so no "≈ €0.00" cost is published.
        self.deadlines = {"smartevse_1": L(11, 11, 7), "smartevse_2": L(11, 11, 6)}
        self.adapter.model.realised_sessions = MagicMock(return_value=[{
            "ledger_id": "a", "outlet": "smartevse_1", "vehicle": "vehicle_1", "vehicle_name": "Volvo XC40",
            "date": "2026-11-10", "closed": False, "energy_kwh": 0.0, "cost": 0.0, "baseline_cost": None,
            "saving": None, "mixed": False, "partial": False}])
        _, (payload, flat) = self.cycle(L(11, 10, 7, 10), xc40=80.0)
        charger = payload["chargers"]["smartevse_1"]
        self.assertEqual((charger["status"], charger["blocks"]), ("reached", []))
        self.assertIsNone(charger["session_cost"])
        self.assertIsNone(charger["saving"])

    def window_row(self, **values):
        row = {"ledger_id": "a", "outlet": "smartevse_1", "vehicle": "vehicle_1", "vehicle_name": "Volvo XC40",
               "date": "2026-11-09", "closed": False, "energy_kwh": 21.6, "cost": 5.65, "baseline_cost": None,
               "saving": None, "mixed": False, "partial": False, "top_up_kwh": 0.0, "id": "smartevse_1-1",
               "deadline": L(11, 10, 7).isoformat(), "baseline_start": L(11, 9, 17, 56).isoformat(),
               "intelligent_kwh": 21.6, "intelligent_priced_kwh": 21.6, "intelligent_cost": 5.65,
               "top_up_cost": 0.0}
        row.update(values)
        return row

    def test_window_of_the_previous_deadline_is_not_priced_with_the_new_one(self):
        # Review round 3: in the roll cycle the model has not closed the old window yet;
        # pairing its 21.6 kWh with the new deadline priced them at 07:00 morning peak.
        self.deadlines = {"smartevse_1": L(11, 11, 7), "smartevse_2": L(11, 11, 6)}
        self.adapter.model.realised_sessions = MagicMock(return_value=[self.window_row()])
        _, (payload, flat) = self.cycle(L(11, 10, 7, 0, 0), xc40=80.0)
        charger = payload["chargers"]["smartevse_1"]
        self.assertEqual((charger["status"], charger["session_cost"], charger["saving"]), ("reached", None, None))
        # Only the EX30's plan counts (since round 5 it is planned in known hours, so it
        # has a saving of its own); the old window adds nothing.
        self.assertEqual(flat["intelligent_planned_saving"], payload["chargers"]["smartevse_2"]["saving"])
        # Once the model closed it the payload is rebuilt in that cycle (window digest).
        self.adapter.model.realised_sessions = MagicMock(return_value=[self.window_row(closed=True, saving=3.45)])
        self.adapter.model.realised_digest = MagicMock(return_value=[["smartevse_1-1", True]])
        _, (after, _) = self.cycle(L(11, 10, 7, 0, 10), xc40=80.0)
        self.assertEqual(after["sessions"][0]["saving"], 3.45)
        self.assertTrue(after["sessions"][0]["closed"])

    def test_metered_top_up_keeps_the_planned_saving(self):
        # Review round 3: the first metered top-up kWh used to drop the planned saving.
        self.adapter.model.realised_sessions = MagicMock(return_value=[self.window_row(
            energy_kwh=10.0, cost=2.5, intelligent_kwh=10.0, intelligent_cost=2.5)])
        _, (base, _) = self.cycle(L(11, 9, 22))
        saving = base["chargers"]["smartevse_1"]["saving"]
        self.assertIsNotNone(saving)
        self.adapter.model.realised_sessions = MagicMock(return_value=[self.window_row(
            energy_kwh=10.2, cost=2.55, top_up_kwh=0.2, top_up_cost=0.05, intelligent_kwh=10.0,
            intelligent_cost=2.5)])
        self.adapter._display_key = None
        _, (topped, _) = self.cycle(L(11, 9, 22, 0, 10))
        self.assertEqual(topped["chargers"]["smartevse_1"]["saving"], saving)
        self.assertEqual(topped["chargers"]["smartevse_1"]["session_cost"],
                         round(base["chargers"]["smartevse_1"]["session_cost"] + 0.05, 2))

    def test_energy_since_the_battery_reading_is_counted_once(self):
        # Review round 3: the plan starts from the last battery reading while the window's
        # cost already includes the energy metered since it, so every rebuild at a unit
        # boundary briefly showed ~€0.20 more (and the saving dipped below €0.10).
        price = NOV_09[22]
        self.adapter.model.realised_sessions = MagicMock(return_value=[self.window_row(
            energy_kwh=2.0, cost=0.5, intelligent_kwh=2.0, intelligent_cost=0.5)])
        self.adapter.model.energy_since_reading = MagicMock(return_value=0.0)
        _, (before, _) = self.cycle(L(11, 9, 22))
        self.adapter.model.realised_sessions = MagicMock(return_value=[self.window_row(
            energy_kwh=3.0, cost=0.5 + price, intelligent_kwh=3.0, intelligent_cost=0.5 + price)])
        self.adapter.model.energy_since_reading = MagicMock(return_value=1.0)
        self.adapter._display_key = None
        _, (after, _) = self.cycle(L(11, 9, 22, 0, 10))
        for key in ("session_cost", "saving"):
            self.assertEqual(after["chargers"]["smartevse_1"][key], before["chargers"]["smartevse_1"][key], key)
        self.assertEqual(after["totals"]["cost"], before["totals"]["cost"])

    def test_reached_car_shows_the_realised_saving_of_its_window(self):
        # Review round 3: a car with nothing left to plan showed a planned saving that
        # differed from History's realised one for the same charge.
        self.adapter.model.realised_sessions = MagicMock(return_value=[self.window_row(
            baseline_cost=6.17, saving=0.52)])
        # (At the car's limit: at 80 % the charging above the minimum is still planned.)
        _, (payload, _) = self.cycle(L(11, 10, 5, 10), xc40=90.0)
        charger = payload["chargers"]["smartevse_1"]
        self.assertEqual((charger["status"], charger["session_cost"], charger["saving"]), ("reached", 5.65, 0.52))

    def test_car_at_its_minimum_goes_on_charging_while_the_other_is_full(self):
        # Owner, 2026-11-11 morning: the XC40 is at its limit (90 %) and the EX30 reached
        # its 70 % minimum (its own limit is 100 %), both ready by 07:00. The minimum is a
        # floor: the EX30 goes on charging; no learned price limit is needed for that.
        # 0.0.13.0: it does so in the hours that are cheap for this night (at or below the
        # average of the 12 hours before 07:00, plus 5 %: 0.3153 here). 05:00 costs 0.2827.
        self.deadlines = {"smartevse_1": L(11, 10, 7), "smartevse_2": L(11, 10, 7)}
        put(self.states, EX30_BATTERY, 70.0, at=L(11, 10, 5, 15), unit_of_measurement="%")
        now = L(11, 10, 5, 16)
        decision, (payload, _) = self.cycle(now, xc40=90.0)
        self.assertEqual((decision.allowed, decision.outlet, decision.reason),
                         (True, "smartevse_2", "intelligent_top_up"))
        xc40, ex30 = payload["chargers"]["smartevse_1"], payload["chargers"]["smartevse_2"]
        self.assertEqual((xc40["status"], xc40["blocks"], xc40["extra_soc"]), ("reached", [], None))
        self.assertEqual((ex30["status"], ex30["need_kwh"], ex30["top_up"]), ("reached", 0.0, True))
        # To 06:00: the last hour before the ready-by time costs 0.3575, the second dearest
        # of the night, so optional energy is not bought there.
        self.assertEqual([(block["end"], block["top_up"]) for block in ex30["blocks"]],
                         [("2026-11-10T04:00:00+00:00", True)])
        self.assertGreater(ex30["extra_soc"], 75.0)
        self.assertLess(ex30["extra_soc"], 100.0)  # 44 minutes are not enough for 30 %
        self.assertIsNone(ex30["extra_waits_until"])
        window = [0.3909, 0.3574, 0.2418, 0.2843, 0.2396, 0.3105, 0.2994, 0.2780, 0.2635, 0.2983, 0.2827, 0.3575]
        limit = round(sum(window) / 12 * 1.05, 4)
        self.assertEqual((ex30["extra_price_limit"], payload["top_up"]["price"]), (limit, limit))
        self.assertEqual(payload["top_up"]["source"], "car_limit")
        # In that last hour the car stays where it is.
        put(self.states, EX30_BATTERY, 78.0, at=L(11, 10, 6, 15), unit_of_measurement="%")
        decision, (payload, _) = self.cycle(L(11, 10, 6, 16), xc40=90.0)
        self.assertEqual((decision.allowed, decision.reason), (False, "intelligent_targets_reached"))
        self.assertEqual(payload["chargers"]["smartevse_2"]["blocks"], [])

    async def test_saving_is_compared_with_charge_with_schedule(self):
        # Owner, 2026-11-11: "saved vs charging at plug-in" is not how the cars were ever
        # charged. With a schedule helper configured the saving is compared with the same
        # energy charged by "Charge with schedule": its weekly windows (00:00-07:00 here)
        # and its settings.
        entity = "schedule.charge_schedule"
        definition = {day: [{"from": time(0, 0), "to": time(7, 0)}] for day in
                      ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}
        calls = []

        async def async_call(domain, service, data, blocking=False, return_response=False):
            calls.append((domain, service, data))
            return {entity: definition}

        put(self.states, entity, "off")
        hass = SimpleNamespace(states=SimpleNamespace(get=self.states.get), services=SimpleNamespace(async_call=async_call))
        self.adapter = IntelligentCharging(hass, {**ENTRY, "schedule_entity": entity}, {}, store=fake_store())
        await self.adapter.async_initialize(None)
        self.deadlines = {"smartevse_1": L(11, 10, 7), "smartevse_2": L(11, 10, 7)}
        self.assertEqual(self.adapter.baseline_kind, "schedule")
        # Until the schedule has been read there is nothing to compare with.
        self.adapter.set_schedule_settings(price_limit=None, duty_minutes=60, first="smartevse_1")
        _, (payload, _) = self.cycle(self.now)
        self.assertEqual(payload["baseline_kind"], "schedule")
        self.assertIsNone(payload["chargers"]["smartevse_1"]["saving"])
        self.assertIsNone(payload["totals"]["saving"])
        await self.adapter.async_refresh_schedule(self.now)
        self.assertEqual(calls, [("schedule", "get_schedule", {"entity_id": entity})])
        _, (payload, _) = self.cycle(self.now + timedelta(seconds=10))
        xc40, ex30 = payload["chargers"]["smartevse_1"], payload["chargers"]["smartevse_2"]
        self.assertIsNotNone(xc40["saving"])
        self.assertIsNotNone(ex30["saving"])
        schedule_total = payload["totals"]["saving"]
        # The schedule charges in the night like the plan does, so the plan saves far
        # less against it than against charging in the dear evening hours at plug-in.
        plug_in = make_adapter(self.states)
        await plug_in.async_initialize(None)
        self.adapter, kept = plug_in, self.adapter
        _, (other, _) = self.cycle(self.now)
        self.adapter = kept
        self.assertEqual(other["baseline_kind"], "plug_in")
        self.assertLess(schedule_total, other["totals"]["saving"] - 1.0)
        self.assertGreater(schedule_total, -0.5)
        # The comparison follows the schedule settings: with the price option on only hours
        # at or below the acceptable price count, and too few of them means no figure.
        self.adapter.set_schedule_settings(price_limit=0.27, duty_minutes=60, first="smartevse_1")
        _, (gated, _) = self.cycle(self.now + timedelta(seconds=20))
        self.assertNotEqual(gated["totals"]["saving"], schedule_total)
        self.adapter.set_schedule_settings(price_limit=None, duty_minutes=15, first="smartevse_2")
        _, (other_order, _) = self.cycle(self.now + timedelta(seconds=30))
        self.assertIsNotNone(other_order["totals"]["saving"])
        # The definition is read again only when the helper changes or after ten minutes.
        await self.adapter.async_refresh_schedule(self.now + timedelta(minutes=5))
        self.assertEqual(len(calls), 1)
        await self.adapter.async_refresh_schedule(self.now + timedelta(minutes=11))
        self.assertEqual(len(calls), 2)
        put(self.states, entity, "on")
        await self.adapter.async_refresh_schedule(self.now + timedelta(minutes=12))
        self.assertEqual(len(calls), 3)
        # A failed read keeps the last definition and is retried a minute later, not every cycle.
        async def broken(*_args, **_kwargs):
            calls.append("broken")
            raise RuntimeError("no schedule")
        hass.services.async_call = broken
        put(self.states, entity, "off")
        await self.adapter.async_refresh_schedule(self.now + timedelta(minutes=13))
        await self.adapter.async_refresh_schedule(self.now + timedelta(minutes=13, seconds=10))
        self.assertEqual(calls.count("broken"), 1)
        self.assertIsNotNone(self.adapter._schedule_definition)
        await self.adapter.async_refresh_schedule(self.now + timedelta(minutes=14, seconds=10))
        self.assertEqual(calls.count("broken"), 2)
        # 0.0.13.0: a schedule too short to deliver the energy by the ready-by time (one hour
        # before 07:00) is compared at the price it would have paid until then. Before, the
        # rest went to the next night, whose prices are not published: no figure.
        short = {day: [{"from": time(6, 0), "to": time(7, 0)}] for day in definition}
        definition.clear()
        definition.update(short)
        hass.services.async_call = async_call
        put(self.states, entity, "on")
        await self.adapter.async_refresh_schedule(self.now + timedelta(minutes=16))
        self.adapter.set_schedule_settings(price_limit=None, duty_minutes=60, first="smartevse_1")
        _, (tight, _) = self.cycle(self.now + timedelta(minutes=16, seconds=10))
        self.assertIsNotNone(tight["chargers"]["smartevse_1"]["saving"])
        # The second car never gets a turn in that hour: nothing to compare its charge with.
        self.assertIsNone(tight["chargers"]["smartevse_2"]["saving"])

    def test_planned_baseline_starts_at_the_windows_baseline_start(self):
        # Review round 3: a late plug-in (06:30, deadline tomorrow 07:00) was anchored at
        # "now", so the saving grew every 15 minutes for the same plan.
        self.deadlines = {"smartevse_1": L(11, 11, 7), "smartevse_2": L(11, 11, 6)}
        anchors = []
        real = cp.baseline_costs

        def spy(sessions, *args, **kwargs):
            anchors.append({session.outlet: session.plugged_at for session in sessions}.get("smartevse_1"))
            return real(sessions, *args, **kwargs)

        with patch.object(intel.cp, "baseline_costs", side_effect=spy):
            for minute in (30, 45, 59):
                row = self.window_row(energy_kwh=0.0, cost=0.0, intelligent_kwh=0.0, intelligent_cost=0.0,
                                      deadline=L(11, 11, 7).isoformat(),
                                      baseline_start=L(11, 10, 6, 30).isoformat())
                self.adapter.model.realised_sessions = MagicMock(return_value=[row])
                self.adapter._display_key = None
                self.cycle(L(11, 10, 6, minute), xc40=40.0)
        self.assertEqual(anchors, [L(11, 10, 6, 30)] * 3)

    def test_policy_change_with_equal_deadlines_rebuilds_order_and_blocks(self):
        # Review round 3: the payload signature ignored the planner's priority input, so
        # "Charges first" and the blocks stayed stale for up to 15 min.
        self.deadlines = {"smartevse_1": L(11, 10, 7), "smartevse_2": L(11, 10, 7)}
        put(self.states, EX30_BATTERY, 53.0, at=L(11, 9, 17, 54), unit_of_measurement="%")
        _, (first, _) = self.cycle(L(11, 9, 20, 31))
        _, (swapped, _) = self.cycle(L(11, 9, 20, 31, 10), first="smartevse_2")
        self.assertNotEqual(list(self.adapter._plan.order), first["order"])
        self.assertEqual(list(self.adapter._plan.order), swapped["order"])

    def test_payload_does_not_switch_to_planned_while_bridging_a_unit_boundary(self):
        since = L(11, 9, 21)
        now = L(11, 9, 21, 0, 20)
        decision, _ = self.cycle(now)
        outlet_key = decision.outlet
        self.assertIsNotNone(outlet_key)
        drawing = {key: PlanInput(key, self.deadlines[key], priority=0 if key == "smartevse_1" else 1,
                                  drawing=key == outlet_key) for key in ("smartevse_1", "smartevse_2")}

        def step(moment):
            outlets = {key: outlet(key, vehicle="vehicle_1" if key == "smartevse_1" else "vehicle_2",
                                   charging=key == outlet_key, mode="Smart" if key == outlet_key else "Off",
                                   plugged_at=L(11, 9, 17, 56), deadline=self.deadlines[key])
                       for key in ("smartevse_1", "smartevse_2")}
            self.adapter.observe(moment, outlets=outlets, settings=SETTINGS, controller_state="intelligent")
            decision = self.adapter.plan(moment, drawing, commitment=(outlet_key, cp.floor15(moment) + cp.UNIT))
            payload_outlets = {key: OutletPayloadInput(deadline=self.deadlines[key]) for key in outlets}
            return decision, self.adapter.build_payload(moment, context(
                allowed=decision.allowed, reason=decision.reason, active=decision.outlet,
                since=since if decision.allowed else None, outlets=payload_outlets))

        _, (before, before_flat) = step(L(11, 9, 21, 14))
        for second in (10, 20, 30, 40, 50):
            decision, (payload, flat) = step(L(11, 9, 21, 14, second))
            # The planner cannot use the last minute; the decision bridges it ...
            self.assertEqual(decision.outlet, outlet_key)
            # ... and the payload and the sensors keep showing the running block.
            self.assertEqual(payload, before)
            self.assertEqual(flat, before_flat)
        self.assertEqual(before["chargers"][outlet_key]["status"], "charging")
        self.assertEqual(before["next_charge"], since.isoformat())


class StoreTests(TzMixin, unittest.IsolatedAsyncioTestCase):
    async def test_saves_within_five_minutes_of_commits_and_not_every_cycle(self):
        store = fake_store()
        states = base_states()
        adapter = make_adapter(states, store=store)
        await adapter.async_initialize(None)
        start = L(11, 9, 18)
        for step in range(40):
            now = start + timedelta(seconds=10 * step)
            adapter.observe(now, outlets={"smartevse_1": outlet("smartevse_1", vehicle="vehicle_1")},
                            settings=SETTINGS)
            await adapter.async_save_if_due(now)
        # The price lists are a commit event: saved at once, then at most every 5 min.
        self.assertGreaterEqual(store.async_save.await_count, 1)
        self.assertLessEqual(store.async_save.await_count, 2)
        saved = store.async_save.await_args_list[0].args[0]
        self.assertEqual(saved["schema"], 1)
        self.assertIsNotNone(saved["last_good"])
        # The saved object is a detached copy.
        saved["last_good"] = None
        self.assertIsNotNone(adapter.model.to_dict()["last_good"])

    async def test_flush_saves_unconditionally(self):
        store = fake_store()
        adapter = make_adapter(base_states(), store=store)
        await adapter.async_initialize(None)
        await adapter.async_flush()
        store.async_save.assert_awaited_once()

    async def test_oserror_sets_model_error_and_control_continues(self):
        store = fake_store()
        store.async_save.side_effect = OSError("disk full")
        states = base_states()
        adapter = make_adapter(states, store=store)
        await adapter.async_initialize(None)
        now = L(11, 9, 18)
        adapter.observe(now, outlets={"smartevse_1": outlet("smartevse_1", vehicle="vehicle_1")}, settings=SETTINGS)
        with self.assertLogs("custom_components.smartevse_dual_charger", level="ERROR"):
            await adapter.async_save_if_due(now)
        self.assertIn("save_failed: OSError", adapter.model_error)
        decision = adapter.plan(now, {"smartevse_1": PlanInput("smartevse_1", L(11, 10, 7))})
        self.assertEqual(decision.controller_error, "intelligent_model_unavailable")
        # Throttled: no retry within 5 min, then a successful save clears the error.
        await adapter.async_save_if_due(now + timedelta(seconds=10))
        self.assertEqual(store.async_save.await_count, 1)
        store.async_save.side_effect = None
        await adapter.async_save_if_due(now + timedelta(minutes=6))
        self.assertIsNone(adapter.model_error)

    async def test_load_failure_runs_read_only_on_defaults(self):
        store = fake_store()
        store.async_load.side_effect = ValueError("unsupported storage version")
        adapter = make_adapter(base_states(), store=store)
        with self.assertLogs("custom_components.smartevse_dual_charger", level="WARNING"):
            await adapter.async_initialize(None)
        store.make_read_only.assert_called_once()
        self.assertIn("storage_load_failed", adapter.model_error)
        await adapter.async_flush()
        store.async_save.assert_not_awaited()

    async def test_unreadable_model_runs_read_only_and_never_overwrites_the_file(self):
        store = fake_store({"schema": 1})
        adapter = make_adapter(base_states(), store=store)
        with patch.object(intel.cm, "ChargeModel", side_effect=[OverflowError("int too large"),
                                                                cm.ChargeModel(None)]):
            with self.assertLogs("custom_components.smartevse_dual_charger", level="WARNING"):
                await adapter.async_initialize(None)
        self.assertIn("model_load_failed: OverflowError", adapter.model_error)
        store.make_read_only.assert_called_once()
        adapter.model._dirty = True
        await adapter.async_flush()
        store.async_save.assert_not_awaited()

    async def test_model_of_another_schema_is_never_overwritten(self):
        # Review round 3: a whole-file schema reject (e.g. a rollback after a newer
        # release) loaded as defaults and saved them over weeks of learning.
        store = fake_store({"schema": 2, "energy": {"vehicle_1": [0.75, 0.76, 0.74], "vehicle_2": []}})
        adapter = make_adapter(base_states(), store=store)
        with self.assertLogs("custom_components.smartevse_dual_charger", level="WARNING"):
            await adapter.async_initialize(None)
        self.assertEqual(adapter.model_error, "model_schema_unsupported")
        store.make_read_only.assert_called_once()
        adapter.model._dirty = True
        await adapter.async_flush()
        store.async_save.assert_not_awaited()

    async def test_corrupt_fields_reset_only_themselves(self):
        stored = {"schema": 1, "energy": {"vehicle_1": [None, "x", -1, 1e9], "vehicle_2": [0.72, 0.71, 0.73]}}
        adapter = make_adapter(base_states(), store=fake_store(stored))
        with self.assertLogs("custom_components.smartevse_dual_charger", level="WARNING"):
            await adapter.async_initialize(None)
        self.assertEqual(adapter.model.kwh_per_pct("vehicle_2").source, "learned")
        self.assertNotEqual(adapter.model.kwh_per_pct("vehicle_1").source, "learned")

    async def test_store_uses_a_private_atomic_file(self):
        hass = SimpleNamespace(states=SimpleNamespace(get={}.get), config=SimpleNamespace(config_dir="/tmp"))
        with patch.object(intel, "ModelStore") as store_class:
            IntelligentCharging(hass, ENTRY, {})
        store_class.assert_called_once_with(hass, 1, "smartevse_dual_charger.test_entry.intelligent")
        hass.data = {}
        adapter = IntelligentCharging(hass, ENTRY, {})
        self.assertIsInstance(adapter._store, ModelStore)
        self.assertTrue(adapter._store._private)
        self.assertTrue(adapter._store._atomic_writes)


class ModelStoreTests(TzMixin, unittest.IsolatedAsyncioTestCase):
    """The real Store used for the model (native Store swallows write errors)."""

    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        self.hass.state = CoreState.running
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        self.path = Path(self.directory.name) / ".storage" / "smartevse_dual_charger.test_entry.intelligent"

    def adapter(self):
        hass = self.hass
        adapter = IntelligentCharging(hass, ENTRY, {})
        adapter.hass = SimpleNamespace(states=SimpleNamespace(get=base_states().get))
        return adapter

    async def test_write_failure_sets_model_error_and_keeps_the_learning_dirty(self):
        adapter = self.adapter()
        await adapter.async_initialize(None)
        adapter.model._dirty = True
        with patch.object(Store, "_async_write_data", AsyncMock(side_effect=WriteError("read-only"))):
            with self.assertLogs("custom_components.smartevse_dual_charger", level="ERROR"):
                await adapter.async_flush()
        self.assertIn("save_failed", adapter.model_error)
        self.assertTrue(adapter.model.dirty)
        await adapter.async_flush()
        self.assertIsNone(adapter.model_error)
        self.assertFalse(adapter.model.dirty)
        self.assertTrue(self.path.exists())

    async def test_corrupt_file_runs_read_only_and_is_left_untouched(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = '{"version": 1, "key": "smartevse_dual_charger.test_entry.intelligent", "data": {"schema": 1, "en'
        self.path.write_text(content)
        adapter = self.adapter()
        with self.assertLogs("custom_components.smartevse_dual_charger", level="WARNING"):
            await adapter.async_initialize(None)
        self.assertIn("storage_load_failed", adapter.model_error)
        adapter.model._dirty = True
        await adapter.async_flush()
        self.assertEqual(self.path.read_text(), content)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), [self.path.name])

    async def test_flush_while_stopping_is_deferred_to_the_final_write(self):
        adapter = self.adapter()
        await adapter.async_initialize(None)
        adapter.model._dirty = True
        self.hass.state = CoreState.stopping
        await adapter.async_flush()
        self.assertIsNone(adapter.model_error)
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
        await self.hass.async_block_till_done()
        self.assertEqual(json.loads(self.path.read_text())["data"]["schema"], 1)


class IsolationTests(TzMixin, unittest.IsolatedAsyncioTestCase):
    async def test_model_observation_failure_still_gives_facts(self):
        adapter = make_adapter(base_states())
        await adapter.async_initialize(None)
        adapter.model.observe_cycle = MagicMock(side_effect=RuntimeError("bad"))
        with self.assertLogs("custom_components.smartevse_dual_charger", level="ERROR"):
            result = adapter.observe(L(11, 9, 18), outlets={"smartevse_1": outlet("smartevse_1", vehicle="vehicle_1")},
                                     settings=SETTINGS)
        self.assertIn("smartevse_1", result.facts)
        self.assertIn("observe_failed", adapter.model_error)

    async def test_estimation_failure_uses_default_facts(self):
        adapter = make_adapter(base_states())
        await adapter.async_initialize(None)
        adapter.model.kwh_per_pct = MagicMock(side_effect=RuntimeError("bad"))
        with self.assertLogs("custom_components.smartevse_dual_charger", level="ERROR"):
            result = adapter.observe(L(11, 9, 18), outlets={"smartevse_1": outlet("smartevse_1", vehicle="vehicle_1")},
                                     settings=SETTINGS)
        facts = result.facts["smartevse_1"]
        self.assertEqual((facts.soc, facts.soc_source, facts.minimum), (None, "unknown", 80))
        self.assertIn("estimate_failed", adapter.model_error)

    async def test_repeated_failures_are_rate_limited_in_the_log(self):
        adapter = make_adapter(base_states())
        await adapter.async_initialize(None)
        adapter.model.observe_cycle = MagicMock(side_effect=RuntimeError("bad"))
        with self.assertLogs("custom_components.smartevse_dual_charger", level="DEBUG") as logs:
            for step in range(5):
                adapter.observe(L(11, 9, 18) + timedelta(seconds=10 * step), outlets={}, settings=SETTINGS)
        errors = [record for record in logs.records if record.levelno >= logging.ERROR]
        self.assertEqual(len(errors), 1)


class DiagnosticsTests(unittest.TestCase):
    def test_habits_are_redacted_and_ledger_times_rounded(self):
        data = {
            "vehicle_1_ready_by": "07:00", "other_vehicle_ready_by": "06:30",
            "intelligent": {
                "chargers": {"smartevse_1": {"deadline": "2026-11-10T05:00:00+00:00", "ready_by": "07:00",
                                             "blocks": [{"start": "2026-11-09T19:00:00+00:00"}]},
                             "smartevse_2": None},
                "vehicles": {"vehicle_1": {"ready_by": "07:00", "suggested_ready_by": "06:45",
                                           "usually_leaves": "07:00", "name": "Volvo XC40"},
                             "other": {"ready_by": "06:30"}},
                "sessions": [{"ledger_id": "a", "saving": 1.2}],
            },
            "charging_costs": {"chargers": {"smartevse_1": {
                "session": {"started_at": "2026-11-09T21:17:43+00:00", "ended_at": None},
                "history": [{"started_at": "2026-11-08T22:01:05+00:00", "ended_at": "2026-11-09T01:27:19+00:00"}]}}},
        }
        original = deepcopy(data)
        result = redact_runtime_state(data)
        self.assertEqual(data, original)  # the coordinator data itself is untouched
        self.assertEqual(result["vehicle_1_ready_by"], REDACTED)
        self.assertEqual(result["other_vehicle_ready_by"], REDACTED)
        charger = result["intelligent"]["chargers"]["smartevse_1"]
        self.assertEqual((charger["deadline"], charger["ready_by"]), (REDACTED, REDACTED))
        self.assertEqual(charger["blocks"], [{"start": "2026-11-09T19:00:00+00:00"}])
        vehicle = result["intelligent"]["vehicles"]["vehicle_1"]
        self.assertEqual((vehicle["ready_by"], vehicle["suggested_ready_by"], vehicle["usually_leaves"]),
                         (REDACTED, REDACTED, REDACTED))
        self.assertEqual(vehicle["name"], "Volvo XC40")
        record = result["charging_costs"]["chargers"]["smartevse_1"]
        self.assertEqual(record["session"]["started_at"], "2026-11-09T21:00:00+00:00")
        self.assertEqual(record["history"][0]["ended_at"], "2026-11-09T01:00:00+00:00")
        self.assertIsNone(record["session"]["ended_at"])


if __name__ == "__main__":
    unittest.main()
