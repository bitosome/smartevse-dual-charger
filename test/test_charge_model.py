"""Learned charging model (charge_model.py): pure logic, no Home Assistant.

Fixtures follow production facts (Nov 2026): night max-phase household base
2-4 A with the NIBE heat pump on phase B, 0.235 kW per phase-amp, XC40
0.754 kWh/% (78.182 kWh sensor), EX30 0.719 kWh/% (69 kWh), Real Electricity
Price lists running 01:00->01:00 local. The real ledger (charging_cost.py) is
used to meter energy, exactly as in production.
"""
from copy import deepcopy
from datetime import UTC, datetime, time, timedelta
import importlib.util
import json
import math
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parents[1] / "custom_components/smartevse_dual_charger"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cm = _load("charge_model")
cost = _load("charging_cost")
cp = _load("charge_planner")


def planner_baseline(calls=None):
    """The adapter's baseline callback (IntelligentCharging._baseline): the planner's
    sequential one-charger baseline from each session's plug-in anchor."""
    def baseline(sessions, price_slots, power_slots):
        if calls is not None:
            calls.append(deepcopy(sessions))
        prices = [cp.PriceSlot(s["start"], s["end"], s.get("price"), s.get("kind") or cp.PRICE_UNKNOWN)
                  for s in price_slots]
        powers = [cp.PowerSlot(s["start"], s["end"], float(s["expected_kw"]), float(s["expected_kw"]))
                  for s in power_slots]
        cap = max((p.expected_kw for p in powers), default=10.7) or 10.7
        return cp.baseline_costs([cp.BaselineSession(outlet=s["key"], plugged_at=s["plugged_at"],
                                                     energy_kwh=float(s["energy_kwh"]), cap_kw=cap)
                                  for s in sessions], prices, powers)
    return baseline

TZ = ZoneInfo("Europe/Tallinn")
NAMES = {"vehicle_1": "Volvo XC40", "vehicle_2": "Volvo EX30"}
CAPACITIES = {"vehicle_1": 78.182, "vehicle_2": 69.0}


def L(year, month, day, hour=0, minute=0, second=0):
    """Local Tallinn wall-clock time as an aware UTC datetime."""
    return datetime(year, month, day, hour, minute, second, tzinfo=TZ).astimezone(UTC)


# Live prices from production 2026-11-09 18:00 .. 2026-11-11 01:00 (EUR/kWh, local hours);
# 11-10 07..21 synthesised to the published min/max/avg (review-tmp/prices.py).
KNOWN = [
    ((11, 9, 18), 0.4434), ((11, 9, 19), 0.3909), ((11, 9, 20), 0.3574), ((11, 9, 21), 0.2418),
    ((11, 9, 22), 0.2843), ((11, 9, 23), 0.2396),
    ((11, 10, 0), 0.3105), ((11, 10, 1), 0.2994), ((11, 10, 2), 0.2780), ((11, 10, 3), 0.2635),
    ((11, 10, 4), 0.2983), ((11, 10, 5), 0.2827), ((11, 10, 6), 0.3575),
] + [((11, 10, 7 + k), p) for k, p in enumerate(
    [0.4300, 0.6950, 0.6200, 0.5400, 0.5000, 0.4700, 0.4600, 0.4700, 0.5000, 0.5600, 0.6400,
     0.6500, 0.5600, 0.4480, 0.3544])] + [
    ((11, 10, 22), 0.3137), ((11, 10, 23), 0.3534), ((11, 11, 0), 0.3052)]


def rep_entry(start_local, price, *, weekend=False, holiday=False):
    """A Real Electricity Price hourly_prices item (ISO strings with local offset)."""
    end_local = (start_local.astimezone(UTC) + timedelta(hours=1)).astimezone(TZ)
    return {"start_time": start_local.isoformat(), "end_time": end_local.isoformat(),
            "nord_pool_price": None if price is None else round(price / 1.24, 6),
            "actual_price": price, "tariff": "off_peak", "is_holiday": holiday,
            "is_weekend": weekend}


def known_entries():
    return [rep_entry(datetime(2026, m, d, h, tzinfo=TZ), p) for (m, d, h), p in KNOWN]


def outlet_defaults():
    return {"available": True, "connected": False, "charging": False, "vehicle": None,
            "offered_current": 16.0, "max_current": 16.0, "max_mains": 16.0, "min_current": 6.0}


def fake_baseline(calls=None):
    """One-charger sequential baseline over the snapshot (stands in for the planner)."""
    def baseline(sessions, price_slots, power_slots):
        if calls is not None:
            calls.append(deepcopy(sessions))
        results, free_at = {}, None
        for session in sessions:
            at = session["start"] if free_at is None else max(session["start"], free_at)
            remaining, total = session["energy_kwh"], 0.0
            for price, power in zip(price_slots, power_slots):
                if remaining <= 1e-9:
                    break
                if power["end"] <= at or power["expected_kw"] <= 0:
                    continue
                begin = max(at, power["start"])
                energy = min(remaining, power["expected_kw"] * (power["end"] - begin).total_seconds() / 3600)
                if price["price"] is None:
                    total = None
                    break
                total += energy * price["price"]
                remaining -= energy
                at = begin + timedelta(hours=energy / power["expected_kw"])
            results[session["key"]] = None if total is None or remaining > 1e-6 else total
            free_at = at
        return results
    return baseline


class Sim:
    """Drives the model like the controller: real ledger, 2-min Volvo SoC reports."""

    def __init__(self, start, *, model=None, prices=None, step=30, baseline=None):
        self.model = model if model is not None else cm.ChargeModel(
            vehicle_names=NAMES, baseline=baseline or fake_baseline())
        self.ledger = cost.ChargingLedger()
        self.now, self.step = start, step
        self.outlets = {k: outlet_defaults() for k in cm.OUTLETS}
        self.base = (2.0, 2.5, 1.8)        # household per phase; NIBE on phase B
        self.ev_phases = (0.0, 0.0, 0.0)
        self.power = 0.003                  # idle EV meter reading, kW
        self.price = 0.25
        self.prices = prices
        self.state, self.reason = "intelligent", "intelligent_waiting_for_planned_slot"
        self.cars = {}
        self.ledger_on = True
        self._last_power, self._last_exclusive = self.power, None
        self.cycle()

    # vehicles ----------------------------------------------------------------
    def add_car(self, vehicle, soc, *, outlet, kpp, integer=True, changed_at=None):
        self.cars[vehicle] = {"true": soc, "kpp": kpp, "integer": integer, "outlet": outlet,
                              "reported": soc, "changed_at": changed_at or self.now,
                              "last_report": self.now, "unavailable": False}

    def socs(self):
        result = {}
        for vehicle, car in self.cars.items():
            result[vehicle] = (None if car["unavailable"] else
                               {"soc": car["reported"], "changed_at": car["changed_at"]})
        return result

    def _update_cars(self, seconds):
        for car in self.cars.values():
            if self._last_exclusive == car["outlet"] and self._last_power > 1:
                car["true"] = min(100.0, car["true"] + self._last_power * seconds / 3600 / car["kpp"])
            if (self.now - car["last_report"]).total_seconds() >= 120:
                car["last_report"] = self.now
                value = math.floor(car["true"]) if car["integer"] else math.floor(car["true"] * 10) / 10
                if value != car["reported"]:
                    car["reported"], car["changed_at"] = float(value), self.now

    # chargers ----------------------------------------------------------------
    def plug(self, outlet, vehicle=None):
        self.outlets[outlet].update(connected=True, vehicle=vehicle)

    def unplug(self, outlet):
        self.outlets[outlet].update(connected=False, charging=False, vehicle=None)

    def charge(self, outlet, kw=10.2, phases=(14.5, 14.4, 14.5)):
        self.outlets[outlet]["charging"] = True
        self.power, self.ev_phases = kw, phases

    def stop(self, outlet):
        self.outlets[outlet]["charging"] = False
        if not any(o["charging"] for o in self.outlets.values()):
            self.power, self.ev_phases = 0.003, (0.0, 0.0, 0.0)

    def exclusive(self):
        charging = [k for k, o in self.outlets.items() if o["available"] and o["charging"]]
        return charging[0] if len(charging) == 1 else None

    # cycles ------------------------------------------------------------------
    def cycle(self):
        statuses = {k: {"available": o["available"], "connected": o["connected"],
                        "charging": o["charging"], "complete": False,
                        "vehicle": NAMES.get(o["vehicle"], "unknown")}
                    for k, o in self.outlets.items()}
        self.ledger.observe(self.now, statuses, self.power, self.price, "EUR",
                            ["sensor.ev_power_total", "sensor.price"])
        mains = tuple(b + e for b, e in zip(self.base, self.ev_phases))
        events = self.model.observe_cycle(
            self.now, TZ, outlets=deepcopy(self.outlets), ev_power_kw=self.power,
            mains_phase_currents=mains, ev_phase_currents=self.ev_phases, socs=self.socs(),
            capacities=CAPACITIES, ledger=self.ledger.snapshot() if self.ledger_on else None,
            prices=self.prices, price_currency="EUR", controller_state=self.state,
            charge_reason=self.reason)
        self._last_power, self._last_exclusive = self.power, self.exclusive()
        return events

    def advance(self, seconds, hook=None):
        events = set()
        steps = int(seconds // self.step)
        for _ in range(steps):
            self.now += timedelta(seconds=self.step)
            self._update_cars(self.step)
            if hook:
                hook(self)
            events |= self.cycle()
        return events

    def jump(self, seconds):
        """Time passes without observations (HA down)."""
        self.now += timedelta(seconds=seconds)


def stored_with(**sections):
    state = cm.default_state()
    for key, value in sections.items():
        state[key] = value
    return state


def household_state(mean, p90, *, hours=range(24), n=5, days=8, day_class="weekday"):
    buckets = {dc: {} for dc in cm.DAY_CLASSES}
    for hour in hours:
        buckets[day_class][str(hour)] = [[mean, p90] for _ in range(n)]
    day_list = [[(datetime(2026, 11, 1) + timedelta(days=i)).date().isoformat(), 24] for i in range(days)]
    return {"buckets": buckets, "days": day_list}


# ---------------------------------------------------------------------------

class ModuleContractTests(unittest.TestCase):
    def test_pure_stdlib_no_home_assistant_or_relative_imports(self):
        source = (ROOT / "charge_model.py").read_text()
        imports = [line.strip() for line in source.splitlines()
                   if line.strip().startswith(("import ", "from "))]
        self.assertTrue(imports)
        for line in imports:
            self.assertNotIn("homeassistant", line)
            self.assertFalse(line.startswith("from ."), line)
        self.assertNotIn("astimezone()", source)  # never the host time zone

    def test_cold_start_estimators(self):
        model = cm.ChargeModel()
        at = L(2026, 11, 10, 2)
        self.assertEqual(model.base_load(at, TZ), cm.BaseLoad(3.0, 5.0, "default"))
        # I_set = min(16, 16 - 3) = 13 A -> 13 x 3 x 0.235; conservative 11 A.
        self.assertEqual(model.hour_power(at, TZ), cm.HourPower(9.165, 7.755, "default"))
        self.assertEqual(model.kw_per_phase_amp(), cm.Estimate(0.235, "default"))
        self.assertEqual(model.calibration(), cm.Estimate(1.0, "default"))
        self.assertEqual(model.car_power("vehicle_1"), cm.CarPower(10.716, 10.18, 15.2, "default"))
        self.assertEqual(model.kwh_per_pct("vehicle_2", 69.0), cm.KwhPerPct(0.7245, 0.7245, "capacity", 0))
        self.assertEqual(model.kwh_per_pct("vehicle_2"), cm.KwhPerPct(0.7875, 0.7875, "default", 0))
        self.assertEqual(model.budget_kwh(None), cm.Estimate(30.0, "default"))
        self.assertEqual(model.price_forecast(at, TZ), (None, "unknown"))
        self.assertIsNone(model.departure_suggestion("vehicle_1", "07:00"))
        self.assertEqual(model.realised_sessions(TZ), [])
        self.assertEqual(model.household_summary(at, TZ), {"learned_days": 0, "night_expected_kw": None})


class StateValidationTests(unittest.TestCase):
    def learned_state(self):
        state = stored_with(
            household=household_state(1.0, 1.5),
            power={"kw_per_phase_amp": [0.236] * 5, "calibration": [1.04] * 5,
                   "car_cap": {"vehicle_1": [15.2] * 5, "vehicle_2": [15.1] * 5}},
            energy={"vehicle_1": [0.754] * 4, "vehicle_2": [0.719] * 4},
            budget={"vehicle_1": [[f"x{i}", 28.0, i] for i in range(4)],
                    "vehicle_2": [[f"e{i}", 51.0, i] for i in range(4)]},
            departures={"vehicle_1": {"weekday": [455] * 9, "weekend": []},
                        "vehicle_2": {"weekday": [], "weekend": []}},
            prices={"weekday": {"2": [[100 + i, 0.27] for i in range(4)]}, "weekend": {}},
        )
        state["last_good"] = {"fetched_at": "2026-11-09T13:00:00+00:00", "currency": "EUR",
                              "slots": [[29000000, 29000060, 0.3]]}
        return state

    def summary(self, model):
        at = L(2026, 11, 10, 2)
        return (model.hour_power(at, TZ), model.kw_per_phase_amp(), model.calibration(),
                model.car_power("vehicle_1"), model.kwh_per_pct("vehicle_1", 78.182),
                model.budget_kwh("vehicle_2"), model.price_forecast(at, TZ),
                model.departure_suggestion("vehicle_1", "06:00"))

    def test_none_gives_defaults_without_issues(self):
        model = cm.ChargeModel(None)
        self.assertEqual(model.load_issues, ())
        self.assertEqual(model.to_dict(), cm.default_state())
        self.assertFalse(model.dirty)

    def test_learned_state_round_trips_through_json(self):
        stored = json.loads(json.dumps(self.learned_state()))
        model = cm.ChargeModel(stored)
        self.assertEqual(model.load_issues, ())
        again = cm.ChargeModel(json.loads(json.dumps(model.to_dict())))
        self.assertEqual(self.summary(model), self.summary(again))
        power = model.hour_power(L(2026, 11, 10, 2), TZ)
        self.assertEqual(power.source, "learned")
        self.assertGreater(power.conservative_kw, 8.5)  # 8 committed days lift the cold-start cap
        # P20 07:35 - 10 min = 07:25, floored to the quarter.
        self.assertEqual(model.departure_suggestion("vehicle_1", "06:00")["suggested_ready_by"], "07:15")

    def test_each_corrupt_field_resets_only_itself(self):
        corruptions = (None, "x", -1, 1e9, True, float("nan"))
        paths = {
            "household.buckets.weekday.2": lambda s, v: s["household"]["buckets"]["weekday"].__setitem__("2", [[v, 1.5]] * 5),
            "household.days": lambda s, v: s["household"].__setitem__("days", [["2026-11-01", v]]),
            "power.kw_per_phase_amp": lambda s, v: s["power"].__setitem__("kw_per_phase_amp", [v] * 5),
            "power.calibration": lambda s, v: s["power"].__setitem__("calibration", [v] * 5),
            "power.car_cap.vehicle_1": lambda s, v: s["power"]["car_cap"].__setitem__("vehicle_1", [v] * 5),
            "energy.vehicle_1": lambda s, v: s["energy"].__setitem__("vehicle_1", [v] * 4),
            "budget.vehicle_2": lambda s, v: s["budget"].__setitem__("vehicle_2", [["e", v, 0]]),
            "departures.vehicle_1.weekday": lambda s, v: s["departures"]["vehicle_1"].__setitem__("weekday", [v] * 9),
            "prices.weekday.2": lambda s, v: s["prices"]["weekday"].__setitem__("2", [[1 + i, v] for i in range(3)]),
            "last_good": lambda s, v: s["last_good"].__setitem__("slots", [[1, 61, v]]),
        }
        reference = cm.ChargeModel(self.learned_state())
        for path, corrupt in paths.items():
            for value in corruptions:
                if value == -1 and path.startswith(("prices", "last_good")):
                    continue  # negative prices are valid
                with self.subTest(path=path, value=value):
                    state = self.learned_state()
                    corrupt(state, value)
                    model = cm.ChargeModel(state)
                    self.assertIn(path, model.load_issues)
                    self.assertEqual(len(model.load_issues), 1, model.load_issues)
                    # Everything else stays learned.
                    if path != "energy.vehicle_1":
                        self.assertEqual(model.kwh_per_pct("vehicle_1", 78.182),
                                         reference.kwh_per_pct("vehicle_1", 78.182))
                    if not path.startswith("budget"):
                        self.assertEqual(model.budget_kwh("vehicle_2"), reference.budget_kwh("vehicle_2"))

    def test_huge_integers_and_odd_keys_reset_only_their_field(self):
        # JSON returns 10**400 as an int; math.isfinite() cannot convert it (OverflowError)
        # and the next save would have replaced the whole learned model with defaults.
        reference = cm.ChargeModel(self.learned_state())
        for value in (10 ** 400, -10 ** 30, 2 ** 53 + 1):
            for path in ("power.calibration", "energy.vehicle_1", "budget.vehicle_2", "prices.weekday.2"):
                with self.subTest(value=str(value)[:12], path=path):
                    state = self.learned_state()
                    if path == "power.calibration":
                        state["power"]["calibration"] = [value] * 5
                    elif path == "energy.vehicle_1":
                        state["energy"]["vehicle_1"] = [value] * 4
                    elif path == "budget.vehicle_2":
                        state["budget"]["vehicle_2"] = [["e", value, 0]]
                    else:
                        state["prices"]["weekday"]["2"] = [[1, value]]
                    model = cm.ChargeModel(state)
                    self.assertEqual(model.load_issues, (path,))
                    self.assertEqual(model.kwh_per_pct("vehicle_2", 69.0), reference.kwh_per_pct("vehicle_2", 69.0))
        for key in ("9" * 5000, "\u00b2", "002"):
            state = self.learned_state()
            state["household"]["buckets"]["weekday"][key] = [[1.0, 1.5]]
            model = cm.ChargeModel(state)
            self.assertEqual(model.load_issues, (f"household.buckets.weekday.{key}",))

    def test_corrupt_field_returns_the_default_with_default_source(self):
        state = self.learned_state()
        state["energy"]["vehicle_1"] = [0.754, None, 0.75, 0.76]
        state["power"]["kw_per_phase_amp"] = "x"
        state["power"]["calibration"] = [1e9] * 3
        state["budget"]["vehicle_2"] = [["e", -1, 0]]
        model = cm.ChargeModel(state)
        self.assertEqual(model.kwh_per_pct("vehicle_1"), cm.KwhPerPct(0.7875, 0.7875, "default", 0))
        self.assertEqual(model.kw_per_phase_amp(), cm.Estimate(0.235, "default"))
        self.assertEqual(model.calibration(), cm.Estimate(1.0, "default"))
        self.assertEqual(model.budget_kwh("vehicle_2"), cm.Estimate(30.0, "default"))

    def test_oversized_lists_reset(self):
        state = self.learned_state()
        state["energy"]["vehicle_1"] = [0.754] * 8
        state["household"]["buckets"]["weekday"]["2"] = [[1.0, 1.5]] * 15
        state["power"]["kw_per_phase_amp"] = [0.235] * 49
        state["departures"]["vehicle_1"]["weekday"] = [455] * 29
        state["prices"]["weekday"]["2"] = [[i, 0.3] for i in range(15)]
        state["budget"]["vehicle_1"] = [[f"x{i}", 28.0, i] for i in range(11)]
        state["realised"] = [{}] * 40
        issues = set(cm.ChargeModel(state).load_issues)
        self.assertEqual(issues, {"energy.vehicle_1", "household.buckets.weekday.2",
                                  "power.kw_per_phase_amp", "departures.vehicle_1.weekday",
                                  "prices.weekday.2", "budget.vehicle_1", "realised"})

    def test_fully_corrupt_state_gives_exactly_the_cold_start_model(self):
        garbage = {"schema": 1, "household": "x", "power": {"kw_per_phase_amp": None,
                   "calibration": [-1], "car_cap": 7}, "energy": {"vehicle_1": [1e9]},
                   "budget": None, "departures": {"vehicle_1": "x"}, "prices": [1],
                   "last_good": {"fetched_at": "x"}, "realised": "x"}
        model = cm.ChargeModel(garbage)
        self.assertTrue(model.load_issues)
        self.assertEqual(self.summary(model), self.summary(cm.ChargeModel()))
        self.assertEqual(model.to_dict(), cm.default_state())

    def test_schema_mismatch_and_non_dict_reset_everything(self):
        for stored in ({"schema": 2, "energy": {"vehicle_1": [0.7] * 3}}, [], "x", {"energy": {}}):
            with self.subTest(stored=stored):
                model = cm.ChargeModel(stored)
                self.assertEqual(model.load_issues, ("schema",))
                self.assertEqual(model.to_dict(), cm.default_state())

    def test_nan_saved_as_null_by_json_is_rejected(self):
        state = self.learned_state()
        state["household"]["buckets"]["weekday"]["2"] = [[float("nan"), 1.5]] * 5
        stored = json.loads(json.dumps(state).replace("NaN", "null"))
        model = cm.ChargeModel(stored)
        self.assertEqual(model.load_issues, ("household.buckets.weekday.2",))
        self.assertEqual(model.base_load(L(2026, 11, 10, 2), TZ).source, "default")

    def test_invalid_realised_record_dropped_individually(self):
        sim = Sim(L(2026, 11, 9, 18), prices=known_entries())
        sim.plug("smartevse_1", "vehicle_1")
        sim.advance(60)
        state = sim.model.to_dict()
        good = deepcopy(state["realised"][0])
        bad = deepcopy(good)
        bad["id"], bad["outlet"] = "other", "smartevse_2"
        bad["last"]["energy_kwh"] = "x"
        state["realised"] = [good, bad]
        model = cm.ChargeModel(state)
        self.assertEqual(model.load_issues, ("realised.1",))
        self.assertEqual([r["id"] for r in model.to_dict()["realised"]], [good["id"]])

    def test_to_dict_is_detached(self):
        model = cm.ChargeModel(self.learned_state())
        data = model.to_dict()
        data["energy"]["vehicle_1"].append(5.0)
        data["household"]["buckets"]["weekday"].clear()
        self.assertEqual(model.to_dict(), cm.ChargeModel(self.learned_state()).to_dict())


class HouseholdLoadTests(unittest.TestCase):
    def observe_hour(self, model, start, mains, *, step=60, ev=(0.0, 0.0, 0.0), charging=False,
                     seconds=3600):
        outlets = {"smartevse_1": {**outlet_defaults(), "connected": charging, "charging": charging}}
        t = start
        while t < start + timedelta(seconds=seconds):
            model.observe_cycle(t, TZ, outlets=outlets, mains_phase_currents=mains,
                                ev_phase_currents=ev)
            t += timedelta(seconds=step)
        return t

    def test_hour_commits_mean_and_p90_of_max_phase_base(self):
        model = cm.ChargeModel()
        start = L(2026, 11, 10, 2)  # Tuesday 02:00 local
        t = start
        for i in range(60):
            nibe = 4.0 if i % 4 == 0 else 0.4   # NIBE cycles on phase B
            model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(1.6, 0.44 + nibe, 1.2),
                                ev_phase_currents=(0.0, 0.0, 0.0))
            t += timedelta(minutes=1)
        self.assertFalse(model.dirty)  # the hour is still accumulating (in memory only)
        events = model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(1.6, 0.84, 1.2))
        self.assertIn("household_hour", events)
        bucket = model.to_dict()["household"]["buckets"]["weekday"]["2"]
        samples = [max(1.6, 0.44 + (4.0 if i % 4 == 0 else 0.4), 1.2) for i in range(60)]
        self.assertEqual(bucket, [[round(sum(samples) / 60, 3), round(cm._quantile(samples, 0.9), 3)]])
        self.assertEqual(bucket[0], [2.31, 4.44])

    def test_base_is_mains_minus_ev_per_phase(self):
        model = cm.ChargeModel()
        # SmartEVSE holds the max phase near 16 A while the car takes 14.5 A on each phase.
        t = self.observe_hour(model, L(2026, 11, 10, 1), (16.6, 18.4, 16.1),
                              ev=(14.5, 14.5, 14.5), charging=True)
        model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(1, 1, 1))
        self.assertEqual(model.to_dict()["household"]["buckets"]["weekday"]["1"], [[3.9, 3.9]])

    def test_missing_ev_phases_skip_samples_only_while_charging(self):
        model = cm.ChargeModel()
        t = self.observe_hour(model, L(2026, 11, 10, 1), (16.6, 18.4, 16.1), ev=None, charging=True)
        t = self.observe_hour(model, t, (2.0, 3.0, 1.0), ev=None)
        model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(1, 1, 1))
        buckets = model.to_dict()["household"]["buckets"]["weekday"]
        self.assertNotIn("1", buckets)
        self.assertEqual(buckets["2"], [[3.0, 3.0]])

    def test_needs_30_samples_to_commit(self):
        model = cm.ChargeModel()
        t = self.observe_hour(model, L(2026, 11, 10, 3), (2, 2, 2), step=125)  # 29 samples
        model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(1, 1, 1))
        self.assertEqual(model.to_dict()["household"]["buckets"]["weekday"], {})
        t = self.observe_hour(model, L(2026, 11, 10, 5), (2, 2, 2), step=120)  # 30 samples
        model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(1, 1, 1))
        self.assertIn("5", model.to_dict()["household"]["buckets"]["weekday"])

    def test_prediction_median_of_means_and_conservative_p80_of_p90(self):
        state = stored_with(household={"buckets": {"weekday": {"2": [
            [2.1, 3.0], [1.8, 2.6], [2.4, 4.7], [2.0, 2.9], [3.5, 3.8]]}, "weekend": {}}, "days": []})
        model = cm.ChargeModel(state)
        base = model.base_load(L(2026, 11, 10, 2, 30), TZ)
        self.assertEqual(base.expected_a, 2.1)
        self.assertEqual(base.conservative_a, round(max(cm._quantile([3.0, 2.6, 4.7, 2.9, 3.8], 0.8), 3.1), 3))
        self.assertEqual(base.conservative_a, 3.98)
        self.assertEqual(base.source, "learned")
        # Two entries are not enough: defaults.
        state["household"]["buckets"]["weekday"]["2"] = [[2.1, 3.0], [1.8, 2.6]]
        self.assertEqual(cm.ChargeModel(state).base_load(L(2026, 11, 10, 2), TZ),
                         cm.BaseLoad(3.0, 5.0, "default"))

    def test_conservative_is_at_least_expected_plus_one_amp(self):
        state = stored_with(household={"buckets": {"weekday": {"2": [[2.0, 2.2]] * 4},
                                                   "weekend": {}}, "days": []})
        self.assertEqual(cm.ChargeModel(state).base_load(L(2026, 11, 10, 2), TZ).conservative_a, 3.0)

    def test_recent_household_load_is_a_floor_for_now_and_unlearned_hours(self):
        # Review round 3: the NIBE heat pump adds 5 A on phase B for 40 min of each hour;
        # the plan kept assuming the 3 A default all night (cold start).
        model = cm.ChargeModel(stored_with(household=household_state(2.0, 2.5, hours=range(0, 6))))
        start = L(2026, 11, 9, 20)  # Monday
        t = start
        while t < start + timedelta(minutes=60):
            phase_b = 7.5 if t.minute < 40 else 2.5
            model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(2.0, phase_b, 1.8))
            t += timedelta(seconds=60)
        # Re-read once per 15 min in 0.5 A steps: at 21:00 the last hour is 41 x 7.5 A, 20 x 2.5 A.
        model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(2.0, 7.5, 1.8))
        mean = math.ceil((41 * 7.5 + 20 * 2.5) / 61 / 0.5) * 0.5
        now_hour = model.base_load(L(2026, 11, 9, 20, 59), TZ)
        self.assertAlmostEqual(now_hour.expected_a, mean, delta=0.05)
        self.assertAlmostEqual(now_hour.conservative_a, 7.5, delta=0.05)
        self.assertAlmostEqual(model.base_load(L(2026, 11, 9, 21, 30), TZ).expected_a, mean, delta=0.05)
        # An hour without a learned bucket uses it as well (instead of 3 A / 5 A) ...
        unlearned = model.base_load(L(2026, 11, 9, 23), TZ)
        self.assertEqual(unlearned.source, "default")
        self.assertAlmostEqual(unlearned.expected_a, mean, delta=0.05)
        # ... while a learned night hour keeps its own statistics.
        self.assertEqual(model.base_load(L(2026, 11, 10, 2), TZ), cm.BaseLoad(2.0, 3.0, "learned"))

    def test_recent_evening_peak_is_bounded_for_unlearned_later_hours(self):
        model = cm.ChargeModel()
        t = L(2026, 11, 9, 18)
        while t < L(2026, 11, 9, 19):
            model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(14.0, 13.0, 12.0))
            t += timedelta(seconds=60)
        self.assertEqual(model.base_load(L(2026, 11, 9, 18, 59), TZ).expected_a, 14.0)  # what it is now
        later = model.base_load(L(2026, 11, 10, 2), TZ)
        self.assertEqual((later.expected_a, later.conservative_a), (8.0, 9.0))
        self.assertGreater(model.hour_power(L(2026, 11, 10, 2), TZ).expected_kw, 5.0)

    def test_bucket_keeps_last_14(self):
        model = cm.ChargeModel()
        start = L(2026, 11, 2, 4)  # Monday
        for day in range(16):
            when = start + timedelta(days=day)
            if when.astimezone(TZ).weekday() >= 5:
                continue
            t = self.observe_hour(model, when, (1.0 + day / 10, 1.0, 1.0), step=120)
            model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(1, 1, 1))
        bucket = model.to_dict()["household"]["buckets"]["weekday"]["4"]
        self.assertEqual(len(bucket), 12)  # 12 weekdays in 16 days
        for day in range(16, 40):
            when = start + timedelta(days=day)
            if when.astimezone(TZ).weekday() >= 5:
                continue
            t = self.observe_hour(model, when, (2.0, 1.0, 1.0), step=120)
            model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(1, 1, 1))
        bucket = model.to_dict()["household"]["buckets"]["weekday"]["4"]
        self.assertEqual(bucket, [[2.0, 2.0]] * 14)

    def test_day_class_from_price_flags_else_saturday_sunday(self):
        model = cm.ChargeModel()
        # Monday 2026-11-09 flagged as a holiday by its own list (hours >= 01:00).
        holiday = [rep_entry(datetime(2026, 11, 9, h, tzinfo=TZ), 0.2, holiday=True) for h in range(1, 24)]
        # Tuesday's local 00:00 belongs to Monday's CET list and carries its flag: ignored.
        holiday.append(rep_entry(datetime(2026, 11, 10, 0, tzinfo=TZ), 0.2, holiday=True))
        start = L(2026, 11, 9, 5)
        t = start
        while t < start + timedelta(hours=1):
            model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(2, 2, 2), prices=holiday)
            t += timedelta(minutes=1)
        model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(2, 2, 2), prices=holiday)
        self.assertIn("5", model.to_dict()["household"]["buckets"]["weekend"])
        self.assertEqual(model.day_class(datetime(2026, 11, 10).date()), "weekday")
        self.assertEqual(model.day_class(datetime(2026, 11, 14).date()), "weekend")  # Saturday
        self.assertEqual(cm.day_class(datetime(2026, 11, 15).date()), "weekend")      # Sunday

    def test_cold_start_caps_conservative_until_seven_committed_days(self):
        model = cm.ChargeModel()
        start = L(2026, 11, 2, 0)  # Monday 00:00 local
        t = start
        caps = []
        while t <= start + timedelta(days=8, minutes=2):
            # Low base: P90 1.4 A, so learned conservative would be 13.9 A -> 9.8 kW.
            phase_b = 1.4 if t.minute % 10 == 0 else 1.0
            model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(0.6, phase_b, 0.8))
            if t.astimezone(TZ).hour == 12 and t.minute == 0:
                caps.append((model.household_learned_days(),
                             model.hour_power(L(2026, 11, 10, 2), TZ).conservative_kw))
            t += timedelta(seconds=120)
        for days, conservative in caps:
            if days < 7:
                self.assertLessEqual(conservative, 8.5)
        self.assertEqual(model.household_learned_days(), 8)
        learned = model.hour_power(L(2026, 11, 10, 2), TZ)
        self.assertEqual(learned.source, "learned")
        self.assertGreater(learned.conservative_kw, 8.5)
        self.assertLessEqual(learned.conservative_kw, learned.expected_kw)

    def test_dst_fall_back_day_has_25_hours_and_two_samples_for_03(self):
        model = cm.ChargeModel()
        start = L(2026, 10, 25, 0)  # Sunday; 04:00 EEST -> 03:00 EET
        t = start
        while t <= L(2026, 10, 26, 0, 2):
            model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(2.0, 3.0, 1.0))
            t += timedelta(seconds=120)
        state = model.to_dict()["household"]
        self.assertEqual(state["days"], [["2026-10-25", 25]])
        self.assertEqual(len(state["buckets"]["weekend"]["3"]), 2)
        self.assertEqual(len(state["buckets"]["weekend"]["4"]), 1)
        self.assertEqual(sum(len(v) for v in state["buckets"]["weekend"].values()), 25)

    def test_dst_spring_forward_day_has_23_hours_and_no_03(self):
        model = cm.ChargeModel()
        t = L(2026, 3, 29, 0)  # Sunday; 03:00 EET -> 04:00 EEST
        while t <= L(2026, 3, 30, 0, 2):
            model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(2.0, 3.0, 1.0))
            t += timedelta(seconds=120)
        state = model.to_dict()["household"]
        self.assertEqual(state["days"], [["2026-03-29", 23]])
        self.assertNotIn("3", state["buckets"]["weekend"])

    def test_power_slots_follow_local_hours_across_dst(self):
        model = cm.ChargeModel()
        autumn = model.power_slots(L(2026, 10, 25, 0), L(2026, 10, 26, 0), TZ)
        spring = model.power_slots(L(2026, 3, 29, 0), L(2026, 3, 30, 0), TZ)
        self.assertEqual((len(autumn), len(spring)), (25, 23))
        self.assertEqual([s["start"].astimezone(TZ).hour for s in autumn][2:6], [2, 3, 3, 4])
        for slots in (autumn, spring):
            for left, right in zip(slots, slots[1:]):
                self.assertEqual(left["end"], right["start"])

    def test_recent_household_floor_is_quantised_and_refreshed_per_unit(self):
        # Confirmation round 3: the last-hour floor moved the power slots on every
        # cycle of a spiky house (payload churn up to 3.4x).
        model = cm.ChargeModel()
        t = L(2026, 11, 9, 20, 0)
        seen = set()
        for index in range(int(75 * 60 / 10)):
            spike = 6.0 if index % 30 < 4 else 0.0
            model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(2.13 + spike, 1.0, 1.0))
            recent = model.recent_base()
            if recent is not None:
                seen.add((model._recent_snapshot[0], recent[1:]))
                _, mean, p90 = recent
                self.assertEqual((mean * 2) % 1, 0)
                self.assertEqual((p90 * 2) % 1, 0)
                self.assertGreaterEqual(p90, mean)
            t += timedelta(seconds=10)
        # 30 min to reach the minimum span, then one read per 15-minute unit.
        self.assertLessEqual(len(seen), 4)
        self.assertGreaterEqual(len(seen), 2)

    def test_next_hour_keeps_the_floor_right_after_an_hour_boundary(self):
        # Review round 4: 'near' followed the snapshot's sample time, so at hh:00:10 the
        # next hour lost its floor for up to 15 min.
        model = cm.ChargeModel(stored_with(household=household_state(2.0, 2.5, hours=range(0, 24))))
        t = L(2026, 11, 9, 19, 7, 23)
        while t <= L(2026, 11, 9, 21, 0, 10):
            model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(2.0, 7.5, 1.8))
            if t >= L(2026, 11, 9, 20, 0):
                # The hour after the current one keeps the 7.5 A floor at every cycle.
                current = t.astimezone(TZ).replace(minute=0, second=0, microsecond=0)
                self.assertEqual(model.base_load(current + timedelta(hours=1), TZ).expected_a, 7.5, t)
            t += timedelta(seconds=10)
        # Refreshes happen on unit boundaries only.
        self.assertEqual(model._recent_snapshot[0], L(2026, 11, 9, 21, 0, 3))

    def test_household_summary(self):
        model = cm.ChargeModel(stored_with(household=household_state(2.1, 3.8, days=3)))
        summary = model.household_summary(L(2026, 11, 9, 18), TZ)
        # (16 - 2.1) A x 3 x 0.235 = 9.8 kW
        self.assertEqual(summary, {"learned_days": 3, "night_expected_kw": 9.8})


class PowerTests(unittest.TestCase):
    def test_physics_setpoint_minimum_and_car_cap(self):
        model = cm.ChargeModel(stored_with(household=household_state(0.5, 1.0)))
        at = L(2026, 11, 10, 2)
        # I_set = min(16, 16 - 0.5) = 15.5 A; the car cap (15.2 A default) only with the car.
        self.assertEqual(model.hour_power(at, TZ).expected_kw, round(15.5 * 3 * 0.235, 3))
        self.assertEqual(model.hour_power(at, TZ, vehicle="vehicle_1", include_car_cap=True).expected_kw,
                         round(15.2 * 3 * 0.235, 3))
        # Below MinCurrent SmartEVSE pauses: 0 kW.
        self.assertEqual(model.hour_power(at, TZ, max_mains=6.4), cm.HourPower(0.0, 0.0, "learned"))
        cold = cm.ChargeModel()
        self.assertEqual(cold.hour_power(at, TZ, max_mains=8.9), cm.HourPower(0.0, 0.0, "default"))
        # A higher MinCurrent stops conservative charging first.
        self.assertEqual(cold.hour_power(at, TZ, min_current=12),
                         cm.HourPower(9.165, 0.0, "default"))

    def test_missing_or_invalid_limits_use_16_16_6(self):
        model = cm.ChargeModel()
        at = L(2026, 11, 10, 2)
        reference = model.hour_power(at, TZ)
        for value in (None, "x", 0, -5, float("inf"), True):
            with self.subTest(value=value):
                self.assertEqual(model.hour_power(at, TZ, max_current=value, max_mains=value,
                                                  min_current=value), reference)
        self.assertEqual(model.hour_power(at, TZ, max_current="10").expected_kw, round(10 * 3 * 0.235, 3))

    def test_calibration_raises_expected_but_never_loosens_conservative(self):
        high = cm.ChargeModel(stored_with(power={"kw_per_phase_amp": [], "calibration": [1.3] * 5,
                                                 "car_cap": {"vehicle_1": [], "vehicle_2": []}}))
        # The floor is 0.5 since review round 3 (calibration is physics-only now).
        low = cm.ChargeModel(stored_with(power={"kw_per_phase_amp": [], "calibration": [0.4] * 5,
                                                "car_cap": {"vehicle_1": [], "vehicle_2": []}}))
        at = L(2026, 11, 10, 2)
        self.assertEqual(high.calibration(), cm.Estimate(1.1, "learned"))
        self.assertEqual(high.hour_power(at, TZ), cm.HourPower(round(9.165 * 1.1, 3), 7.755, "default"))
        self.assertEqual(low.calibration(), cm.Estimate(0.5, "learned"))
        self.assertEqual(low.hour_power(at, TZ), cm.HourPower(round(9.165 * 0.5, 3),
                                                             round(7.755 * 0.5, 3), "default"))

    def night(self, sim, minutes, outlet="smartevse_2", vehicle="vehicle_2", **charge):
        sim.plug(outlet, vehicle)
        sim.add_car(vehicle, 30.0, outlet=outlet, kpp=0.719, integer=False)
        sim.advance(60)
        sim.charge(outlet, **charge)
        return sim.advance(minutes * 60)

    def test_windows_learn_kw_per_phase_amp_car_cap_and_calibration(self):
        sim = Sim(L(2026, 11, 8, 0, 5), step=10)
        # EX30 at full offer: 15.2 A per phase, 3 x 15.2 x 0.235 = 10.72 kW.
        events = self.night(sim, 41, kw=10.72, phases=(15.2, 15.1, 15.3))
        self.assertIn("power_window", events)
        power = sim.model.to_dict()["power"]
        self.assertEqual(len(power["kw_per_phase_amp"]), 7)  # 8 windows, the first is discarded
        self.assertEqual(sim.model.kw_per_phase_amp(), cm.Estimate(round(10.72 / 45.6, 5), "learned"))
        self.assertEqual(sim.model.car_current_cap("vehicle_2"), cm.Estimate(15.3, "learned"))
        self.assertEqual(sim.model.car_current_cap("vehicle_1").source, "default")
        # Predicted from the measured 2.5 A base: min(13.5, cap) x 3 x 0.235 -> observed/predicted > 1.10.
        self.assertEqual(sim.model.calibration(), cm.Estimate(1.1, "learned"))
        car = sim.model.car_power("vehicle_2")
        self.assertEqual(car.cap_kw, round(3 * 0.23509 * 15.3, 3))
        self.assertEqual(car.cap_low_kw, round(car.cap_kw * 0.95, 3))

    def test_calibration_ignores_the_household_forecast_error_of_the_hour(self):
        # Review round 3: a 2-hour evening charge whose real base (2.5 A) is far below the
        # learned evening bucket (6 A) set calibration to 1.10 for every hour tonight.
        model = cm.ChargeModel(stored_with(household=household_state(6.0, 8.0)), vehicle_names=NAMES,
                               baseline=fake_baseline())
        sim = Sim(L(2026, 11, 9, 17, 30), model=model, step=10)
        # Smart mode offers 16 - 2.5 = 13.5 A; the car takes it: 3 x 13.5 x 0.235 = 9.52 kW.
        self.night(sim, 120, kw=9.52, phases=(13.5, 13.5, 13.5))
        self.assertGreater(len(sim.model.to_dict()["power"]["calibration"]), 10)
        self.assertAlmostEqual(sim.model.calibration().value, 1.0, delta=0.02)
        tonight = sim.model.hour_power(L(2026, 11, 10, 2), TZ, vehicle="vehicle_2", include_car_cap=True)
        uncalibrated = sim.model.hour_power(L(2026, 11, 10, 2), TZ, vehicle="vehicle_2", include_car_cap=True,
                                            calibrated=False)
        self.assertAlmostEqual(tonight.expected_kw, uncalibrated.expected_kw, delta=0.2)

    def test_car_limited_window_is_not_a_calibration_sample(self):
        # A cold battery drawing 8 A of a 13.5 A offer says nothing about Smart mode: it
        # never lowers the calibration (skipped while the car's limit is unknown, and
        # once the car's own current limit reflects it the physics already match).
        sim = Sim(L(2026, 11, 9, 1, 0), step=10)
        self.night(sim, 41, kw=5.64, phases=(8.0, 8.0, 8.0))
        self.assertTrue(all(value >= 0.98 for value in sim.model.to_dict()["power"]["calibration"]))
        self.assertGreaterEqual(sim.model.calibration().value, 0.98)
        self.assertEqual(len(sim.model.to_dict()["power"]["kw_per_phase_amp"]), 7)

    def test_car_limited_session_plans_at_what_the_car_accepts(self):
        # Confirmation round 3: with car-limited windows skipped, a cold battery taught the
        # model nothing and the plan kept 10.7 kW for a car taking 5.6 kW. The session's
        # own windows now set its current cap until it draws more again.
        sim = Sim(L(2026, 11, 9, 1, 0), step=10)
        sim.outlets["smartevse_2"]["offered_current"] = 13.5  # below a full offer: no car-cap sample
        self.night(sim, 41, kw=5.64, phases=(8.0, 8.0, 8.0))
        nominal = sim.model.car_power("vehicle_2")
        self.assertEqual(nominal.source, "default")
        cold = sim.model.car_power("vehicle_2", outlet="smartevse_2", now=sim.now)
        self.assertEqual((cold.current_cap_a, cold.source), (8.0, "session"))
        self.assertAlmostEqual(cold.cap_kw, 3 * sim.model.kw_per_phase_amp().value * 8.0, places=2)
        # Another outlet or vehicle, or stale windows, keep the learned cap.
        self.assertEqual(sim.model.car_power("vehicle_2", outlet="smartevse_1", now=sim.now), nominal)
        self.assertEqual(sim.model.car_power("vehicle_1", outlet="smartevse_2", now=sim.now).source, "default")
        # The evidence lasts the plug session while the car waits for a cheaper slot.
        sim.stop("smartevse_2")
        sim.advance(2 * 3600)
        self.assertEqual(sim.model.car_power("vehicle_2", outlet="smartevse_2", now=sim.now).source, "session")
        # Heat-pump half hour: the offer drops to 8 A and the car takes all of it. That
        # is no evidence that it warmed up, so the cap stays (review round 4).
        sim.base = (2.0, 8.0, 1.8)
        sim.charge("smartevse_2", kw=5.64, phases=(8.0, 8.0, 8.0))
        sim.advance(31 * 60)
        self.assertEqual(sim.model.car_power("vehicle_2", outlet="smartevse_2", now=sim.now).source, "session")
        sim.base = (2.0, 2.5, 1.8)
        # Warmed up: the car takes the offer again and the session cap lifts.
        sim.charge("smartevse_2", kw=9.52, phases=(13.5, 13.5, 13.5))
        sim.advance(31 * 60)
        warm = sim.model.car_power("vehicle_2", outlet="smartevse_2", now=sim.now)
        self.assertEqual(warm.source, "default")
        # Unplugging ends the session evidence.
        sim.charge("smartevse_2", kw=5.64, phases=(8.0, 8.0, 8.0))
        sim.advance(16 * 60)
        self.assertEqual(sim.model.car_power("vehicle_2", outlet="smartevse_2", now=sim.now).source, "session")
        sim.unplug("smartevse_2")
        sim.advance(60)
        self.assertIsNone(sim.model.session_current_cap("smartevse_2", "vehicle_2", sim.now))

    def test_session_cap_is_the_median_of_car_limited_windows(self):
        model = cm.ChargeModel()
        t0 = L(2026, 11, 9, 23, 0)
        session = model._new_session(t0, True)
        model._sessions["smartevse_1"] = session

        def windows(*items):
            session["power_windows"] = [(t0 + timedelta(minutes=5 * i), "vehicle_1", a, limited)
                                        for i, (a, limited) in enumerate(items)]

        # Heat-pump hours lower the offer and a cold car takes less still: 7.3 A and 9.9 A.
        windows((7.3, True), (7.3, True), (9.9, True), (7.3, True), (9.9, True), (7.4, True))
        self.assertEqual(model.session_current_cap("smartevse_1", "vehicle_1"), 7.35)
        # Only car-limited windows are kept; a stray non-limited entry never counts.
        windows((9.5, False), (10.0, True), (9.4, False), (10.1, True), (9.6, False), (10.0, True))
        self.assertEqual(model.session_current_cap("smartevse_1", "vehicle_1"), 10.0)
        # One limited window is not enough; another vehicle's windows never count.
        windows((9.5, False), (8.0, True), (9.4, False))
        self.assertIsNone(model.session_current_cap("smartevse_1", "vehicle_1"))
        self.assertIsNone(model.session_current_cap("smartevse_1", "vehicle_2"))
        # Windows stamped after ``now`` (a clock step back) are ignored.
        windows((8.0, True), (8.0, True))
        self.assertIsNone(model.session_current_cap("smartevse_1", "vehicle_1", t0 - timedelta(minutes=1)))

    def test_remap_drops_the_session_cap(self):
        sim = Sim(L(2026, 11, 9, 1, 0), step=10)
        sim.outlets["smartevse_2"]["offered_current"] = 13.5
        self.night(sim, 41, kw=5.64, phases=(8.0, 8.0, 8.0))
        self.assertIsNotNone(sim.model.session_current_cap("smartevse_2", "vehicle_2", sim.now))
        sim.model.discard_session("smartevse_2")
        self.assertIsNone(sim.model.session_current_cap("smartevse_2", "vehicle_2", sim.now))

    def test_mains_limited_offer_is_not_a_car_cap_sample(self):
        sim = Sim(L(2026, 11, 8, 0, 5), step=10)
        sim.outlets["smartevse_2"]["offered_current"] = 15.2  # 11-08 01:00: offered 15.2 A
        self.night(sim, 41, kw=10.2, phases=(14.6, 14.4, 14.5))
        self.assertEqual(sim.model.to_dict()["power"]["car_cap"]["vehicle_2"], [])
        self.assertEqual(len(sim.model.to_dict()["power"]["kw_per_phase_amp"]), 7)

    def test_first_window_of_a_run_is_discarded(self):
        sim = Sim(L(2026, 11, 8, 0, 5), step=10)
        self.night(sim, 9)
        self.assertEqual(sim.model.to_dict()["power"]["kw_per_phase_amp"], [])
        sim.advance(120)
        self.assertEqual(len(sim.model.to_dict()["power"]["kw_per_phase_amp"]), 1)

    def test_rejected_samples_break_the_run(self):
        cases = {
            "low power tail": lambda s: setattr(s, "power", 1.2),
            "soc 90": lambda s: s.cars["vehicle_2"].update(reported=90.0, true=90.0, last_report=s.now),
            "unmapped": lambda s: s.outlets["smartevse_2"].update(vehicle=None),
            "other outlet unavailable": lambda s: s.outlets["smartevse_1"].update(available=False),
            "both charging": lambda s: s.outlets["smartevse_1"].update(connected=True, charging=True),
            "implausible for offer": lambda s: s.outlets["smartevse_2"].update(offered_current=6.0),
        }
        for name, spoil in cases.items():
            with self.subTest(name):
                sim = Sim(L(2026, 11, 8, 0, 5), step=10)
                self.night(sim, 11)  # windows 0 (discarded) and 1 (accepted)
                accepted = len(sim.model.to_dict()["power"]["kw_per_phase_amp"])
                self.assertEqual(accepted, 1)
                backup = (deepcopy(sim.outlets), sim.power, deepcopy(sim.cars))
                spoil(sim)
                sim.advance(10)
                sim.outlets, sim.power, sim.cars = backup
                sim.advance(10 * 60 + 10)  # new run: its first window is discarded again
                self.assertEqual(len(sim.model.to_dict()["power"]["kw_per_phase_amp"]), accepted + 1)

    def test_observation_gap_restarts_the_run(self):
        sim = Sim(L(2026, 11, 8, 0, 5), step=10)
        self.night(sim, 11)
        sim.jump(180)
        sim.advance(9 * 60)
        self.assertEqual(len(sim.model.to_dict()["power"]["kw_per_phase_amp"]), 1)
        sim.advance(120)
        self.assertEqual(len(sim.model.to_dict()["power"]["kw_per_phase_amp"]), 2)

    def test_remap_removes_this_sessions_car_cap_samples(self):
        sim = Sim(L(2026, 11, 8, 0, 5), step=10)
        self.night(sim, 31, kw=10.72, phases=(15.2, 15.1, 15.3))
        self.assertEqual(len(sim.model.to_dict()["power"]["car_cap"]["vehicle_2"]), 5)
        sim.outlets["smartevse_2"]["vehicle"] = "vehicle_1"  # mapping corrected mid-session
        sim.advance(10)
        power = sim.model.to_dict()["power"]
        self.assertEqual(power["car_cap"]["vehicle_2"], [])
        self.assertEqual(len(power["kw_per_phase_amp"]), 5)  # physics samples are vehicle-free

    def test_learned_buckets_drive_the_prediction(self):
        state = stored_with(household={"buckets": {"weekday": {
            "2": [[2.1, 3.8]] * 5, "18": [[9.0, 14.0]] * 5}, "weekend": {}}, "days": []})
        model = cm.ChargeModel(state)
        night = model.hour_power(L(2026, 11, 10, 2), TZ)
        evening = model.hour_power(L(2026, 11, 10, 18), TZ)
        # Conservative base = max(P80 of P90s 3.8, 2.1 + 1) -> 12.2 A = 8.601 kW, capped at
        # 8.5 kW while fewer than 7 household days are committed.
        self.assertEqual(night, cm.HourPower(round(13.9 * 0.705, 3), 8.5, "learned"))
        state["household"]["days"] = household_state(0, 0)["days"]
        self.assertEqual(cm.ChargeModel(state).hour_power(L(2026, 11, 10, 2), TZ).conservative_kw,
                         round(12.2 * 0.705, 3))
        self.assertEqual(evening.expected_kw, round(7.0 * 0.705, 3))
        self.assertEqual(evening.conservative_kw, 0.0)  # 16 - 14 = 2 A < 6 A


class EnergyPerPercentTests(unittest.TestCase):
    def session(self, sim, vehicle, outlet, soc0, soc1, *, kpp, integer=True, kw=10.2, hook=None):
        sim.plug(outlet, vehicle)
        sim.add_car(vehicle, soc0, outlet=outlet, kpp=kpp, integer=integer,
                    changed_at=sim.now - timedelta(minutes=1))
        sim.advance(60)
        sim.charge(outlet, kw=kw)
        hours = (soc1 - soc0) * kpp / kw
        sim.advance(hours * 3600, hook)
        sim.stop(outlet)
        sim.advance(300)
        sim.unplug(outlet)
        return sim.advance(60)

    def test_xc40_session_sample_and_learning_after_three(self):
        sim = Sim(L(2026, 11, 9, 21), step=10)
        events = self.session(sim, "vehicle_1", "smartevse_1", 40.0, 75.0, kpp=0.754)
        self.assertIn("kwh_sample", events)
        sample = sim.model.to_dict()["energy"]["vehicle_1"][0]
        self.assertAlmostEqual(sample, 0.754, delta=0.754 * 0.03)
        self.assertEqual(sim.model.kwh_per_pct("vehicle_1", 78.182).source, "capacity")
        self.assertEqual(sim.model.learned_sessions("vehicle_1"), 1)
        for _ in range(2):
            sim.jump(3600)
            self.session(sim, "vehicle_1", "smartevse_1", 30.0, 70.0, kpp=0.754)
        learned = sim.model.kwh_per_pct("vehicle_1", 78.182)
        self.assertEqual(learned.source, "learned")
        self.assertAlmostEqual(learned.expected, 0.754, delta=0.02)

    def test_ex30_tenth_percent_session(self):
        sim = Sim(L(2026, 11, 8, 0), step=10)
        self.session(sim, "vehicle_2", "smartevse_2", 29.0, 60.0, kpp=0.719, integer=False)
        self.assertAlmostEqual(sim.model.to_dict()["energy"]["vehicle_2"][0], 0.719, delta=0.01)

    def test_needs_15_points(self):
        sim = Sim(L(2026, 11, 9, 21), step=10)
        self.session(sim, "vehicle_1", "smartevse_1", 50.0, 65.0, kpp=0.754)  # 51 -> 64/65
        self.assertEqual(sim.model.to_dict()["energy"]["vehicle_1"], [])

    def test_changes_above_90_are_not_used(self):
        sim = Sim(L(2026, 11, 9, 21), step=10)
        self.session(sim, "vehicle_1", "smartevse_1", 78.0, 99.0, kpp=0.754)  # usable 79..90
        self.assertEqual(sim.model.to_dict()["energy"]["vehicle_1"], [])

    def test_out_of_band_ratio_rejected(self):
        sim = Sim(L(2026, 11, 9, 21), step=10)
        # 1.2 kWh/% is outside 0.85..1.35 x 69/100 for the EX30 (e.g. a remapped XC40 + heating).
        self.session(sim, "vehicle_2", "smartevse_2", 30.0, 50.0, kpp=1.2)
        self.assertEqual(sim.model.to_dict()["energy"]["vehicle_2"], [])

    def test_remap_or_battery_unavailable_drops_the_session(self):
        def remap(s):
            if s.cars["vehicle_1"]["true"] > 60:
                s.outlets["smartevse_1"]["vehicle"] = "vehicle_2"
        sim = Sim(L(2026, 11, 9, 21), step=10)
        sim.add_car("vehicle_2", 50.0, outlet="smartevse_2", kpp=0.719)
        self.session(sim, "vehicle_1", "smartevse_1", 30.0, 75.0, kpp=0.754, hook=remap)
        self.assertEqual(sim.model.to_dict()["energy"], {"vehicle_1": [], "vehicle_2": []})

        def unavailable(s):
            s.cars["vehicle_1"]["unavailable"] = 55 < s.cars["vehicle_1"]["true"] < 56
        sim = Sim(L(2026, 11, 9, 21), step=10)
        self.session(sim, "vehicle_1", "smartevse_1", 30.0, 75.0, kpp=0.754, hook=unavailable)
        self.assertEqual(sim.model.to_dict()["energy"]["vehicle_1"], [])

    def test_gap_freezes_the_segment(self):
        def restart(s):
            if not s.__dict__.get("restarted") and s.cars["vehicle_1"]["true"] > 40.5:
                s.restarted = True
                s.jump(240)
        sim = Sim(L(2026, 11, 9, 21), step=10)
        self.session(sim, "vehicle_1", "smartevse_1", 30.0, 75.0, kpp=0.754, hook=restart)
        self.assertEqual(sim.model.to_dict()["energy"]["vehicle_1"], [])  # 31..40 < 15 points

    def test_stale_soc_at_plug_in_never_anchors_a_sample(self):
        sim = Sim(L(2026, 11, 9, 21), step=10)
        sim.plug("smartevse_1", "vehicle_1")
        sim.add_car("vehicle_1", 53.0, outlet="smartevse_1", kpp=0.754,
                    changed_at=sim.now - timedelta(hours=6))
        sim.cars["vehicle_1"]["last_report"] = sim.now + timedelta(days=1)  # cloud never updates
        sim.advance(60)
        sim.charge("smartevse_1")
        sim.advance(3 * 3600)
        sim.unplug("smartevse_1")
        sim.advance(60)
        self.assertEqual(sim.model.to_dict()["energy"]["vehicle_1"], [])

    def test_median_resists_one_bad_sample_and_conservative_is_max_of_last_five(self):
        state = stored_with(energy={"vehicle_1": [0.75, 0.76, 0.745, 0.755, 0.75, 0.76, 1.0],
                                    "vehicle_2": [0.719, 0.72, 0.71]})
        model = cm.ChargeModel(state)
        self.assertEqual(model.kwh_per_pct("vehicle_1"), cm.KwhPerPct(0.755, 1.0, "learned", 7))
        self.assertEqual(model.kwh_per_pct("vehicle_2"), cm.KwhPerPct(0.719, 0.72, "learned", 3))
        self.assertEqual(model.kwh_per_pct(None, 69.0).source, "capacity")


class SocTests(unittest.TestCase):
    now = L(2026, 11, 9, 21, 30)
    plugged = L(2026, 11, 9, 17, 56)

    def test_sources(self):
        self.assertEqual(cm.resolve_soc(self.now, live_soc=53.0, live_changed_at=self.now), (53.0, "vehicle"))
        self.assertEqual(cm.resolve_soc(self.now, last_soc=61.0, last_soc_at=self.now), (61.0, "last_known"))
        self.assertEqual(cm.resolve_soc(self.now, live_soc="unavailable"), (None, "unknown"))
        self.assertEqual(cm.resolve_soc(self.now, live_soc=150), (None, "unknown"))

    def test_estimated_while_charging_with_an_old_reading(self):
        args = dict(live_soc=53.0, live_changed_at=self.now - timedelta(minutes=20),
                    plugged_at=self.plugged, charging=True, energy_since_kwh=7.54, kwh_per_pct=0.754)
        self.assertEqual(cm.resolve_soc(self.now, **args), (63.0, "estimated"))
        self.assertEqual(cm.resolve_soc(self.now, **args, car_limit=60), (60.0, "estimated"))
        # Review round 3: the energy stays in the battery when the charger stops, so the
        # estimate holds until a newer reading arrives (it used to fall back to 53 %).
        self.assertEqual(cm.resolve_soc(self.now, **{**args, "charging": False}), (63.0, "estimated"))
        self.assertEqual(cm.resolve_soc(self.now, **{**args, "charging": False, "energy_since_kwh": 0.0}),
                         (53.0, "vehicle"))
        fresh = {**args, "live_changed_at": self.now - timedelta(minutes=5)}
        self.assertEqual(cm.resolve_soc(self.now, **fresh), (53.0, "vehicle"))

    def test_unverified_when_unchanged_since_well_before_plug_in(self):
        old = dict(live_soc=80.0, live_changed_at=self.plugged - timedelta(hours=5), plugged_at=self.plugged)
        self.assertEqual(cm.resolve_soc(self.now, **old), (80.0, "unverified"))
        recent = {**old, "live_changed_at": self.plugged - timedelta(minutes=2)}
        self.assertEqual(cm.resolve_soc(self.now, **recent), (80.0, "vehicle"))
        charging = {**old, "charging": True, "energy_since_kwh": 3.77, "kwh_per_pct": 0.754}
        self.assertEqual(cm.resolve_soc(self.now, **charging), (85.0, "unverified"))

    def test_reached_hysteresis(self):
        self.assertTrue(cm.reached_with_hysteresis(80, 80))
        self.assertFalse(cm.reached_with_hysteresis(79.9, 80))
        self.assertTrue(cm.reached_with_hysteresis(78.0, 80, True))
        self.assertFalse(cm.reached_with_hysteresis(77.9, 80, True))
        self.assertTrue(cm.reached_with_hysteresis(None, 80, True))
        self.assertFalse(cm.reached_with_hysteresis(None, 80, False))

    def test_model_estimate_uses_energy_since_the_reading(self):
        sim = Sim(L(2026, 11, 9, 17, 56), step=10)
        sim.plug("smartevse_1", "vehicle_1")
        sim.add_car("vehicle_1", 53.0, outlet="smartevse_1", kpp=0.754,
                    changed_at=sim.now - timedelta(minutes=2))
        sim.cars["vehicle_1"]["last_report"] = sim.now + timedelta(days=1)  # Volvo cloud stalls
        sim.advance(60)
        self.assertEqual(sim.model.soc_estimate("smartevse_1", sim.now), (53.0, "vehicle"))
        sim.now = L(2026, 11, 9, 21)
        sim.cycle()
        sim.charge("smartevse_1")
        sim.advance(1800)
        delivered = sim.model.delivered_kwh("smartevse_1")
        self.assertAlmostEqual(delivered, 5.1, delta=0.05)
        soc, source = sim.model.soc_estimate("smartevse_1", sim.now, kwh_per_pct=0.754, car_limit=90)
        self.assertEqual(source, "estimated")
        self.assertAlmostEqual(soc, 53.0 + delivered / 0.754, delta=0.1)
        # The charger stops (e.g. the car reached its minimum) while the reading is still
        # stalled: the estimate must not fall back to the old 53 % (Off/Smart flapping).
        sim.stop("smartevse_1")
        sim.advance(600)
        after, source = sim.model.soc_estimate("smartevse_1", sim.now, kwh_per_pct=0.754, car_limit=90)
        self.assertEqual(source, "estimated")
        self.assertAlmostEqual(after, soc, delta=0.2)


class BudgetTests(unittest.TestCase):
    def ledger(self, records):
        history = {k: [] for k in cm.OUTLETS}
        for i, (outlet, vehicle, energy, extra) in enumerate(records):
            record = {"id": f"r{i:02d}", "charger": outlet, "vehicle": vehicle,
                      "started_at": (L(2026, 10, 20) + timedelta(days=i)).isoformat(),
                      "ended_at": None, "status": "finished", "cost": energy * 0.3,
                      "currency": "EUR", "energy_kwh": energy, "priced_kwh": energy,
                      "unpriced_kwh": 0.0, "unobserved_seconds": 0.0, "issues": [],
                      "method": "power_integration"}
            record.update(extra)
            history[outlet].insert(0, record)
        return {"version": 1, "chargers": {k: {"session": h[0] if h else None, "history": h}
                                           for k, h in history.items()}}

    def test_polluted_history_still_plans_at_least_30_kwh(self):
        energies = [53.49, 51.04, 3.36, 0.05, 2.1, 4.0, 51.0, 0.04, 3.1, 2.9]
        model = cm.ChargeModel(vehicle_names=NAMES)
        model.seed_from_ledger(self.ledger([("smartevse_2", "Volvo EX30", e, {}) for e in energies]))
        clean = [item[1] for item in model.to_dict()["budget"]["vehicle_2"]]
        self.assertEqual(sorted(clean), [51.0, 51.04, 53.49])
        budget = model.budget_kwh("vehicle_2")
        self.assertEqual(budget, cm.Estimate(round(cm._quantile(clean, 0.8), 3), "learned"))
        self.assertGreater(budget.value, 43.1)  # EX30 20 -> 80 % needs 43.1 kWh

    def test_dirty_records_are_excluded(self):
        records = [
            ("smartevse_1", "Volvo XC40", 28.0, {"issues": ["start_not_observed"]}),
            ("smartevse_1", "Volvo XC40", 29.0, {"issues": ["restart_or_telemetry_gap"]}),
            ("smartevse_1", "Volvo XC40", 30.0, {"unobserved_seconds": 121.0}),
            ("smartevse_1", "Volvo XC40", 31.0, {"status": "charging"}),
            ("smartevse_1", "unknown", 32.0, {}),
            ("smartevse_1", "Volvo XC40", 4.9, {}),
            ("smartevse_1", "Volvo XC40", 33.0, {"issues": ["shared_meter_ambiguous"], "unobserved_seconds": 10.0}),
            ("smartevse_1", "volvo xc40 ", 34.0, {}),
        ]
        model = cm.ChargeModel(vehicle_names=NAMES)
        self.assertEqual(model.seed_from_ledger(self.ledger(records)), 2)
        self.assertEqual([i[1] for i in model.to_dict()["budget"]["vehicle_1"]], [33.0, 34.0])
        self.assertTrue(model.dirty)

    def test_unidentified_vehicle_uses_max_of_both_and_default_30(self):
        model = cm.ChargeModel(vehicle_names=NAMES)
        self.assertEqual(model.budget_kwh(None), cm.Estimate(30.0, "default"))
        model.seed_from_ledger(self.ledger([("smartevse_1", "Volvo XC40", e, {}) for e in (26.0, 28.0, 31.4)]
                                           + [("smartevse_2", "Volvo EX30", e, {}) for e in (51.0, 53.5)]))
        self.assertEqual(model.budget_kwh("vehicle_1"), cm.Estimate(30.04, "learned"))  # P80
        self.assertEqual(model.budget_kwh(None), model.budget_kwh("vehicle_2"))
        self.assertEqual(model.budget_kwh(None).value, 53.0)  # P80 of 51.0 / 53.5

    def test_keeps_last_10_and_dedupes_on_reseed(self):
        records = [("smartevse_1", "Volvo XC40", 20.0 + i, {}) for i in range(12)]
        model = cm.ChargeModel(vehicle_names=NAMES)
        model.seed_from_ledger(self.ledger(records))
        model.seed_from_ledger(self.ledger(records))
        again = cm.ChargeModel(model.to_dict(), vehicle_names=NAMES)
        self.assertEqual(again.seed_from_ledger(self.ledger(records)), 0)
        self.assertEqual([i[1] for i in again.to_dict()["budget"]["vehicle_1"]], [22.0 + i for i in range(10)])

    def test_remaining_shrinks_as_energy_is_metered_and_new_records_are_learned(self):
        sim = Sim(L(2026, 11, 9, 23), step=30)
        sim.plug("smartevse_2")  # unmapped: unidentified vehicle, SoC unknown
        sim.advance(60)
        self.assertEqual(sim.model.session_budget("smartevse_2", None),
                         cm.Budget(30.0, 30.0, 0.0, "default"))
        sim.charge("smartevse_2", kw=10.0)
        sim.advance(3600)
        budget = sim.model.session_budget("smartevse_2", None)
        self.assertAlmostEqual(budget.delivered_kwh, 10.0, delta=0.1)
        self.assertAlmostEqual(budget.remaining_kwh, 20.0, delta=0.1)
        sim.advance(3 * 3600)
        self.assertEqual(sim.model.session_budget("smartevse_2", None).remaining_kwh, 0.0)
        sim.outlets["smartevse_2"]["vehicle"] = "vehicle_2"  # later identified
        sim.advance(60)
        sim.stop("smartevse_2")
        sim.unplug("smartevse_2")
        events = sim.advance(60)
        self.assertIn("budget", events)
        self.assertAlmostEqual(sim.model.budget_kwh("vehicle_2").value, 40.0, delta=0.3)
        self.assertEqual(sim.model.session_budget("smartevse_2", "vehicle_2").delivered_kwh, 0.0)


class DepartureTests(unittest.TestCase):
    def overnight(self, sim, plug_local, unplug_local, *, vehicle="vehicle_1", outlet="smartevse_1",
                  clear_mapping=False, remap=False):
        sim.now = plug_local
        sim.cycle()
        sim.plug(outlet, None)
        sim.advance(30)
        sim.outlets[outlet]["vehicle"] = vehicle
        sim.advance(60)
        if remap:
            sim.outlets[outlet]["vehicle"] = "vehicle_2" if vehicle == "vehicle_1" else "vehicle_1"
            sim.advance(30)
        sim.now = unplug_local - timedelta(seconds=60)
        if clear_mapping:
            sim.outlets[outlet]["vehicle"] = None  # Volvo "disconnected" cleared it first
        sim.cycle()
        sim.advance(30)
        sim.unplug(outlet)
        sim.advance(30)

    def confirm(self, sim, minutes=61):
        sim.now += timedelta(minutes=minutes)
        return sim.cycle()

    def test_commit_only_after_60_minutes_without_reconnect(self):
        sim = Sim(L(2026, 11, 9, 17, 56))
        self.overnight(sim, L(2026, 11, 9, 17, 56), L(2026, 11, 10, 7, 40))
        self.assertEqual(sim.model.departure_samples("vehicle_1"), 0)
        sim.now += timedelta(minutes=30)
        sim.cycle()
        self.assertEqual(sim.model.departure_samples("vehicle_1"), 0)
        self.assertIn("departure", self.confirm(sim, 31))
        self.assertEqual(sim.model.to_dict()["departures"]["vehicle_1"]["weekday"], [7 * 60 + 40])

    def test_reconnect_rules(self):
        cases = {"same car again": ("smartevse_1", "vehicle_1", 0),
                 "same car on the other outlet": ("smartevse_2", "vehicle_1", 0),
                 "never identified": ("smartevse_2", None, 0),
                 "the other car arrives": ("smartevse_2", "vehicle_2", 1)}
        for name, (outlet, vehicle, expected) in cases.items():
            with self.subTest(name):
                sim = Sim(L(2026, 11, 9, 17, 56))
                self.overnight(sim, L(2026, 11, 9, 17, 56), L(2026, 11, 10, 7, 40))
                sim.now += timedelta(minutes=20)
                sim.cycle()
                sim.plug(outlet, None)
                sim.advance(60)
                sim.outlets[outlet]["vehicle"] = vehicle
                sim.advance(60)
                self.confirm(sim)
                self.assertEqual(sim.model.departure_samples("vehicle_1"), expected)

    def test_session_must_span_03_and_unplug_between_04_and_13(self):
        cases = [((L(2026, 11, 10, 3, 30), L(2026, 11, 10, 7, 30)), 0),   # plugged after 03:00
                 ((L(2026, 11, 9, 22), L(2026, 11, 10, 13, 30)), 0),      # daytime unplug
                 ((L(2026, 11, 9, 22), L(2026, 11, 10, 1, 10)), 0),       # car moved at night
                 ((L(2026, 11, 9, 22), L(2026, 11, 10, 4, 0)), 1),
                 ((L(2026, 11, 9, 22), L(2026, 11, 10, 13, 0)), 1)]
        for (plug, unplug), expected in cases:
            with self.subTest(plug=plug, unplug=unplug):
                sim = Sim(plug - timedelta(minutes=1))
                self.overnight(sim, plug, unplug)
                self.confirm(sim)
                self.assertEqual(sim.model.departure_samples("vehicle_1"), expected)

    def test_last_non_null_mapping_is_used_and_any_remap_drops(self):
        sim = Sim(L(2026, 11, 9, 21))
        self.overnight(sim, L(2026, 11, 9, 21), L(2026, 11, 10, 7, 35), clear_mapping=True)
        self.confirm(sim)
        self.assertEqual(sim.model.departure_samples("vehicle_1"), 1)
        sim = Sim(L(2026, 11, 9, 21))
        self.overnight(sim, L(2026, 11, 9, 21), L(2026, 11, 10, 7, 35), remap=True)
        self.confirm(sim)
        self.assertEqual(sim.model.departure_samples("vehicle_1") + sim.model.departure_samples("vehicle_2"), 0)

    def test_recorded_in_every_mode_and_discard_session(self):
        sim = Sim(L(2026, 11, 9, 21))
        sim.state, sim.reason = "idle", "waiting_for_schedule_window"
        self.overnight(sim, L(2026, 11, 9, 21), L(2026, 11, 10, 7, 35))
        self.confirm(sim)
        self.assertEqual(sim.model.departure_samples("vehicle_1"), 1)
        sim2 = Sim(L(2026, 11, 9, 21))
        self.overnight(sim2, L(2026, 11, 9, 21), L(2026, 11, 10, 7, 35))
        sim2.model.discard_session("smartevse_1")  # cross-check swap after the unplug
        self.confirm(sim2)
        self.assertEqual(sim2.model.departure_samples("vehicle_1"), 0)

    def test_api_blip_is_not_an_unplug(self):
        sim = Sim(L(2026, 11, 9, 21))
        sim.plug("smartevse_1", "vehicle_1")
        sim.advance(60)
        sim.now = L(2026, 11, 10, 7, 0)
        sim.outlets["smartevse_1"].update(available=False, connected=False)
        sim.cycle()
        sim.outlets["smartevse_1"].update(available=True, connected=True, vehicle="vehicle_1")
        sim.advance(60)
        self.confirm(sim, 120)
        self.assertEqual(sim.model.departure_samples("vehicle_1"), 0)
        self.assertIsNotNone(sim.model.session_info("smartevse_1"))

    def test_spring_forward_night_spans_the_missing_03(self):
        sim = Sim(L(2026, 3, 28, 22))
        self.overnight(sim, L(2026, 3, 28, 22), L(2026, 3, 29, 7, 30))
        self.confirm(sim)
        self.assertEqual(sim.model.to_dict()["departures"]["vehicle_1"]["weekend"], [450])

    def suggestion_model(self, weekday, weekend=()):
        minutes = lambda text: int(text[:2]) * 60 + int(text[3:])
        return cm.ChargeModel(stored_with(departures={
            "vehicle_1": {"weekday": [minutes(x) for x in weekday], "weekend": [minutes(x) for x in weekend]},
            "vehicle_2": {"weekday": [], "weekend": []}}))

    def test_suggestion_needs_8_after_dropping_outliers(self):
        clean = ["07:40", "07:35", "07:45", "07:50", "07:30", "07:40", "07:35", "07:45"]
        model = self.suggestion_model(clean + ["05:20"])
        # 05:20 is > 90 min from the median and dropped; P20 of the rest 07:35 -> -10 -> 07:15.
        self.assertEqual(model.departure_suggestion("vehicle_1", "06:00"),
                         {"suggested_ready_by": "07:15", "usually_leaves": "07:45",
                          "day_class": "weekday", "samples": 8})
        self.assertIsNone(self.suggestion_model(clean[:7] + ["05:20"]).departure_suggestion("vehicle_1"))
        self.assertIsNone(model.departure_suggestion("vehicle_2"))

    def test_one_suggestion_per_vehicle_the_earliest_day_class(self):
        model = self.suggestion_model(["07:40"] * 8, ["10:30"] * 8)
        self.assertEqual(model.suggested_ready_by("vehicle_1", "09:00"), "07:30")
        late = self.suggestion_model(["08:40"] * 8, ["06:10"] * 8)
        self.assertEqual(late.departure_suggestion("vehicle_1", "07:00")["day_class"], "weekend")
        self.assertEqual(late.suggested_ready_by("vehicle_1", "07:00"), "06:00")

    def test_hidden_within_15_minutes_of_the_current_ready_by(self):
        model = self.suggestion_model(["07:40"] * 8)  # suggests 07:30
        for ready_by in ("07:30", "07:15", "07:45:00", time(7, 30)):
            with self.subTest(ready_by=ready_by):
                self.assertIsNone(model.suggested_ready_by("vehicle_1", ready_by))
        for ready_by in ("07:00", "08:00", None, "garbage"):
            with self.subTest(ready_by=ready_by):
                self.assertEqual(model.suggested_ready_by("vehicle_1", ready_by), "07:30")

    def test_raw_minutes_never_leave_the_private_state(self):
        model = self.suggestion_model(["07:41", "07:37", "07:44", "07:52", "07:33", "07:47", "07:38", "07:46"])
        suggestion = model.departure_suggestion("vehicle_1", "05:00")
        for value in (suggestion["suggested_ready_by"], suggestion["usually_leaves"]):
            self.assertEqual(int(value[3:]) % 15, 0)
        summary = model.learning_summary()
        self.assertEqual(summary["departure_samples"], {"vehicle_1": 8, "vehicle_2": 0})
        self.assertNotIn("07:41", json.dumps(summary))
        self.assertNotIn(461, json.loads(json.dumps(summary)).values())


class PriceForecastTests(unittest.TestCase):
    def observe(self, model, now, entries):
        return model.observe_cycle(now, TZ, outlets={}, prices=entries, price_currency="EUR")

    def test_ingest_dedupes_by_start(self):
        model = cm.ChargeModel()
        events = self.observe(model, L(2026, 11, 9, 18, 30), known_entries())
        self.assertIn("price_forecast", events)
        self.assertIn("price_list", events)
        first = model.to_dict()["prices"]
        self.assertEqual(sum(len(v) for dc in first.values() for v in dc.values()), len(KNOWN))
        again = self.observe(model, L(2026, 11, 9, 18, 31), known_entries())
        self.assertEqual(again, frozenset())
        self.assertEqual(model.to_dict()["prices"], first)
        reloaded = cm.ChargeModel(model.to_dict())
        self.assertNotIn("price_forecast", self.observe(reloaded, L(2026, 11, 9, 19), known_entries()))

    def test_persistence_fallback_then_median_after_three_days(self):
        model = cm.ChargeModel()
        self.observe(model, L(2026, 11, 9, 18, 30), known_entries())
        # 11-11 03:00 (Wednesday) is unpublished: same local hour of the most recent known day.
        self.assertEqual(model.price_forecast(L(2026, 11, 11, 3), TZ), (0.2635, "forecast"))
        self.assertEqual(model.price_forecast(L(2026, 11, 12, 18), TZ), (0.6500, "forecast"))
        # Three more weekday nights at 03:00 -> median of the four bucket values.
        for day, price in ((3, 0.21), (4, 0.33), (5, 0.25)):
            self.observe(model, L(2026, 11, day, 12),
                         [rep_entry(datetime(2026, 11, day, 3, tzinfo=TZ), price)])
        self.assertEqual(model.price_forecast(L(2026, 11, 11, 3), TZ),
                         (round((0.25 + 0.2635) / 2, 6), "forecast"))
        # The weekend bucket is separate (and empty): persistence from the latest 03:00.
        self.assertEqual(model.price_forecast(L(2026, 11, 14, 3), TZ), (0.2635, "forecast"))

    def test_bucket_keeps_last_14_and_negative_prices(self):
        model = cm.ChargeModel()
        start = datetime(2026, 9, 1, 13, tzinfo=TZ)
        entries = [rep_entry(start + timedelta(days=i), -0.01 * i) for i in range(30)]
        self.observe(model, L(2026, 10, 15), entries)
        buckets = model.to_dict()["prices"]
        values = [p for dc in buckets.values() for p in [x[1] for x in dc.get("13", [])]]
        self.assertEqual(len(buckets["weekday"]["13"]), 14)
        self.assertLess(min(values), 0)

    def test_price_slots_known_then_forecast_then_unknown(self):
        model = cm.ChargeModel()
        now = L(2026, 11, 9, 18, 20)
        slots = model.price_slots(now, L(2026, 11, 10, 2), TZ)
        self.assertEqual({s["kind"] for s in slots}, {"unknown"})
        self.observe(model, now, known_entries())
        slots = model.price_slots(now, L(2026, 11, 11, 7), TZ)
        self.assertEqual(slots[0]["start"], L(2026, 11, 9, 18))
        self.assertEqual(slots[0]["price"], 0.4434)
        kinds = [s["kind"] for s in slots]
        self.assertEqual(kinds.count("known"), len(KNOWN))
        self.assertEqual(kinds[len(KNOWN):], ["forecast"] * 6)
        self.assertEqual(slots[len(KNOWN)]["start"], L(2026, 11, 11, 1))
        self.assertEqual(slots[len(KNOWN)]["price"], 0.2994)
        for left, right in zip(slots, slots[1:]):
            self.assertEqual(left["end"], right["start"])

    def test_last_good_list_is_used_while_live_lists_are_missing(self):
        model = cm.ChargeModel()
        self.observe(model, L(2026, 11, 9, 18, 20), known_entries())
        fetched, currency, entries = model.last_good_prices()
        self.assertEqual((fetched, currency, len(entries)), (L(2026, 11, 9, 18, 20), "EUR", len(KNOWN)))
        events = self.observe(model, L(2026, 11, 9, 20, 20), [])  # REP unavailable
        self.assertNotIn("price_list", events)
        reloaded = cm.ChargeModel(json.loads(json.dumps(model.to_dict())))
        slots = reloaded.price_slots(L(2026, 11, 9, 20, 20), L(2026, 11, 10, 7), TZ)
        self.assertEqual([s["kind"] for s in slots], ["known"] * 11)
        self.assertEqual(slots[1]["price"], 0.2418)
        # Placeholder lists (null prices) never replace the last good list.
        placeholders = [rep_entry(datetime(2026, 11, 10, h, tzinfo=TZ), None) for h in range(1, 24)]
        self.assertNotIn("price_list", self.observe(model, L(2026, 11, 9, 20, 30), placeholders))
        self.assertEqual(model.last_good_prices()[0], L(2026, 11, 9, 18, 20))

    def test_normalize_price_entries(self):
        tz2 = "+02:00"
        entries = [
            {"start_time": f"2026-11-10T01:00:00{tz2}", "end_time": f"2026-11-10T02:00:00{tz2}", "actual_price": None},
            {"start": L(2026, 11, 10, 1), "end": L(2026, 11, 10, 2), "price": 299.4},
            {"start_time": "2026-11-10T02:00:00", "end_time": "2026-11-10T03:00:00", "actual_price": 1},  # naive
            {"start_time": f"2026-11-10T03:00:00{tz2}", "end_time": f"2026-11-10T03:00:00{tz2}", "actual_price": 1},
            "garbage", {"start": "x"},
            {"start_time": f"2026-11-10T04:00:00{tz2}", "end_time": f"2026-11-10T05:00:00{tz2}",
             "actual_price": "0.2983", "is_weekend": False, "is_holiday": True},
        ]
        result = cm.normalize_price_entries(entries, factor=0.001)
        self.assertEqual([(e["start"], e["price"], e["off_day"]) for e in result],
                         [(L(2026, 11, 10, 1), 0.2994, None),
                          (L(2026, 11, 10, 4), 0.2983 * 0.001, True)])
        self.assertEqual(result[0]["start"].tzinfo, UTC)

    def test_forecast_buckets_across_dst(self):
        model = cm.ChargeModel()
        start = L(2026, 10, 25, 0)  # 25-hour Sunday
        entries = [{"start": start + timedelta(hours=i), "end": start + timedelta(hours=i + 1),
                    "price": 0.1 + i / 100} for i in range(25)]
        self.observe(model, L(2026, 10, 25, 12), entries)
        weekend = model.to_dict()["prices"]["weekend"]
        self.assertEqual(len(weekend["3"]), 2)
        self.assertEqual(sum(len(v) for v in weekend.values()), 25)


class RealisedBaselineStartTests(unittest.TestCase):
    """Review round 3: every window's baseline starts where its snapshot can price it."""

    def test_window_opened_by_a_deadline_roll_gets_a_priced_baseline(self):
        # Ready by 01:00, still below the minimum: catch-up after the roll until 03:00.
        calls = []
        sim = Sim(L(2026, 11, 9, 20, 59), prices=known_entries(), baseline=planner_baseline(calls))
        sim.plug("smartevse_1", "vehicle_1")
        sim.outlets["smartevse_1"]["deadline"] = L(2026, 11, 10, 1)
        sim.advance(60)
        sim.reason = "intelligent_charging"
        sim.charge("smartevse_1")
        sim.advance((L(2026, 11, 10, 1) - sim.now).total_seconds())
        sim.outlets["smartevse_1"]["deadline"] = L(2026, 11, 11, 1)  # the roll
        sim.reason = "intelligent_catch_up"
        sim.advance(2 * 3600)
        sim.stop("smartevse_1")
        sim.unplug("smartevse_1")
        sim.advance(60)
        sessions = sim.model.realised_sessions(TZ)
        self.assertEqual(len(sessions), 2)
        for session in sessions:
            self.assertIsNotNone(session["baseline_cost"], session)
            self.assertIsNotNone(session["saving"], session)
        records = sorted(sim.model.to_dict()["realised"], key=lambda r: r["start"])
        self.assertEqual(records[1].get("baseline_start"), records[1]["start"])  # the roll, not 21:00

    def test_intelligent_switched_on_after_the_plug_in_prices_from_the_plug_in(self):
        sim = Sim(L(2026, 11, 9, 18, 9), prices=known_entries(), baseline=planner_baseline())
        sim.state, sim.reason = "schedule", "waiting_for_schedule_window"
        sim.plug("smartevse_1", "vehicle_1")
        sim.outlets["smartevse_1"]["deadline"] = L(2026, 11, 10, 7)
        sim.advance((L(2026, 11, 9, 19, 30) - sim.now).total_seconds())
        sim.state, sim.reason = "intelligent", "intelligent_waiting_for_planned_slot"
        sim.advance(60)
        [record] = sim.model.to_dict()["realised"]
        self.assertEqual(record["snapshot"]["t0"], int(L(2026, 11, 9, 18).timestamp() // 60))
        self.assertEqual(record.get("baseline_start"), L(2026, 11, 9, 18, 9, 30).isoformat())
        sim.advance((L(2026, 11, 10, 2) - sim.now).total_seconds())
        sim.reason = "intelligent_charging"
        sim.charge("smartevse_1")
        sim.advance(3 * 3600)
        sim.stop("smartevse_1")
        sim.unplug("smartevse_1")
        sim.advance(60)
        [session] = sim.model.realised_sessions(TZ)
        self.assertIsNotNone(session["saving"])
        self.assertGreater(session["saving"], 0)

    def test_known_price_of_a_later_snapshot_wins_over_an_earlier_forecast(self):
        def record(rid, start, kinds, prices, energy):
            return {"id": rid, "outlet": "smartevse_1" if rid == "a" else "smartevse_2", "vehicle": None,
                    "plugged_at": start, "start": start, "baseline_start": start,
                    "snapshot": {"t0": int(datetime.fromisoformat(start).timestamp() // 60) // 60 * 60,
                                 "prices": prices, "kinds": kinds, "kw": [10.0] * len(prices)},
                    "intelligent": {"energy_kwh": energy, "cost": 0.0, "priced_kwh": energy}}
        start = L(2026, 11, 9, 22)
        a = record("a", start.isoformat(), "kff", [0.3, 0.2, 0.2], 5.0)
        b = record("b", (start + timedelta(hours=1)).isoformat(), "kk", [0.25, 0.25], 15.0)
        results = cm.ChargeModel()._call_baseline([a, b], planner_baseline())
        self.assertIsNotNone(results["a"])
        self.assertIsNotNone(results["b"])  # 23:00-00:30 known in b's own snapshot

    def test_hourly_snapshot_price_is_the_time_weighted_mean_of_quarters(self):
        start = datetime(2026, 11, 9, 18, tzinfo=TZ)
        entries = []
        for hour in range(14):
            for quarter, price in enumerate((0.20, 0.30, 0.40, 0.50)):
                at = start + timedelta(hours=hour, minutes=15 * quarter)
                entries.append({"start": at, "end": at + timedelta(minutes=15), "price": price})
        model = cm.ChargeModel(vehicle_names=NAMES)
        model.observe_cycle(start.astimezone(UTC) + timedelta(seconds=30), TZ,
                            outlets={"smartevse_1": {"available": True, "connected": True, "vehicle": "vehicle_1",
                                                     "deadline": datetime(2026, 11, 10, 7, tzinfo=TZ)}},
                            prices=entries, controller_state="intelligent",
                            charge_reason="intelligent_waiting_for_planned_slot", ledger={"version": 1, "chargers": {}})
        [record] = model.to_dict()["realised"]
        self.assertEqual(record["snapshot"]["prices"][:2], [0.35, 0.35])
        self.assertEqual(record["snapshot"]["kinds"][:2], "kk")

    def test_stored_times_out_of_range_drop_only_that_record(self):
        sim = Sim(L(2026, 11, 9, 18), prices=known_entries())
        sim.plug("smartevse_1", "vehicle_1")
        sim.advance(60)
        state = sim.model.to_dict()
        bad = deepcopy(state["realised"][0])
        bad["id"], bad["outlet"], bad["start"] = "far", "smartevse_2", "9999-12-31T23:00:00+00:00"
        state["realised"].append(bad)
        model = cm.ChargeModel(state)
        self.assertEqual(model.load_issues, ("realised.1",))
        self.assertEqual(len(model.realised_sessions(TZ)), 1)


class RealisedSavingTests(unittest.TestCase):
    def night(self, sim, *, plug=L(2026, 11, 9, 18), blocks=((21, 22),), unplug=L(2026, 11, 10, 7, 30),
              outlet="smartevse_1", vehicle="vehicle_1", during=None, reached_at=None):
        """Plug at 18:00, charge in planned local-hour blocks, unplug in the morning."""
        sim.now = plug
        sim.cycle()
        sim.plug(outlet, vehicle)
        sim.outlets[outlet]["deadline"] = L(2026, 11, 10, 7)
        sim.advance(60)
        for start_hour, end_hour in blocks:
            target = L(2026, 11, 9 if start_hour >= 12 else 10, start_hour)
            sim.advance((target - sim.now).total_seconds())
            sim.reason = "intelligent_charging"
            sim.charge(outlet)
            end = L(2026, 11, 9 if end_hour >= 12 else 10, end_hour)
            sim.advance((end - sim.now).total_seconds(), during)
            sim.stop(outlet)
            sim.reason = "intelligent_waiting_for_planned_slot"
        if reached_at is not None:
            sim.outlets[outlet]["reached"] = True
        sim.advance((unplug - sim.now).total_seconds())
        sim.unplug(outlet)
        sim.advance(60)

    def test_window_opens_at_plug_in_and_closes_at_unplug(self):
        calls = []
        sim = Sim(L(2026, 11, 9, 17, 59, 30), prices=known_entries(), baseline=fake_baseline(calls))
        sim.price = 0.2418  # the 21:00 hour
        self.night(sim)
        [record] = sim.model.to_dict()["realised"]
        self.assertEqual(record["start"], L(2026, 11, 9, 18, 0, 30).isoformat())  # first connected cycle
        self.assertIsNotNone(record["closed_at"])
        [session] = sim.model.realised_sessions(TZ)
        self.assertEqual((session["closed"], session["mixed"], session["partial"]), (True, False, False))
        self.assertEqual(session["vehicle_name"], "Volvo XC40")
        self.assertEqual(session["date"], "2026-11-09")
        self.assertAlmostEqual(session["energy_kwh"], 10.2, delta=0.05)
        self.assertAlmostEqual(session["cost"], 10.2 * 0.2418, delta=0.02)
        # Baseline = charging right at plug-in (18:00, 0.4434) at the snapshot's expected kW.
        self.assertEqual(len(calls[0]), 1)
        self.assertAlmostEqual(calls[0][0]["energy_kwh"], 10.2, delta=0.05)
        energy = calls[0][0]["energy_kwh"]
        expected_kw = cm.ChargeModel().hour_power(L(2026, 11, 9, 18), TZ, vehicle="vehicle_1",
                                                 include_car_cap=True).expected_kw
        self.assertEqual(sim.model.to_dict()["realised"][0]["snapshot"]["kw"][0], expected_kw)
        hour1 = min(energy, expected_kw * 59.5 / 60)  # the window starts at 18:00:30
        baseline = hour1 * 0.4434 + (energy - hour1) * 0.3909
        self.assertEqual(session["baseline_cost"], round(baseline, 2))
        self.assertEqual(session["saving"], round(round(baseline, 2) - sim.model.to_dict()["realised"][0]["intelligent"]["cost"], 2))
        self.assertGreater(session["saving"], 1.5)
        self.assertTrue(sim.model.to_dict()["realised"][0]["final"])
        # Final values are not recomputed.
        sim.model.realised_sessions(TZ)
        self.assertEqual(len(calls), 1)

    def test_force_energy_makes_the_window_mixed(self):
        def force(s):
            if s.now >= L(2026, 11, 9, 21, 30):
                s.state, s.reason = "force", "force_charge"
        sim = Sim(L(2026, 11, 9, 17, 59, 30), prices=known_entries())
        self.night(sim, during=force)
        [session] = sim.model.realised_sessions(TZ)
        self.assertTrue(session["mixed"])
        self.assertIsNone(session["saving"])
        self.assertAlmostEqual(session["energy_kwh"], 10.2, delta=0.05)

    def test_intelligent_switched_on_later_excludes_earlier_schedule_energy(self):
        sim = Sim(L(2026, 11, 9, 17, 59, 30), prices=known_entries())
        sim.state, sim.reason = "schedule", "schedule_window_active"
        sim.plug("smartevse_1", "vehicle_1")
        sim.charge("smartevse_1")
        sim.advance(1800)
        self.assertEqual(sim.model.to_dict()["realised"], [])
        sim.state, sim.reason = "intelligent", "intelligent_charging"
        sim.advance(1800)
        sim.unplug("smartevse_1")
        sim.advance(60)
        [session] = sim.model.realised_sessions(TZ)
        self.assertFalse(session["mixed"])
        self.assertAlmostEqual(session["energy_kwh"], 5.1, delta=0.1)

    def test_unpriced_energy_or_unobserved_time_is_partial(self):
        def no_price(s):
            s.price = None if s.now < L(2026, 11, 9, 21, 10) else 0.2418
        calls = []
        sim = Sim(L(2026, 11, 9, 17, 59, 30), prices=known_entries(), baseline=fake_baseline(calls))
        self.night(sim, during=no_price)
        [session] = sim.model.realised_sessions(TZ)
        self.assertTrue(session["partial"])
        self.assertIsNone(session["saving"])
        # The baseline is priced on priced_kwh only, never on unpriced energy.
        record = sim.model.to_dict()["realised"][0]
        self.assertAlmostEqual(calls[0][0]["energy_kwh"], record["intelligent"]["priced_kwh"], places=3)
        self.assertAlmostEqual(record["unpriced_kwh"], 10.2 * 9.5 / 60, delta=0.01)  # 21:00:30-21:10
        self.assertAlmostEqual(calls[0][0]["energy_kwh"],
                               session["energy_kwh"] - record["unpriced_kwh"], delta=0.01)

        def restart(s):
            if not s.__dict__.get("restarted") and s.now >= L(2026, 11, 9, 21, 30):
                s.restarted = True
                s.jump(300)
        sim = Sim(L(2026, 11, 9, 17, 59, 30), prices=known_entries())
        self.night(sim, during=restart)
        [session] = sim.model.realised_sessions(TZ)
        self.assertTrue(session["partial"])

    def test_handover_overlaps_up_to_10_s_are_tolerated(self):
        sim = Sim(L(2026, 11, 9, 20, 59), prices=known_entries(), step=10)
        sim.plug("smartevse_1", "vehicle_1")
        sim.plug("smartevse_2", "vehicle_2")
        sim.advance(60)
        for _ in range(13):  # 13 hand-offs, each with one 10 s cycle where both charge
            sim.charge("smartevse_1")
            sim.advance(300)
            sim.charge("smartevse_2")
            sim.advance(10)
            sim.stop("smartevse_1")
            sim.advance(300)
            sim.stop("smartevse_2")
        sim.unplug("smartevse_1")
        sim.unplug("smartevse_2")
        sim.advance(30)
        unobserved = sim.ledger.snapshot()["chargers"]["smartevse_1"]["session"]["unobserved_seconds"]
        self.assertGreater(unobserved, 120)
        for session in sim.model.realised_sessions(TZ):
            self.assertFalse(session["partial"], session)
            self.assertIsNotNone(session["saving"])

    def test_deadline_roll_closes_the_window_and_opens_the_next(self):
        sim = Sim(L(2026, 11, 13, 17, 59), step=60)  # Friday evening -> Monday morning
        sim.plug("smartevse_1", "vehicle_1")
        for day in (14, 15, 16):
            sim.outlets["smartevse_1"]["deadline"] = L(2026, 11, day, 7)
            sim.advance((L(2026, 11, day, 1) - sim.now).total_seconds())
            sim.charge("smartevse_1")
            sim.advance(1800)
            sim.stop("smartevse_1")
            sim.advance((L(2026, 11, day, 7) - sim.now).total_seconds())
        sim.outlets["smartevse_1"]["deadline"] = L(2026, 11, 17, 7)
        sim.advance(120)
        records = sim.model.to_dict()["realised"]
        self.assertEqual(len(records), 4)
        self.assertEqual([r["closed_at"] is None for r in records], [False, False, False, True])
        sessions = sim.model.realised_sessions(TZ)
        self.assertEqual([s["date"] for s in sessions], ["2026-11-16", "2026-11-15", "2026-11-14", "2026-11-13"])
        for session in sessions[1:]:
            self.assertAlmostEqual(session["energy_kwh"], 5.1, delta=0.1)
        self.assertEqual(records[1]["start"], L(2026, 11, 14, 7, 1).isoformat())

    def test_ready_by_edit_before_the_deadline_is_not_a_roll(self):
        sim = Sim(L(2026, 11, 9, 17, 59, 30), prices=known_entries())
        sim.plug("smartevse_1", "vehicle_1")
        sim.outlets["smartevse_1"]["deadline"] = L(2026, 11, 10, 7)
        sim.advance(60)
        sim.outlets["smartevse_1"]["deadline"] = L(2026, 11, 10, 6, 30)
        sim.advance(60)
        [record] = sim.model.to_dict()["realised"]
        self.assertIsNone(record["closed_at"])
        self.assertEqual(record["deadline"], L(2026, 11, 10, 6, 30).isoformat())

    def test_top_up_energy_is_excluded_from_the_saving(self):
        calls = []

        def top_up(s):
            if s.now >= L(2026, 11, 9, 21, 30):
                s.reason = "intelligent_top_up"
        sim = Sim(L(2026, 11, 9, 17, 59, 30), prices=known_entries(), baseline=fake_baseline(calls))
        self.night(sim, during=top_up)
        [session] = sim.model.realised_sessions(TZ)
        self.assertAlmostEqual(session["top_up_kwh"], 5.1, delta=0.1)
        self.assertAlmostEqual(session["energy_kwh"], 10.2, delta=0.1)
        self.assertAlmostEqual(calls[0][0]["energy_kwh"], 5.1, delta=0.1)
        record = sim.model.to_dict()["realised"][0]
        self.assertEqual(session["saving"], round(session["baseline_cost"] - record["intelligent"]["cost"], 2))

    def test_two_cars_get_a_sequential_baseline_finalised_in_plug_order(self):
        calls = []
        sim = Sim(L(2026, 11, 9, 17, 59, 30), prices=known_entries(), baseline=fake_baseline(calls), step=60)
        sim.plug("smartevse_1", "vehicle_1")              # XC40 first
        sim.advance(600)
        sim.plug("smartevse_2", "vehicle_2")              # EX30 ten minutes later
        sim.advance((L(2026, 11, 10, 1) - sim.now).total_seconds())
        sim.charge("smartevse_2")
        sim.advance(3600)
        sim.stop("smartevse_2")
        sim.unplug("smartevse_2")                         # the later car leaves first
        sim.advance(60)
        sessions = {s["outlet"]: s for s in sim.model.realised_sessions(TZ)}
        # Provisional only: its baseline queues behind the XC40, which is still plugged in.
        self.assertTrue(sessions["smartevse_2"]["closed"])
        self.assertFalse(sim.model.to_dict()["realised"][1]["final"])
        sim.charge("smartevse_1")
        sim.advance(3600)
        sim.stop("smartevse_1")
        sim.unplug("smartevse_1")
        sim.advance(60)
        sessions = {s["outlet"]: s for s in sim.model.realised_sessions(TZ)}
        self.assertEqual([s["outlet"] for s in calls[-1]], ["smartevse_1", "smartevse_2"])
        # The EX30's baseline starts after the XC40's simulated plug-in charge.
        xc40_hours = calls[-1][0]["energy_kwh"] / cm.ChargeModel().hour_power(
            L(2026, 11, 9, 18), TZ, vehicle="vehicle_1", include_car_cap=True).expected_kw
        self.assertGreater(xc40_hours, 1.0)
        self.assertTrue(all(r["final"] for r in sim.model.to_dict()["realised"]))
        self.assertIsNotNone(sessions["smartevse_2"]["saving"])
        self.assertIsNotNone(sessions["smartevse_1"]["saving"])

    def test_provisional_saving_once_the_minimum_is_reached(self):
        sim = Sim(L(2026, 11, 9, 17, 59, 30), prices=known_entries())
        sim.plug("smartevse_1", "vehicle_1")
        sim.advance((L(2026, 11, 9, 21) - sim.now).total_seconds())
        sim.charge("smartevse_1")
        sim.advance(3600)
        sim.stop("smartevse_1")
        sim.advance(60)
        [session] = sim.model.realised_sessions(TZ)
        self.assertEqual((session["closed"], session["saving"], session["baseline_cost"]), (False, None, None))
        sim.outlets["smartevse_1"]["reached"] = True
        sim.advance(60)
        [session] = sim.model.realised_sessions(TZ)
        self.assertFalse(session["closed"])
        self.assertIsNotNone(session["saving"])
        self.assertFalse(sim.model.to_dict()["realised"][0]["final"])

    def test_keeps_last_30_closed_windows(self):
        sim = Sim(L(2026, 10, 1, 17, 59), step=120)
        for day in range(33):
            sim.now = L(2026, 10, 1, 18) + timedelta(days=day)
            sim.cycle()
            sim.plug("smartevse_1", "vehicle_1")
            sim.advance(240)
            sim.unplug("smartevse_1")
            sim.advance(120)
        records = sim.model.to_dict()["realised"]
        self.assertEqual(len(records), 30)
        self.assertEqual(records[0]["start"][:10], "2026-10-04")

    def test_baseline_failure_only_hides_the_saving(self):
        def broken(*_):
            raise RuntimeError("planner bug")
        sim = Sim(L(2026, 11, 9, 17, 59, 30), prices=known_entries(), baseline=broken)
        self.night(sim)
        [session] = sim.model.realised_sessions(TZ)
        self.assertIsNone(session["saving"])
        self.assertIn("planner bug", sim.model.last_error)
        self.assertFalse(sim.model.to_dict()["realised"][0]["final"])
        [session] = sim.model.realised_sessions(TZ, baseline=fake_baseline())
        self.assertIsNotNone(session["saving"])

    def test_open_window_survives_a_restart(self):
        sim = Sim(L(2026, 11, 9, 17, 59, 30), prices=known_entries())
        sim.plug("smartevse_1", "vehicle_1")
        sim.advance((L(2026, 11, 9, 21) - sim.now).total_seconds())
        sim.reason = "intelligent_charging"
        sim.charge("smartevse_1")
        sim.advance(1800)
        stored = json.loads(json.dumps(sim.model.to_dict()))
        sim.model = cm.ChargeModel(stored, vehicle_names=NAMES, baseline=fake_baseline())
        self.assertEqual(sim.model.load_issues, ())
        sim.jump(200)  # HA restart: the ledger books the gap as unobserved
        sim.advance(1800)
        sim.stop("smartevse_1")
        sim.unplug("smartevse_1")
        sim.advance(60)
        [session] = sim.model.realised_sessions(TZ)
        self.assertAlmostEqual(session["energy_kwh"], 10.2, delta=0.1)
        self.assertTrue(session["closed"])
        self.assertTrue(session["partial"])
        self.assertFalse(session["mixed"])

    def test_window_closes_when_the_car_left_while_home_assistant_was_down(self):
        sim = Sim(L(2026, 11, 9, 17, 59, 30), prices=known_entries())
        sim.plug("smartevse_1", "vehicle_1")
        sim.advance(120)
        stored = json.loads(json.dumps(sim.model.to_dict()))
        sim.model = cm.ChargeModel(stored, vehicle_names=NAMES, baseline=fake_baseline())
        sim.unplug("smartevse_1")
        sim.jump(3600)
        events = sim.cycle()
        self.assertIn("realised", events)
        [session] = sim.model.realised_sessions(TZ)
        self.assertTrue(session["closed"])
        self.assertIsNone(session["saving"])  # nothing was charged: no saving claim

    def test_no_window_outside_intelligent(self):
        sim = Sim(L(2026, 11, 9, 17, 59, 30), prices=known_entries())
        sim.state = "idle"
        self.night(sim)
        self.assertEqual(sim.model.realised_sessions(TZ), [])


class PersistenceTests(unittest.TestCase):
    def test_commits_mark_dirty_and_saves_are_throttled_to_5_minutes(self):
        model = cm.ChargeModel()
        now = L(2026, 11, 9, 18, 20)
        self.assertFalse(model.should_save(now))
        model.observe_cycle(now, TZ, outlets={}, prices=known_entries())
        self.assertTrue(model.dirty)
        self.assertTrue(model.should_save(now))
        model.mark_saved(now)
        self.assertFalse(model.dirty)
        model.observe_cycle(now + timedelta(minutes=1), TZ, outlets={},
                            prices=known_entries()[:-1] + [rep_entry(datetime(2026, 11, 11, 0, tzinfo=TZ), 0.31)])
        self.assertTrue(model.dirty)
        self.assertFalse(model.should_save(now + timedelta(minutes=4)))
        self.assertTrue(model.should_save(now + timedelta(minutes=5)))
        model.mark_save_failed(now + timedelta(minutes=5))
        self.assertTrue(model.dirty)
        self.assertFalse(model.should_save(now + timedelta(minutes=9)))
        self.assertTrue(model.should_save(now + timedelta(minutes=10)))

    def test_every_cycle_charging_still_saves_within_5_minutes(self):
        """Regression: a debounced delay_save never wrote while cycles kept arriving."""
        sim = Sim(L(2026, 11, 9, 20, 59), prices=known_entries(), step=10)
        sim.plug("smartevse_1", "vehicle_1")
        sim.charge("smartevse_1")
        saves = []

        def adapter(s):
            if s.model.should_save(s.now):
                saves.append(s.now)
                s.model.mark_saved(s.now)
        sim.cycle()
        adapter(sim)
        for _ in range(40):
            sim.advance(10)
            adapter(sim)
        self.assertGreaterEqual(len(saves), 1)
        sim.advance(400, adapter)
        self.assertGreaterEqual(len(saves), 2)
        gaps = [(b - a).total_seconds() for a, b in zip(saves, saves[1:])]
        self.assertTrue(all(gap >= 300 for gap in gaps), gaps)

    def test_transient_accumulators_are_never_persisted(self):
        sim = Sim(L(2026, 11, 9, 21, 0, 5), step=10)
        sim.plug("smartevse_1", "vehicle_1")
        sim.add_car("vehicle_1", 40.0, outlet="smartevse_1", kpp=0.754)
        sim.state = "idle"
        sim.charge("smartevse_1")
        sim.advance(9 * 60)  # inside one hour; one discarded power window; kWh segment open
        self.assertFalse(sim.model.dirty)
        self.assertEqual(sim.model.to_dict(), cm.default_state())

    def test_a_failing_step_is_isolated(self):
        model = cm.ChargeModel()

        def boom(*_args, **_kwargs):
            raise ZeroDivisionError("bad")
        model._observe_prices = boom
        t = L(2026, 11, 10, 2)
        for _ in range(61):
            events = model.observe_cycle(t, TZ, outlets={}, mains_phase_currents=(2, 3, 1),
                                         prices=known_entries())
            t += timedelta(minutes=1)
        self.assertIn("prices: ZeroDivisionError", model.last_error)
        self.assertIn("household_hour", events)

    def test_naive_now_is_rejected(self):
        with self.assertRaises(ValueError):
            cm.ChargeModel().observe_cycle(datetime(2026, 11, 9, 18), TZ, outlets={})

    def test_garbage_inputs_never_raise(self):
        model = cm.ChargeModel()
        events = model.observe_cycle(
            L(2026, 11, 9, 18), TZ, outlets={"smartevse_1": "x", "smartevse_2": {"available": "yes"}},
            ev_power_kw="nan", mains_phase_currents=[1, 2], ev_phase_currents={"L1": -1},
            socs={"vehicle_1": "53.0%", "vehicle_2": (None, "x")}, capacities={"vehicle_1": "x"},
            ledger={"chargers": "x"}, prices=[None, 1, {"start": 1}], price_currency=5,
            controller_state=object(), charge_reason=None)
        self.assertEqual(events, frozenset())
        self.assertIsNone(model.last_error)


if __name__ == "__main__":
    unittest.main()
