"""Intelligent charging planner for one charger shared by up to two cars.

Pure standard library: no Home Assistant imports and no relative imports, so
tests and tools can load this file by path.

Energy-capacity formulation
---------------------------
The window from ``floor15(now)`` to the latest deadline is cut into 15-minute
units on the UTC grid. A unit has an energy capacity: the kWh it can deliver
at the expected (or conservative) charging power for its usable minutes. A car
needs a "virtual" energy, the kWh it would take at full power (charging time
times power), from its SoC to its minimum, including the taper prior, a margin
and the start overhead. Only one car charges at a time, and a unit can serve a
car only when it ends by that car's deadline.

``D(S)``, the most energy units ``S`` can deliver to the cars, is a max-flow
value with nested eligibility, which makes it a polymatroid rank function.
Edmonds' greedy (units by price, marginal gain ``D(S + u) - D(S)``) therefore
gives the cheapest plan, also with partly used units and negative prices, and
chronological earliest-deadline-first filling turns the selected capacities
into a schedule. Everything else (commitment, protection of cars that can
still make it, "most cars at minimum", the clamped safety buffer, merging of
short isolated blocks, top-up) is layered on top of that core.

All datetimes must be timezone-aware. They are compared and returned in UTC.
Identical inputs always give an identical plan.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

UTC = timezone.utc

# --- Constants (spec 7.6) -------------------------------------------------
UNIT_MINUTES = 15
MIN_START_MINUTES = 5
START_PENALTY_EUR = 0.10
EXPECTED_MARGIN = 0.03
EXPECTED_OVERHEAD_MIN = 3
CONSERVATIVE_MARGIN = 0.10
CONSERVATIVE_OVERHEAD_MIN = 10
FORECAST_UPLIFT = 0.10  # unused since round 5: forecasts no longer rank units
UNKNOWN_RANK_ADD = 0.05  # unused since round 5
# Round 5 (owner decision 2026-11-10): plan in hours with known prices. An hour without
# a known price (forecast or unknown) is used only for energy the known hours cannot
# deliver before the deadline, as late as possible. While tomorrow's prices are still
# expected (PlannerConfig.prices_expected_at), known hours before that time are used
# only when waiting for the prices would miss the minimum.
RANK_TIER_UNPRICED = 1000.0
RANK_TIER_BEFORE_PRICES = 2000.0
RANK_LATE_STEP = 0.001  # per hour before the plan horizon: later unpriced hours first
TAIL_MERGE_MAX_MINUTES = 30

# Supporting constants.
MIN_COMMITTED_MINUTES = 1  # the committed car may use unit 0 down to 1 minute
# A start inside a unit gets at least MIN_START_MINUTES plus this many minutes
# there, so it still fits unit 0 when the next cycle (seconds later) replans
# after its start time (see _start_pad).
START_PAD_EXTRA_MINUTES = 1
# A car whose block starts this soon inside the current unit owns the outlet
# now: starting up to a minute early in the same unit costs nothing, while a
# cycle just after the start would find less than MIN_START_MINUTES left.
START_LEAD = timedelta(seconds=60)
# A late car projected within this many points of its target shows "tight", not
# "at_risk" (half a point: what the card rounds to the target).
COVER_TOLERANCE_PCT = 0.5
COMMIT_SEARCH_STEPS = 24  # bisection steps for the committed share of unit 0
EXPECTED_TAPER_START_SOC = 93.0  # expected taper prior: full power up to 93 %
EXPECTED_TAPER_END_RATIO = 0.5  # ... then linear down to 50 % power at 100 %
CONSERVATIVE_TAPER_OFFSET_W = 1000.0  # evcc: taper from 100 - (P_W - 1000) / 980 %
CONSERVATIVE_TAPER_W_PER_PCT = 980.0
CONSERVATIVE_TAPER_END_KW = 1.0  # evcc: linear down to 1 kW at 100 %
DEFAULT_BUDGET_KWH = 30.0  # unknown SoC and no budget given
DEFAULT_KWH_PER_PCT = 0.7875  # 75 kWh / 100 * 1.05
MAX_PLAN_PASSES = 3  # passes to settle the per-block start overhead
MAX_HORIZON_HOURS = 48  # planning never looks further than this
BASELINE_MAX_HOURS = 168  # a baseline session that never finishes is unpriced

UNIT = timedelta(minutes=UNIT_MINUTES)
_EPS = 1e-9
_MIN_EPS = 1e-6  # minutes
_REL_TOL = 1e-7

PRICE_KNOWN = "known"
PRICE_FORECAST = "forecast"
PRICE_UNKNOWN = "unknown"
PRICE_KINDS = (PRICE_KNOWN, PRICE_FORECAST, PRICE_UNKNOWN)

STATUS_REACHED = "reached"
STATUS_PLANNED = "planned"
STATUS_CHARGING = "charging"
STATUS_TIGHT = "tight"
STATUS_AT_RISK = "at_risk"
STATUS_CATCH_UP = "catch_up"
STATUS_DONE = "done"
STATUS_EXCLUDED = "excluded"

REASON_CHARGING = "intelligent_charging"
REASON_TOP_UP = "intelligent_top_up"
REASON_CATCH_UP = "intelligent_catch_up"
REASON_NO_PRICE_DATA = "intelligent_no_price_data"
REASON_WAITING = "intelligent_waiting_for_planned_slot"
REASON_TARGETS_REACHED = "intelligent_targets_reached"
REASON_NO_EV = "intelligent_waiting_for_connected_ev"

# Segment kinds (how a car got the minutes of a unit).
KIND_COMMITTED = "committed"
KIND_OVERRUN = "overrun"
KIND_MINIMUM = "minimum"
KIND_LATE = "late"
KIND_TOP_UP = "top_up"


# --- Public data types (spec 7.7) -----------------------------------------
@dataclass(frozen=True)
class PriceSlot:
    """Price of ``[start, end)`` in currency/kWh; ``kind`` known|forecast|unknown."""

    start: datetime
    end: datetime
    price: float | None
    kind: str = PRICE_KNOWN


@dataclass(frozen=True)
class PowerSlot:
    """Charging power Smart mode is expected to offer in ``[start, end)``."""

    start: datetime
    end: datetime
    expected_kw: float
    conservative_kw: float


@dataclass(frozen=True)
class CarRequest:
    """One connected car. ``target_soc`` is the effective minimum in %.

    ``excluded`` (an addition to the spec field list, default False) marks an
    outlet that charge policy keeps out of the plan; it gets status
    ``excluded`` and no energy.
    ``reached`` (an addition, default False) marks a car that reached its
    minimum in this session (the §8.4 hysteresis): it has no minimum need but
    may still be topped up from max(soc, target) to its car limit (§7.5 step 10).
    ``done`` is for cars that get nothing at all (complete, not accepting,
    unavailable).
    """

    outlet: str
    vehicle: str | None
    settings_key: str
    name: str | None
    soc: float | None
    soc_source: str
    target_soc: float
    car_limit: float | None
    deadline: datetime
    kwh_per_pct: float
    kwh_per_pct_conservative: float
    cap_kw: float
    cap_low_kw: float
    budget_kwh: float | None
    done: bool
    overrun: bool
    drawing: bool
    priority: int
    excluded: bool = False
    reached: bool = False


@dataclass(frozen=True)
class Commitment:
    """The outlet started for the current unit keeps it until ``until``."""

    outlet: str
    until: datetime


@dataclass(frozen=True)
class PlannerConfig:
    top_up_price: float | None = None
    no_price_mode: bool = False
    # When the missing prices are expected to be published (tomorrow's day-ahead
    # prices); None when nothing is pending or the time has passed.
    prices_expected_at: datetime | None = None


@dataclass(frozen=True)
class Block:
    start: datetime
    end: datetime
    top_up: bool = False


@dataclass(frozen=True)
class Segment:
    """Minutes of one unit given to one car (``energy_kwh`` = planned capacity)."""

    outlet: str
    start: datetime
    end: datetime
    minutes: float
    energy_kwh: float
    top_up: bool
    kind: str
    price: float | None
    price_kind: str


@dataclass(frozen=True)
class CarPlan:
    outlet: str
    need_kwh: float
    status: str
    feasible: bool
    estimated: bool
    blocks: tuple[Block, ...]
    next_start: datetime | None
    projected_soc: float | None
    cost: float | None
    includes_forecast: bool
    partial: bool
    top_up_cost: float | None
    # Additions to the spec field list (all derived from the plan).
    energy_kwh: float = 0.0
    top_up_kwh: float = 0.0
    need_expected_kwh: float = 0.0
    need_conservative_kwh: float = 0.0
    top_up: bool = False
    tight: bool = False
    latest_start: datetime | None = None
    # Left out of the on-time set (R11): its margined need does not fit. It may
    # still show "tight"/"charging" when the plan covers its unmargined minimum.
    late: bool = False

    def as_dict(self) -> dict:
        return {
            "outlet": self.outlet,
            "need_kwh": self.need_kwh,
            "status": self.status,
            "feasible": self.feasible,
            "estimated": self.estimated,
            "blocks": [
                {"start": _iso(b.start), "end": _iso(b.end), "top_up": b.top_up}
                for b in self.blocks
            ],
            "next_start": _iso(self.next_start),
            "projected_soc": self.projected_soc,
            "cost": self.cost,
            "includes_forecast": self.includes_forecast,
            "partial": self.partial,
            "top_up_cost": self.top_up_cost,
            "energy_kwh": self.energy_kwh,
            "top_up_kwh": self.top_up_kwh,
            "need_expected_kwh": self.need_expected_kwh,
            "need_conservative_kwh": self.need_conservative_kwh,
            "top_up": self.top_up,
            "tight": self.tight,
            "latest_start": _iso(self.latest_start),
            "late": self.late,
        }


@dataclass(frozen=True)
class Plan:
    now: datetime
    reason: str
    current_outlet: str | None
    cars: tuple[CarPlan, ...]
    segments: tuple[Segment, ...]
    order: tuple[str, ...]
    next_start: datetime | None
    no_price_mode: bool
    known_until: datetime | None
    total_cost: float | None
    total_top_up_cost: float | None
    total_energy_kwh: float
    includes_forecast: bool
    partial: bool

    def car(self, outlet: str) -> CarPlan | None:
        for car in self.cars:
            if car.outlet == outlet:
                return car
        return None

    def minutes_between(self, outlet: str, start: datetime, end: datetime) -> float:
        """Planned charging minutes of ``outlet`` inside ``[start, end)``."""
        start, end = _utc(start), _utc(end)
        total = 0.0
        for seg in self.segments:
            if seg.outlet != outlet:
                continue
            overlap = (min(seg.end, end) - max(seg.start, start)).total_seconds()
            if overlap > 0:
                total += overlap / 60
        return total

    def as_dict(self) -> dict:
        return {
            "now": _iso(self.now),
            "reason": self.reason,
            "current_outlet": self.current_outlet,
            "no_price_mode": self.no_price_mode,
            "known_until": _iso(self.known_until),
            "next_start": _iso(self.next_start),
            "order": list(self.order),
            "totals": {
                "cost": self.total_cost,
                "top_up_cost": self.total_top_up_cost,
                "energy_kwh": self.total_energy_kwh,
                "includes_forecast": self.includes_forecast,
                "partial": self.partial,
            },
            "cars": {car.outlet: car.as_dict() for car in self.cars},
            "segments": [
                {
                    "outlet": s.outlet,
                    "start": _iso(s.start),
                    "end": _iso(s.end),
                    "energy_kwh": round(s.energy_kwh, 3),
                    "top_up": s.top_up,
                    "kind": s.kind,
                }
                for s in self.segments
            ],
        }


@dataclass(frozen=True)
class BaselineSession:
    """One plug session for the "charging at plug-in" baseline (spec 7.8).

    ``energy_kwh`` is the baseline energy: energy delivered so far in the
    window plus the remaining planned minimum energy (top-up excluded).
    ``soc``/``kwh_per_pct`` (SoC at plug-in) enable the taper prior.
    """

    outlet: str
    plugged_at: datetime
    energy_kwh: float
    cap_kw: float
    soc: float | None = None
    kwh_per_pct: float | None = None


# --- Small helpers ----------------------------------------------------------
def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime; the planner needs timezone-aware datetimes")
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value.astimezone(UTC)
    rounded = value.replace(second=0, microsecond=0)
    if value - rounded >= timedelta(seconds=30):
        rounded += timedelta(minutes=1)
    return rounded.isoformat()


def _finite(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _money(value: float | None) -> float | None:
    if value is None:
        return None
    result = round(value + 0.0, 2)
    return 0.0 if result == 0 else result


def _start_pad() -> float:
    """Minutes a start inside a unit gets there (0 when the start rule is off)."""
    return MIN_START_MINUTES + START_PAD_EXTRA_MINUTES if MIN_START_MINUTES > 0 else 0.0


def floor15(value: datetime) -> datetime:
    """Start of the 15-minute UTC grid unit containing ``value``."""
    value = _utc(value)
    return value.replace(minute=value.minute - value.minute % UNIT_MINUTES, second=0, microsecond=0)


class _Taper:
    """Power ratio vs SoC: 1.0 up to ``start``, then linear to ``end_ratio`` at 100 %."""

    __slots__ = ("start", "end_ratio", "alpha")

    def __init__(self, start: float, end_ratio: float) -> None:
        self.start = _clamp(start, 0.0, 100.0)
        self.end_ratio = _clamp(end_ratio, 1e-3, 1.0)
        span = 100.0 - self.start
        self.alpha = (1.0 - self.end_ratio) / span if span > _EPS else 0.0

    def ratio(self, soc: float) -> float:
        if soc <= self.start or self.alpha <= 0:
            return 1.0
        return max(self.end_ratio, 1.0 - self.alpha * (min(soc, 100.0) - self.start))

    def virtual(self, kwh_per_pct: float, low: float, high: float) -> float:
        """kWh at full power needed to go from ``low`` to ``high`` %."""
        if high <= low:
            return 0.0
        total = 0.0
        if low < self.start:
            flat_end = min(high, self.start)
            total += (flat_end - low) * kwh_per_pct
            low = flat_end
        if high > low:
            if self.alpha <= 0:
                total += (high - low) * kwh_per_pct
            else:
                total += kwh_per_pct / self.alpha * (
                    math.log(self.ratio(low)) - math.log(self.ratio(high))
                )
        return total

    def advance(self, kwh_per_pct: float, soc: float, virtual_kwh: float, limit: float) -> float:
        """SoC after spending ``virtual_kwh`` of full-power energy from ``soc``."""
        if virtual_kwh <= 0 or soc >= limit:
            return soc
        if soc < self.start:
            flat_end = min(self.start, limit)
            cost = (flat_end - soc) * kwh_per_pct
            if virtual_kwh <= cost:
                return soc + virtual_kwh / kwh_per_pct
            virtual_kwh -= cost
            soc = flat_end
            if soc >= limit:
                return limit
        if self.alpha <= 0:
            return min(limit, soc + virtual_kwh / kwh_per_pct)
        if virtual_kwh >= self.virtual(kwh_per_pct, soc, limit):
            return limit
        ratio = self.ratio(soc) * math.exp(-self.alpha * virtual_kwh / kwh_per_pct)
        return min(limit, self.start + (1.0 - ratio) / self.alpha)


EXPECTED_TAPER = _Taper(EXPECTED_TAPER_START_SOC, EXPECTED_TAPER_END_RATIO)
NO_TAPER = _Taper(100.0, 1.0)


def conservative_taper(power_kw: float) -> _Taper:
    """evcc's linear prior: full power to 100 - (P_W - 1000)/980 %, then to 1 kW."""
    if power_kw <= CONSERVATIVE_TAPER_END_KW:
        return NO_TAPER
    start = 100.0 - (power_kw * 1000.0 - CONSERVATIVE_TAPER_OFFSET_W) / CONSERVATIVE_TAPER_W_PER_PCT
    return _Taper(start, CONSERVATIVE_TAPER_END_KW / power_kw)


# --- Slot preparation ---------------------------------------------------------
class _Price:
    __slots__ = ("start", "end", "price", "kind")

    def __init__(self, start, end, price, kind):
        self.start, self.end, self.price, self.kind = start, end, price, kind


def _prepare_prices(price_slots: Iterable[PriceSlot]) -> list[_Price]:
    out = []
    for slot in price_slots or ():
        start, end = _utc(slot.start), _utc(slot.end)
        if end <= start:
            continue
        price = _finite(slot.price)
        kind = slot.kind if slot.kind in PRICE_KINDS else PRICE_KNOWN
        if price is None or kind == PRICE_UNKNOWN:
            price, kind = None, PRICE_UNKNOWN
        out.append(_Price(start, end, price, kind))
    # Most trustworthy first, then the most specific slot.
    rank = {PRICE_KNOWN: 0, PRICE_FORECAST: 1, PRICE_UNKNOWN: 2}
    out.sort(key=lambda p: (rank[p.kind], p.end - p.start, p.start, p.price if p.price is not None else 0.0))
    return out


def _price_at(prices: list[_Price], moment: datetime) -> tuple[float | None, str]:
    for slot in prices:
        if slot.start <= moment < slot.end:
            return slot.price, slot.kind
    return None, PRICE_UNKNOWN


class _Power:
    __slots__ = ("start", "end", "expected", "conservative")

    def __init__(self, start, end, expected, conservative):
        self.start, self.end, self.expected, self.conservative = start, end, expected, conservative


def _prepare_powers(power_slots: Iterable[PowerSlot]) -> list[_Power]:
    out = []
    for slot in power_slots or ():
        start, end = _utc(slot.start), _utc(slot.end)
        if end <= start:
            continue
        expected = max(0.0, _finite(slot.expected_kw) or 0.0)
        conservative = _clamp(_finite(slot.conservative_kw) or 0.0, 0.0, expected)
        out.append(_Power(start, end, expected, conservative))
    out.sort(key=lambda p: (p.start, p.end))
    return out


def _power_at(powers: list[_Power], moment: datetime) -> tuple[float, float]:
    """Covering slot, else the nearest earlier one, else the nearest later one."""
    if not powers:
        return math.inf, math.inf
    covering = [p for p in powers if p.start <= moment < p.end]
    if covering:
        slot = covering[-1]
        return slot.expected, slot.conservative
    earlier = [p for p in powers if p.end <= moment]
    slot = earlier[-1] if earlier else powers[0]
    return slot.expected, slot.conservative


# --- Planning internals -------------------------------------------------------
class _Unit:
    __slots__ = (
        "index", "start", "end", "minutes", "price", "kind", "rank",
        "kw_e", "kw_c", "usable_all", "usable_committed",
    )


class _Alloc:
    __slots__ = ("outlet", "minutes", "kind", "energy")

    def __init__(self, outlet, minutes, kind, energy):
        self.outlet, self.minutes, self.kind, self.energy = outlet, minutes, kind, energy


class _Car:
    """Sanitised request plus per-pass needs."""

    __slots__ = (
        "req", "index", "outlet", "deadline", "soc", "target", "limit", "k_e", "k_c",
        "cap", "cap_low", "p_ref", "budget", "done", "reached", "overrun", "drawing", "priority",
        "excluded", "committed", "tie", "e_actual", "e_e", "e_c", "e_v", "top_need",
        "rem_e", "rem_c", "scale_e", "scale_c",
    )

    def edf(self):
        return (self.deadline, self.tie)


def _sanitise(req: CarRequest, index: int, commit_outlet: str | None) -> _Car:
    car = _Car()
    car.req = req
    car.index = index
    car.outlet = req.outlet
    car.deadline = _utc(req.deadline)
    soc = _finite(req.soc)
    car.soc = None if soc is None else _clamp(soc, 0.0, 100.0)
    car.target = _clamp(_finite(req.target_soc) or 0.0, 0.0, 100.0)
    limit = _finite(req.car_limit)
    car.limit = None if limit is None else _clamp(limit, 0.0, 100.0)
    k_e = _finite(req.kwh_per_pct)
    car.k_e = k_e if k_e is not None and k_e > 0 else DEFAULT_KWH_PER_PCT
    k_c = _finite(req.kwh_per_pct_conservative)
    car.k_c = k_c if k_c is not None and k_c > 0 else car.k_e
    cap = _finite(req.cap_kw)
    car.cap = cap if cap is not None and cap > 0 else 0.0
    cap_low = _finite(req.cap_low_kw)
    car.cap_low = car.cap if cap_low is None else _clamp(cap_low, 0.0, car.cap)
    car.p_ref = car.cap
    budget = _finite(req.budget_kwh)
    car.budget = DEFAULT_BUDGET_KWH if budget is None else max(0.0, budget)
    car.done = bool(req.done)
    car.reached = bool(getattr(req, "reached", False)) and not car.done
    car.overrun = bool(req.overrun)
    car.drawing = bool(req.drawing)
    priority = _finite(req.priority)
    car.priority = 0.0 if priority is None else priority
    car.excluded = bool(getattr(req, "excluded", False))
    car.committed = commit_outlet is not None and req.outlet == commit_outlet
    car.tie = (0 if (car.drawing or car.committed) else 1, car.priority, car.outlet)
    return car


def _set_needs(car: _Car, start_blocks: int) -> None:
    """Spec 7.3: actual, expected (E_e) and conservative (E_c) energy needs."""
    car.e_actual = car.e_e = car.e_c = car.e_v = car.top_need = 0.0
    if car.excluded or car.done or car.reached:
        pass  # a reached car keeps only its top-up need (below)
    elif car.soc is None:
        car.e_actual = car.budget
        car.e_e = car.e_v = car.e_actual
        car.e_c = car.e_actual * (1.0 + CONSERVATIVE_MARGIN)
    elif car.soc < car.target:
        car.e_actual = (car.target - car.soc) * car.k_e
        virtual_e = EXPECTED_TAPER.virtual(car.k_e, car.soc, car.target)
        virtual_c = conservative_taper(car.p_ref).virtual(car.k_c, car.soc, car.target)
        car.e_v = virtual_e  # without margin and start overhead: "can it still make it"
        car.e_e = (
            virtual_e * (1.0 + EXPECTED_MARGIN)
            + car.p_ref * EXPECTED_OVERHEAD_MIN / 60.0 * max(1, start_blocks)
        )
        car.e_c = virtual_c * (1.0 + CONSERVATIVE_MARGIN) + car.p_ref * CONSERVATIVE_OVERHEAD_MIN / 60.0
    if (
        not car.excluded
        and not car.done
        and car.soc is not None
        and car.limit is not None
    ):
        base = max(car.soc, car.target)
        if car.limit > base + _EPS:
            car.top_need = EXPECTED_TAPER.virtual(car.k_e, base, car.limit)
    car.rem_e = car.e_e
    car.rem_c = car.e_c


class _Nested:
    """D(S) for cars with nested eligibility: unit u serves car k iff u.end <= d_k.

    D(S) = min(total need, min_k [cap(S, end <= d_k) + sum of later needs]).
    """

    __slots__ = ("deadlines", "total", "suffix", "cap")

    def __init__(self, cars: Sequence[_Car], needs: Sequence[float]) -> None:
        self.deadlines = [car.deadline for car in cars]
        self.total = sum(needs)
        self.suffix = [sum(needs[k + 1:]) for k in range(len(needs))]
        self.cap = [0.0] * len(needs)

    def value(self, end: datetime | None = None, amount: float = 0.0) -> float:
        best = self.total
        for k, deadline in enumerate(self.deadlines):
            cap = self.cap[k] + (amount if end is not None and end <= deadline else 0.0)
            best = min(best, cap + self.suffix[k])
        return best

    def gain(self, end: datetime, amount: float) -> float:
        return self.value(end, amount) - self.value()

    def add(self, end: datetime, amount: float) -> None:
        for k, deadline in enumerate(self.deadlines):
            if end <= deadline:
                self.cap[k] += amount

    def satisfied(self) -> bool:
        return self.value() >= self.total - _REL_TOL * max(1.0, self.total)


class _Context:
    def __init__(self, now, price_slots, power_slots, cars, config, commitment):
        self.now = _utc(now)
        self.config = config if config is not None else PlannerConfig()
        self.prices = _prepare_prices(price_slots)
        self.powers = _prepare_powers(power_slots)
        seen = set()
        for req in cars:
            if req.outlet in seen:
                raise ValueError(f"duplicate outlet {req.outlet!r}")
            seen.add(req.outlet)
        self.commit_outlet = None
        if commitment is not None:
            until = _utc(commitment.until)
            if until > self.now and any(
                req.outlet == commitment.outlet and not getattr(req, "excluded", False) for req in cars
            ):
                self.commit_outlet = commitment.outlet
        self.requests = list(cars)
        probe = [_sanitise(req, i, self.commit_outlet) for i, req in enumerate(self.requests)]
        active = [car for car in probe if not car.excluded]
        horizon = max((car.deadline for car in active), default=self.now)
        horizon = min(horizon, self.now + timedelta(hours=MAX_HORIZON_HOURS))
        self.units = self._build_units(horizon)
        known = [u.price for u in self.units if u.kind == PRICE_KNOWN]
        # Without an active car there is no window to price: that is not missing prices.
        self.no_price = bool(self.config.no_price_mode) or (bool(active) and not known)
        expected = getattr(self.config, "prices_expected_at", None)
        expected = _utc(expected) if isinstance(expected, datetime) else None
        if expected is not None and expected <= self.now:
            expected = None
        self.prices_expected_at = expected
        end = self.units[-1].end if self.units else self.now
        for unit in self.units:
            if self.no_price:
                unit.rank = 0.0
            elif unit.kind == PRICE_KNOWN:
                unit.rank = unit.price
                if expected is not None and unit.start < expected:
                    unit.rank += RANK_TIER_BEFORE_PRICES
            else:
                # Forecast or unknown: only for what the known hours cannot deliver,
                # and as late as possible (the prices may still arrive before then).
                unit.rank = RANK_TIER_UNPRICED + (end - unit.start).total_seconds() / 3600.0 * RANK_LATE_STEP
        known_ends = [p.end for p in self.prices if p.kind == PRICE_KNOWN]
        self.known_until = max(known_ends) if known_ends else None

    def _build_units(self, horizon: datetime) -> list[_Unit]:
        units = []
        start = floor15(self.now)
        while start + UNIT <= horizon:
            end = start + UNIT
            unit = _Unit()
            unit.index = len(units)
            unit.start = max(start, self.now)
            unit.end = end
            unit.minutes = (end - unit.start).total_seconds() / 60.0
            unit.price, unit.kind = _price_at(self.prices, start)
            unit.kw_e, unit.kw_c = _power_at(self.powers, start)
            unit.usable_all = unit.minutes >= MIN_START_MINUTES - _MIN_EPS
            unit.usable_committed = unit.minutes >= MIN_COMMITTED_MINUTES - _MIN_EPS
            unit.rank = 0.0
            units.append(unit)
            start = end
        return units


class _Pass:
    """One run of steps 1-10 for a given per-car start count."""

    def __init__(self, ctx: _Context, start_blocks: dict) -> None:
        self.ctx = ctx
        self.now = ctx.now
        self.units = ctx.units
        self.n = len(self.units)
        self.cars = [_sanitise(req, i, ctx.commit_outlet) for i, req in enumerate(ctx.requests)]
        for car in self.cars:
            _set_needs(car, start_blocks.get(car.outlet, 1))
        self.by_outlet = {car.outlet: car for car in self.cars}
        self.commit_car = self.by_outlet.get(ctx.commit_outlet) if ctx.commit_outlet else None
        # Units are measured at the fastest car's power. A slower car's needs are
        # scaled up by (fastest cap / its cap), so it gets the minutes it really needs
        # and never shrinks another car's planned power. Where the unit's own power is
        # below the fastest cap the scaling over-reserves the slower car (safe).
        # Outputs (segment kWh, _simulate, _latest_start) use the car's own power.
        need_cars = [car for car in self.cars
                     if (car.e_e > _EPS or car.top_need > _EPS) and car.cap > 0]
        self.k_e = max((car.cap for car in need_cars), default=math.inf)
        self.k_c = max((car.cap_low for car in need_cars), default=math.inf)
        for car in self.cars:
            car.scale_e = car.scale_c = 1.0
            if car in need_cars and math.isfinite(self.k_e):
                car.scale_e = self.k_e / car.cap if car.cap < self.k_e else 1.0
                if car.cap_low > 0 and car.cap_low < self.k_c:
                    car.scale_c = self.k_c / car.cap_low
                car.e_e *= car.scale_e
                car.e_v *= car.scale_e
                car.top_need *= car.scale_e
                car.e_c *= car.scale_c
                car.rem_e = car.e_e
                car.rem_c = car.e_c
        self.residual = [unit.minutes for unit in self.units]
        self.allocs = [[] for _ in self.units]
        self.reserved = set()
        self.late = set()
        self.ontime = []
        self.tight = set()
        self.protect_short = 0.0  # conservative kWh the live cars' protection could not cover
        self.late_order: list[_Car] = []  # late live cars, the one served first first
        self.late_need: dict[str, float] = {}  # most energy each late car can still get
        self.anchor_prev = any(car.drawing or car.committed for car in self.cars if not car.excluded)

    # Capacity helpers (expected / conservative metric of the minimum phase).
    def kw_e(self, i: int) -> float:
        return min(self.units[i].kw_e, self.k_e)

    def kw_c(self, i: int) -> float:
        return min(self.units[i].kw_c, self.k_c, self.kw_e(i))

    def cap_e(self, i: int, minutes: float | None = None) -> float:
        minutes = self.residual[i] if minutes is None else minutes
        return self.kw_e(i) * minutes / 60.0

    def cap_c(self, i: int, minutes: float | None = None) -> float:
        minutes = self.residual[i] if minutes is None else minutes
        return self.kw_c(i) * minutes / 60.0

    def usable(self, i: int, car: _Car) -> bool:
        unit = self.units[i]
        if car.cap <= 0 or self.residual[i] <= _MIN_EPS or unit.end > car.deadline or not (self.kw_e(i) > 0):
            return False
        return unit.usable_all or (car is self.commit_car and unit.usable_committed)

    def take(self, i: int, car: _Car, energy: float, kind: str) -> float:
        """Give ``car`` up to ``energy`` (expected kWh) of unit ``i``."""
        kw = self.kw_e(i)
        if energy <= _EPS or kw <= 0:
            return 0.0
        minutes = min(self.residual[i], energy / kw * 60.0)
        if minutes <= _MIN_EPS:
            return 0.0
        self.residual[i] -= minutes
        if self.residual[i] < _MIN_EPS:
            self.residual[i] = 0.0
        given = self.cap_e(i, minutes)
        car.rem_e = max(0.0, car.rem_e - given)
        car.rem_c = max(0.0, car.rem_c - self.cap_c(i, minutes))
        self.allocs[i].append(_Alloc(car.outlet, minutes, kind, given))
        return given

    def feasible(self, cars: list[_Car]) -> bool:
        if not cars:
            return True
        ordered = sorted(cars, key=_Car.edf)
        flow = _Nested(ordered, [car.rem_e for car in ordered])
        for i, unit in enumerate(self.units):
            if i in self.reserved or not unit.usable_all or self.residual[i] <= _MIN_EPS:
                continue
            flow.add(unit.end, self.cap_e(i))
        return flow.satisfied()

    # Step 1
    def commit(self) -> None:
        """The committed car keeps unit 0, but only as much of it as leaves on
        time every car that is on time without the commitment, leaves every
        late car that R11 serves before it (one that can still reach its
        minimum, or an earlier deadline) as much energy as it could get, and
        leaves the latest-start protection of the live cars as complete as it
        was. A late or overrun car therefore never takes the part of unit 0
        that a car ranked before it needs; the hand-off then happens inside
        the unit."""
        car = self.commit_car
        if car is None or not self.n or car.rem_e <= _EPS or not self.usable(0, car):
            return
        full = min(self.cap_e(0), car.rem_e)
        reference = self._probe(car, 0.0)
        amount = full
        if not self._commit_keeps(car, full, reference):
            low, high = 0.0, full
            for _ in range(COMMIT_SEARCH_STEPS):
                middle = (low + high) / 2.0
                if self._commit_keeps(car, middle, reference):
                    low = middle
                else:
                    high = middle
            # A share shorter than one controller cycle cannot be carried out:
            # the car would keep charging until the next cycle and push the car
            # it must make room for past its deadline. Hand over now instead.
            amount = low if low >= self.kw_e(0) * START_LEAD.total_seconds() / 3600.0 else 0.0
        if amount > _EPS:
            self.take(0, car, amount, KIND_COMMITTED)

    def _snapshot(self):
        return (list(self.residual), [list(a) for a in self.allocs],
                {car.outlet: (car.rem_e, car.rem_c) for car in self.cars},
                set(self.late), list(self.ontime), set(self.reserved), set(self.tight), self.protect_short)

    def _restore(self, saved) -> None:
        (self.residual, self.allocs, rems, self.late, self.ontime, self.reserved, self.tight,
         self.protect_short) = saved
        for car in self.cars:
            car.rem_e, car.rem_c = rems[car.outlet]

    def _probe(self, car: _Car, amount: float) -> tuple[set, list, dict, float]:
        """After giving ``car`` ``amount`` kWh of unit 0, steps 2-4 and the R11
        late order: the cars on time, the late cars in serving order with the
        most each can get, and the protection shortfall. The pass state is
        left unchanged."""
        saved = self._snapshot()
        try:
            if amount > _EPS:
                self.take(0, car, amount, KIND_COMMITTED)
            self.protect_and_overrun()
            self.select_on_time()
            candidates = [i for i, unit in enumerate(self.units)
                          if unit.usable_all and self.residual[i] > _MIN_EPS and self.kw_e(i) > 0]
            order, needs = self._late_plan(candidates)
            return {c.outlet for c in self.ontime}, [c.outlet for c in order], needs, self.protect_short
        finally:
            self._restore(saved)

    def _commit_keeps(self, car: _Car, amount: float, reference: tuple) -> bool:
        ontime, _, needs, short = self._probe(car, amount)
        ref_ontime, ref_order, ref_needs, ref_short = reference
        if not ref_ontime <= ontime or short > ref_short + 1e-9:
            return False
        if car.outlet in ref_ontime:
            return True  # an on-time car is served before every late car
        ahead = ref_order[:ref_order.index(car.outlet)] if car.outlet in ref_order else ref_order
        return all(needs.get(outlet, 0.0) >= ref_needs[outlet] - _REL_TOL * max(1.0, ref_needs[outlet]) - 1e-6
                   for outlet in ahead)

    # Steps 2-3
    def protect_and_overrun(self) -> None:
        overrun = sorted(
            (car for car in self.cars if car.overrun and car.rem_e > _EPS and not car.excluded),
            key=_Car.edf,
        )
        if not overrun:
            return
        live = [
            car for car in self.cars
            if not car.overrun and not car.excluded and car.deadline > self.now and car.rem_e > _EPS
        ]
        protect = set()
        short = 0.0
        for car in sorted(live, key=_Car.edf, reverse=True):
            got = 0.0
            for i in range(self.n - 1, -1, -1):
                if got >= car.rem_c - _EPS:
                    break
                if i in protect or not self.usable(i, car):
                    continue
                protect.add(i)
                got += self.cap_c(i)
            short += max(0.0, car.rem_c - got)
        self.protect_short = short
        for car in overrun:
            for i in range(self.n):
                if car.rem_e <= _EPS:
                    break
                if i in protect or not self.usable(i, car):
                    continue
                self.take(i, car, min(self.cap_e(i), car.rem_e), KIND_OVERRUN)

    # Step 4 (R11: maximise the number of cars at their minimum)
    def select_on_time(self) -> None:
        ontime = []
        candidates = sorted(
            (car for car in self.cars if not car.overrun and not car.excluded and car.rem_e > _EPS),
            key=_Car.edf,
        )
        for car in candidates:
            if car.deadline <= self.now or car.cap <= 0:
                self.late.add(car.outlet)
                continue
            ontime.append(car)
            if not self.feasible(ontime):
                worst = self._drop_choice(ontime)
                ontime.remove(worst)
                self.late.add(worst.outlet)
        self.ontime = sorted(ontime, key=_Car.edf)

    def _drop_choice(self, ontime: list[_Car]) -> _Car:
        """The car that becomes late when ``ontime`` no longer fits.

        R11 for the two outlets: a car that does not fit even alone goes;
        when each fits alone but not both, the earlier ready-by stays on time,
        then the charge-policy priority car. The remaining need is never the
        tie-break: it shrinks while a car charges, which would hand the outlet
        back and forth at every unit. More cars: Moore-Hodgson (drop the
        largest need) maximises the number on time.
        """
        if len(ontime) <= 2:
            restoring = [c for c in ontime if self.feasible([o for o in ontime if o is not c])]
            return max(restoring, key=lambda c: (c.deadline, c.priority, c.outlet))
        return max(ontime, key=lambda c: (c.rem_e, c.deadline, c.priority, c.outlet))

    # Step 5
    def buffer(self) -> None:
        for car in sorted(self.ontime, key=_Car.edf, reverse=True):
            pool = [i for i in range(self.n) if i not in self.reserved and self.usable(i, car)]
            sum_e = sum(self.cap_e(i) for i in pool)
            sum_c = sum(self.cap_c(i) for i in pool)
            rho = sum_c / sum_e if sum_e > _EPS else 1.0
            deficit = car.rem_c - rho * car.rem_e
            if deficit <= _EPS:
                continue
            got = 0.0
            for i in reversed(pool):
                if got >= deficit - _EPS:
                    break
                self.reserved.add(i)
                if not self.feasible(self.ontime):
                    self.reserved.discard(i)
                    break
                got += self.cap_c(i)
            if got < deficit - _EPS:
                self.tight.add(car.outlet)

    # Steps 6, 7 and 8
    def greedy(self) -> None:
        """Cheapest units for the on-time cars' needs together with, for each
        late car, the most energy it can still get by its deadline (R11: "as
        much as possible"), so prices are optimised only over units a late car
        does not need."""
        candidates = [
            i for i, unit in enumerate(self.units)
            if i not in self.reserved
            and unit.usable_all
            and self.residual[i] > _MIN_EPS
            and self.kw_e(i) > 0
        ]
        if self.ctx.no_price and self.ontime:
            # All units rank equal: latest first, which is contiguous by itself.
            self._continue_running()
            candidates = [i for i in candidates if self.residual[i] > _MIN_EPS]
        self._plan_late(candidates)
        late = [car for car in self.late_order if self.late_need.get(car.outlet, 0.0) > _EPS]
        cars = sorted(self.ontime + late, key=_Car.edf)
        if not cars:
            return
        latest = max(car.deadline for car in cars)
        candidates = [i for i in candidates if self.units[i].end <= latest]
        anchors = set() if self.ctx.no_price else self._anchors()
        needs = {car.outlet: car.rem_e for car in self.ontime}
        needs.update({car.outlet: self.late_need[car.outlet] for car in late})
        kinds = {car.outlet: KIND_MINIMUM for car in self.ontime}
        kinds.update({car.outlet: KIND_LATE for car in late})
        caps = {i: self.cap_e(i) for i in candidates}
        flow = _Nested(cars, [needs[car.outlet] for car in cars])
        chosen = _greedy(self.units, candidates, caps, flow, anchors, self.ctx.no_price)
        self._fill(chosen, cars, kinds, needs)

    def _late_cars(self) -> list[_Car]:
        return [
            self.by_outlet[outlet] for outlet in sorted(self.late)
            if self.by_outlet[outlet].deadline > self.now and self.by_outlet[outlet].rem_e > _EPS
            and self.by_outlet[outlet].cap > 0 and not self.by_outlet[outlet].overrun
        ]

    def _best_late(self, car: _Car, fixed: list, caps: list) -> float:
        """Most energy ``car`` can get by its deadline from ``caps`` with the
        needs in ``fixed`` [(car, kWh)] still delivered."""
        items = sorted(fixed + [(car, car.rem_e)], key=lambda item: _Car.edf(item[0]))
        flow = _Nested([c for c, _ in items], [need for _, need in items])
        for end, cap in caps:
            flow.add(end, cap)
        return _clamp(flow.value() - sum(need for _, need in fixed), 0.0, car.rem_e)

    def _late_reach(self, candidates: list[int]) -> tuple[dict, set]:
        """Per late live car, the most it can get with the on-time cars' needs
        fixed, and the cars that still reach their minimum without the safety
        margin (expected need only)."""
        caps = [(self.units[i].end, self.cap_e(i)) for i in candidates]
        fixed = [(car, car.rem_e) for car in self.ontime]
        alone, reaching = {}, set()
        for car in self._late_cars():
            alone[car.outlet] = self._best_late(car, fixed, caps)
            unmargined = max(0.0, car.e_v - (car.e_e - car.rem_e))
            if alone[car.outlet] >= unmargined - _REL_TOL * max(1.0, car.e_v):
                reaching.add(car.outlet)
        return alone, reaching

    def _late_plan(self, candidates: list[int]) -> tuple[list, dict]:
        """R11 for the cars that cannot all make it: each late live car gets
        the most energy it can still get by its deadline with the on-time
        cars' needs fixed. Cars that can still reach their minimum without the
        safety margin are served first, then earlier deadline and priority.
        Returns (late cars in serving order, outlet -> kWh)."""
        _, reaching = self._late_reach(candidates)
        caps = [(self.units[i].end, self.cap_e(i)) for i in candidates]
        fixed = [(car, car.rem_e) for car in self.ontime]
        order = sorted(self._late_cars(), key=lambda car: (car.outlet not in reaching, car.edf()))
        needs = {}
        for car in order:
            amount = self._best_late(car, fixed, caps)
            needs[car.outlet] = amount
            if amount > _EPS:
                fixed.append((car, amount))
        return order, needs

    def _plan_late(self, candidates: list[int]) -> None:
        self.late_order, self.late_need = self._late_plan(candidates)

    def _continue_running(self) -> None:
        """No-price mode: a car that is already charging keeps charging from
        now (prices are all equal, so this only avoids a stop and restart),
        as long as the other on-time cars still fit."""
        for car in self.ontime:
            if not (car.drawing or car.committed) or car.rem_e <= _EPS:
                continue
            saved = (list(self.residual), [list(a) for a in self.allocs], car.rem_e, car.rem_c)
            for i in range(self.n):
                if car.rem_e <= _EPS:
                    break
                if i in self.reserved or not self.usable(i, car):
                    break
                self.take(i, car, min(self.cap_e(i), car.rem_e), KIND_MINIMUM)
            others = [c for c in self.ontime if c is not car]
            if not self.feasible(others):
                self.residual, self.allocs, car.rem_e, car.rem_c = saved

    def _anchors(self) -> set:
        anchors = {i for i in range(self.n) if self.allocs[i]}
        if self.anchor_prev:
            anchors.add(-1)
        return anchors

    def _fill(self, chosen: dict, cars: list[_Car], kinds: dict, needs: dict) -> None:
        """Chronological EDF fill of the chosen capacities into ``cars`` (each
        up to its need: a late car's need is what it can get with the on-time
        cars still on time). Between equal deadlines an on-time car goes first:
        charging the late car first would leave the on-time car no room for
        any slip at the end of the window."""
        ontime = {car.outlet for car in self.ontime}
        ordered = sorted(cars, key=lambda car: (car.deadline, car.outlet not in ontime, car.tie))
        left_need = dict(needs)
        for i in sorted(chosen):
            left = chosen[i]
            for car in ordered:
                if left <= _EPS:
                    break
                need = min(car.rem_e, left_need[car.outlet])
                if need <= _EPS or self.units[i].end > car.deadline:
                    continue
                given = self.take(i, car, min(left, need), kinds[car.outlet])
                left -= given
                left_need[car.outlet] -= given

    # Step 7
    def late_fill(self) -> None:
        overrun = sorted(
            (c for c in self.cars if c.overrun and not c.excluded and c.rem_e > _EPS), key=_Car.edf
        )
        rest = sorted((self.by_outlet[o] for o in self.late if self.by_outlet[o] not in self.late_order),
                      key=_Car.edf)
        late = list(self.late_order) + rest
        for car, kind in [(c, KIND_OVERRUN) for c in overrun] + [(c, KIND_LATE) for c in late]:
            for i in range(self.n):
                if car.rem_e <= _EPS:
                    break
                if self.usable(i, car):
                    self.take(i, car, min(self.cap_e(i), car.rem_e), kind)

    # Step 9
    def merge_tails(self) -> None:
        """Move short isolated blocks next to the car's nearest block and close
        gaps that a partly used unit leaves inside a car's run, when that costs
        at most START_PENALTY_EUR. Every accepted change lowers the number of
        blocks, so this terminates."""
        if self.ctx.no_price:
            return
        for _ in range(4 * self.n + 4):
            if not (self._merge_once() or self._close_gap_once()):
                return

    def _close_gap_once(self) -> bool:
        blocks = self._time_blocks()
        total_before = sum(len(b) for b in blocks.values())
        running = self._running()
        for car in self.ontime:
            mine = blocks.get(car.outlet, [])
            for first, second in zip(mine, mine[1:]):
                if first.get("virtual"):
                    continue
                gap = (second["start"] - first["end"]).total_seconds() / 60.0
                if gap > UNIT_MINUTES:
                    continue
                last_unit = max(i for i, _ in first["items"])
                first_unit = min(i for i, _ in second["items"])
                if first_unit - last_unit > 1:
                    continue
                fill = []
                for i in sorted({last_unit, first_unit}):
                    if self.residual[i] > _MIN_EPS:
                        fill.append((i, self.cap_e(i)))
                energy = sum(e for _, e in fill)
                if energy <= _EPS:
                    continue
                gain_cost = sum(self.units[i].rank * e for i, e in fill)
                best = None
                for block in mine:
                    units = [i for i, _ in block["items"]]
                    if not units:
                        continue
                    for edge in sorted({min(units), max(units)}):
                        if edge in (last_unit, first_unit):
                            continue
                        if edge == 0 and car.outlet in running:
                            continue
                        alloc = next(
                            (a for i, a in block["items"] if i == edge and a.kind == KIND_MINIMUM), None
                        )
                        if alloc is None or alloc.energy < energy - _EPS:
                            continue
                        extra = gain_cost - self.units[edge].rank * energy
                        if extra <= START_PENALTY_EUR + 1e-12 and (best is None or extra < best[0] - 1e-12):
                            best = (extra, edge, alloc)
                if best is None:
                    continue
                _, edge, alloc = best
                saved = (list(self.residual), [list(a) for a in self.allocs], car.rem_e, car.rem_c)
                minutes = min(alloc.minutes, energy / self.kw_e(edge) * 60.0)
                self.allocs[edge] = [a for a in self.allocs[edge] if a is not alloc]
                kept = alloc.minutes - minutes
                if kept > _MIN_EPS:
                    self.allocs[edge].append(_Alloc(car.outlet, kept, alloc.kind, self.cap_e(edge, kept)))
                self.residual[edge] = min(self.units[edge].minutes, self.residual[edge] + minutes)
                car.rem_e += self.cap_e(edge, minutes)
                car.rem_c += self.cap_c(edge, minutes)
                for i, amount in fill:
                    self.take(i, car, amount, KIND_MINIMUM)
                after = self._time_blocks()
                if (
                    len(after.get(car.outlet, [])) < len(mine)
                    and sum(len(b) for b in after.values()) < total_before
                    and car.rem_e <= _REL_TOL * max(1.0, car.e_e) + 1e-6
                ):
                    return True
                self.residual, self.allocs, car.rem_e, car.rem_c = saved
        return False

    def extend_short_starts(self) -> None:
        """A car that starts inside a unit (placed at its end) gets at least
        ``_start_pad()`` minutes there, so the start still fits unit 0 when that unit
        becomes unit 0 and the replan runs seconds after the start time. When
        the unit has no idle minutes for that (another car runs first), the
        car's minutes there move behind its block instead, so it starts at the
        unit boundary rather than never."""
        running = self._running()
        pad = _start_pad()
        for i in range(self.n):
            items = [a for a in self.allocs[i] if a.minutes > _MIN_EPS]
            if not items:
                continue
            idle = self.units[i].minutes - sum(a.minutes for a in items)
            if idle <= _MIN_EPS:
                continue
            if i == 0:
                prev = running
            else:
                prev = {a.outlet for a in self.allocs[i - 1] if a.minutes > _MIN_EPS}
            nxt = {a.outlet for a in self.allocs[i + 1] if a.minutes > _MIN_EPS} if i + 1 < self.n else set()
            for outlet in sorted({a.outlet for a in items}):
                if outlet in prev or outlet not in nxt:
                    continue
                own = [a for a in items if a.outlet == outlet]
                used = sum(a.minutes for a in own)
                if used >= pad - _MIN_EPS:
                    continue
                extra = min(pad - used, idle, self.residual[i])
                if used + max(0.0, extra) < pad - _MIN_EPS and len(own) < len(items):
                    # Split unit: another car runs first and leaves no room.
                    if self._move_short_start(i, outlet):
                        idle = self.units[i].minutes - sum(
                            a.minutes for a in self.allocs[i] if a.minutes > _MIN_EPS)
                        items = [a for a in self.allocs[i] if a.minutes > _MIN_EPS]
                        continue
                if extra <= _MIN_EPS:
                    continue
                alloc = own[0]
                added = self.cap_e(i, extra)
                alloc.minutes += extra
                alloc.energy += added
                self.residual[i] = max(0.0, self.residual[i] - extra)
                idle -= extra
                car = self.by_outlet[outlet]
                car.rem_e = max(0.0, car.rem_e - added)
                car.rem_c = max(0.0, car.rem_c - self.cap_c(i, extra))

    def _move_short_start(self, i: int, outlet: str) -> bool:
        """Move ``outlet``'s minutes in unit ``i`` (a start too short to happen)
        into free units right after its block that starts in unit ``i + 1``."""
        car = self.by_outlet[outlet]
        own = [a for a in self.allocs[i] if a.outlet == outlet and a.minutes > _MIN_EPS]
        energy = sum(a.energy for a in own)
        j = i + 1
        while j < self.n and any(a.outlet == outlet and a.minutes > _MIN_EPS for a in self.allocs[j]):
            j += 1
        placement = []
        left = energy
        while left > _EPS:
            if j >= self.n:
                return False
            unit = self.units[j]
            if (unit.end > car.deadline or j in self.reserved or not unit.usable_all or self.kw_e(j) <= 0
                    or any(a.minutes > _MIN_EPS for a in self.allocs[j])):
                return False
            take = min(self.cap_e(j), left)
            placement.append((j, take))
            left -= take
            j += 1
        kind = own[0].kind
        self.allocs[i] = [a for a in self.allocs[i] if a.outlet != outlet]
        for alloc in own:
            self.residual[i] = min(self.units[i].minutes, self.residual[i] + alloc.minutes)
            car.rem_e += alloc.energy
            car.rem_c += self.cap_c(i, alloc.minutes)
        for j, amount in placement:
            self.take(j, car, amount, kind)
        return True

    def _running(self) -> set:
        return {car.outlet for car in self.cars if car.drawing or car.committed}

    def _time_blocks(self) -> dict:
        """Minimum (non-top-up) blocks per outlet in time, from the provisional
        placement. A running car whose plan does not continue now gets a
        virtual block ending now: the charging in progress."""
        running = self._running()
        placed = _place(self.units, self.allocs, running)
        blocks = {}
        for i, alloc, start, end in placed:
            if alloc.kind == KIND_TOP_UP or end <= start:
                continue
            own = blocks.setdefault(alloc.outlet, [])
            if own and (start - own[-1]["end"]).total_seconds() <= 1.0:
                own[-1]["end"] = max(own[-1]["end"], end)
                own[-1]["items"].append((i, alloc))
            else:
                own.append({"start": start, "end": end, "items": [(i, alloc)]})
        for outlet in sorted(running):
            own = blocks.setdefault(outlet, [])
            if not own or own[0]["start"] > self.now:
                own.insert(0, {"start": self.now, "end": self.now, "items": [], "virtual": True})
        return blocks

    def _merge_once(self) -> bool:
        blocks = self._time_blocks()
        total_before = sum(len(b) for b in blocks.values())
        running = self._running()
        for car in self.ontime:
            mine = blocks.get(car.outlet, [])
            if len(mine) < 2:
                continue
            for block in mine:
                if block.get("virtual"):
                    continue
                minutes = sum(a.minutes for _, a in block["items"])
                if minutes >= TAIL_MERGE_MAX_MINUTES - _MIN_EPS:
                    continue
                if any(a.kind != KIND_MINIMUM for _, a in block["items"]):
                    continue
                if car.outlet in running and any(i == 0 for i, _ in block["items"]):
                    continue
                if self._move_block(car, block, mine, total_before):
                    return True
        return False

    def _move_block(self, car: _Car, block: dict, mine: list, total_before: int) -> bool:
        others = [b for b in mine if b is not block]

        def gap(other):
            if other["end"] <= block["start"]:
                return (block["start"] - other["end"]).total_seconds()
            return (other["start"] - block["end"]).total_seconds()

        nearest = min(others, key=lambda b: (gap(b), b["start"]))
        moving = block["items"]
        energy = sum(a.energy for _, a in moving)
        old_cost = sum(self.units[i].rank * a.energy for i, a in moving)
        freed = {}
        for i, alloc in moving:
            freed[i] = freed.get(i, 0.0) + alloc.minutes
        moving_ids = {id(a) for _, a in moving}
        best = None
        for side in ("before", "after"):
            if nearest.get("virtual"):
                if side == "before":
                    continue
                edge = -1
            else:
                units_of_nearest = [i for i, _ in nearest["items"]]
                edge = max(units_of_nearest) if side == "after" else min(units_of_nearest)
            placement = self._walk(car, edge, 1 if side == "after" else -1, energy, freed, moving_ids)
            if placement is None:
                continue
            extra = sum(self.units[i].rank * e for i, e in placement) - old_cost
            if extra <= START_PENALTY_EUR + 1e-12 and (best is None or extra < best[0] - 1e-12):
                best = (extra, placement)
        if best is None:
            return False
        saved = (list(self.residual), [list(a) for a in self.allocs], car.rem_e, car.rem_c)
        for i, alloc in moving:
            self.allocs[i] = [a for a in self.allocs[i] if a is not alloc]
            self.residual[i] = min(self.units[i].minutes, self.residual[i] + alloc.minutes)
            car.rem_e += alloc.energy
            car.rem_c += self.cap_c(i, alloc.minutes)
        for i, amount in best[1]:
            self.take(i, car, amount, KIND_MINIMUM)
        after = self._time_blocks()
        if (
            len(after.get(car.outlet, [])) < len(mine)
            and sum(len(b) for b in after.values()) < total_before
            and car.rem_e <= _REL_TOL * max(1.0, car.e_e) + 1e-6
        ):
            return True
        self.residual, self.allocs, car.rem_e, car.rem_c = saved
        return False

    def _walk(self, car, edge, step, energy, freed, moving_ids):
        """Energy per unit when the moving block is placed right after (step 1)
        or before (step -1) the nearest block, starting in its edge unit."""
        placement = []
        left = energy
        i = edge
        first = True
        if edge < 0:  # right after the charging in progress: start in unit 0
            i, first = 0, False
        while left > _EPS:
            if i < 0 or i >= self.n:
                return None
            unit = self.units[i]
            if not first and (
                i in self.reserved
                or not unit.usable_all
                or unit.end > car.deadline
                or self.kw_e(i) <= 0
            ):
                return None
            residual = min(unit.minutes, self.residual[i] + freed.get(i, 0.0))
            others = [a for a in self.allocs[i] if a.outlet != car.outlet and id(a) not in moving_ids]
            if residual > _MIN_EPS:
                take = min(self.cap_e(i, residual), left)
                placement.append((i, take))
                left -= take
            elif not first:
                return None
            if not first and others and left > _EPS:
                return None
            first = False
            i += step
        return placement

    # Step 10
    def top_up(self) -> None:
        threshold = _finite(self.ctx.config.top_up_price)
        if threshold is None or self.ctx.no_price:
            return
        cars = sorted((c for c in self.cars if c.top_need > _EPS and c.cap > 0), key=_Car.edf)
        if not cars:
            return
        k_t = self.k_e if math.isfinite(self.k_e) else max(car.cap for car in cars)
        need = {car.outlet: car.top_need for car in cars}

        def kw_t(i):
            return min(self.units[i].kw_e, k_t)

        def qualifies(i):
            unit = self.units[i]
            return unit.kind == PRICE_KNOWN and unit.price <= threshold + 1e-12 and kw_t(i) > 0

        def give(i, car, energy):
            kw = kw_t(i)
            minutes = min(self.residual[i], energy / kw * 60.0)
            if minutes <= _MIN_EPS:
                return 0.0
            self.residual[i] = max(0.0, self.residual[i] - minutes)
            given = kw * minutes / 60.0
            need[car.outlet] = max(0.0, need[car.outlet] - given)
            self.allocs[i].append(_Alloc(car.outlet, minutes, KIND_TOP_UP, given))
            return given

        commit = self.commit_car
        if (
            commit is not None
            and commit in cars
            and self.n
            and not self.units[0].usable_all
            and self.usable(0, commit)
            and qualifies(0)
        ):
            give(0, commit, need[commit.outlet])
        cars = [car for car in cars if need[car.outlet] > _EPS]
        if not cars:
            return
        latest = max(car.deadline for car in cars)
        candidates = [
            i for i, unit in enumerate(self.units)
            if unit.usable_all and self.residual[i] > _MIN_EPS and unit.end <= latest and qualifies(i)
        ]
        caps = {i: kw_t(i) * self.residual[i] / 60.0 for i in candidates}
        flow = _Nested(cars, [need[car.outlet] for car in cars])
        chosen = _greedy(self.units, candidates, caps, flow, self._anchors(), False)
        for i in sorted(chosen):
            left = chosen[i]
            for car in cars:
                if left <= _EPS:
                    break
                if need[car.outlet] <= _EPS or self.units[i].end > car.deadline:
                    continue
                left -= give(i, car, min(left, need[car.outlet]))

    def run(self) -> None:
        self.commit()
        self.protect_and_overrun()
        self.select_on_time()
        self.buffer()
        self.greedy()
        self.late_fill()
        self.merge_tails()
        self.extend_short_starts()
        self.top_up()


def _greedy(units, candidates, caps, flow: _Nested, anchors: set, later_first: bool) -> dict:
    """Edmonds' polymatroid greedy: cheapest rank first; within a rank, units
    next to already chosen/committed units first, then by start (latest first
    in no-price mode). Returns unit index -> capacity used."""
    if later_first:
        remaining = sorted(candidates, key=lambda i: (units[i].rank, -i))
    else:
        remaining = sorted(candidates, key=lambda i: (units[i].rank, i))
    chosen = {}
    while remaining and not flow.satisfied():
        rank = units[remaining[0]].rank
        pick = 0
        for pos, i in enumerate(remaining):
            if units[i].rank != rank:
                break
            if (i - 1) in anchors or (i + 1) in anchors:
                pick = pos
                break
        i = remaining.pop(pick)
        cap = caps[i]
        gain = flow.gain(units[i].end, cap)
        if gain > _EPS:
            flow.add(units[i].end, cap)
            chosen[i] = min(gain, cap)
            anchors.add(i)
    return chosen


# --- Output -------------------------------------------------------------------
def _place(units, allocs, running) -> list:
    """Step 11 placement: put each unit's allocations in time.

    Cars that continue from the previous unit (for unit 0: the running car)
    go first, cars that continue into the next unit go last, so idle minutes
    of a partly used unit sit at a hand-off or at a block edge. A car that is
    neither goes first. Returns ``(unit index, alloc, start, end)``.
    """
    n = len(units)
    placed = []

    def owns(i, outlet):
        if i < 0:
            return outlet in running
        if i >= n:
            return False
        return any(a.outlet == outlet and a.minutes > _MIN_EPS for a in allocs[i])

    for i, unit in enumerate(units):
        items = [a for a in allocs[i] if a.minutes > _MIN_EPS]
        if not items:
            continue
        outlets = []
        for alloc in items:
            if alloc.outlet not in outlets:
                outlets.append(alloc.outlet)
        head = [o for o in outlets if owns(i - 1, o)]
        tail = [o for o in outlets if o not in head and owns(i + 1, o)]
        middle = [o for o in outlets if o not in head and o not in tail]
        front = [a for o in head + middle for a in items if a.outlet == o]
        back = [a for o in tail for a in items if a.outlet == o]
        used = sum(a.minutes for a in front) + sum(a.minutes for a in back)
        idle = max(0.0, unit.minutes - used)
        offset = 0.0
        for group, start_offset in ((front, 0.0), (back, sum(a.minutes for a in front) + idle)):
            offset = start_offset
            for alloc in group:
                start = unit.start + timedelta(seconds=round(offset * 60))
                offset += alloc.minutes
                end = min(unit.end, unit.start + timedelta(seconds=round(offset * 60)))
                placed.append((i, alloc, start, end))
    placed.sort(key=lambda item: (item[2], item[3], item[1].outlet))
    return placed


def _place_segments(p: "_Pass") -> list[Segment]:
    segments = []
    for i, alloc, start, end in _place(p.units, p.allocs, p._running()):
        if end <= start:
            continue
        unit = p.units[i]
        segments.append(
            Segment(
                outlet=alloc.outlet,
                start=start,
                end=end,
                minutes=alloc.minutes,
                # The car's own expected power (units are measured at the fastest car's).
                energy_kwh=alloc.minutes * min(unit.kw_e, p.by_outlet[alloc.outlet].cap) / 60.0,
                top_up=alloc.kind == KIND_TOP_UP,
                kind=alloc.kind,
                price=unit.price,
                price_kind=unit.kind,
            )
        )
    return segments


def _blocks(segments: list[Segment]) -> tuple[Block, ...]:
    blocks = []
    for seg in segments:
        if seg.end <= seg.start:
            continue
        if (
            blocks
            and blocks[-1][2] == seg.top_up
            and (seg.start - blocks[-1][1]).total_seconds() <= 1.0
        ):
            blocks[-1][1] = max(blocks[-1][1], seg.end)
        else:
            blocks.append([seg.start, seg.end, seg.top_up])
    return tuple(Block(start, end, top_up) for start, end, top_up in blocks)


def _start_count(car: _Car, segments: list[Segment], now: datetime) -> int:
    """Planned minimum blocks that need a new start (the running one does not)."""
    blocks = _blocks([s for s in segments if not s.top_up])
    count = len(blocks)
    if count and (car.drawing or car.committed) and blocks[0].start <= now:
        count -= 1
    return count


def _latest_start(car: _Car, units: list, now: datetime) -> datetime | None:
    """Latest time charging this car alone without a break, at expected
    power, still delivers its expected need by the deadline."""
    if car.excluded or car.e_e <= _EPS or car.cap <= 0:
        return None
    left = car.e_e / (getattr(car, "scale_e", 1.0) or 1.0)
    for unit in reversed(units):
        if unit.end > car.deadline:
            continue
        kw = min(unit.kw_e, car.cap)
        energy = kw * unit.minutes / 60.0
        if kw > 0 and energy >= left:
            return unit.end - timedelta(seconds=round(left / kw * 3600))
        left -= energy
    return now


def _simulate(car: _Car, segments: list[Segment], units_by_start: dict) -> dict:
    """Step 13: chronological delivery at the car's expected power."""
    soc = car.soc
    left = car.e_actual
    result = {
        "cost": 0.0, "top_up_cost": 0.0, "energy": 0.0, "top_up_energy": 0.0,
        "forecast": False, "partial": False,
    }
    limit_top = car.limit if car.limit is not None else 100.0
    for seg in segments:
        unit = units_by_start.get(floor15(seg.start))
        kw_unit = unit.kw_e if unit is not None else math.inf
        power = min(kw_unit, car.cap)
        if power <= 0 or seg.minutes <= 0:
            continue
        virtual = power * seg.minutes / 60.0
        if soc is None:
            if seg.top_up:
                continue
            energy = min(virtual, left)
            left -= energy
        else:
            limit = limit_top if seg.top_up else min(car.target, limit_top)
            new_soc = EXPECTED_TAPER.advance(car.k_e, soc, virtual, limit)
            energy = max(0.0, (new_soc - soc) * car.k_e)
            soc = max(soc, new_soc)
        if energy <= _EPS:
            continue
        key = "top_up" if seg.top_up else ""
        result["top_up_energy" if seg.top_up else "energy"] += energy
        if seg.price is None or seg.price_kind == PRICE_UNKNOWN:
            result["partial"] = True
        else:
            result[(key + "_cost") if key else "cost"] += seg.price * energy
            if seg.price_kind == PRICE_FORECAST:
                result["forecast"] = True
    result["soc"] = soc
    return result


def _covers_minimum(car: _Car, sim: dict) -> bool:
    """The planned minimum energy reaches the target (no margin): the projection
    rounds to the target (the card shows whole percent), or the budget is met."""
    if car.soc is not None:
        return sim["soc"] >= car.target - COVER_TOLERANCE_PCT
    return sim["energy"] >= car.e_actual - 1e-6


def _build_output(ctx: _Context, p: _Pass) -> tuple[Plan, dict]:
    now = ctx.now
    segments = _place_segments(p)
    by_car = {car.outlet: [s for s in segments if s.outlet == car.outlet] for car in p.cars}
    owner_seg = None
    if segments and (segments[0].start <= now or (
            segments[0].start - now <= START_LEAD and segments[0].start < floor15(now) + UNIT)):
        # A block starting within START_LEAD in this unit starts now (see START_LEAD).
        owner_seg = segments[0]
    owner = owner_seg.outlet if owner_seg is not None else None
    units_by_start = {floor15(u.start): u for u in ctx.units}
    starts = {}
    car_plans = []
    for car in p.cars:
        mine = by_car[car.outlet]
        starts[car.outlet] = _start_count(car, mine, now)
        sim = _simulate(car, mine, units_by_start)
        fully = car.rem_e <= _REL_TOL * max(1.0, car.e_e) + 1e-6
        if car.excluded:
            status, feasible = STATUS_EXCLUDED, False
        elif car.e_e <= _EPS:
            reached = car.reached or (car.soc is not None and car.soc >= car.target)
            status, feasible = (STATUS_REACHED if reached else STATUS_DONE), True
        elif car.overrun:
            status, feasible = STATUS_CATCH_UP, fully
        elif car.outlet in p.late and not _covers_minimum(car, sim):
            status, feasible = STATUS_AT_RISK, False
        elif owner == car.outlet:
            status, feasible = STATUS_CHARGING, True
        else:
            # A late car whose plan still delivers its unmargined minimum is only
            # short of the safety margin: "tight", not "at risk". Otherwise the
            # status flips with every whole-percent SoC step near the deadline
            # while the projection stays at the target.
            status = STATUS_TIGHT if car.outlet in p.tight or car.outlet in p.late else STATUS_PLANNED
            feasible = True
        has_top_up = any(s.top_up for s in mine)
        projected = None
        if car.soc is not None:
            projected = round(_clamp(sim["soc"], 0.0, 100.0), 1)
        car_plans.append(
            CarPlan(
                outlet=car.outlet,
                need_kwh=round(car.e_actual, 2),
                status=status,
                feasible=feasible,
                estimated=car.soc is None and not car.excluded,
                blocks=_blocks(mine),
                next_start=mine[0].start if mine else None,
                projected_soc=projected,
                cost=None if car.excluded else _money(sim["cost"]),
                includes_forecast=sim["forecast"],
                partial=sim["partial"],
                top_up_cost=_money(sim["top_up_cost"]) if has_top_up else None,
                energy_kwh=round(sim["energy"], 2),
                top_up_kwh=round(sim["top_up_energy"], 2),
                need_expected_kwh=round(car.e_e / car.scale_e, 3),
                need_conservative_kwh=round(car.e_c / car.scale_c, 3),
                top_up=has_top_up,
                tight=car.outlet in p.tight,
                latest_start=_latest_start(car, ctx.units, now),
                late=car.outlet in p.late,
            )
        )
    plans_by_outlet = {plan.outlet: plan for plan in car_plans}
    active = [car for car in p.cars if not car.excluded]
    if owner is not None:
        car = p.by_outlet[owner]
        status = plans_by_outlet[owner].status
        if ctx.no_price:
            reason = REASON_NO_PRICE_DATA
        elif status in (STATUS_CATCH_UP, STATUS_AT_RISK):
            reason = REASON_CATCH_UP
        elif owner_seg.top_up and (car.reached or (car.soc is not None and car.soc >= car.target)):
            reason = REASON_TOP_UP
        else:
            reason = REASON_CHARGING
    elif not active:
        reason = REASON_NO_EV
    elif segments or any(car.rem_e > 1e-6 for car in active):
        reason = REASON_WAITING
    else:
        reason = REASON_TARGETS_REACHED
    order = tuple(
        outlet for _, outlet in sorted((by_car[o][0].start, o) for o in by_car if by_car[o])
    )
    costs = [plan.cost for plan in car_plans if plan.cost is not None]
    top_costs = [plan.top_up_cost for plan in car_plans if plan.top_up_cost is not None]
    plan = Plan(
        now=now,
        reason=reason,
        current_outlet=owner,
        cars=tuple(car_plans),
        segments=tuple(segments),
        order=order,
        next_start=segments[0].start if segments else None,
        no_price_mode=ctx.no_price,
        known_until=ctx.known_until,
        total_cost=_money(sum(costs)) if costs else None,
        total_top_up_cost=_money(sum(top_costs)) if top_costs else None,
        total_energy_kwh=round(sum(plan.energy_kwh for plan in car_plans), 2),
        includes_forecast=any(plan.includes_forecast for plan in car_plans),
        partial=any(plan.partial for plan in car_plans),
    )
    return plan, starts


def build_plan(
    now: datetime,
    price_slots: Sequence[PriceSlot],
    power_slots: Sequence[PowerSlot],
    cars: Sequence[CarRequest],
    config: PlannerConfig = PlannerConfig(),
    commitment: Commitment | None = None,
) -> Plan:
    """Plan the cheapest charging that gets every car to its minimum (spec 7.5)."""
    ctx = _Context(now, price_slots, power_slots, list(cars or ()), config, commitment)
    hints = {req.outlet: 1 for req in ctx.requests}
    plan = None
    for _ in range(MAX_PLAN_PASSES):
        current = _Pass(ctx, hints)
        current.run()
        plan, starts = _build_output(ctx, current)
        new_hints = {o: max(hints[o], starts.get(o, 0), 1) for o in hints}
        if new_hints == hints:
            break
        hints = new_hints
    return plan


# --- Baseline (spec 7.8) ------------------------------------------------------
def baseline_costs(
    sessions: Sequence[BaselineSession],
    price_slots: Sequence[PriceSlot],
    power_slots: Sequence[PowerSlot],
    *,
    allow_forecast: bool = False,
) -> dict:
    """Cost of "charging at plug-in" on one charger, first come first served.

    Each session starts at max(its plug-in, the previous session's simulated
    end) and charges continuously at expected power until its energy is
    delivered. Returns outlet -> cost, or None when any of its energy falls in
    an unknown-priced period (or a forecast-priced one unless
    ``allow_forecast``), or when it never finishes.
    """
    prices = _prepare_prices(price_slots)
    powers = _prepare_powers(power_slots)
    edges = sorted({p.start for p in prices} | {p.end for p in prices}
                   | {p.start for p in powers} | {p.end for p in powers})
    results = {}
    previous_end = None
    for session in sorted(sessions or (), key=lambda s: (_utc(s.plugged_at), s.outlet)):
        start = _utc(session.plugged_at)
        if previous_end is not None:
            start = max(start, previous_end)
        cost, end = _baseline_one(session, start, prices, powers, edges, allow_forecast)
        previous_end = end if end is not None else start + timedelta(hours=BASELINE_MAX_HOURS)
        if session.outlet in results:
            prior = results[session.outlet]
            cost = None if prior is None or cost is None else prior + cost
        results[session.outlet] = cost
    return {outlet: _money(cost) for outlet, cost in results.items()}


def _baseline_one(session, start, prices, powers, edges, allow_forecast):
    energy = max(0.0, _finite(session.energy_kwh) or 0.0)
    cap = max(0.0, _finite(session.cap_kw) or 0.0)
    soc = _finite(session.soc)
    k = _finite(session.kwh_per_pct)
    taper = EXPECTED_TAPER
    if soc is None or k is None or k <= 0:
        soc, k, taper = 0.0, 1.0, NO_TAPER
    soc = _clamp(soc, 0.0, 100.0)
    if energy <= _EPS:
        return 0.0, start
    if cap <= 0:
        return None, None
    stop = start + timedelta(hours=BASELINE_MAX_HOURS)
    t = start
    left = energy
    cost = 0.0
    priced = True
    while left > 1e-9:
        if t >= stop:
            return None, None
        kw, _ = _power_at(powers, t)
        power = min(kw, cap)
        nxt = next((edge for edge in edges if edge > t), None)
        seg_end = min(nxt, stop) if nxt is not None else stop
        if power <= 0:
            t = seg_end
            continue
        hours = (seg_end - t).total_seconds() / 3600.0
        # Full-power energy still needed for ``left`` actual kWh (taper prior).
        soc_goal = soc + left / k
        if taper is not NO_TAPER and soc_goal > 100.0:
            soc_goal = 100.0
        needed = taper.virtual(k, soc, soc_goal) if taper is not NO_TAPER else left
        available = power * hours
        if available >= needed:
            delivered = left
            seg_end = t + timedelta(hours=needed / power)
            if taper is not NO_TAPER and soc_goal >= 100.0:
                delivered = (soc_goal - soc) * k
                left = 0.0
        else:
            if taper is NO_TAPER:
                delivered = available
            else:
                delivered = (taper.advance(k, soc, available, 100.0) - soc) * k
        price, kind = _price_at(prices, t)
        if price is None or kind == PRICE_UNKNOWN or (kind == PRICE_FORECAST and not allow_forecast):
            if delivered > _EPS:
                priced = False
        else:
            cost += price * delivered
        left = max(0.0, left - delivered)
        if taper is not NO_TAPER:
            soc = min(100.0, soc + delivered / k)
        t = seg_end
    return (cost if priced else None), t
