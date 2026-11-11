"""Pure Intelligent-charging planner (charge_planner.py); no Home Assistant needed.

Price fixtures are the production Real Electricity Price consumer prices of
the 2026-11-09/10 nights (EET, UTC+2); the 11-10 07..21 hours are synthesised
to match the published min/max/average, and the 11-11 01..07 night is a
synthetic copy of the 11-10 night (both as in the design review).
"""
import contextlib
import dataclasses
import importlib.util
import itertools
import json
import math
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

_PATH = Path(__file__).parents[1] / 'custom_components/smartevse_dual_charger/charge_planner.py'
_spec = importlib.util.spec_from_file_location('charge_planner_under_test', _PATH)
m = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = m
_spec.loader.exec_module(m)

try:
    from zoneinfo import ZoneInfo
    TALLINN = ZoneInfo('Europe/Tallinn')
except Exception:  # pragma: no cover - tzdata missing
    TALLINN = None

UTC = timezone.utc
EET = timezone(timedelta(hours=2))
H = timedelta(hours=1)
Q = timedelta(minutes=15)

# (month, day, hour) local EET -> EUR/kWh
NIGHT_11_09 = [
    ((11, 9, 18), 0.4434), ((11, 9, 19), 0.3909), ((11, 9, 20), 0.3574), ((11, 9, 21), 0.2418),
    ((11, 9, 22), 0.2843), ((11, 9, 23), 0.2396),
    ((11, 10, 0), 0.3105), ((11, 10, 1), 0.2994), ((11, 10, 2), 0.2780), ((11, 10, 3), 0.2635),
    ((11, 10, 4), 0.2983), ((11, 10, 5), 0.2827), ((11, 10, 6), 0.3575),
]
DAY_11_10 = [((11, 10, 7 + k), p) for k, p in enumerate(
    [0.4300, 0.6950, 0.6200, 0.5400, 0.5000, 0.4700, 0.4600, 0.4700, 0.5000, 0.5600, 0.6400,
     0.6500, 0.5600, 0.4480, 0.3544])]
EVENING_11_10 = [((11, 10, 22), 0.3137), ((11, 10, 23), 0.3534), ((11, 11, 0), 0.3052)]
KNOWN = NIGHT_11_09 + DAY_11_10 + EVENING_11_10  # published until 11-11 01:00
NIGHT_11_11 = [((11, 11, h), p) for h, p in
               [(1, 0.2994), (2, 0.2780), (3, 0.2635), (4, 0.2983), (5, 0.2827), (6, 0.2900), (7, 0.45)]]


def L(month, day, hour, minute=0, second=0):
    return datetime(2026, month, day, hour, minute, second, tzinfo=EET)


def price_slots(rows=KNOWN, kind='known'):
    return [m.PriceSlot(L(mo, d, h), L(mo, d, h) + H, p, kind) for (mo, d, h), p in rows]


def hourly(start, values, kind='known'):
    return [m.PriceSlot(start + k * H, start + (k + 1) * H, v, kind) for k, v in enumerate(values)]


def power_slots(start=None, hours=72, expected=11.0, conservative=8.5):
    start = start or L(11, 9, 12)
    return [m.PowerSlot(start + k * H, start + (k + 1) * H, expected, conservative) for k in range(hours)]


POWERS = power_slots()
XC40 = dict(outlet='smartevse_1', vehicle='vehicle_1', settings_key='vehicle_1', name='Volvo XC40',
            kwh_per_pct=0.754, cap_kw=10.3, car_limit=90.0, priority=0)
EX30 = dict(outlet='smartevse_2', vehicle='vehicle_2', settings_key='vehicle_2', name='Volvo EX30',
            kwh_per_pct=0.719, cap_kw=10.2, car_limit=100.0, priority=1)


def car(base, soc, target, deadline, **overrides):
    fields = dict(
        outlet=base['outlet'], vehicle=base['vehicle'], settings_key=base['settings_key'], name=base['name'],
        soc=soc, soc_source='vehicle' if soc is not None else 'unknown', target_soc=target,
        car_limit=base['car_limit'], deadline=deadline, kwh_per_pct=base['kwh_per_pct'],
        kwh_per_pct_conservative=base['kwh_per_pct'], cap_kw=base['cap_kw'], cap_low_kw=base['cap_kw'] * 0.95,
        budget_kwh=None, done=False, overrun=False, drawing=False, priority=base['priority'])
    fields.update(overrides)
    return m.CarRequest(**fields)


def plan(now, cars, prices=None, powers=None, config=None, commitment=None):
    return m.build_plan(now, price_slots() if prices is None else prices, POWERS if powers is None else powers,
                        cars, config or m.PlannerConfig(), commitment)


def loc(t):
    return t.astimezone(EET).strftime('%H:%M')


def segs(p, outlet, top_up=None):
    return [s for s in p.segments if s.outlet == outlet and (top_up is None or s.top_up == top_up)]


def min_energy(p, outlet):
    return sum(s.energy_kwh for s in segs(p, outlet, top_up=False))


@contextlib.contextmanager
def patched(**values):
    old = {k: getattr(m, k) for k in values}
    try:
        for k, v in values.items():
            setattr(m, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(m, k, v)


def min_cost_flow(caps, costs, ends, cars):
    """Exact min-cost delivery of every car's need (successive shortest paths,
    Bellman-Ford; fractional capacities). cars: [(deadline, need)]. Returns the
    cost, or None when the needs cannot all be delivered."""
    n_units, n_cars = len(caps), len(cars)
    sink = n_units + n_cars + 1
    graph = [[] for _ in range(sink + 1)]

    def add(u, v, cap, cost):
        graph[u].append([v, cap, cost, len(graph[v])])
        graph[v].append([u, 0.0, -cost, len(graph[u]) - 1])

    for i in range(n_units):
        add(0, 1 + i, caps[i], costs[i])
        for j, (deadline, _) in enumerate(cars):
            if ends[i] <= deadline:
                add(1 + i, 1 + n_units + j, 1e18, 0.0)
    for j, (_, need) in enumerate(cars):
        add(1 + n_units + j, sink, need, 0.0)
    target = sum(need for _, need in cars)
    flow = cost = 0.0
    for _ in range(10000):
        if flow >= target - 1e-9:
            return cost
        dist = [math.inf] * (sink + 1)
        prev = [None] * (sink + 1)
        dist[0] = 0.0
        for _ in range(sink + 1):
            changed = False
            for u in range(sink + 1):
                if dist[u] == math.inf:
                    continue
                for k, (v, cap, c, _) in enumerate(graph[u]):
                    if cap > 1e-12 and dist[u] + c < dist[v] - 1e-15:
                        dist[v] = dist[u] + c
                        prev[v] = (u, k)
                        changed = True
            if not changed:
                break
        if dist[sink] == math.inf:
            return None
        push, v = target - flow, sink
        while v != 0:
            u, k = prev[v]
            push = min(push, graph[u][k][1])
            v = u
        v = sink
        while v != 0:
            u, k = prev[v]
            graph[u][k][1] -= push
            back = graph[u][k][3]
            graph[v][back][1] += push
            v = u
        flow += push
        cost += push * dist[sink]
    raise AssertionError('min cost flow did not converge')


def execute(p, request, prices):
    """Charge exactly along the plan at expected power; stop at the minimum
    (or the car limit inside top-up segments). Returns (soc, cost)."""
    soc, cost = request.soc, 0.0
    lookup = {s.start: s.price for s in prices}
    for seg in segs(p, request.outlet):
        price = lookup[seg.start.astimezone(EET).replace(minute=0, second=0, microsecond=0)]
        limit = request.car_limit if seg.top_up else request.target_soc
        power = min(11.0, request.cap_kw)
        new = m.EXPECTED_TAPER.advance(request.kwh_per_pct, soc, power * seg.minutes / 60, limit)
        cost += (new - soc) * request.kwh_per_pct * price
        soc = max(soc, new)
    return soc, cost


class Sim:
    """Receding-horizon controller model: replan every step with the car's
    true SoC, keep the commitment for the current unit, roll deadlines and
    set overrun like the controller does."""

    def __init__(self, cars, start, end, prices, true_kw=lambda t, outlet: 11.0, step=1, quant=None,
                 config=None, hook=None):
        self.state = {}
        for base, soc, target, deadline in cars:
            self.state[base['outlet']] = dict(base=base, soc=float(soc), target=target, deadline=deadline,
                                              overrun=False, at_deadline={}, starts=0)
        commitment, prev, last_on, self.switches = None, None, None, 0
        t = start
        while t < end:
            for s in self.state.values():
                if t >= s['deadline']:
                    s['at_deadline'][s['deadline']] = s['soc']
                    s['overrun'] = s['soc'] < s['target'] - 1e-9
                    s['deadline'] += timedelta(days=1)
                if s['overrun'] and s['soc'] >= s['target']:
                    s['overrun'] = False
            if commitment is not None and commitment.until <= t:
                commitment = None
            requests = []
            for outlet, s in self.state.items():
                soc = s['soc'] if quant is None else math.floor(s['soc'] / quant + 1e-9) * quant
                requests.append(car(s['base'], soc, s['target'], s['deadline'], overrun=s['overrun'],
                                    drawing=prev == outlet))
            p = m.build_plan(t, prices, POWERS, requests, config or m.PlannerConfig(), commitment)
            if hook:
                hook(t, p)
            outlet = p.current_outlet
            if outlet:
                s = self.state[outlet]
                if prev != outlet:
                    s['starts'] += 1
                if last_on is not None and last_on != outlet:
                    self.switches += 1
                last_on = outlet
                power = min(true_kw(t, outlet), s['base']['cap_kw']) * m.EXPECTED_TAPER.ratio(s['soc'])
                s['soc'] = min(s['base']['car_limit'], s['soc'] + power * step / 60 / s['base']['kwh_per_pct'])
                commitment = m.Commitment(outlet, m.floor15(t) + Q)
            prev = outlet
            t += timedelta(minutes=step)

    def soc_at(self, outlet, deadline):
        return self.state[outlet]['at_deadline'][deadline]


# ---------------------------------------------------------------------------
class TaperAndNeedTests(unittest.TestCase):
    def test_expected_taper_prior(self):
        taper = m.EXPECTED_TAPER
        self.assertEqual(taper.ratio(50), 1.0)
        self.assertEqual(taper.ratio(93), 1.0)
        self.assertAlmostEqual(taper.ratio(96.5), 0.75)
        self.assertAlmostEqual(taper.ratio(100), 0.5)
        self.assertAlmostEqual(taper.virtual(1.0, 50, 93), 43.0)
        self.assertAlmostEqual(taper.virtual(1.0, 93, 100), 14 * math.log(2))

    def test_advance_inverts_virtual(self):
        for taper in (m.EXPECTED_TAPER, m.conservative_taper(10.3), m.NO_TAPER):
            for low, high in [(20, 80), (85, 99), (93, 100), (50, 95.5)]:
                v = taper.virtual(0.75, low, high)
                self.assertAlmostEqual(taper.advance(0.75, low, v, 100), high, places=6)
                self.assertAlmostEqual(taper.advance(0.75, low, v * 10, high), high)

    def test_conservative_prior_is_evcc_linear(self):
        taper = m.conservative_taper(10.3)
        self.assertAlmostEqual(taper.start, 100 - 9300 / 980)
        self.assertAlmostEqual(taper.ratio(100), 1 / 10.3)
        self.assertIs(m.conservative_taper(0.8), m.NO_TAPER)

    def test_need_formulas(self):
        p = plan(L(11, 9, 18, 30), [car(XC40, 53, 80, L(11, 10, 7))])
        c = p.car('smartevse_1')
        e_actual = 27 * 0.754
        self.assertAlmostEqual(c.need_kwh, round(e_actual, 2))
        # E_e = virtual * 1.03 + P_ref * 3 min * starts (two planned starts here).
        starts = len(c.blocks)
        self.assertAlmostEqual(c.need_expected_kwh, round(e_actual * 1.03 + 10.3 * 3 / 60 * starts, 3), places=3)
        self.assertAlmostEqual(c.need_conservative_kwh, round(e_actual * 1.10 + 10.3 * 10 / 60, 3), places=3)

    def test_taper_counts_above_93_percent(self):
        flat = plan(L(11, 9, 18, 30), [car(XC40, 80, 90, L(11, 10, 7))]).car('smartevse_1')
        high = plan(L(11, 9, 18, 30), [car(XC40, 90, 100, L(11, 10, 7), car_limit=100)]).car('smartevse_1')
        self.assertAlmostEqual(flat.need_kwh, high.need_kwh)
        self.assertGreater(high.need_expected_kwh, flat.need_expected_kwh + 1.0)

    def test_unknown_soc_uses_budget(self):
        p = plan(L(11, 9, 18, 30), [car(XC40, None, 80, L(11, 10, 7), budget_kwh=22.0)])
        c = p.car('smartevse_1')
        self.assertTrue(c.estimated)
        self.assertIsNone(c.projected_soc)
        self.assertEqual(c.need_kwh, 22.0)
        self.assertAlmostEqual(c.need_expected_kwh, 22.0)
        self.assertAlmostEqual(c.need_conservative_kwh, 24.2)
        self.assertAlmostEqual(c.energy_kwh, 22.0, places=2)
        default = plan(L(11, 9, 18, 30), [car(XC40, None, 80, L(11, 10, 7))]).car('smartevse_1')
        self.assertEqual(default.need_kwh, m.DEFAULT_BUDGET_KWH)

    def test_done_reached_and_excluded(self):
        now, dl = L(11, 9, 18, 30), L(11, 10, 7)
        p = plan(now, [car(XC40, 82, 80, dl), car(EX30, 50, 80, dl, done=True)])
        self.assertEqual(p.car('smartevse_1').status, 'reached')
        self.assertEqual(p.car('smartevse_2').status, 'done')
        self.assertEqual(p.segments, ())
        self.assertEqual(p.reason, m.REASON_TARGETS_REACHED)
        p = plan(now, [car(XC40, 50, 80, dl, excluded=True), car(EX30, 50, 80, dl)])
        self.assertEqual(p.car('smartevse_1').status, 'excluded')
        self.assertIsNone(p.car('smartevse_1').cost)
        self.assertEqual({s.outlet for s in p.segments}, {'smartevse_2'})
        alone = plan(now, [car(EX30, 50, 80, dl)])
        self.assertEqual(p.car('smartevse_2').blocks, alone.car('smartevse_2').blocks)


class RealPriceScenarioTests(unittest.TestCase):
    def test_single_car_night_plan(self):
        request = car(XC40, 53, 80, L(11, 10, 7))
        p = plan(L(11, 9, 18, 30), [request])
        c = p.car('smartevse_1')
        self.assertEqual((c.status, c.feasible, p.reason), ('planned', True, m.REASON_WAITING))
        self.assertEqual(c.projected_soc, 80.0)
        self.assertEqual([(loc(b.start), loc(b.end)) for b in c.blocks], [('21:00', '22:00'), ('22:51', '00:00')])
        self.assertEqual(loc(c.next_start), '21:00')
        self.assertAlmostEqual(c.cost, 4.96, delta=0.02)
        # Planned cost/SoC are what charging exactly along the plan gives.
        soc, cost = execute(p, request, price_slots())
        self.assertAlmostEqual(soc, 80.0, places=6)
        self.assertAlmostEqual(cost, c.cost, delta=0.01)
        # Much cheaper than charging at plug-in (18:30, sequential at expected power).
        baseline = m.baseline_costs([m.BaselineSession('smartevse_1', L(11, 9, 18, 30), c.energy_kwh, 10.3, 53, 0.754)],
                                    price_slots(), POWERS)
        self.assertGreater(baseline['smartevse_1'] - c.cost, 1.0)

    def test_two_cars_equal_deadline(self):
        dl = L(11, 10, 7)
        requests = [car(XC40, 53, 80, dl), car(EX30, 40, 80, dl)]
        p = plan(L(11, 9, 18, 30), requests)
        self.assertEqual(p.order, ('smartevse_1', 'smartevse_2'))
        for request in requests:
            c = p.car(request.outlet)
            self.assertEqual((c.status, c.feasible, c.projected_soc), ('planned', True, 80.0))
            soc, cost = execute(p, request, price_slots())
            self.assertAlmostEqual(soc, 80.0, places=6)
            self.assertAlmostEqual(cost, c.cost, delta=0.01)
        # XC40 (priority) runs as one block, the hand-off idle sits between the cars.
        self.assertEqual(len(p.car('smartevse_1').blocks), 1)
        self.assertLessEqual(len(p.car('smartevse_2').blocks), 2)
        self.assertAlmostEqual(p.total_cost, 12.87, delta=0.03)
        self.assertNoOverlap(p)
        owners = [s.outlet for s in p.segments]
        self.assertEqual(sum(1 for a, b in zip(owners, owners[1:]) if a != b), 1)

    def test_two_cars_different_deadlines(self):
        requests = [car(XC40, 30, 80, L(11, 10, 5)), car(EX30, 30, 80, L(11, 10, 9))]
        p = plan(L(11, 9, 20, 0), requests)
        for request in requests:
            c = p.car(request.outlet)
            self.assertTrue(c.feasible)
            self.assertEqual(c.projected_soc, 80.0)
            self.assertTrue(all(s.end <= request.deadline for s in segs(p, request.outlet)))
        self.assertNoOverlap(p)

    def assertNoOverlap(self, p):
        ordered = sorted(p.segments, key=lambda s: s.start)
        for a, b in zip(ordered, ordered[1:]):
            self.assertLessEqual(a.end, b.start)


class OptimalityTests(unittest.TestCase):
    """Brute force on small random instances (spec 7.4/7.5 step 6 core)."""

    def test_greedy_matches_unit_subset_enumeration(self):
        rng = random.Random(1)
        t0 = datetime(2026, 11, 9, 22, tzinfo=UTC)
        checked = 0
        for _ in range(400):
            n = rng.randint(3, 10)
            prices = [round(rng.choice([rng.uniform(-0.05, 0.6), rng.choice([0.2, 0.25])]), 3) for _ in range(n)]
            if rng.random() < 0.5:
                prices = [prices[i // 4 * 4] for i in range(n)]
            units = [SimpleNamespace(rank=prices[i], end=t0 + (i + 1) * Q) for i in range(n)]
            cars = []
            for j in range(rng.choice([1, 2])):
                cars.append(SimpleNamespace(deadline=t0 + rng.randint(1, n) * Q, need=rng.randint(1, 4), j=j))
            if len(cars) == 2 and rng.random() < 0.3:
                cars[1].deadline = cars[0].deadline
            cars.sort(key=lambda c: (c.deadline, c.j))
            total = sum(c.need for c in cars)

            def feasible(subset):
                cum = 0
                for c in cars:
                    cum += c.need
                    if cum > sum(1 for i in subset if units[i].end <= c.deadline):
                        return False
                return all(units[i].end <= cars[-1].deadline for i in subset)

            best = min((sum(prices[i] for i in s) for s in itertools.combinations(range(n), total) if feasible(s)),
                       default=None)
            flow = m._Nested(cars, [c.need for c in cars])
            chosen = m._greedy(units, list(range(n)), {i: 1.0 for i in range(n)}, flow, set(), False)
            if best is None:
                self.assertFalse(flow.satisfied())
                continue
            checked += 1
            self.assertTrue(flow.satisfied())
            self.assertAlmostEqual(sum(prices[i] * x for i, x in chosen.items()), best, places=9)
        self.assertGreater(checked, 150)

    def test_greedy_matches_min_cost_flow_with_partial_units(self):
        rng = random.Random(2)
        t0 = datetime(2026, 11, 9, 22, tzinfo=UTC)
        checked = 0
        for _ in range(300):
            n = rng.randint(2, 9)
            prices = [round(rng.uniform(-0.1, 0.6), 4) for _ in range(n)]
            if rng.random() < 0.4:
                prices = [prices[i // 4 * 4] for i in range(n)]
            caps = [round(rng.uniform(0.3, 2.6), 3) for _ in range(n)]
            units = [SimpleNamespace(rank=prices[i], end=t0 + (i + 1) * Q) for i in range(n)]
            cars = [SimpleNamespace(deadline=t0 + rng.randint(1, n) * Q, need=round(rng.uniform(0.2, 6), 3))
                    for _ in range(rng.choice([1, 2]))]
            if len(cars) == 2 and rng.random() < 0.3:
                cars[1].deadline = cars[0].deadline
            cars.sort(key=lambda c: c.deadline)
            exact = min_cost_flow(caps, prices, [u.end for u in units], [(c.deadline, c.need) for c in cars])
            flow = m._Nested(cars, [c.need for c in cars])
            chosen = m._greedy(units, list(range(n)), dict(enumerate(caps)), flow, set(), False)
            if exact is None:
                self.assertFalse(flow.satisfied())
                continue
            checked += 1
            self.assertTrue(flow.satisfied())
            self.assertAlmostEqual(sum(prices[i] * x for i, x in chosen.items()), exact, places=7)
            for i, x in chosen.items():
                self.assertLessEqual(x, caps[i] + 1e-9)
        self.assertGreater(checked, 100)

    def test_build_plan_is_cost_optimal_on_random_instances(self):
        """End to end, with the buffer, merging and the 5-minute rule switched
        off so the result must equal the exact optimum for the energy each car
        is planned."""
        rng = random.Random(3)
        checked = 0
        with patched(START_PENALTY_EUR=-1.0, MIN_START_MINUTES=0, CONSERVATIVE_OVERHEAD_MIN=0):
            for _ in range(240):
                t0 = datetime(2026, 11, 9, 20, tzinfo=UTC)
                now = t0 + timedelta(minutes=rng.choice([0, 0, 4, 9, 13]))
                hours = rng.randint(2, 5)
                prices = [m.PriceSlot(t0 + k * H, t0 + (k + 1) * H, round(rng.uniform(-0.1, 0.6), 3), 'known')
                          for k in range(hours)]
                powers = [m.PowerSlot(t0 + k * H, t0 + (k + 1) * H, rng.choice([4.0, 7.5, 11.0]), 3.0)
                          for k in range(hours)]
                requests = []
                for j, base in enumerate([XC40, EX30][:rng.choice([1, 2])]):
                    deadline = t0 + rng.randint(2, hours * 4) * Q + timedelta(minutes=rng.choice([0, 0, 7]))
                    if j == 1 and rng.random() < 0.3:
                        deadline = requests[0].deadline
                    soc = rng.uniform(20, 70)
                    requests.append(car(base, soc, min(90, soc + rng.uniform(3, 25)), deadline,
                                        kwh_per_pct_conservative=1e-6, cap_kw=rng.choice([7.0, 10.3]),
                                        cap_low_kw=7.0))
                p = m.build_plan(now, prices, powers, requests)
                if any(p.car(r.outlet).status not in ('planned', 'charging') or p.car(r.outlet).late
                       for r in requests):
                    continue
                checked += 1
                # Units are measured at the fastest effective power (cap clamped to the
                # best unit power before the car's deadline); a slower car's need is
                # scaled by (fastest / its own) (review rounds 4 and 6).
                def effective(r):
                    reach = [pw.expected_kw for pw in powers
                             if max(pw.start, m.floor15(now)) + Q <= m._utc(r.deadline) and pw.end > m.floor15(now)]
                    return min(r.cap_kw, max(reach or [pw.expected_kw for pw in powers]))

                cap_k = max(effective(r) for r in requests)
                units, caps, costs = [], [], []
                start = m.floor15(now)
                while start + Q <= max(r.deadline for r in requests):
                    begin = max(start, now)
                    kw = min(powers[int((start - t0) / H)].expected_kw, cap_k)
                    units.append(start + Q)
                    caps.append(kw * (start + Q - begin).total_seconds() / 3600)
                    costs.append(prices[int((start - t0) / H)].price)
                    start += Q
                def metric(seg):
                    return seg.minutes * min(powers[int((m.floor15(seg.start) - t0) / H)].expected_kw, cap_k) / 60

                needs = []
                for r in requests:
                    planned = sum(metric(seg) for seg in segs(p, r.outlet, top_up=False))
                    scale = cap_k / effective(r)
                    self.assertAlmostEqual(planned, p.car(r.outlet).need_expected_kwh * scale, delta=2e-3 * scale)
                    needs.append((m._utc(r.deadline), planned))
                exact = min_cost_flow(caps, costs, units, needs)
                got = sum(s.price * metric(s) for s in p.segments)
                self.assertIsNotNone(exact)
                self.assertAlmostEqual(got, exact, places=6)
        self.assertGreater(checked, 60)

    def test_negative_prices_are_used_first(self):
        t0 = L(11, 9, 22)
        prices = hourly(t0, [0.30, -0.05, 0.10, -0.20, 0.40])
        p = plan(t0, [car(XC40, 60, 70, t0 + 5 * H)], prices=prices, powers=power_slots(t0, 6))
        hours = {s.start.astimezone(EET).hour for s in p.segments}
        self.assertTrue(hours <= {23, 1})
        self.assertIn(1, hours)
        self.assertLess(p.car('smartevse_1').cost, 0)


class OnTimeSelectionTests(unittest.TestCase):
    """R11: maximise the number of cars at their minimum (spec 7.5 step 4)."""

    def test_0100_case_serves_the_car_that_can_make_it(self):
        # Review algorithm #8: EX30 5->90 % by 06:00 cannot make it; XC40 60->80 % by 07:00 can.
        p = plan(L(11, 10, 1), [car(XC40, 60, 80, L(11, 10, 7)), car(EX30, 5, 90, L(11, 10, 6))])
        xc, ex = p.car('smartevse_1'), p.car('smartevse_2')
        self.assertEqual((xc.status, xc.feasible, xc.projected_soc), ('planned', True, 80.0))
        self.assertEqual((ex.status, ex.feasible), ('at_risk', False))
        # R11: the late car gets as much as it can by 06:00 with the XC40 (and its
        # buffer) still on time, not only what the XC40's price plan leaves over
        # (that gave 53.6 %).
        self.assertAlmostEqual(ex.projected_soc, 57.2, delta=1.0)
        # The late car gets the remaining time as early as possible: it charges now.
        self.assertEqual((p.current_outlet, p.reason), ('smartevse_2', m.REASON_CATCH_UP))
        self.assertTrue(all(s.end <= L(11, 10, 6) for s in segs(p, 'smartevse_2')))

    def test_feasible_car_is_not_marked_failing(self):
        # Review algorithm #8 first scenario: only the later car falls short.
        p = plan(L(11, 10, 2), [car(XC40, 30, 80, L(11, 10, 7)), car(EX30, 10, 90, L(11, 10, 9))])
        self.assertIn(p.car('smartevse_1').status, ('planned', 'charging', 'tight'))
        self.assertEqual(p.car('smartevse_1').projected_soc, 80.0)
        self.assertEqual(p.car('smartevse_2').status, 'at_risk')
        self.assertLess(p.car('smartevse_2').projected_soc, 90)

    def test_largest_need_is_moved_late_with_ties_by_priority(self):
        now, dl = L(11, 10, 3), L(11, 10, 7)
        bigger = plan(now, [car(XC40, 40, 65, dl), car(EX30, 40, 70, dl)])
        self.assertEqual(bigger.car('smartevse_2').status, 'at_risk')
        self.assertNotEqual(bigger.car('smartevse_1').status, 'at_risk')
        twins = [car(XC40, 40, 70, dl, kwh_per_pct=0.719, cap_kw=10.2), car(EX30, 40, 70, dl)]
        p = plan(now, twins)
        self.assertEqual(p.car('smartevse_2').status, 'at_risk')  # priority 1 = lower priority
        swapped = plan(now, [dataclasses.replace(twins[0], priority=1), dataclasses.replace(twins[1], priority=0)])
        self.assertEqual(swapped.car('smartevse_1').status, 'at_risk')

    def test_number_of_on_time_cars_is_maximal(self):
        rng = random.Random(4)
        checked = 0
        for _ in range(150):
            t0 = datetime(2026, 11, 9, 23, tzinfo=UTC)
            hours = rng.randint(2, 7)
            prices = hourly(t0, [round(rng.uniform(0, 0.5), 3) for _ in range(hours)])
            powers = [m.PowerSlot(t0 + k * H, t0 + (k + 1) * H, rng.choice([6.0, 11.0]), 5.0) for k in range(hours)]
            requests = []
            for base in (XC40, EX30):
                soc = rng.uniform(10, 70)
                requests.append(car(base, soc, min(95, soc + rng.uniform(10, 50)),
                                    t0 + rng.randint(4, hours * 4) * Q))
            p = m.build_plan(t0, prices, powers, requests)
            k = max(r.cap_kw for r in requests)
            ends, caps = [], []
            start = t0
            while start + Q <= max(r.deadline for r in requests):
                ends.append(start + Q)
                caps.append(min(powers[int((start - t0) / H)].expected_kw, k) / 4)
                start += Q
            needs = {r.outlet: p.car(r.outlet).need_expected_kwh * k / r.cap_kw for r in requests}

            def fits(subset, slack=0.0):
                return min_cost_flow(caps, [0.0] * len(caps), ends,
                                     [(m._utc(r.deadline), needs[r.outlet] + slack) for r in subset]) is not None

            subsets = [s for size in (2, 1) for s in itertools.combinations(requests, size)]
            if any(fits(s, -0.02) != fits(s, 0.02) for s in subsets):
                continue  # too close to the boundary for a rounded need
            best = max((len(s) for s in subsets if fits(s)), default=0)
            # The R11 on-time set (a late car that still covers its unmargined minimum
            # shows "tight", but is not in the on-time set).
            on_time = sum(1 for r in requests if not p.car(r.outlet).late)
            for r in requests:
                c = p.car(r.outlet)
                self.assertEqual(c.status == 'at_risk',
                                 c.late and c.projected_soc < round(r.target_soc - m.COVER_TOLERANCE_PCT, 1) - 0.05)
            checked += 1
            self.assertEqual(on_time, best)
        self.assertGreater(checked, 80)


class OverrunProtectionTests(unittest.TestCase):
    """Review algorithm #1: an overrunning car must not take units a car with
    a live deadline needs (latest-start protection P)."""

    def test_overrun_car_charges_around_the_protected_car(self):
        now = L(11, 10, 6)
        p = plan(now, [car(XC40, 60, 80, L(11, 10, 9)), car(EX30, 55, 90, L(11, 11, 6), overrun=True)])
        xc, ex = p.car('smartevse_1'), p.car('smartevse_2')
        self.assertTrue(xc.feasible)
        self.assertEqual(xc.projected_soc, 80.0)
        self.assertTrue(all(s.end <= L(11, 10, 9) for s in segs(p, 'smartevse_1')))
        self.assertEqual(ex.status, 'catch_up')
        # The overrun car charges now, but only outside the XC40's latest-start window.
        self.assertEqual((p.current_outlet, p.reason), ('smartevse_2', m.REASON_CATCH_UP))
        protected_from = L(11, 10, 9) - timedelta(hours=xc.need_conservative_kwh / 8.5)
        self.assertTrue(all(s.end <= protected_from or s.start >= L(11, 10, 9)
                            for s in segs(p, 'smartevse_2') if s.kind == 'overrun' and s.start < L(11, 10, 9)))
        # With only 2 h left the conservative need covers the whole window: XC40 first.
        p = plan(now, [car(XC40, 60, 80, L(11, 10, 8)), car(EX30, 55, 90, L(11, 11, 6), overrun=True)])
        self.assertEqual(p.current_outlet, 'smartevse_1')
        self.assertEqual(p.car('smartevse_1').projected_soc, 80.0)

    def test_protected_car_that_cannot_make_it_still_comes_first(self):
        now = L(11, 10, 6)
        p = plan(now, [car(XC40, 60, 80, L(11, 10, 7)), car(EX30, 55, 90, L(11, 11, 6), overrun=True)])
        self.assertEqual(p.car('smartevse_1').status, 'at_risk')
        self.assertEqual(p.current_outlet, 'smartevse_1')
        self.assertTrue(all(s.start >= L(11, 10, 7) for s in segs(p, 'smartevse_2')))

    def test_receding_horizon_overrun_does_not_starve_the_other_car(self):
        for ready_by in (L(11, 10, 7), L(11, 10, 8)):
            sim = Sim([(XC40, 60, 80, ready_by), (EX30, 5, 90, L(11, 10, 6))], L(11, 10, 1),
                      ready_by + timedelta(minutes=1), price_slots(), step=2)
            self.assertGreaterEqual(sim.soc_at('smartevse_1', ready_by), 80.0)
            self.assertGreater(sim.soc_at('smartevse_2', L(11, 10, 6)), 50.0)


class BufferTests(unittest.TestCase):
    """Clamped latest-start buffer (spec 7.5 step 5, review algorithm #2)."""

    def test_buffer_keeps_the_latest_units_free(self):
        t0 = L(11, 9, 20)
        prices = hourly(t0, [0.5, 0.45, 0.4, 0.35, 0.3, 0.25, 0.2, 0.15, 0.12, 0.1, 0.05])
        p = plan(t0, [car(XC40, 50, 77, L(11, 10, 7))], prices=prices)
        last = max(s.end for s in p.segments)
        self.assertLessEqual(last, L(11, 10, 6))
        self.assertEqual(p.car('smartevse_1').status, 'planned')
        self.assertFalse(p.car('smartevse_1').tight)

    def test_tight_night_keeps_part_of_the_buffer(self):
        prices = price_slots(KNOWN + NIGHT_11_11)
        dl = L(11, 11, 7)
        p = plan(L(11, 10, 22), [car(XC40, 20, 80, dl), car(EX30, 45, 80, dl)], prices=prices)
        statuses = {c.status for c in p.cars}
        self.assertTrue(statuses <= {'planned', 'tight'})
        self.assertIn('tight', statuses)
        for c in p.cars:
            self.assertGreaterEqual(min_energy(p, c.outlet), c.need_expected_kwh - 2e-3)
        free = 9 * 60 - sum(s.minutes for s in p.segments)
        self.assertGreater(free, 15)  # a reduced buffer is still kept

    def test_tight_night_survives_power_drops(self):
        prices = price_slots(KNOWN + NIGHT_11_11)
        dl = L(11, 11, 7)
        drops = [lambda t, o: 8.5 if t >= L(11, 11, 0) else 11.0,
                 lambda t, o: 8.5 if t >= L(11, 11, 2) else 11.0,
                 lambda t, o: 9.0 if t >= L(11, 11, 0, 30) else 11.0]
        for true_kw in drops:
            sim = Sim([(XC40, 20, 80, dl), (EX30, 45, 80, dl)], L(11, 10, 22), dl + timedelta(minutes=1),
                      prices, true_kw=true_kw, step=2)
            self.assertGreaterEqual(sim.soc_at('smartevse_1', dl), 80.0)
            self.assertGreaterEqual(sim.soc_at('smartevse_2', dl), 80.0)

    def test_buffer_never_makes_a_feasible_plan_infeasible(self):
        rng = random.Random(5)
        for _ in range(150):
            t0 = datetime(2026, 11, 9, 20, tzinfo=UTC)
            hours = rng.randint(3, 10)
            prices = hourly(t0, [round(rng.uniform(-0.05, 0.6), 3) for _ in range(hours)])
            now = t0 + timedelta(minutes=rng.choice([0, 3, 7, 11]))
            requests = [car(base, rng.uniform(10, 79), 80, t0 + rng.randint(2, hours * 4) * Q)
                        for base in (XC40, EX30)]
            p = m.build_plan(now, prices, POWERS, requests)
            for r in requests:
                c = p.car(r.outlet)
                if c.status in ('planned', 'tight', 'charging') and not c.late:
                    self.assertGreaterEqual(min_energy(p, r.outlet), c.need_expected_kwh - 2e-3)
                    # the start rule may add at most one unit's padded start minutes per block
                    pad = m.MIN_START_MINUTES + m.START_PAD_EXTRA_MINUTES
                    self.assertLessEqual(min_energy(p, r.outlet),
                                         c.need_expected_kwh + len(c.blocks) * r.cap_kw * pad / 60 + 2e-3)
                    self.assertEqual(c.projected_soc, 80.0)


class CommitmentAndUnitZeroTests(unittest.TestCase):
    def test_unit_zero_needs_five_minutes(self):
        p = plan(L(11, 9, 21, 11), [car(XC40, 53, 80, L(11, 10, 7))])
        self.assertEqual(p.reason, m.REASON_WAITING)
        self.assertGreaterEqual(p.next_start, L(11, 9, 21, 15))
        p = plan(L(11, 9, 21, 10), [car(XC40, 53, 80, L(11, 10, 7))])
        self.assertEqual((p.current_outlet, p.reason), ('smartevse_1', m.REASON_CHARGING))

    def test_committed_car_keeps_unit_zero_down_to_one_minute(self):
        request = car(XC40, 53, 80, L(11, 10, 7), drawing=True)
        commitment = m.Commitment('smartevse_1', L(11, 9, 21, 15))
        p = plan(L(11, 9, 21, 13, 30), [request], commitment=commitment)
        self.assertEqual((p.current_outlet, p.car('smartevse_1').status), ('smartevse_1', 'charging'))
        self.assertIsNone(plan(L(11, 9, 21, 14, 30), [request], commitment=commitment).current_outlet)
        expired = m.Commitment('smartevse_1', L(11, 9, 21, 0))
        self.assertIsNone(plan(L(11, 9, 21, 13, 30), [request], commitment=expired).current_outlet)

    def test_commitment_holds_unit_zero_even_when_it_is_expensive(self):
        request = car(XC40, 53, 80, L(11, 10, 7), drawing=True)
        p = plan(L(11, 9, 19, 5), [request], commitment=m.Commitment('smartevse_1', L(11, 9, 19, 15)))
        self.assertEqual(p.current_outlet, 'smartevse_1')
        self.assertEqual(segs(p, 'smartevse_1')[0].kind, 'committed')
        self.assertIsNone(plan(L(11, 9, 19, 5), [request]).current_outlet)

    def test_unit_zero_counts_its_remaining_minutes_near_the_deadline(self):
        # Review algorithm #4: at 06:52 only 8 minutes are left.
        commitment = m.Commitment('smartevse_1', L(11, 10, 7))
        ok = plan(L(11, 10, 6, 52), [car(XC40, 79.0, 80, L(11, 10, 7), drawing=True)], commitment=commitment)
        self.assertEqual((ok.car('smartevse_1').status, ok.car('smartevse_1').feasible), ('charging', True))
        short = plan(L(11, 10, 6, 52), [car(XC40, 78.5, 80, L(11, 10, 7), drawing=True)], commitment=commitment)
        # The margined need does not fit (late), the unmargined 1.1 kWh does: since
        # review round 3 that reads "charging" (it reaches 80 %), not "at_risk".
        self.assertTrue(short.car('smartevse_1').late)
        self.assertEqual((short.car('smartevse_1').status, short.car('smartevse_1').projected_soc), ('charging', 80.0))
        self.assertEqual(short.current_outlet, 'smartevse_1')
        self.assertAlmostEqual(sum(s.minutes for s in short.segments), 8.0)
        hopeless = plan(L(11, 10, 6, 52), [car(XC40, 70.0, 80, L(11, 10, 7), drawing=True)], commitment=commitment)
        self.assertEqual(hopeless.car('smartevse_1').status, 'at_risk')
        # Projected 79.8 % reads as the 80 % target on the card: no at_risk flip on every
        # whole-percent SoC step near the deadline (review round 3, seed 126).
        edge = plan(L(11, 10, 6, 52), [car(XC40, 78.0, 80, L(11, 10, 7), drawing=True)], commitment=commitment)
        self.assertEqual((edge.car('smartevse_1').status, edge.car('smartevse_1').projected_soc, edge.reason),
                         ('charging', 79.8, m.REASON_CHARGING))

    def test_drawing_car_is_not_interrupted_on_equal_deadlines(self):
        # Review algorithm #10: EX30 (lower priority) is charging, XC40 just plugged in.
        dl = L(11, 10, 7)
        p = plan(L(11, 9, 23, 20), [car(XC40, 53, 80, dl), car(EX30, 50, 80, dl, drawing=True)],
                 commitment=m.Commitment('smartevse_2', L(11, 9, 23, 30)))
        self.assertEqual(p.current_outlet, 'smartevse_2')
        owners = [s.outlet for s in p.segments]
        self.assertEqual(sum(1 for a, b in zip(owners, owners[1:]) if a != b), 1)

    def test_start_inside_a_unit_leaves_at_least_five_minutes(self):
        for now in [L(11, 9, 18, 30), L(11, 9, 22, 50), L(11, 9, 22, 47)]:
            p = plan(now, [car(XC40, 53, 80, L(11, 10, 7))])
            for block in p.car('smartevse_1').blocks:
                unit_end = m.floor15(block.start) + Q
                if block.start > m.floor15(block.start) and block.start > now:
                    self.assertGreaterEqual((unit_end - block.start).total_seconds(), 5 * 60 - 1)

    def test_plan_is_time_consistent_with_exact_soc(self):
        """Replanning every minute with the true SoC never adds a future unit."""
        previous = {}
        additions = []

        def hook(t, p):
            future = {m.floor15(s.start) for s in p.segments if m.floor15(s.start) > m.floor15(t)}
            if 'f' in previous and not future <= previous['f']:
                additions.append((loc(t), sorted(loc(x) for x in future - previous['f'])))
            previous['f'] = future

        sim = Sim([(XC40, 53, 80, L(11, 10, 7))], L(11, 9, 18, 30), L(11, 10, 7, 1), price_slots(), hook=hook)
        self.assertEqual(additions, [])
        self.assertGreaterEqual(sim.soc_at('smartevse_1', L(11, 10, 7)), 80.0)
        self.assertLessEqual(sim.state['smartevse_1']['starts'], 2)


class TailMergeTests(unittest.TestCase):
    """Isolated short blocks (spec 7.5 step 9, review algorithm #5)."""

    def test_margin_unit_is_not_left_as_an_isolated_quarter(self):
        p = plan(L(11, 9, 18, 30), [car(XC40, 53, 80, L(11, 10, 7))])
        blocks = p.car('smartevse_1').blocks
        self.assertLessEqual(len(blocks), 2)
        self.assertTrue(all((b.end - b.start) >= timedelta(minutes=30) for b in blocks))

    def test_merge_only_within_the_start_penalty(self):
        t0 = L(11, 9, 21)
        cheap_far = hourly(t0, [0.10, 1.00, 1.00, 0.05, 1.00])
        p = plan(t0, [car(XC40, 50, 65, t0 + 5 * H)], prices=cheap_far, powers=power_slots(t0, 6))
        blocks = p.car('smartevse_1').blocks
        self.assertEqual(len(blocks), 2)
        self.assertLess(blocks[0].end - blocks[0].start, timedelta(minutes=30))
        near = hourly(t0, [0.10, 0.11, 0.11, 0.05, 1.00])
        p = plan(t0, [car(XC40, 50, 65, t0 + 5 * H)], prices=near, powers=power_slots(t0, 6))
        self.assertEqual(len(p.car('smartevse_1').blocks), 1)

    def test_partly_used_unit_does_not_split_a_run(self):
        p = plan(L(11, 9, 18, 30), [car(XC40, 53, 80, L(11, 10, 7)), car(EX30, 40, 80, L(11, 10, 7))])
        xc = p.car('smartevse_1').blocks
        self.assertEqual(len(xc), 1)
        self.assertEqual(loc(xc[0].start), '21:00')

    def test_running_car_finishes_its_short_tail_now(self):
        # At 04:00 the commitment has ended; moving the last minutes to 05:00 saves cents only.
        p = plan(L(11, 10, 4), [car(EX30, 78.4, 80, L(11, 10, 7), drawing=True)])
        self.assertEqual(p.current_outlet, 'smartevse_2')
        self.assertEqual(len(p.car('smartevse_2').blocks), 1)
        idle = plan(L(11, 10, 4), [car(EX30, 78.4, 80, L(11, 10, 7))])
        self.assertIsNone(idle.current_outlet)


class ReviewRegressionTests(unittest.TestCase):
    """Second design review: commitment, R11 late cars, ties and in-unit starts."""

    T0 = datetime(2026, 11, 9, 21, tzinfo=UTC)

    def utc(self, day, hour, minute=0, second=0):
        return datetime(2026, 11, day, hour, minute, second, tzinfo=UTC)

    def request(self, outlet, soc, target, deadline, kwh_per_pct, priority, *, cap=11.0, drawing=False,
                overrun=False):
        return m.CarRequest(outlet=outlet, vehicle=None, settings_key='other', name=None, soc=soc,
                            soc_source='vehicle', target_soc=target, car_limit=100.0, deadline=deadline,
                            kwh_per_pct=kwh_per_pct, kwh_per_pct_conservative=kwh_per_pct, cap_kw=cap,
                            cap_low_kw=cap * 0.95, budget_kwh=None, done=False, overrun=overrun,
                            drawing=drawing, priority=priority)

    def slots(self, prices, powers):
        return ([m.PriceSlot(self.T0 + k * H, self.T0 + (k + 1) * H, p, 'known') for k, p in enumerate(prices)],
                [m.PowerSlot(self.T0 + k * H, self.T0 + (k + 1) * H, e, c) for k, (e, c) in enumerate(powers)])

    def test_committed_late_car_leaves_the_on_time_car_its_share_of_unit_zero(self):
        prices, powers = self.slots([0.163, 0.272, 0.001, 0.141, 0.423],
                                    [(11, 8.8), (9, 9), (11, 8.8), (11, 11), (11, 11)])
        deadline = self.utc(10, 0, 15)
        now = self.utc(9, 22, 45, 14)
        hopeless = self.request('smartevse_1', 55.07, 90, deadline, 0.75, 0, drawing=True)
        on_time = self.request('smartevse_2', 30.9, 60, deadline, 0.5, 1)
        free = m.build_plan(now, prices, powers, [hopeless, on_time])
        committed = m.build_plan(now, prices, powers, [hopeless, on_time],
                                 commitment=m.Commitment('smartevse_1', self.utc(9, 23)))
        for p in (free, committed):
            self.assertIn(p.car('smartevse_2').status, ('planned', 'tight', 'charging'))
            self.assertEqual(p.car('smartevse_2').projected_soc, 60.0)
            self.assertEqual(p.car('smartevse_1').status, 'at_risk')
        # The late car keeps charging only until the on-time car must start.
        first = committed.car('smartevse_2').blocks[0].start
        self.assertLess(first, self.utc(9, 23))
        self.assertTrue(all(s.end <= first for s in segs(committed, 'smartevse_1') if s.start < self.utc(9, 23)))

    def test_committed_overrun_car_does_not_take_the_live_cars_protection(self):
        prices, powers = self.slots([0.2] * 40, [(11, 11)] * 40)
        now = self.utc(9, 22, 50, 10)
        overrun = self.request('smartevse_1', 30, 80, self.utc(10, 22), 0.75, 0, drawing=True, overrun=True)
        live = self.request('smartevse_2', 70, 80, self.utc(9, 23, 45), 0.75, 1)
        p = m.build_plan(now, prices, powers, [overrun, live],
                         commitment=m.Commitment('smartevse_1', self.utc(9, 23)))
        self.assertEqual((p.current_outlet, p.car('smartevse_2').status), ('smartevse_2', 'charging'))
        self.assertEqual(p.car('smartevse_2').projected_soc, 80.0)
        self.assertEqual(p.car('smartevse_1').status, 'catch_up')

    def test_two_late_cars_serve_the_one_that_can_still_make_it_first(self):
        start = datetime(2026, 11, 9, 21, tzinfo=UTC)
        prices = [m.PriceSlot(start + k * H, start + (k + 1) * H, p, 'known')
                  for k, p in enumerate([0.405, 0.032, 0.007, -0.016, 0.152, -0.043, 0.362])]
        powers = [m.PowerSlot(start + k * H, start + (k + 1) * H, e, c)
                  for k, (e, c) in enumerate([(11, 8.8), (11, 8.8), (11, 11), (9, 9), (11, 11), (7, 7), (11, 11)])]
        hopeless = self.request('smartevse_1', 54.0, 95, self.utc(9, 23, 38), 0.5, 0, cap=7.4)
        tight = self.request('smartevse_2', 30.7, 95, self.utc(10, 2, 45), 0.5, 1, cap=10.3)
        p = m.build_plan(self.utc(9, 22, 10), prices, powers, [hopeless, tight])
        # It still reaches 95 % and is served before the car that cannot make it at
        # all. Since review round 4 its units are measured at its own 10.3 kW, not at
        # the hopeless car's 7.4 kW, so its margined need fits again (not late).
        # The hopeless car may only use the minutes the on-time car can spare.
        self.assertFalse(p.car('smartevse_2').late)
        self.assertEqual(p.car('smartevse_2').status, 'planned')
        self.assertEqual(p.car('smartevse_2').projected_soc, 95.0)
        self.assertEqual(p.car('smartevse_1').status, 'at_risk')
        first = [s for s in p.segments if s.outlet == 'smartevse_1']
        self.assertLessEqual(sum(s.minutes for s in first), 15)
        alone = m.build_plan(self.utc(9, 22, 10), prices, powers, [tight])
        self.assertEqual(alone.car('smartevse_2').projected_soc, p.car('smartevse_2').projected_soc)

    def test_late_car_gets_all_it_can_while_the_other_stays_on_time(self):
        prices, powers = self.slots([0.312, 0.317, 0.028, -0.044, 0.156, 0.101, 0.396],
                                    [(11, 11), (9, 7.2), (9, 7.2), (11, 8.8), (11, 11), (7, 5.6), (11, 8.8)])
        now = self.utc(9, 22, 22, 5)
        first = self.request('smartevse_1', 52.0, 80, self.utc(10, 1, 30), 0.75, 0, cap=7.4)
        second = self.request('smartevse_2', 45.2, 60, self.utc(10, 2), 0.3, 1, cap=7.4)
        p = m.build_plan(now, prices, powers, [first, second])
        # Each fits alone, not both: R11 keeps the earlier ready-by (and priority) car on time.
        self.assertIn(p.car('smartevse_1').status, ('charging', 'planned', 'tight'))
        self.assertEqual(p.car('smartevse_1').projected_soc, 80.0)
        self.assertTrue(p.car('smartevse_2').late)
        # The late car is not left with what the on-time car's price plan spares:
        # it gets the most it can by 02:00 and still reaches 60 % — so it reads
        # "tight" (short of the margin only), not "at_risk" (review round 3).
        self.assertEqual(p.car('smartevse_2').projected_soc, 60.0)
        self.assertEqual(p.car('smartevse_2').status, 'tight')

    def test_r11_tie_keeps_the_earlier_ready_by_then_priority_not_the_smaller_need(self):
        now, dl = L(11, 10, 3), L(11, 10, 7)
        bigger_first = [car(XC40, 40, 70, dl), car(EX30, 40, 66, dl)]
        p = plan(now, bigger_first)
        self.assertNotEqual(p.car('smartevse_1').status, 'at_risk')  # priority 0, larger need
        self.assertEqual(p.car('smartevse_2').status, 'at_risk')
        earlier = [car(XC40, 40, 70, L(11, 10, 6, 45), priority=1), car(EX30, 40, 66, dl, priority=0)]
        p = plan(now, earlier)
        self.assertNotEqual(p.car('smartevse_1').status, 'at_risk')  # earlier ready-by wins over priority
        self.assertEqual(p.car('smartevse_2').status, 'at_risk')

    def test_tied_on_time_choice_is_stable_while_the_chosen_car_charges(self):
        dl = L(11, 10, 1)
        sim = Sim([(XC40, 40, 70, dl), (EX30, 40, 70, dl)], L(11, 9, 22), dl + timedelta(minutes=1),
                  price_slots(), step=1)
        self.assertLessEqual(sim.switches, 1)
        self.assertGreaterEqual(sim.soc_at('smartevse_1', dl), 70.0)

    def test_start_inside_a_unit_happens_even_when_the_next_cycle_is_seconds_late(self):
        start = datetime(2026, 11, 9, 21, tzinfo=UTC)
        prices = [m.PriceSlot(start + k * H, start + (k + 1) * H, p, 'known')
                  for k, p in enumerate([0.348, 0.245, 0.223, 0.406])]
        powers = [m.PowerSlot(start + k * H, start + (k + 1) * H, e, e) for k, e in enumerate([11, 11, 9, 9])]

        def request(drawing=False):
            return self.request('smartevse_1', 55.0, 95, self.utc(9, 23, 41), 0.3, 0, cap=10.3, drawing=drawing)

        p = m.build_plan(self.utc(9, 22, 7, 24), prices, powers, [request()])
        block = p.car('smartevse_1').blocks[0]
        unit_end = m.floor15(block.start) + Q
        self.assertGreater(block.start, m.floor15(block.start))
        self.assertGreaterEqual(unit_end - block.start, timedelta(minutes=m.MIN_START_MINUTES + 1))
        # A cycle up to a minute before the planned start already starts the car ...
        early = m.build_plan(block.start - timedelta(seconds=50), prices, powers, [request()])
        self.assertEqual((early.current_outlet, early.reason), ('smartevse_1', m.REASON_CHARGING))
        # ... and the cycles after it keep it (Smart since last cycle, committed for the unit).
        commitment = m.Commitment('smartevse_1', unit_end)
        for delay in (10, 70):
            later = m.build_plan(block.start + timedelta(seconds=delay), prices, powers, [request(True)],
                                 commitment=commitment)
            self.assertEqual(later.current_outlet, 'smartevse_1')
            self.assertEqual(later.car('smartevse_1').projected_soc, 95.0)


class MixedCarPowerTests(unittest.TestCase):
    """Review round 4: units were measured at the lowest cap of all cars that still
    needed charge, so a cold car (session cap) slowed the warm car's plan too."""

    def test_a_slow_car_does_not_shrink_the_other_cars_power(self):
        now, dl = L(11, 9, 21), L(11, 10, 7)
        cold = car(XC40, 30, 70, dl, cap_kw=5.64, cap_low_kw=5.36)
        warm = car(EX30, 30, 70, dl, cap_kw=10.72, cap_low_kw=10.18)
        both = plan(now, [cold, warm])
        alone = plan(now, [warm])
        minutes = lambda p: sum(seg.minutes for seg in segs(p, 'smartevse_2', top_up=False))
        self.assertFalse(both.car('smartevse_2').late)
        self.assertEqual(both.car('smartevse_2').status, 'planned')
        self.assertAlmostEqual(minutes(both), minutes(alone), delta=5.0)  # buffer and tail merging
        # The cold car gets the minutes its own power needs, and both reach 70 %.
        for outlet in ('smartevse_1', 'smartevse_2'):
            self.assertGreaterEqual(both.car(outlet).projected_soc, 70.0)
        kwh = sum(seg.energy_kwh for seg in segs(both, 'smartevse_1', top_up=False))
        self.assertGreaterEqual(kwh, both.car('smartevse_1').need_expected_kwh - 1e-3)
        self.assertTrue(all(seg.energy_kwh <= seg.minutes * 5.64 / 60 + 1e-9
                            for seg in segs(both, 'smartevse_1')))


class EffectivePowerTests(unittest.TestCase):
    """Review 6: units were scaled by nominal caps. On a 16 A feeder every unit gives
    less than the fastest cap, so the slower car was over-reserved and shown late."""

    def setUp(self):
        self.now = L(11, 9, 18)
        self.powers = [m.PowerSlot(self.now + k * H, self.now + (k + 1) * H, 6.7, 6.0) for k in range(8)]
        self.prices = hourly(self.now, [0.30] * 8)

    def request(self, base, kwh, deadline, cap, **extra):
        return car(base, 50, 50 + kwh / 0.5, deadline, kwh_per_pct=0.5, kwh_per_pct_conservative=0.5, cap_kw=cap,
                   cap_low_kw=cap * 0.95, car_limit=100.0, **extra)

    def minutes(self, p, outlet):
        return sum(seg.minutes for seg in segs(p, outlet, top_up=False))

    def test_both_cars_are_planned_at_the_power_the_house_leaves(self):
        dl = self.now + 7 * H
        fast, slow = self.request(XC40, 20, dl, 10.7), self.request(EX30, 20, dl, 8.0)
        both = m.build_plan(self.now, self.prices, self.powers, [fast, slow])
        for outlet, alone in (("smartevse_1", fast), ("smartevse_2", slow)):
            single = m.build_plan(self.now, self.prices, self.powers, [alone])
            self.assertFalse(both.car(outlet).late, outlet)
            self.assertAlmostEqual(self.minutes(both, outlet), self.minutes(single, outlet), delta=6.0)

    def test_an_idle_car_at_its_minimum_does_not_change_the_other_cars_plan(self):
        dl = self.now + 7 * H
        busy = self.request(EX30, 19, dl, 7.4)
        idle = car(XC40, 85, 80, dl, cap_kw=10.7, cap_low_kw=10.2, car_limit=100.0, reached=True)
        alone = m.build_plan(self.now, self.prices, self.powers, [busy])
        with_idle = m.build_plan(self.now, self.prices, self.powers, [idle, busy])
        self.assertEqual(with_idle.car("smartevse_2").late, alone.car("smartevse_2").late)
        self.assertAlmostEqual(self.minutes(with_idle, "smartevse_2"), self.minutes(alone, "smartevse_2"), delta=1.0)
        self.assertEqual(with_idle.car("smartevse_2").status, alone.car("smartevse_2").status)


class TopUpTests(unittest.TestCase):
    def test_cars_keep_charging_to_their_limit_in_the_cheapest_known_units(self):
        # 0.0.11.0 (owner): the minimum is a floor. After it each car goes on to its own
        # limit in the cheapest known-price units left before its ready-by; no price limit.
        dl = L(11, 10, 7)
        requests = [car(XC40, 53, 80, dl), car(EX30, 40, 80, dl)]
        base = plan(L(11, 9, 18, 30), requests)
        p = plan(L(11, 9, 18, 30), requests, config=m.PlannerConfig(above_minimum=True))
        top = [s for s in p.segments if s.top_up]
        self.assertTrue(top)
        for seg in top:
            self.assertEqual(seg.price_kind, 'known')
            self.assertLessEqual(seg.end, dl)
        for request in requests:
            c = p.car(request.outlet)
            self.assertTrue(c.top_up)
            self.assertIsNotNone(c.top_up_cost)
            self.assertEqual(c.projected_soc, 80.0)  # what is guaranteed
            self.assertEqual(c.extra_soc, request.car_limit)  # the night is long enough for both
            self.assertEqual(c.energy_kwh, base.car(request.outlet).energy_kwh)
            self.assertAlmostEqual(c.cost, base.car(request.outlet).cost, delta=0.35)
        self.assertAlmostEqual(p.total_top_up_cost, sum(c.top_up_cost for c in p.cars), places=2)
        # The dear evening hours stay unused, and nothing runs past the ready-by.
        self.assertTrue(all(L(11, 9, 21) <= seg.start and seg.end <= dl for seg in p.segments))
        # Minimums first, then one car's extra charging after the other's: the cars do
        # not take turns (two hand-overs here: XC40 -> EX30 -> XC40).
        owners = [seg.outlet for seg in p.segments]
        self.assertLessEqual(sum(1 for a, b in zip(owners, owners[1:]) if a != b), 2)
        xc40_minimum = max(seg.end for seg in segs(p, 'smartevse_1', top_up=False))
        ex30_minimum = max(seg.end for seg in segs(p, 'smartevse_2', top_up=False))
        self.assertTrue(all(seg.start >= xc40_minimum - Q for seg in segs(p, 'smartevse_2', top_up=True)))
        # (The XC40's first minutes above 80 % are the end of its own minimum block.)
        self.assertGreaterEqual(max(seg.start for seg in segs(p, 'smartevse_1', top_up=True)), ex30_minimum - Q)
        # Without the option nothing is charged above the minimum.
        self.assertFalse([s for s in base.segments if s.top_up])
        self.assertTrue(all(c.extra_soc is None for c in base.cars))

    def test_guaranteed_and_extra_charging_are_split_by_battery_level(self):
        # An extra hour that comes before a minimum hour fills the minimum first: in time
        # order the charging is "guaranteed" until the minimum is in and "extra" after.
        dl = L(11, 10, 7)
        p = plan(L(11, 9, 18, 30), [car(XC40, 53, 80, dl), car(EX30, 40, 80, dl)],
                 config=m.PlannerConfig(above_minimum=True))
        for outlet in ('smartevse_1', 'smartevse_2'):
            flags = [seg.top_up for seg in segs(p, outlet)]
            self.assertEqual(flags, sorted(flags))  # guaranteed first, then extra
            c = p.car(outlet)
            self.assertAlmostEqual(min_energy(p, outlet), c.need_kwh, delta=0.2)
            self.assertGreaterEqual(sum(seg.energy_kwh for seg in segs(p, outlet)), c.need_expected_kwh)

    def test_top_up_need_starts_at_the_current_soc_above_target(self):
        p = plan(L(11, 9, 18, 30), [car(XC40, 85, 80, L(11, 10, 7))], config=m.PlannerConfig(above_minimum=True))
        c = p.car('smartevse_1')
        self.assertEqual((c.status, c.need_kwh), ('reached', 0.0))
        self.assertAlmostEqual(c.top_up_kwh, 5 * 0.754, places=2)
        self.assertEqual((c.projected_soc, c.extra_soc), (85.0, 90.0))
        self.assertEqual(loc(c.next_start), '23:00')  # 23:00 is the cheapest hour
        at = plan(L(11, 9, 23, 0), [car(XC40, 85, 80, L(11, 10, 7))], config=m.PlannerConfig(above_minimum=True))
        self.assertEqual((at.current_outlet, at.reason), ('smartevse_1', m.REASON_TOP_UP))

    def test_reached_car_has_no_minimum_need_but_is_topped_up(self):
        # Review round 3: the reached hysteresis must not make a car "done" — a car at 79 %
        # that already reached 80 % gets no minimum need but its top-up to the car limit.
        p = plan(L(11, 9, 18, 30), [car(XC40, 79, 80, L(11, 10, 7), reached=True)],
                 config=m.PlannerConfig(above_minimum=True))
        c = p.car('smartevse_1')
        self.assertEqual((c.status, c.need_kwh, c.energy_kwh), ('reached', 0.0, 0.0))
        self.assertTrue(c.top_up)
        self.assertTrue(segs(p, 'smartevse_1', top_up=True))
        self.assertFalse(segs(p, 'smartevse_1', top_up=False))
        at = plan(L(11, 9, 23, 0), [car(XC40, 79, 80, L(11, 10, 7), reached=True)],
                  config=m.PlannerConfig(above_minimum=True))
        self.assertEqual((at.current_outlet, at.reason), ('smartevse_1', m.REASON_TOP_UP))
        # Without top-up it is simply reached; done still gets nothing at all.
        off = plan(L(11, 9, 23, 0), [car(XC40, 79, 80, L(11, 10, 7), reached=True)])
        self.assertEqual((off.current_outlet, off.reason, off.car('smartevse_1').status),
                         (None, m.REASON_TARGETS_REACHED, 'reached'))
        done = plan(L(11, 9, 23, 0), [car(XC40, 79, 80, L(11, 10, 7), reached=True, done=True)],
                    config=m.PlannerConfig(above_minimum=True))
        self.assertFalse(done.segments)

    def test_top_up_may_use_buffer_units(self):
        t0 = L(11, 9, 20)
        prices = hourly(t0, [0.5, 0.45, 0.4, 0.35, 0.3, 0.25, 0.2, 0.15, 0.12, 0.1, 0.05])
        p = plan(t0, [car(XC40, 50, 77, L(11, 10, 7))], prices=prices, config=m.PlannerConfig(above_minimum=True))
        top = [s for s in p.segments if s.top_up]
        self.assertTrue(top)
        # The last hour is the cheapest; the minimum plan keeps it as its safety buffer
        # and the extra charging uses it.
        self.assertTrue(any(s.start >= L(11, 10, 6) for s in top))
        self.assertEqual(p.car('smartevse_1').extra_soc, 90.0)

    def test_committed_unit_exception_is_only_for_the_committed_car(self):
        # Review algorithm #9a: EX30 reached 80 % at 03:57 in its committed unit.
        dl = L(11, 10, 7)
        p = plan(L(11, 10, 3, 57), [car(XC40, 85, 80, dl), car(EX30, 80, 80, dl, drawing=True, car_limit=80)],
                 config=m.PlannerConfig(above_minimum=True),
                 commitment=m.Commitment('smartevse_2', L(11, 10, 4)))
        self.assertTrue(all(s.start >= L(11, 10, 4) for s in segs(p, 'smartevse_1')))
        self.assertTrue(segs(p, 'smartevse_1', top_up=True))

    def test_no_top_up_without_soc_limit_or_when_done(self):
        config = m.PlannerConfig(above_minimum=True)
        dl = L(11, 10, 7)
        # (89.6 % with a 90 % limit: within half a percent the car is full and would not draw.)
        for request in [car(XC40, None, 80, dl, budget_kwh=5), car(XC40, 85, 80, dl, car_limit=None),
                        car(XC40, 85, 80, dl, done=True), car(XC40, 89.6, 80, dl)]:
            p = plan(L(11, 9, 18, 30), [request], config=config)
            self.assertFalse([s for s in p.segments if s.top_up])
            self.assertIsNone(p.car('smartevse_1').top_up_cost)

    def test_top_up_ignores_forecast_prices(self):
        t0 = L(11, 9, 22)
        prices = hourly(t0, [0.4, 0.4]) + hourly(t0 + 2 * H, [0.01, 0.01], kind='forecast')
        p = plan(t0, [car(XC40, 85, 80, t0 + 4 * H)], prices=prices, config=m.PlannerConfig(above_minimum=True))
        self.assertTrue(p.segments)
        self.assertTrue(all(s.top_up and s.price_kind == 'known' and s.end <= t0 + 2 * H for s in p.segments))

    def test_minimums_come_first_in_time(self):
        # Found with the regression nights (2026-11-11): a car that charged above its
        # minimum before the other car's minimum had started used up the time that car
        # needed when its real power turned out 30 % lower. Extra charging is planned
        # only after the last unit of another car's minimum.
        t0 = L(11, 9, 12)
        prices = hourly(t0, [0.2, 0.2, 0.1, 0.6, 0.4, 0.4, 0.3, 0.2, 0.4, 0.4, 0.5, 0.4, 0.5, 0.3, 0.1, 0.1, 0.4, 0.1, 0.4, 0.4])
        powers = power_slots(t0, 24, expected=7.0, conservative=5.6)
        config = m.PlannerConfig(above_minimum=True)

        def at(now, xc40=33, xc40_drawing=False, ex30_drawing=False, commitment=None, **xc40_extra):
            requests = [car(XC40, xc40, 90, L(11, 10, 8), car_limit=100, drawing=xc40_drawing, **xc40_extra),
                        car(EX30, 80, 80, L(11, 10, 7, 30), reached=True, drawing=ex30_drawing)]
            return plan(now, requests, prices=prices, powers=powers, config=config, commitment=commitment)

        p = at(L(11, 9, 16, 30))
        last_minimum = max(seg.end for seg in segs(p, 'smartevse_1', top_up=False))
        ex30 = segs(p, 'smartevse_2')
        self.assertTrue(ex30)  # the EX30 still gets its extra charge, later
        self.assertTrue(all(seg.top_up and seg.start >= last_minimum - Q for seg in ex30))
        self.assertEqual(p.car('smartevse_1').projected_soc, 90.0)
        # Alone, the EX30 would use the cheapest hours from now on.
        alone = plan(L(11, 9, 16, 30), [car(EX30, 80, 80, L(11, 10, 7, 30), reached=True)], prices=prices, powers=powers,
                     config=config)
        self.assertLess(min(seg.start for seg in alone.segments), min(seg.start for seg in ex30))
        # A car whose minimum cannot be fully planned leaves no extra charging before its ready-by.
        short = plan(L(11, 10, 3), [car(XC40, 33, 90, L(11, 10, 8), car_limit=100),
                                    car(EX30, 80, 80, L(11, 10, 7, 30), reached=True)],
                     prices=prices, powers=powers, config=config)
        self.assertIn(short.car('smartevse_1').status, ('at_risk', 'tight'))
        self.assertFalse(segs(short, 'smartevse_2'))
        # With the XC40's minimum in, both go on above it and nobody waits for the other.
        done = at(L(11, 9, 16, 30), xc40=90, reached=True)
        self.assertTrue(segs(done, 'smartevse_1', top_up=True))
        self.assertTrue(segs(done, 'smartevse_2', top_up=True))

    def test_running_block_continues_through_a_unit_boundary(self):
        # In the last seconds of a unit (too short to plan, the commitment has run out)
        # the charging in progress still continues into the next equally priced unit.
        t0 = L(11, 9, 12)
        prices = hourly(t0, [0.2, 0.2, 0.1, 0.6, 0.4, 0.4, 0.3, 0.2, 0.4, 0.4, 0.5, 0.4, 0.5, 0.3, 0.1, 0.1, 0.4, 0.1, 0.4, 0.4])
        powers = power_slots(t0, 24, expected=7.0, conservative=5.6)
        requests = [car(XC40, 35, 90, L(11, 10, 8), car_limit=100, drawing=True),
                    car(EX30, 80, 80, L(11, 10, 7, 30), reached=True)]
        config = m.PlannerConfig(above_minimum=True)
        commitment = m.Commitment('smartevse_1', L(11, 9, 16, 45))
        before = plan(L(11, 9, 16, 44), requests, prices=prices, powers=powers, config=config, commitment=commitment)
        self.assertEqual(before.current_outlet, 'smartevse_1')
        boundary = plan(L(11, 9, 16, 44, 30), requests, prices=prices, powers=powers, config=config, commitment=commitment)
        self.assertEqual([(seg.outlet, seg.start, seg.top_up) for seg in boundary.segments[:1]],
                         [('smartevse_1', L(11, 9, 16, 45), False)])

    def test_no_start_for_a_few_minutes_of_extra_charging(self):
        # 0.4 % below the car limit would be a block of its own of about two minutes in
        # the cheapest hour: not worth a start. While the car is charging it may finish.
        dl = L(11, 10, 7)
        idle = plan(L(11, 9, 18, 30), [car(XC40, 89.2, 80, dl)], config=m.PlannerConfig(above_minimum=True))
        self.assertEqual((idle.segments, idle.reason), ((), m.REASON_TARGETS_REACHED))
        self.assertIsNone(idle.car('smartevse_1').extra_soc)
        running = plan(L(11, 9, 23, 2), [car(XC40, 89.2, 80, dl, drawing=True)], config=m.PlannerConfig(above_minimum=True),
                       commitment=m.Commitment('smartevse_1', L(11, 9, 23, 15)))
        self.assertEqual(running.current_outlet, 'smartevse_1')
        # A longer remainder is planned as before.
        more = plan(L(11, 9, 18, 30), [car(XC40, 85, 80, dl)], config=m.PlannerConfig(above_minimum=True))
        self.assertTrue(segs(more, 'smartevse_1', top_up=True))

    def test_extra_charging_waits_for_prices_that_are_still_due(self):
        # 13:00, tomorrow's list is due at 15:00 and the ready-by is tomorrow morning:
        # the night may be cheaper than what is known now, so nothing extra is planned yet.
        t0 = L(11, 9, 13)
        prices = hourly(t0, [0.20] * 12)  # known to 01:00
        dl = L(11, 10, 7)
        waiting = m.PlannerConfig(above_minimum=True, extra_prices_expected_at=L(11, 9, 15))
        p = plan(t0, [car(XC40, 85, 80, dl)], prices=prices, config=waiting)
        self.assertEqual((p.segments, p.reason), ((), m.REASON_TARGETS_REACHED))
        # A car whose own window is fully priced does not wait for the other car's prices.
        both = plan(t0, [car(XC40, 85, 80, dl), car(EX30, 85, 80, L(11, 9, 20))], prices=prices, config=waiting)
        self.assertFalse(segs(both, 'smartevse_1'))
        self.assertTrue(segs(both, 'smartevse_2', top_up=True))
        # The prices are late (the time has passed): the known hours are used.
        late = plan(L(11, 9, 16), [car(XC40, 85, 80, dl)], prices=prices, config=waiting)
        self.assertTrue(segs(late, 'smartevse_1', top_up=True))
        self.assertTrue(all(s.price_kind == 'known' for s in late.segments))
        # The minimum never waits because of this setting.
        need = plan(t0, [car(XC40, 50, 80, L(11, 9, 17))], prices=prices, config=waiting)
        self.assertEqual(need.car('smartevse_1').projected_soc, 80.0)


class PriceKindTests(unittest.TestCase):
    def test_waits_for_tomorrows_prices_when_that_is_safe(self):
        # Round 5 (owner): EX30 10->100 % plugged in at 08:00, tomorrow not published yet
        # (expected 15:00). Known hours before 15:00 are not used while the evening's
        # known hours and the night can still deliver; nothing goes into forecast hours
        # the known hours do not need.
        forecast = hourly(L(11, 11, 1), [0.30, 0.28, 0.26, 0.30, 0.28, 0.36], kind='forecast')
        request = car(EX30, 10, 100, L(11, 11, 7))
        config = m.PlannerConfig(prices_expected_at=L(11, 10, 15))
        p = plan(L(11, 10, 8), [request], prices=price_slots() + forecast, config=config)
        self.assertTrue(p.segments)
        self.assertTrue(all(s.start >= L(11, 10, 15) for s in p.segments))
        self.assertTrue(all(s.price_kind == 'known' for s in p.segments))
        self.assertFalse(p.car('smartevse_2').includes_forecast)
        self.assertTrue(p.car('smartevse_2').feasible)
        self.assertIsNone(p.current_outlet)
        # Without a pending publication (prices are late) the cheapest known hours of the
        # day are used, still never a forecast hour that is not needed.
        late = plan(L(11, 10, 8), [request], prices=price_slots() + forecast)
        self.assertTrue(all(s.price_kind == 'known' for s in late.segments))
        self.assertTrue(any(s.start < L(11, 10, 15) for s in late.segments))
        # A time already past is ignored.
        past = plan(L(11, 10, 8), [request], prices=price_slots() + forecast,
                    config=m.PlannerConfig(prices_expected_at=L(11, 10, 7)))
        self.assertEqual([(s.start, s.end) for s in past.segments], [(s.start, s.end) for s in late.segments])

    def test_known_hours_before_the_publication_are_used_when_waiting_would_miss(self):
        # Ready by 20:00: waiting for 15:00 leaves 5 h for a 6.8 h need, so known hours
        # before 15:00 are used, but only what the minimum needs.
        request = car(EX30, 10, 100, L(11, 10, 20))
        config = m.PlannerConfig(prices_expected_at=L(11, 10, 15))
        p = plan(L(11, 10, 8), [request], prices=price_slots(), config=config)
        self.assertTrue(any(s.start < L(11, 10, 15) for s in p.segments))
        self.assertGreaterEqual(p.car('smartevse_2').projected_soc, 100.0 - m.COVER_TOLERANCE_PCT)
        self.assertFalse(p.car('smartevse_2').late)
        # With a later ready-by the same car waits for the prices entirely.
        relaxed = plan(L(11, 10, 8), [car(EX30, 40, 100, L(11, 10, 23))], prices=price_slots(), config=config)
        self.assertTrue(relaxed.segments)
        self.assertTrue(all(s.start >= L(11, 10, 15) for s in relaxed.segments))
        eager = plan(L(11, 10, 8), [car(EX30, 40, 100, L(11, 10, 23))], prices=price_slots())
        self.assertTrue(any(s.start < L(11, 10, 15) for s in eager.segments))

    def test_unpriced_hours_take_only_the_rest_and_as_late_as_possible(self):
        # Known prices end at 01:00; 21:00-01:00 holds most of a 40->100 % need.
        forecast = hourly(L(11, 11, 1), [0.05, 0.06, 0.04, 0.07, 0.30, 0.36], kind='forecast')
        request = car(EX30, 40, 100, L(11, 11, 7))
        p = plan(L(11, 10, 21), [request], prices=price_slots() + forecast)
        known = sum(s.minutes for s in p.segments if s.price_kind == 'known')
        self.assertAlmostEqual(known, 4 * 60, delta=0.5)  # 21:00-01:00, all of it
        unpriced = [s for s in p.segments if s.price_kind != 'known']
        self.assertTrue(unpriced)
        # As late as possible while the safety reserve before the deadline stays free,
        # whatever the forecast says (01:00-03:00 is forecast cheapest).
        latest = p.car('smartevse_2').latest_start
        self.assertTrue(all(s.start >= latest for s in unpriced))
        self.assertTrue(all(s.start >= L(11, 11, 3) for s in unpriced))
        self.assertGreaterEqual(p.car('smartevse_2').projected_soc, 100.0 - m.COVER_TOLERANCE_PCT)

    def test_unknown_hours_rank_after_every_known_hour(self):
        request = car(EX30, 10, 100, L(11, 11, 7))
        p = plan(L(11, 10, 8), [request])
        self.assertFalse(p.car('smartevse_2').partial)
        self.assertTrue(all(s.price_kind == 'known' for s in p.segments))
        big = plan(L(11, 10, 20), [car(EX30, 10, 100, L(11, 11, 7))])
        unknown = [s for s in big.segments if s.price_kind == 'unknown']
        self.assertTrue(unknown)
        self.assertTrue(big.car('smartevse_2').partial)
        known_minutes = sum(s.minutes for s in big.segments if s.price_kind == 'known')
        self.assertAlmostEqual(known_minutes, 5 * 60, delta=0.01)  # every known unit is used first

    def test_forecast_uplift_prefers_an_equal_known_price(self):
        t0 = L(11, 9, 22)
        for value in (0.30, -0.10):
            prices = hourly(t0, [value, 0.9]) + hourly(t0 + 2 * H, [value], kind='forecast')
            p = plan(t0, [car(XC40, 70, 75, t0 + 3 * H)], prices=prices, powers=power_slots(t0, 4))
            self.assertTrue(all(s.price_kind == 'known' for s in p.segments), value)

    def test_known_until_is_reported(self):
        self.assertEqual(plan(L(11, 9, 18, 30), [car(XC40, 53, 80, L(11, 10, 7))]).known_until, L(11, 11, 1))


class NoPriceModeTests(unittest.TestCase):
    def test_without_known_prices_charging_is_as_late_as_safe(self):
        dl = L(11, 10, 7)
        p = plan(L(11, 9, 18, 30), [car(XC40, 53, 80, dl)], prices=[])
        c = p.car('smartevse_1')
        self.assertTrue(p.no_price_mode)
        self.assertEqual(p.reason, m.REASON_WAITING)
        self.assertEqual(len(c.blocks), 1)
        # E_c at the conservative 8.5 kW (about 2 h 50 min) still fits before the deadline.
        conservative_start = dl - timedelta(hours=c.need_conservative_kwh / 8.5)
        self.assertLessEqual(c.blocks[0].start, conservative_start)
        self.assertGreater(c.blocks[0].start, L(11, 10, 3))
        self.assertTrue(c.partial)
        at_start = plan(c.blocks[0].start, [car(XC40, 53, 80, dl)], prices=[])
        self.assertEqual((at_start.current_outlet, at_start.reason), ('smartevse_1', m.REASON_NO_PRICE_DATA))

    def test_config_and_forecast_only_force_no_price_mode(self):
        request = car(XC40, 53, 80, L(11, 10, 7))
        self.assertTrue(plan(L(11, 9, 18, 30), [request], config=m.PlannerConfig(no_price_mode=True)).no_price_mode)
        forecast_only = price_slots(kind='forecast')
        p = plan(L(11, 9, 18, 30), [request], prices=forecast_only)
        self.assertTrue(p.no_price_mode)
        self.assertTrue(p.car('smartevse_1').includes_forecast)
        self.assertFalse(p.car('smartevse_1').partial)

    def test_running_car_keeps_charging_without_prices(self):
        p = plan(L(11, 10, 3), [car(XC40, 60, 80, L(11, 10, 7), drawing=True)], prices=[])
        self.assertEqual(p.current_outlet, 'smartevse_1')
        self.assertEqual(len(p.car('smartevse_1').blocks), 1)
        # The other car is still planned as late as safe, not straight after the running one.
        two = plan(L(11, 9, 22), [car(XC40, 60, 80, L(11, 10, 7), drawing=True), car(EX30, 60, 80, L(11, 10, 7))],
                   prices=[])
        self.assertEqual(two.current_outlet, 'smartevse_1')
        xc_end = max(s.end for s in segs(two, 'smartevse_1'))
        ex_start = min(s.start for s in segs(two, 'smartevse_2'))
        self.assertLess(xc_end, L(11, 9, 22, 0) + timedelta(hours=2))
        self.assertGreater(ex_start, xc_end + timedelta(hours=3))

    def test_no_top_up_in_no_price_mode(self):
        p = plan(L(11, 9, 18, 30), [car(XC40, 85, 80, L(11, 10, 7))],
                 config=m.PlannerConfig(above_minimum=True, no_price_mode=True))
        self.assertEqual(p.segments, ())


@unittest.skipIf(TALLINN is None, 'tzdata unavailable')
class DstAndGridTests(unittest.TestCase):
    def tallinn_prices(self, start, end, value=0.2, overrides=None):
        slots = []
        t = start.astimezone(UTC)
        while t < end.astimezone(UTC):
            slots.append(m.PriceSlot(t, t + H, (overrides or {}).get(t, value), 'known'))
            t += H
        return slots

    def test_fall_back_night_has_ten_hours(self):
        start = datetime(2026, 10, 24, 22, tzinfo=TALLINN)
        deadline = datetime(2026, 10, 25, 7, tzinfo=TALLINN)
        self.assertEqual(deadline.astimezone(UTC) - start.astimezone(UTC), timedelta(hours=10))
        second_three = datetime(2026, 10, 25, 1, tzinfo=UTC)  # 03:00 EET, after the clock went back
        prices = self.tallinn_prices(start, deadline, 0.3, {second_three: 0.01})
        powers = [m.PowerSlot(s.start, s.end, 10.0, 9.0) for s in prices]
        request = car(XC40, 10, 90, deadline, cap_kw=10.0, kwh_per_pct=1.1)  # ~9.3 h at 10 kW
        p = m.build_plan(start, prices, powers, [request])
        # ~9.1 h of charging: fits the 10-hour night (it would not fit 9 hours).
        self.assertGreater(p.car('smartevse_1').need_expected_kwh, 90.0)
        self.assertIn(p.car('smartevse_1').status, ('charging', 'tight'))
        self.assertTrue(p.car('smartevse_1').feasible)
        self.assertEqual(p.car('smartevse_1').projected_soc, 90.0)
        self.assertGreater(p.minutes_between('smartevse_1', second_three, second_three + H), 59.9)
        self.assertLessEqual(max(s.end for s in p.segments), deadline)

    def test_spring_forward_night_has_eight_hours(self):
        start = datetime(2027, 3, 27, 22, tzinfo=TALLINN)
        deadline = datetime(2027, 3, 28, 7, tzinfo=TALLINN)
        self.assertEqual(deadline.astimezone(UTC) - start.astimezone(UTC), timedelta(hours=8))
        prices = self.tallinn_prices(start, deadline)
        powers = [m.PowerSlot(s.start, s.end, 10.0, 9.0) for s in prices]
        p = m.build_plan(start, prices, powers, [car(XC40, 10, 90, deadline, cap_kw=10.0, kwh_per_pct=1.1)])
        self.assertEqual(p.car('smartevse_1').status, 'at_risk')
        self.assertAlmostEqual(sum(s.minutes for s in p.segments), 8 * 60)
        self.assertEqual(p.current_outlet, 'smartevse_1')

    def test_deadline_not_on_a_quarter(self):
        dl = L(11, 10, 6, 50)
        p = plan(L(11, 10, 4), [car(XC40, 60, 80, dl)])
        self.assertLessEqual(max(s.end for s in p.segments), L(11, 10, 6, 45))
        late = plan(L(11, 10, 6, 40), [car(XC40, 79, 80, dl)])
        # Only 06:40-06:45 is usable: too short for the margined need (late), enough for
        # the 0.75 kWh to reach 80 % (charging since review round 3, not at_risk).
        self.assertTrue(late.car('smartevse_1').late)
        self.assertEqual(late.car('smartevse_1').status, 'charging')
        later = plan(L(11, 10, 6, 40), [car(XC40, 75, 80, dl)])
        self.assertEqual(later.car('smartevse_1').status, 'at_risk')

    def test_grid_is_utc_quarters_for_any_input_zone(self):
        now = datetime(2026, 11, 9, 23, 7, tzinfo=TALLINN)
        p = plan(now, [car(XC40, 53, 80, L(11, 10, 7))])
        self.assertEqual(p.now, now.astimezone(UTC))
        for seg in p.segments:
            self.assertEqual(seg.start.tzinfo, UTC)
            self.assertEqual(m.floor15(seg.start), m.floor15(seg.end - timedelta(microseconds=1)))


class DeterminismAndOutputTests(unittest.TestCase):
    def test_identical_inputs_identical_output(self):
        dl = L(11, 10, 7)
        requests = [car(XC40, 53, 80, dl), car(EX30, 40, 80, dl)]
        config = m.PlannerConfig(above_minimum=True)
        first = plan(L(11, 9, 18, 31, 7), requests, config=config).as_dict()
        second = plan(L(11, 9, 18, 31, 7), requests, config=config).as_dict()
        self.assertEqual(first, second)
        shuffled_prices, shuffled_powers = price_slots(), list(POWERS)
        random.Random(6).shuffle(shuffled_prices)
        random.Random(7).shuffle(shuffled_powers)
        third = m.build_plan(L(11, 9, 18, 31, 7), shuffled_prices, shuffled_powers, requests, config).as_dict()
        self.assertEqual(first, third)

    def test_as_dict_is_json_friendly_and_rounded(self):
        p = plan(L(11, 9, 18, 31, 7), [car(XC40, 53, 80, L(11, 10, 7))], config=m.PlannerConfig(above_minimum=True))
        data = json.loads(json.dumps(p.as_dict()))
        self.assertEqual(data['reason'], m.REASON_WAITING)
        charger = data['cars']['smartevse_1']
        self.assertEqual(set(charger) >= {'need_kwh', 'status', 'feasible', 'estimated', 'blocks', 'next_start',
                                          'projected_soc', 'cost', 'includes_forecast', 'partial', 'top_up_cost'},
                         True)
        for block in charger['blocks']:
            self.assertEqual(datetime.fromisoformat(block['start']).second, 0)
        self.assertEqual(round(charger['cost'], 2), charger['cost'])
        self.assertEqual(data['now'], '2026-11-09T16:31:00+00:00')

    def test_minutes_between(self):
        p = plan(L(11, 9, 18, 30), [car(XC40, 53, 80, L(11, 10, 7))])
        self.assertAlmostEqual(p.minutes_between('smartevse_1', L(11, 9, 21), L(11, 9, 22)), 60)
        total = sum(s.minutes for s in p.segments)
        self.assertAlmostEqual(p.minutes_between('smartevse_1', L(11, 9, 18), L(11, 10, 8)), total, delta=0.05)

    def test_input_validation(self):
        naive = datetime(2026, 11, 9, 18, 30)
        with self.assertRaises(ValueError):
            plan(naive, [car(XC40, 53, 80, L(11, 10, 7))])
        with self.assertRaises(ValueError):
            plan(L(11, 9, 18, 30), [car(XC40, 53, 80, naive)])
        with self.assertRaises(ValueError):
            plan(L(11, 9, 18, 30), [car(XC40, 53, 80, L(11, 10, 7)), car(XC40, 40, 80, L(11, 10, 7))])

    def test_no_cars_and_garbage_numbers(self):
        p = plan(L(11, 9, 18, 30), [])
        self.assertEqual((p.reason, p.segments, p.cars), (m.REASON_NO_EV, (), ()))
        weird = car(XC40, float('nan'), 80, L(11, 10, 7), kwh_per_pct=-1, kwh_per_pct_conservative=float('inf'),
                    budget_kwh=10, cap_low_kw=99)
        c = plan(L(11, 9, 18, 30), [weird]).car('smartevse_1')
        self.assertTrue(c.estimated)
        self.assertEqual(c.need_kwh, 10.0)
        low = car(XC40, 53, 80, L(11, 10, 7), kwh_per_pct=-1)
        self.assertAlmostEqual(plan(L(11, 9, 18, 30), [low]).car('smartevse_1').need_kwh,
                               round(27 * m.DEFAULT_KWH_PER_PCT, 2))

    def test_zero_power_hours_are_not_planned(self):
        t0 = L(11, 9, 22)
        powers = [m.PowerSlot(t0 + k * H, t0 + (k + 1) * H, 0.0 if k == 1 else 11.0, 8.0) for k in range(4)]
        prices = hourly(t0, [0.4, 0.01, 0.3, 0.5])
        p = plan(t0, [car(XC40, 70, 75, t0 + 4 * H)], prices=prices, powers=powers)
        self.assertEqual(p.minutes_between('smartevse_1', t0 + H, t0 + 2 * H), 0)


class RobustnessTests(unittest.TestCase):
    """Odd inputs never raise, and every plan keeps the physical invariants."""

    STATUSES = {'reached', 'planned', 'charging', 'tight', 'at_risk', 'catch_up', 'done', 'excluded'}

    def test_random_inputs_keep_invariants(self):
        rng = random.Random(8)
        t0 = datetime(2026, 11, 9, 20, tzinfo=UTC)
        for _ in range(400):
            now = t0 + timedelta(minutes=rng.randint(0, 59), seconds=rng.choice([0, 0, 17, 59]))
            prices = []
            for k in range(rng.randint(1, 36)):
                if rng.random() < 0.1:
                    continue
                kind = rng.choice(['known'] * 6 + ['forecast', 'unknown'])
                price = None if kind == 'unknown' or rng.random() < 0.03 else round(rng.uniform(-0.2, 0.8), 3)
                prices.append(m.PriceSlot(t0 + k * H, t0 + (k + 1) * H, price, kind))
            powers = [m.PowerSlot(t0 + k * H, t0 + (k + 1) * H, rng.choice([0.0, 3.0, 7.5, 11.0, float('nan')]),
                                  rng.choice([0.0, 2.0, 8.5, 20.0])) for k in range(rng.randint(0, 38))]
            requests = []
            for j, base in enumerate([XC40, EX30][:rng.choice([0, 1, 2, 2])]):
                deadline = now + timedelta(minutes=rng.choice([-30, 0, 3, 10, 50, 120, 300, 600, 1800, 3500])
                                           + rng.choice([0, 0, 7]))
                requests.append(car(base, rng.choice([None, rng.uniform(0, 100), rng.uniform(40, 95)]),
                                    rng.choice([10, 50, 80, 100]), deadline,
                                    car_limit=rng.choice([None, 80, 90, 100]),
                                    kwh_per_pct=rng.choice([0.5, 0.75, 1.1, -1, 0]),
                                    cap_kw=rng.choice([0.0, 4.0, 10.3, 22.0]), cap_low_kw=rng.choice([3.0, 9.0, 50.0]),
                                    budget_kwh=rng.choice([None, 0, 12.0, 60.0]), done=rng.random() < 0.15,
                                    overrun=rng.random() < 0.2, drawing=rng.random() < 0.3,
                                    excluded=rng.random() < 0.1))
            commitment = None
            if requests and rng.random() < 0.4:
                commitment = m.Commitment(rng.choice(requests).outlet, m.floor15(now) + Q * rng.choice([0, 1, 2]))
            config = m.PlannerConfig(above_minimum=rng.random() < 0.7, no_price_mode=rng.random() < 0.1)
            p = m.build_plan(now, prices, powers, requests, config, commitment)
            self.assertEqual(p.as_dict(), m.build_plan(now, prices, powers, requests, config, commitment).as_dict())
            json.dumps(p.as_dict())
            now_utc = now.astimezone(UTC)
            ordered = sorted(p.segments, key=lambda seg: seg.start)
            for a, b in zip(ordered, ordered[1:]):
                self.assertLessEqual(a.end, b.start)  # one charger: never two cars at once
            used = {}
            for seg in p.segments:
                request = next(r for r in requests if r.outlet == seg.outlet)
                self.assertFalse(request.excluded)
                self.assertTrue(now_utc <= seg.start < seg.end <= request.deadline)
                self.assertEqual(m.floor15(seg.start), m.floor15(seg.end - timedelta(microseconds=1)))
                used[m.floor15(seg.start)] = used.get(m.floor15(seg.start), 0.0) + seg.minutes
                if seg.top_up:
                    self.assertTrue(config.above_minimum)
            for unit, minutes in used.items():
                self.assertLessEqual(minutes, (unit + Q - max(unit, now_utc)).total_seconds() / 60 + 1e-6)
            if p.segments and p.segments[0].start <= now_utc:
                self.assertEqual(p.current_outlet, p.segments[0].outlet)
                remaining = (m.floor15(now_utc) + Q - now_utc).total_seconds() / 60
                committed = commitment is not None and commitment.outlet == p.current_outlet and \
                    commitment.until > now_utc
                self.assertTrue(remaining >= 5 - 1e-6 or committed)
            else:
                self.assertIsNone(p.current_outlet)
            for c in p.cars:
                self.assertIn(c.status, self.STATUSES)
                if c.status in ('planned', 'charging', 'tight'):
                    # With extra charging planned the level decides what is "minimum".
                    energy = sum(seg.energy_kwh for seg in segs(p, c.outlet))
                    self.assertGreaterEqual(energy, c.need_expected_kwh - 2e-3)
                    if not c.top_up:
                        self.assertGreaterEqual(min_energy(p, c.outlet), c.need_expected_kwh - 2e-3)
                for value in (c.cost, c.top_up_cost):
                    self.assertTrue(value is None or round(value, 2) == value)

    def test_car_that_cannot_draw_power_does_not_block_the_other(self):
        dl = L(11, 10, 7)
        p = plan(L(11, 9, 18, 30), [car(XC40, 53, 80, dl), car(EX30, 40, 80, dl, cap_kw=0.0)])
        self.assertEqual(p.car('smartevse_2').status, 'at_risk')
        alone = plan(L(11, 9, 18, 30), [car(XC40, 53, 80, dl)])
        self.assertEqual(p.car('smartevse_1').blocks, alone.car('smartevse_1').blocks)

    def test_latest_start(self):
        dl = L(11, 10, 7)
        c = plan(L(11, 9, 18, 30), [car(XC40, 53, 80, dl)]).car('smartevse_1')
        hours = c.need_expected_kwh / 10.3
        self.assertAlmostEqual((dl - c.latest_start).total_seconds() / 3600, hours, delta=0.01)
        self.assertIsNone(plan(L(11, 9, 18, 30), [car(XC40, 85, 80, dl)]).car('smartevse_1').latest_start)


class ScheduleBaselineTests(unittest.TestCase):
    """The saving is compared with "Charge with schedule" (owner, 2026-11-11): the same
    energy charged in the schedule's windows, by its rules."""

    def setUp(self):
        self.t0 = L(11, 9, 18)
        # 18:00-24:00 dear, then the night: 00 0.30, 01 0.20, 02 0.10, 03 0.25, 04 0.15, 05-06 0.40.
        self.prices = hourly(self.t0, [0.50] * 6 + [0.30, 0.20, 0.10, 0.25, 0.15, 0.40, 0.40])
        self.powers = power_slots(self.t0, 13, expected=10.0)
        self.night = [(L(11, 10, 0), L(11, 10, 7))]

    def costs(self, sessions, **rules):
        rules.setdefault("windows", self.night)
        return m.schedule_baseline_costs(sessions, self.prices, self.powers, m.ScheduleRules(**rules))

    def test_one_car_charges_from_the_start_of_the_schedule_window(self):
        # Plugged in at 18:00, the schedule opens at midnight: 15 kWh at 10 kW = 00:00-01:30.
        result = self.costs([m.BaselineSession('smartevse_1', self.t0, 15.0, 10.0)])
        self.assertEqual(result, {'smartevse_1': round(10 * 0.30 + 5 * 0.20, 2)})
        # Charging at plug-in would have used the 0.50 evening hours.
        plug_in = m.baseline_costs([m.BaselineSession('smartevse_1', self.t0, 15.0, 10.0)], self.prices, self.powers)
        self.assertEqual(plug_in, {'smartevse_1': 7.5})
        # A car plugged in inside the window starts at once.
        late = self.costs([m.BaselineSession('smartevse_1', L(11, 10, 2, 30), 10.0, 10.0)])
        self.assertEqual(late, {'smartevse_1': round(5 * 0.10 + 5 * 0.25, 2)})

    def test_two_cars_change_over_by_the_duty_cycle_and_the_policy_names_the_first(self):
        cars = [m.BaselineSession('smartevse_1', self.t0, 10.0, 10.0), m.BaselineSession('smartevse_2', self.t0, 10.0, 10.0)]
        # 60-minute duty cycle, SmartEVSE 1 first: 1 has 00-01 (0.30), 2 has 01-02 (0.20).
        self.assertEqual(self.costs(cars, duty_minutes=60, first='smartevse_1'), {'smartevse_1': 3.0, 'smartevse_2': 2.0})
        self.assertEqual(self.costs(cars, duty_minutes=60, first='smartevse_2'), {'smartevse_1': 2.0, 'smartevse_2': 3.0})
        # 30 minutes: 1 has 00:00-00:30 and 01:00-01:30, 2 the half hours between.
        self.assertEqual(self.costs(cars, duty_minutes=30, first='smartevse_1'), {'smartevse_1': 2.5, 'smartevse_2': 2.5})
        # Without a duty cycle the first car finishes before the other starts; a car that
        # has its energy leaves the rotation, so the other gets all the time after it.
        uneven = [m.BaselineSession('smartevse_1', self.t0, 5.0, 10.0), m.BaselineSession('smartevse_2', self.t0, 20.0, 10.0)]
        self.assertEqual(self.costs(uneven, first='smartevse_1'),
                         {'smartevse_1': 1.5, 'smartevse_2': round(5 * 0.30 + 10 * 0.20 + 5 * 0.10, 2)})

    def test_the_price_option_charges_only_at_or_below_the_acceptable_price(self):
        car_ = [m.BaselineSession('smartevse_1', self.t0, 20.0, 10.0)]
        # Limit 0.20: 01:00-03:00 (0.20, 0.10); 03:00 (0.25) is skipped.
        self.assertEqual(self.costs(car_, price_limit=0.20), {'smartevse_1': round(10 * 0.20 + 10 * 0.10, 2)})
        # 30 kWh needs a third cheap hour: 04:00 (0.15).
        more = [m.BaselineSession('smartevse_1', self.t0, 30.0, 10.0)]
        self.assertEqual(self.costs(more, price_limit=0.20), {'smartevse_1': round(10 * 0.20 + 10 * 0.10 + 10 * 0.15, 2)})
        # Not enough cheap hours in the windows given: the schedule would not finish.
        self.assertEqual(self.costs([m.BaselineSession('smartevse_1', self.t0, 45.0, 10.0)], price_limit=0.20),
                         {'smartevse_1': None})
        # A price that is not published: whether the schedule charges there is not known.
        forecast = hourly(self.t0, [0.50] * 6) + hourly(L(11, 10, 0), [0.10] * 7, kind='forecast')
        gated = m.schedule_baseline_costs(car_, forecast, self.powers, m.ScheduleRules(self.night, price_limit=0.20))
        self.assertEqual(gated, {'smartevse_1': None})

    def test_energy_that_does_not_fit_tonight_continues_in_the_next_window(self):
        # 7 hours give 70 kWh; 80 kWh need the next night, whose prices are not known.
        big = [m.BaselineSession('smartevse_1', self.t0, 80.0, 10.0)]
        two_nights = self.night + [(L(11, 11, 0), L(11, 11, 7))]
        self.assertEqual(self.costs(big, windows=two_nights), {'smartevse_1': None})
        prices = self.prices + hourly(L(11, 10, 7), [0.60] * 17) + hourly(L(11, 11, 0), [0.05] * 7)
        result = m.schedule_baseline_costs(big, prices, power_slots(self.t0, 40, expected=10.0), m.ScheduleRules(two_nights))
        night_one = 10 * (0.30 + 0.20 + 0.10 + 0.25 + 0.15 + 0.40 + 0.40)
        self.assertEqual(result, {'smartevse_1': round(night_one + 10 * 0.05, 2)})
        # No schedule window at all: nothing can be compared.
        self.assertEqual(self.costs(big, windows=[]), {'smartevse_1': None})

    def test_power_limit_taper_zero_energy_and_several_windows_of_one_outlet(self):
        slow = [m.PowerSlot(self.t0, self.t0 + 13 * H, 5.0, 4.0)]
        result = m.schedule_baseline_costs([m.BaselineSession('smartevse_1', self.t0, 7.5, 10.0)], self.prices, slow,
                                           m.ScheduleRules(self.night))
        self.assertEqual(result, {'smartevse_1': round(5 * 0.30 + 2.5 * 0.20, 2)})
        self.assertEqual(self.costs([m.BaselineSession('smartevse_1', self.t0, 0.0, 10.0)]), {'smartevse_1': 0.0})
        self.assertEqual(self.costs([m.BaselineSession('smartevse_1', self.t0, 5.0, 0.0)]), {'smartevse_1': None})
        flat = self.costs([m.BaselineSession('smartevse_1', self.t0, 12.0, 10.0)])['smartevse_1']
        taper = self.costs([m.BaselineSession('smartevse_1', self.t0, 12.0, 10.0, soc=84.0, kwh_per_pct=0.75)])['smartevse_1']
        self.assertLess(taper, flat)  # the taper stretches the charge into the cheaper 02:00 hour
        # The realised ledger keys sessions by window; the charger decides who starts.
        keyed = [m.BaselineSession('window-a', self.t0, 10.0, 10.0, charger='smartevse_2'),
                 m.BaselineSession('window-b', self.t0, 10.0, 10.0, charger='smartevse_1')]
        self.assertEqual(self.costs(keyed, duty_minutes=60, first='smartevse_1'), {'window-a': 2.0, 'window-b': 3.0})

    def test_random_sessions_keep_the_schedule_rules(self):
        rng = random.Random(11)
        for _ in range(60):
            energies = [rng.choice([0.0, 3.0, 12.0, 25.0, 40.0]) for _ in range(2)]
            sessions = [m.BaselineSession(f'smartevse_{i + 1}', self.t0 + timedelta(minutes=rng.choice([0, 95, 400, 520])),
                                          energy, rng.choice([7.0, 10.0, 11.0])) for i, energy in enumerate(energies)]
            rules = m.ScheduleRules(self.night, price_limit=rng.choice([None, None, 0.25]),
                                    duty_minutes=rng.choice([0, 15, 45]), first=rng.choice(['smartevse_1', 'smartevse_2']))
            result = m.schedule_baseline_costs(sessions, self.prices, self.powers, rules)
            self.assertEqual(result, m.schedule_baseline_costs(sessions, self.prices, self.powers, rules))
            for session in sessions:
                cost = result[session.outlet]
                if cost is None:
                    continue
                # Never cheaper than the cheapest night price, never dearer than the dearest.
                self.assertGreaterEqual(cost, round(session.energy_kwh * 0.10, 2) - 0.01)
                self.assertLessEqual(cost, round(session.energy_kwh * 0.40, 2) + 0.01)
                if rules.price_limit is not None:
                    self.assertLessEqual(cost, round(session.energy_kwh * 0.25, 2) + 0.01)


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.t0 = L(11, 9, 18)
        self.prices = hourly(self.t0, [0.40, 0.30, 0.20, 0.10])
        self.powers = power_slots(self.t0, 4)

    def test_sequential_sessions_first_come_first_served(self):
        sessions = [m.BaselineSession('smartevse_2', self.t0 + timedelta(minutes=30), 5.0, 10.0),
                    m.BaselineSession('smartevse_1', self.t0, 10.0, 10.0)]
        result = m.baseline_costs(sessions, self.prices, self.powers)
        self.assertEqual(result, {'smartevse_1': 4.0, 'smartevse_2': 1.5})
        late = [m.BaselineSession('smartevse_1', self.t0, 10.0, 10.0),
                m.BaselineSession('smartevse_2', self.t0 + timedelta(hours=2, minutes=45), 5.0, 10.0)]
        self.assertEqual(m.baseline_costs(late, self.prices, self.powers)['smartevse_2'], 2.5 * 0.2 + 2.5 * 0.1)

    def test_power_is_household_limited_and_spans_hours(self):
        powers = [m.PowerSlot(self.t0 + k * H, self.t0 + (k + 1) * H, 5.0, 4.0) for k in range(4)]
        result = m.baseline_costs([m.BaselineSession('smartevse_1', self.t0, 7.5, 10.0)], self.prices, powers)
        self.assertEqual(result['smartevse_1'], round(5 * 0.4 + 2.5 * 0.3, 2))

    def test_unpriced_or_forecast_energy(self):
        prices = hourly(self.t0, [0.40]) + hourly(self.t0 + H, [0.30], kind='forecast')
        session = [m.BaselineSession('smartevse_1', self.t0, 15.0, 10.0)]
        self.assertIsNone(m.baseline_costs(session, prices, self.powers)['smartevse_1'])
        self.assertEqual(m.baseline_costs(session, prices, self.powers, allow_forecast=True)['smartevse_1'],
                         round(4.0 + 1.5, 2))
        self.assertIsNone(m.baseline_costs(session, hourly(self.t0, [0.4]), self.powers)['smartevse_1'])

    def test_taper_slows_the_baseline(self):
        flat = m.baseline_costs([m.BaselineSession('smartevse_1', self.t0 + timedelta(minutes=45), 4.0, 10.0)],
                                self.prices, self.powers)['smartevse_1']
        taper = m.baseline_costs([m.BaselineSession('smartevse_1', self.t0 + timedelta(minutes=45), 4.0, 10.0,
                                                    soc=95.0, kwh_per_pct=0.75)], self.prices, self.powers)
        self.assertGreater(flat, taper['smartevse_1'])  # slower charging reaches the cheaper hour

    def test_zero_energy_and_never_finishing(self):
        zero = m.baseline_costs([m.BaselineSession('smartevse_1', self.t0, 0.0, 10.0)], self.prices, self.powers)
        self.assertEqual(zero, {'smartevse_1': 0.0})
        dead = [m.PowerSlot(self.t0, self.t0 + 4 * H, 0.0, 0.0)]
        stuck = m.baseline_costs([m.BaselineSession('smartevse_1', self.t0, 5.0, 10.0),
                                  m.BaselineSession('smartevse_2', self.t0, 5.0, 10.0)], self.prices, dead)
        self.assertEqual(stuck, {'smartevse_1': None, 'smartevse_2': None})


if __name__ == '__main__':
    unittest.main()
