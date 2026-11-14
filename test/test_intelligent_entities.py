"""Intelligent charging entities, options, translations and version (spec §4).

Entities are built through each platform's ``async_setup_entry`` with a
SimpleNamespace config entry and MagicMock controller/coordinator, as in
test_cost_adapter.py. Nothing talks to a charger or to Home Assistant.
"""

import json
from copy import deepcopy
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.number import NumberMode
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import Platform
from homeassistant.util import slugify
# After homeassistant: importing it aliases voluptuous to its validation engine.
import voluptuous as vol

from custom_components.smartevse_dual_charger import number as number_platform
from custom_components.smartevse_dual_charger import select as select_platform
from custom_components.smartevse_dual_charger import sensor as sensor_platform
from custom_components.smartevse_dual_charger import switch as switch_platform
from custom_components.smartevse_dual_charger import time as time_platform
from custom_components.smartevse_dual_charger import OPTION_KEYS
from custom_components.smartevse_dual_charger.config_flow import SmartEVSEDualChargerOptionsFlow
from custom_components.smartevse_dual_charger.const import (
    CONF_VEHICLE_1_BATTERY_CAPACITY_KWH,
    CONF_VEHICLE_1_NAME,
    CONF_VEHICLE_2_BATTERY_CAPACITY_KWH,
    DOMAIN,
    INTELLIGENT_STATUSES,
    PLATFORMS,
    VERSION,
)
from custom_components.smartevse_dual_charger.controller import MUTABLE_DEFAULTS, SmartEVSEDualChargerController
from custom_components.smartevse_dual_charger.intelligent import ENTITY_KEYS, empty_payload, flat_values
from custom_components.smartevse_dual_charger.sensor import (
    INTELLIGENT_SENSOR_DESCRIPTIONS,
    SENSOR_DESCRIPTIONS,
    ControllerSensor,
    IntelligentSensor,
)
from custom_components.smartevse_dual_charger.time import ControllerTimeEntity

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "custom_components" / DOMAIN
ENTRY_ID = "entry"

# Every unique-id key that existed before Intelligent charging (0.0.8.x). These
# ids are in users' entity registries and dashboards and must never change.
EXISTING_UNIQUE_KEYS = {
    "switch": {"force_charge", "force_price", "force_timer", "charge_with_schedule", "schedule_price"},
    "number": {"acceptable_price", "force_charge_duration", "duty_cycle_minutes", "update_interval",
               "currents_push_interval", "ev_meter_push_interval"},
    "select": {"charge_policy"},
    "sensor": {
        "controller_state", "charge_reason", "controller_error", "active_smartevse", "duty_cycle_remaining",
        "timer_remaining", "smartevse_1_session_cost", "smartevse_2_session_cost",
        *(f"smartevse_{n}_{name}" for n in (1, 2) for name in (
            "state", "plug_state", "mode", "charge_current", "max_current", "override_current", "error",
            "connected_ev")),
    },
    "time": set(),
}
NEW_UNIQUE_KEYS = {
    "switch": {"intelligent_charge"},
    "number": {"vehicle_1_minimum_charge", "vehicle_2_minimum_charge"},
    "select": set(),
    "sensor": {"intelligent_status", "intelligent_planned_cost", "intelligent_planned_saving",
               "intelligent_next_charge"},
    "time": {"vehicle_1_ready_by", "vehicle_2_ready_by", "other_vehicle_ready_by"},
}
PLATFORM_MODULES = {
    "switch": switch_platform,
    "number": number_platform,
    "select": select_platform,
    "sensor": sensor_platform,
    "time": time_platform,
}


def make_entry(data=None, *, entry_data=None, options=None, controller=None):
    coordinator = MagicMock()
    coordinator.data = {} if data is None else data
    coordinator._async_refresh_now = AsyncMock()
    return SimpleNamespace(
        entry_id=ENTRY_ID,
        title="SmartEVSE Dual Charger",
        data=dict(entry_data or {}),
        options=dict(options or {}),
        runtime_data=SimpleNamespace(coordinator=coordinator, controller=controller or MagicMock()),
    )


async def setup_platform(platform, entry):
    add_entities = MagicMock()
    await PLATFORM_MODULES[platform].async_setup_entry(None, entry, add_entities)
    add_entities.assert_called_once()
    return list(add_entities.call_args.args[0])


async def setup_all(entry):
    return {platform: await setup_platform(platform, entry) for platform in PLATFORM_MODULES}


def keys_of(entities):
    keys = []
    for entity in entities:
        prefix = f"{ENTRY_ID}_"
        assert entity.unique_id.startswith(prefix), entity.unique_id
        keys.append(entity.unique_id[len(prefix):])
    return keys


def by_key(entities):
    return dict(zip(keys_of(entities), entities))


def stub_platform(entity, domain="sensor"):
    """Minimal platform data so ``.state`` resolves units (translation lookups)."""
    entity.platform_data = SimpleNamespace(platform_name=DOMAIN, domain=domain,
                                           default_language_platform_translations={})
    entity.entity_id = f"{domain}.test_{entity.translation_key}"
    return entity


def load_translations():
    return json.loads((PACKAGE / "translations" / "en.json").read_text(encoding="utf-8"))


class UniqueIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_unique_ids_are_unchanged_and_new_ids_are_distinct(self):
        entities = await setup_all(make_entry())
        all_ids = []
        for platform, platform_entities in entities.items():
            keys = keys_of(platform_entities)
            all_ids.extend(entity.unique_id for entity in platform_entities)
            with self.subTest(platform=platform):
                self.assertEqual(len(keys), len(set(keys)), "duplicate unique id")
                self.assertEqual(set(keys), EXISTING_UNIQUE_KEYS[platform] | NEW_UNIQUE_KEYS[platform])
                self.assertTrue(EXISTING_UNIQUE_KEYS[platform].isdisjoint(NEW_UNIQUE_KEYS[platform]))
        # No id is shared across platforms either (registry keys are per domain,
        # but the dashboard and diagnostics treat the suffix as the identity).
        self.assertEqual(len(all_ids), len(set(all_ids)))

    async def test_shared_translation_keys_get_their_own_unique_ids(self):
        entities = await setup_all(make_entry())
        numbers = by_key(entities["number"])
        times = by_key(entities["time"])
        self.assertEqual(numbers["vehicle_1_minimum_charge"].translation_key, "vehicle_minimum_charge")
        self.assertEqual(numbers["vehicle_2_minimum_charge"].translation_key, "vehicle_minimum_charge")
        self.assertNotIn("other_vehicle_minimum_charge", numbers)  # planned by energy: no minimum setting
        self.assertEqual(times["vehicle_1_ready_by"].translation_key, "vehicle_ready_by")
        self.assertEqual(times["vehicle_2_ready_by"].translation_key, "vehicle_ready_by")
        self.assertEqual(times["other_vehicle_ready_by"].translation_key, "other_vehicle_ready_by")
        # Without a unique_key the id keeps today's translation-key pattern.
        switches = by_key(entities["switch"])
        self.assertEqual(switches["charge_with_schedule"]._data_key, "schedule_enabled")
        self.assertEqual(switches["intelligent_charge"]._data_key, "intelligent_enabled")

    async def test_payload_entity_keys_match_the_registered_unique_ids(self):
        # intelligent.entities looks entities up by f"{entry_id}_{key}" (§4).
        entities = await setup_all(make_entry())
        registered = {(platform, key) for platform, items in entities.items() for key in keys_of(items)}

        def specs(value):
            if isinstance(value, dict):
                for item in value.values():
                    yield from specs(item)
            else:
                yield value

        for domain, key in specs(ENTITY_KEYS):
            with self.subTest(key=key):
                self.assertIn((domain, key), registered)

    async def test_vehicle_names_come_from_options_then_entry_data(self):
        entry = make_entry(entry_data={CONF_VEHICLE_1_NAME: "Family car"},
                           options={"vehicle_2_name": "  Runabout  "})
        entities = await setup_all(entry)
        numbers = by_key(entities["number"])
        times = by_key(entities["time"])
        self.assertEqual(numbers["vehicle_1_minimum_charge"].translation_placeholders, {"vehicle": "Family car"})
        self.assertEqual(times["vehicle_2_ready_by"].translation_placeholders, {"vehicle": "Runabout"})
        self.assertEqual(times["other_vehicle_ready_by"].translation_placeholders, {})
        # Defaults when nothing is configured.
        defaults = by_key((await setup_all(make_entry()))["time"])
        self.assertEqual(defaults["vehicle_1_ready_by"].translation_placeholders, {"vehicle": "Volvo XC40"})
        self.assertEqual(defaults["vehicle_2_ready_by"].translation_placeholders, {"vehicle": "Volvo EX30"})

    def test_time_platform_is_forwarded(self):
        self.assertIn(Platform.TIME, PLATFORMS)
        self.assertEqual(PLATFORMS[:4], [Platform.SENSOR, Platform.SWITCH, Platform.NUMBER, Platform.SELECT])


class SwitchAndNumberTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.controller = MagicMock()
        for name in ("async_set_intelligent_enabled", "async_set_vehicle_minimum_charge"):
            setattr(self.controller, name, AsyncMock())
        self.data = {"intelligent_enabled": True,
                     "vehicle_1_minimum_charge": 70, "vehicle_2_minimum_charge": 90,
                     "other_vehicle_minimum_charge": 80}
        self.entry = make_entry(self.data, controller=self.controller)
        self.refresh = self.entry.runtime_data.coordinator._async_refresh_now

    async def test_intelligent_switches_read_and_write_the_controller(self):
        switches = by_key(await setup_platform("switch", self.entry))
        charge = switches["intelligent_charge"]
        self.assertTrue(charge.is_on)
        self.assertNotIn("intelligent_top_up", switches)  # automatic since 0.0.10.0
        await charge.async_turn_off()
        self.controller.async_set_intelligent_enabled.assert_awaited_once_with(False)
        self.assertEqual([call.args[0] for call in self.refresh.await_args_list], ["switch_turn_off"])

    async def test_intelligent_numbers_read_write_and_shape(self):
        numbers = by_key(await setup_platform("number", self.entry))
        self.assertNotIn("intelligent_top_up_price", numbers)  # the price limit is learned

        for key, settings_key, value in (("vehicle_1_minimum_charge", "vehicle_1", 70.0),
                                         ("vehicle_2_minimum_charge", "vehicle_2", 90.0)):
            with self.subTest(key=key):
                entity = numbers[key]
                self.assertEqual(entity.native_value, value)
                self.assertEqual((entity.native_min_value, entity.native_max_value, entity.native_step), (10, 100, 1))
                self.assertEqual(entity.native_unit_of_measurement, "%")
                self.assertEqual(entity.mode, NumberMode.BOX)
                await entity.async_set_native_value(65)
                self.controller.async_set_vehicle_minimum_charge.assert_awaited_with(settings_key, 65)
        # Minimum-charge numbers are runtime settings, never written into the options.
        self.assertEqual(self.entry.options, {})
        self.assertEqual(self.refresh.await_count, 2)  # two vehicle minimums

    async def test_missing_number_keeps_its_zero_fallback(self):
        numbers = by_key(await setup_platform("number", self.entry))
        self.assertEqual(numbers["acceptable_price"].native_value, 0.0)


class TimeEntityTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_value_parses_stored_text_and_rejects_bad_values(self):
        data = {"vehicle_1_ready_by": "06:30"}
        entry = make_entry(data)
        entity = by_key(await setup_platform("time", entry))["vehicle_1_ready_by"]
        cases = (("06:30", time(6, 30)), ("06:45:30", time(6, 45)), ("7:5", time(7, 5)), ("00:00", time(0, 0)),
                 ("25:00", None), ("x", None), ("", None), (None, None), (42, None))
        for stored, expected in cases:
            with self.subTest(stored=stored):
                data["vehicle_1_ready_by"] = stored
                self.assertEqual(entity.native_value, expected)
        data["vehicle_1_ready_by"] = "06:30"
        self.assertEqual(entity.state, "06:30:00")
        data["vehicle_1_ready_by"] = "bad"
        self.assertIsNone(entity.state)

    async def test_set_value_stores_hh_mm_and_refreshes(self):
        controller = MagicMock()
        controller.async_set_vehicle_ready_by = AsyncMock()
        entry = make_entry({}, controller=controller)
        times = by_key(await setup_platform("time", entry))
        for key, settings_key in (("vehicle_1_ready_by", "vehicle_1"), ("vehicle_2_ready_by", "vehicle_2"),
                                  ("other_vehicle_ready_by", "other")):
            with self.subTest(key=key):
                await times[key].async_set_value(time(6, 15, 42))
                controller.async_set_vehicle_ready_by.assert_awaited_with(settings_key, "06:15")
        refresh = entry.runtime_data.coordinator._async_refresh_now
        self.assertEqual([call.args[0] for call in refresh.await_args_list], ["time_set"] * 3)

    async def test_set_value_through_the_real_controller_normalises(self):
        controller = SmartEVSEDualChargerController.__new__(SmartEVSEDualChargerController)
        controller._entry_data = {"entry_id": ENTRY_ID}
        controller._options = {}
        controller._mutable = deepcopy(MUTABLE_DEFAULTS)
        controller._async_save_state = AsyncMock()
        entry = make_entry({}, controller=controller)
        entity = by_key(await setup_platform("time", entry))["vehicle_2_ready_by"]
        await entity.async_set_value(time(5, 45, 59))
        self.assertEqual(controller._mutable["vehicle_2_ready_by"], "05:45")
        controller._async_save_state.assert_awaited()
        # The value the time entity reads back from the cycle data round-trips.
        entry.runtime_data.coordinator.data = {"vehicle_2_ready_by": controller._mutable["vehicle_2_ready_by"]}
        self.assertEqual(entity.native_value, time(5, 45))

    def test_initialisation_normalises_stored_ready_by_and_minimum(self):
        controller = SmartEVSEDualChargerController.__new__(SmartEVSEDualChargerController)
        controller._mutable = {**deepcopy(MUTABLE_DEFAULTS), "vehicle_1_ready_by": "7:0", "vehicle_2_ready_by": "",
                               "other_vehicle_ready_by": None, "vehicle_1_minimum_charge": 5,
                               "vehicle_2_minimum_charge": 150, "other_vehicle_minimum_charge": "x"}
        controller._normalize_intelligent_settings()
        self.assertEqual([controller._mutable[key] for key in
                          ("vehicle_1_ready_by", "vehicle_2_ready_by", "other_vehicle_ready_by")],
                         ["07:00", "07:00", "07:00"])
        self.assertEqual([controller._mutable[key] for key in
                          ("vehicle_1_minimum_charge", "vehicle_2_minimum_charge", "other_vehicle_minimum_charge")],
                         [10, 100, 80])

    def test_time_entity_uses_keyword_unique_key(self):
        entity = ControllerTimeEntity(make_entry(), data_key="vehicle_1_ready_by", translation_key="vehicle_ready_by",
                                      setter=AsyncMock())
        self.assertEqual(entity.unique_id, f"{ENTRY_ID}_vehicle_ready_by")


class SensorTests(unittest.IsolatedAsyncioTestCase):
    async def sensors(self, data):
        return by_key(await setup_platform("sensor", make_entry(data)))

    async def test_status_is_a_translated_enum(self):
        data = {"intelligent_status": "planned"}
        status = stub_platform((await self.sensors(data))["intelligent_status"])
        self.assertEqual(status.device_class, SensorDeviceClass.ENUM)
        self.assertEqual(status.options, list(INTELLIGENT_STATUSES))
        self.assertIsNone(status.state_class)
        self.assertEqual(status.translation_key, "intelligent_status")
        for value in INTELLIGENT_STATUSES:
            with self.subTest(value=value):
                data["intelligent_status"] = value
                self.assertEqual(status.state, value)
        for value in ("bogus", None, 3):
            with self.subTest(value=value):
                data["intelligent_status"] = value
                self.assertIsNone(status.native_value)
                self.assertIsNone(status.state)

    async def test_money_sensors_are_monetary_with_dynamic_unit_and_no_state_class(self):
        data = {"intelligent_planned_cost": None, "intelligent_planned_saving": None, "intelligent_currency": None}
        sensors = await self.sensors(data)
        cost = stub_platform(sensors["intelligent_planned_cost"])
        saving = stub_platform(sensors["intelligent_planned_saving"])
        for sensor in (cost, saving):
            with self.subTest(sensor=sensor.translation_key):
                self.assertEqual(sensor.device_class, SensorDeviceClass.MONETARY)
                self.assertIsNone(sensor.state_class)
                self.assertEqual(sensor.suggested_display_precision, 2)
                self.assertIsNone(sensor.native_value)
                self.assertIsNone(sensor.native_unit_of_measurement)
                self.assertIsNone(sensor.state)
        data.update(intelligent_planned_cost=4.913, intelligent_planned_saving=-1.234, intelligent_currency="EUR")
        self.assertEqual((cost.native_value, cost.native_unit_of_measurement), (4.91, "EUR"))
        self.assertEqual((saving.native_value, saving.native_unit_of_measurement), (-1.23, "EUR"))
        self.assertEqual(saving.state, -1.23)
        # An amount without a currency, or a non-finite amount, is unknown.
        data.update(intelligent_currency=None)
        self.assertIsNone(cost.native_value)
        data.update(intelligent_currency="EUR", intelligent_planned_cost=float("nan"))
        self.assertIsNone(cost.native_value)
        data.update(intelligent_planned_cost="x")
        self.assertIsNone(cost.native_value)

    async def test_next_charge_is_an_aware_timestamp(self):
        data = {"intelligent_next_charge": None}
        next_charge = stub_platform((await self.sensors(data))["intelligent_next_charge"])
        self.assertEqual(next_charge.device_class, SensorDeviceClass.TIMESTAMP)
        self.assertIsNone(next_charge.state)
        start = datetime(2026, 11, 9, 23, 0, tzinfo=timezone(timedelta(hours=2)))
        data["intelligent_next_charge"] = start
        self.assertEqual(next_charge.native_value, start)
        self.assertEqual(next_charge.state, "2026-11-09T21:00:00+00:00")
        data["intelligent_next_charge"] = "2026-11-09T21:15:00+00:00"
        self.assertEqual(next_charge.native_value, datetime(2026, 11, 9, 21, 15, tzinfo=UTC))
        for value in (datetime(2026, 11, 9, 21, 0), "2026-11-09T21:00:00", "soon", 5):
            with self.subTest(value=value):
                data["intelligent_next_charge"] = value
                self.assertIsNone(next_charge.native_value)
                self.assertIsNone(next_charge.state)

    async def test_flat_values_from_a_payload_feed_the_sensors(self):
        payload = empty_payload(enabled=True, currency="EUR")
        payload["status"] = "planned"
        payload["totals"]["cost"] = 3.21
        payload["totals"]["saving"] = 0.75
        payload["next_charge"] = "2026-11-09T21:00:00+00:00"
        data = flat_values(payload)
        sensors = await self.sensors(data)
        self.assertEqual(stub_platform(sensors["intelligent_status"]).state, "planned")
        self.assertEqual(sensors["intelligent_planned_cost"].native_value, 3.21)
        self.assertEqual(sensors["intelligent_planned_saving"].native_unit_of_measurement, "EUR")
        self.assertEqual(stub_platform(sensors["intelligent_next_charge"]).state, "2026-11-09T21:00:00+00:00")
        disabled = flat_values(empty_payload(enabled=False))
        sensors = await self.sensors(disabled)
        self.assertEqual(sensors["intelligent_status"].native_value, "disabled")
        self.assertIsNone(sensors["intelligent_planned_cost"].native_value)
        self.assertIsNone(sensors["intelligent_next_charge"].native_value)

    async def test_controller_state_sensor_publishes_the_unrecorded_payload(self):
        payload = empty_payload(enabled=True)
        sensors = await self.sensors({"intelligent": payload, "charging_costs": {"chargers": {}}})
        state_sensor = sensors["controller_state"]
        self.assertIs(state_sensor.extra_state_attributes["intelligent"], payload)
        self.assertIn("intelligent", ControllerSensor._unrecorded_attributes)
        self.assertIn("charging_costs", ControllerSensor._unrecorded_attributes)
        combined = ControllerSensor._Entity__combined_unrecorded_attributes
        self.assertTrue({"intelligent", "charging_costs"} <= combined)
        # Other controller sensors carry no attributes.
        self.assertIsNone(sensors["charge_reason"].extra_state_attributes)

    def test_intelligent_sensor_descriptions_do_not_overlap_controller_sensors(self):
        controller_keys = {description.key for description in SENSOR_DESCRIPTIONS}
        intelligent_keys = {description.key for description in INTELLIGENT_SENSOR_DESCRIPTIONS}
        self.assertTrue(controller_keys.isdisjoint(intelligent_keys))
        entity = IntelligentSensor(make_entry(), INTELLIGENT_SENSOR_DESCRIPTIONS[0])
        self.assertEqual(entity.unique_id, f"{ENTRY_ID}_intelligent_status")


class TranslationTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_translated_entity_has_a_name_and_placeholders_format(self):
        translations = load_translations()["entity"]
        entities = await setup_all(make_entry(options={CONF_VEHICLE_1_NAME: "Family car"}))
        checked = 0
        for platform, items in entities.items():
            for entity in items:
                if entity.translation_key is None:
                    continue  # ChargingCostSensor uses a fixed name
                with self.subTest(platform=platform, key=entity.translation_key):
                    name = translations[platform][entity.translation_key]["name"]
                    placeholders = dict(entity.translation_placeholders or {})
                    self.assertEqual(set(re.findall(r"{(\w+)}", name)), set(placeholders))
                    formatted = name.format(**placeholders)
                    self.assertNotIn("{", formatted)
                    self.assertTrue(formatted.strip())
                    checked += 1
        self.assertGreaterEqual(checked, 40)
        self.assertEqual(translations["time"]["vehicle_ready_by"]["name"].format(vehicle="Family car"),
                         "Family car ready by")
        self.assertEqual(translations["number"]["vehicle_minimum_charge"]["name"].format(vehicle="Volvo EX30"),
                         "Volvo EX30 minimum charge")

    async def test_names_resolve_through_home_assistant_translation_lookup(self):
        # Feed the real en.json to Home Assistant's own name resolution (the same
        # lookup the entity platform uses for the friendly name and entity_id).
        entity_translations = load_translations()["entity"]
        flat = {
            f"component.{DOMAIN}.entity.{platform}.{key}.name": value["name"]
            for platform, items in entity_translations.items()
            for key, value in items.items()
        }
        entry = make_entry()
        entities = await setup_all(entry)
        expected = {
            ("time", "vehicle_1_ready_by"): ("Volvo XC40 ready by", "smartevse_dual_charger_volvo_xc40_ready_by"),
            ("time", "other_vehicle_ready_by"): ("Unidentified vehicle ready by",
                                                 "smartevse_dual_charger_unidentified_vehicle_ready_by"),
            ("number", "vehicle_2_minimum_charge"): ("Volvo EX30 minimum charge",
                                                     "smartevse_dual_charger_volvo_ex30_minimum_charge"),
            ("switch", "intelligent_charge"): ("Intelligent charging", "smartevse_dual_charger_intelligent_charging"),
            ("switch", "charge_with_schedule"): ("Charge with schedule", "smartevse_dual_charger_charge_with_schedule"),
            ("sensor", "intelligent_next_charge"): ("Intelligent next charge",
                                                    "smartevse_dual_charger_intelligent_next_charge"),
        }
        for (platform, key), (name, object_id) in expected.items():
            with self.subTest(platform=platform, key=key):
                entity = by_key(entities[platform])[key]
                entity.platform_data = SimpleNamespace(
                    platform_name=DOMAIN, domain=platform, platform_translations=flat,
                    object_id_platform_translations=flat, component_translations={},
                    object_id_component_translations={}, default_language_platform_translations={})
                self.assertEqual(entity.name, name)
                self.assertEqual(slugify(f"{entry.title} {entity.suggested_object_id}"), object_id)

    def test_status_states_are_translated_exactly(self):
        states = load_translations()["entity"]["sensor"]["intelligent_status"]["state"]
        self.assertEqual(set(states), set(INTELLIGENT_STATUSES))
        self.assertTrue(all(isinstance(text, str) and text.strip() for text in states.values()))

    def test_options_fields_and_errors_are_translated(self):
        options = load_translations()["options"]
        labels = options["step"]["init"]["data"]
        schema = SmartEVSEDualChargerOptionsFlow(SimpleNamespace(data={}, options={}))._build_options_schema({})
        for marker in schema.schema:
            with self.subTest(field=marker.schema):
                self.assertIn(marker.schema, labels)
        for key in (CONF_VEHICLE_1_BATTERY_CAPACITY_KWH, CONF_VEHICLE_2_BATTERY_CAPACITY_KWH):
            self.assertIn(key, options["step"]["init"]["data_description"])
        self.assertIn("invalid_battery_capacity", options["error"])


class OptionsTests(unittest.IsolatedAsyncioTestCase):
    def flow(self, data=None, options=None):
        flow = SmartEVSEDualChargerOptionsFlow(SimpleNamespace(data=dict(data or {}), options=dict(options or {})))
        flow.flow_id = "test_flow"
        flow.handler = ENTRY_ID
        return flow

    def test_schema_offers_capacity_fields_with_automatic_default(self):
        schema = self.flow()._build_options_schema({})
        markers = {marker.schema: marker for marker in schema.schema}
        for key in (CONF_VEHICLE_1_BATTERY_CAPACITY_KWH, CONF_VEHICLE_2_BATTERY_CAPACITY_KWH):
            with self.subTest(key=key):
                self.assertEqual(markers[key].default(), 0.0)
                self.assertEqual(schema.schema[markers[key]](77.5), 77.5)
        schema = self.flow()._build_options_schema({CONF_VEHICLE_1_BATTERY_CAPACITY_KWH: 64.0,
                                                    CONF_VEHICLE_2_BATTERY_CAPACITY_KWH: "junk"})
        markers = {marker.schema: marker for marker in schema.schema}
        self.assertEqual(markers[CONF_VEHICLE_1_BATTERY_CAPACITY_KWH].default(), 64.0)
        self.assertEqual(markers[CONF_VEHICLE_2_BATTERY_CAPACITY_KWH].default(), 0.0)
        # The box selector itself enforces the 0-300 kWh range.
        capacity_selector = schema.schema[markers[CONF_VEHICLE_1_BATTERY_CAPACITY_KWH]]
        self.assertEqual(capacity_selector(0), 0)
        for value in (-1, 301):
            with self.subTest(value=value), self.assertRaises(vol.Invalid):
                capacity_selector(value)

    def test_capacity_options_reach_the_controller_options(self):
        self.assertIn(CONF_VEHICLE_1_BATTERY_CAPACITY_KWH, OPTION_KEYS)
        self.assertIn(CONF_VEHICLE_2_BATTERY_CAPACITY_KWH, OPTION_KEYS)

    async def test_step_accepts_capacity_and_automatic(self):
        result = await self.flow().async_step_init({CONF_VEHICLE_1_BATTERY_CAPACITY_KWH: 66.66,
                                                    CONF_VEHICLE_2_BATTERY_CAPACITY_KWH: 0})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"][CONF_VEHICLE_1_BATTERY_CAPACITY_KWH], 66.7)
        self.assertEqual(result["data"][CONF_VEHICLE_2_BATTERY_CAPACITY_KWH], 0.0)
        result = await self.flow().async_step_init({})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"][CONF_VEHICLE_1_BATTERY_CAPACITY_KWH], 0.0)

    async def test_step_rejects_unrealistic_capacity_per_field(self):
        for value in (5, 301, -1, float("nan"), "big", True):
            with self.subTest(value=value):
                result = await self.flow().async_step_init({CONF_VEHICLE_1_BATTERY_CAPACITY_KWH: 64,
                                                            CONF_VEHICLE_2_BATTERY_CAPACITY_KWH: value})
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"], {CONF_VEHICLE_2_BATTERY_CAPACITY_KWH: "invalid_battery_capacity"})


class VersionTests(unittest.TestCase):
    def test_version_matches_manifest_and_readme(self):
        manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(VERSION, manifest["version"])
        self.assertEqual(VERSION, "0.0.16.0")
        # Review round 3: no after_dependency on the price integration — HA refuses to set
        # up an integration whose after_dependency failed, which would stop Schedule/Force
        # too. The registry-based derivation works in any setup order.
        self.assertNotIn("after_dependencies", manifest)
        self.assertNotIn("dependencies", manifest)
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        match = re.search(r"^Version: `([^`]+)`$", readme, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), VERSION)


if __name__ == "__main__":
    unittest.main()
