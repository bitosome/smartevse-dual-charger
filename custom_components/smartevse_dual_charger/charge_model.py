"""Learned charging model for Intelligent charging (pure stdlib, no I/O).

The model learns, from plain values the Home Assistant adapter (``intelligent.py``)
passes once per controller cycle, everything the planner needs that is not a
setting: household headroom per hour, Smart-mode charging power, energy per
battery percent, typical session energy, departure habits, a price forecast for
unpublished hours, and realised savings. It never imports Home Assistant, never
uses relative imports (it is loadable by path), never reads the host time zone
(every function that needs local time takes an explicit ``tz``) and never does
I/O: persistence is the adapter's job, driven by :attr:`ChargeModel.dirty` and
:meth:`ChargeModel.should_save`.

Units: currents A, power kW, energy kWh, prices currency/kWh (already normalised
by the adapter), SoC percent (0-100). Datetimes must be timezone-aware; returned
datetimes are UTC.

Adapter API
-----------
Construction and persistence::

    model = ChargeModel(stored_dict_or_None, vehicle_names={"vehicle_1": "Volvo XC40",
                        "vehicle_2": "Volvo EX30"}, baseline=callable_or_None)
    model.load_issues          # tuple of state paths that were invalid and reset
    model.seed_from_ledger(controller.charging_costs())   # once at startup
    ...
    model.observe_cycle(...)   # every controller cycle, in every charge mode
    if model.should_save(now):          # dirty and >= 5 min since the last save
        data = model.to_dict()          # detached deep copy (Store data_func)
        try: await store.async_save(data); model.mark_saved(now)
        except Exception: model.mark_save_failed(now)
    # on shutdown: if model.dirty: save unconditionally (async_flush)

``observe_cycle(now, tz, *, outlets, ev_power_kw, mains_phase_currents,
ev_phase_currents, socs, capacities, ledger, prices, price_currency,
controller_state, charge_reason)`` (all keyword except ``now``/``tz``):

* ``outlets``: ``{"smartevse_1": {...}, "smartevse_2": {...}}`` with keys
  ``available`` (bool, False = API blip: the outlet is ignored this cycle),
  ``connected``, ``charging`` (polled state == "Charging"), ``vehicle``
  ("vehicle_1"|"vehicle_2"|None, the current mapping), ``offered_current``
  (settings.charge_current, A), ``max_current``, ``max_mains``, ``min_current``
  (A, None = default 16/16/6), optional ``plugged_at`` (controller's
  ``intelligent_plugged_at``, used after a restart), optional ``deadline``
  (current Intelligent deadline; a change with ``now >= old deadline`` is a
  deadline roll) and optional ``reached`` (car at its minimum: enables the
  provisional realised saving). Missing outlets count as unavailable.
* ``ev_power_kw``: EV meter power (kW, None when unavailable).
* ``mains_phase_currents`` / ``ev_phase_currents``: three phase currents (A) as
  a sequence or ``{"L1","L2","L3"}`` mapping, or None.
* ``socs``: ``{"vehicle_1": {"soc": 53.0, "changed_at": dt}, ...}``; a bare
  number or ``(soc, changed_at)`` also works; None/unavailable = unknown.
* ``capacities``: ``{"vehicle_1": kWh|None, ...}`` (options override, else the
  Volvo capacity sensor) for the kWh/% acceptance band.
* ``ledger``: ``controller.charging_costs()`` snapshot (or the raw ledger data).
* ``prices``: merged price list entries (Real Electricity Price
  ``hourly_prices`` items or ``{"start","end","price"}`` dicts) with prices
  already converted to currency/kWh; see :func:`normalize_price_entries`.
* ``controller_state`` / ``charge_reason``: this cycle's controller state and
  reason ("intelligent", "intelligent_top_up", ...).

It returns the frozenset of commit events of this cycle (``household_hour``,
``power_window``, ``kwh_sample``, ``departure``, ``budget``, ``price_forecast``,
``price_list``, ``realised``, ``realised_update``). Each sub-step runs in its
own try/except; :attr:`ChargeModel.last_error` names failed steps.

Estimators (every getter returns a value plus ``source`` in
"learned"|"capacity"|"default"):

* ``base_load(at, tz) -> BaseLoad(expected_a, conservative_a, source)``
* ``hour_power(at, tz, *, max_current, max_mains, min_current, vehicle=None,
  include_car_cap=False) -> HourPower(expected_kw, conservative_kw, source)``
* ``power_slots(start, end, tz, **limits) -> [{start, end, expected_kw,
  conservative_kw, source}]`` hourly on local clock hours (planner PowerSlot).
* ``kw_per_phase_amp()``, ``calibration()``, ``car_current_cap(vehicle,
  max_current)`` -> ``Estimate(value, source)``;
  ``car_power(vehicle, max_current) -> CarPower(cap_kw, cap_low_kw,
  current_cap_a, source)``
* ``kwh_per_pct(vehicle, capacity_kwh) -> KwhPerPct(expected, conservative,
  source, samples)``; ``learned_sessions(vehicle)``
* ``budget_kwh(vehicle) -> Estimate``; ``session_budget(outlet, vehicle) ->
  Budget(remaining_kwh, budget_kwh, delivered_kwh, source)`` (vehicle None =
  unidentified: max over both vehicles)
* ``soc_estimate(outlet, now, *, car_limit, kwh_per_pct, capacity_kwh,
  last_soc, last_soc_at) -> (soc, soc_source)`` using the pure
  :func:`resolve_soc`; :func:`reached_with_hysteresis` for the reached flag.
* ``price_forecast(at, tz) -> (price|None, "forecast"|"unknown")`` and
  ``price_slots(start, end, tz, entries=None) -> [{start, end, price, kind}]``
  (live known prices, then the persisted last good list, then forecast).
* ``departure_suggestion(vehicle, ready_by) -> {suggested_ready_by,
  usually_leaves, day_class, samples} | None`` and ``suggested_ready_by(...)``.
  Raw departure minutes never leave the private state.
* ``household_summary(now, tz, **limits) -> {learned_days, night_expected_kw}``
* ``realised_sessions(tz, *, limit=10, baseline=None) -> [{ledger_id, outlet,
  vehicle, vehicle_name, date, closed, energy_kwh, cost, baseline_cost,
  saving, mixed, partial, top_up_kwh}]`` (newest first). The baseline
  callback is ``baseline(sessions, price_slots, power_slots) -> {key: cost}``
  where each session is ``{key, outlet, vehicle, plugged_at, start,
  energy_kwh}`` ordered by plug-in (use ``key`` as the planner's outlet id),
  ``price_slots`` are ``{start, end, price, kind}`` and ``power_slots``
  ``{start, end, expected_kw, conservative_kw}`` from the window snapshots.
* ``discard_session(outlet)``: §8.11 cross-check swap; drops this session's
  kWh/% sample, departure and car-current samples.
* ``learning_summary()``: counts only, for diagnostics.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import math
from typing import Any, NamedTuple

SCHEMA_VERSION = 1
VEHICLES = ("vehicle_1", "vehicle_2")
OUTLETS = ("smartevse_1", "smartevse_2")
DAY_CLASSES = ("weekday", "weekend")
INTELLIGENT_STATE = "intelligent"
TOP_UP_REASON = "intelligent_top_up"
SOURCE_LEARNED = "learned"
SOURCE_CAPACITY = "capacity"
SOURCE_DEFAULT = "default"
SOURCE_SESSION = "session"  # this plug session's evidence (a car drawing less than offered)

# §8.1 household load
HOUSEHOLD_MIN_SAMPLES = 30
HOUSEHOLD_BUCKET_LIMIT = 14
HOUSEHOLD_MIN_BUCKET = 3
HOUSEHOLD_DAY_MIN_HOURS = 12  # a "committed day" has at least this many committed hours
HOUSEHOLD_DAYS_LIMIT = 31
HOUSEHOLD_RECENT_WINDOW = timedelta(minutes=60)  # persistence: the last hour of samples
HOUSEHOLD_RECENT_MIN_SPAN = timedelta(minutes=30)
HOUSEHOLD_RECENT_NEAR = timedelta(hours=2)  # the current and the next local hour
HOUSEHOLD_RECENT_MAX_EXPECTED_A = 8.0  # bounds for hours without a learned bucket
HOUSEHOLD_RECENT_MAX_CONSERVATIVE_A = 9.0
# The persistence floor is re-read on the first cycle of each 15-minute planning unit
# (UTC grid, as the planner) and in 0.5 A steps rounded up, so a spiky house does not
# change the power slots (and the payload) in the middle of a unit.
HOUSEHOLD_RECENT_REFRESH = timedelta(minutes=15)
HOUSEHOLD_RECENT_STEP_A = 0.5
DEFAULT_BASE_EXPECTED_A = 3.0
DEFAULT_BASE_CONSERVATIVE_A = 5.0
BASE_CONSERVATIVE_MARGIN_A = 1.0
BASE_CONSERVATIVE_QUANTILE = 0.8
HOUR_P90 = 0.9
COLD_START_DAYS = 7
COLD_START_MAX_CONSERVATIVE_KW = 8.5
MAX_MAINS_PHASE_A = 200.0
MAX_EV_PHASE_A = 100.0

# §8.2 power physics
PHASES = 3
DEFAULT_MAX_CURRENT = 16.0
DEFAULT_MAX_MAINS = 16.0
DEFAULT_MIN_CURRENT = 6.0
DEFAULT_KW_PER_PHASE_AMP = 0.235
KW_PER_PHASE_AMP_BOUNDS = (0.20, 0.25)
KW_PER_PHASE_AMP_SAMPLE_BOUNDS = (0.15, 0.30)
KW_PER_PHASE_AMP_LIMIT = 48
KW_PER_PHASE_AMP_MIN_SAMPLES = 3
CAR_CAP_OFFSET_A = 0.8
CAR_CAP_FULL_OFFER_MARGIN_A = 0.5
CAR_CAP_QUANTILE = 0.9
CAR_CAP_LIMIT = 48
CAR_CAP_MIN_SAMPLES = 3
CAR_CAP_BOUNDS = (1.0, 80.0)
CAP_LOW_FACTOR = 0.95
CALIBRATION_LIMIT = 24
CALIBRATION_MIN_SAMPLES = 3
# Calibration compares against the measured base (physics only), so a car or charger
# that really delivers less (e.g. 75 % of nominal) is followed down to half.
CALIBRATION_BOUNDS = (0.5, 1.10)
CALIBRATION_SAMPLE_BOUNDS = (0.3, 3.0)
CALIBRATION_CAR_LIMITED_A = 2.0  # car drew this much below its offer: not a calibration window
# A car that keeps drawing clearly less than offered (cold battery, its own limit) in
# this plug session: the plan uses the median of its last six car-limited windows as
# its current cap (two are enough). Windows where it took the whole, lower offer
# (heat-pump hours) do not end it; a window where it draws more than the cap + 1 A
# does. The evidence lasts the plug session (a waiting battery does not warm up).
SESSION_CAP_WINDOWS = 6
SESSION_CAP_MIN_LIMITED = 2
SESSION_CAP_LIFT_A = 1.0
POWER_WINDOW_SECONDS = 300
POWER_WINDOW_MIN_SAMPLES = 3
POWER_MIN_KW = 1.5
POWER_MAX_SOC = 90.0
POWER_SANITY_FACTOR = 1.1  # P <= 1.1 x offered x 3 x upper kW/phase-amp bound
MAX_GAP_SECONDS = 120

# §8.3 energy per percent
KWH_MIN_POINTS = 15.0
KWH_MAX_SOC = 90.0
KWH_BAND = (0.85, 1.35)
KWH_SAMPLE_BOUNDS = (0.05, 2.0)
KWH_LIMIT = 7
KWH_CONSERVATIVE_LAST = 5
KWH_MIN_SAMPLES = 3
KWH_DEFAULT_FACTOR = 1.05
KWH_UNOBSERVED_TOLERANCE_S = 30.0
DEFAULT_CAPACITY_KWH = 75.0
CAPACITY_BOUNDS = (1.0, 300.0)

# §8.4 SoC handling
SOC_ESTIMATE_AGE = timedelta(minutes=10)
SOC_UNVERIFIED_AGE = timedelta(minutes=15)
REACHED_HYSTERESIS = 2.0

# §8.5 unknown-SoC budget
BUDGET_MIN_RECORD_KWH = 5.0
BUDGET_MAX_UNOBSERVED_S = 120.0
BUDGET_DIRTY_ISSUES = frozenset({"start_not_observed", "restart_or_telemetry_gap"})
BUDGET_LIMIT = 10
BUDGET_FLOOR_KWH = 30.0
BUDGET_QUANTILE = 0.8
BUDGET_MAX_KWH = 300.0

# §8.6 departures
DEPARTURE_SPAN_MINUTE = 3 * 60
DEPARTURE_WINDOW = (4 * 60, 13 * 60)
DEPARTURE_RECONNECT = timedelta(minutes=60)
DEPARTURE_LIMIT = 28
DEPARTURE_MIN_SAMPLES = 8
DEPARTURE_OUTLIER_MINUTES = 90
DEPARTURE_QUANTILE = 0.2
DEPARTURE_LEAD_MINUTES = 10
DEPARTURE_STEP_MINUTES = 15
DEPARTURE_HIDE_MINUTES = 15

# §8.7 price forecast
PRICE_BUCKET_LIMIT = 14
PRICE_MIN_BUCKET = 3
PRICE_ABS_MAX = 100.0

# §8.8 realised saving
REALISED_LIMIT = 30
REALISED_SNAPSHOT_HOURS = 24
# The plug-in baseline of a window starts at most this long before its deadline
# (one day, plus the late plug-in hour of §6.2), and so does its snapshot.
REALISED_BASELINE_LOOKBACK = timedelta(hours=25)
REALISED_SNAPSHOT_MAX = 2 * REALISED_SNAPSHOT_HOURS + 6
# Stored times outside this range are corrupt (and would overflow time zone maths).
def _unit_start(value: datetime) -> datetime:
    """Start of the 15-minute UTC planning unit containing ``value``."""
    value = value.astimezone(UTC)
    return value.replace(minute=value.minute - value.minute % 15, second=0, microsecond=0)


_TIME_RANGE = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC))
REALISED_MAX_UNOBSERVED_S = 120.0
SHARED_OVERLAP_TOLERANCE_S = 10.0
MIXED_TOLERANCE_KWH = 0.05
UNPRICED_TOLERANCE_KWH = 1e-6
REALISED_MIN_KWH = 0.1  # no charge in the window -> no saving claim

# §8.9 last good prices
LAST_GOOD_LIMIT = 400

# §8.10 persistence
SAVE_INTERVAL_SECONDS = 300

_LEDGER_FIELDS = ("energy_kwh", "cost", "priced_kwh", "unpriced_kwh", "unobserved_seconds")
_PART_FIELDS = ("energy_kwh", "cost", "priced_kwh")
_KIND_CODES = {"known": "k", "forecast": "f", "unknown": "u"}
_KIND_NAMES = {code: name for name, code in _KIND_CODES.items()}
_MONEY_MAX = 1e6


class Estimate(NamedTuple):
    value: float
    source: str


class BaseLoad(NamedTuple):
    expected_a: float
    conservative_a: float
    source: str


class HourPower(NamedTuple):
    expected_kw: float
    conservative_kw: float
    source: str


class CarPower(NamedTuple):
    cap_kw: float
    cap_low_kw: float
    current_cap_a: float
    source: str


class KwhPerPct(NamedTuple):
    expected: float
    conservative: float
    source: str
    samples: int


class Budget(NamedTuple):
    remaining_kwh: float
    budget_kwh: float
    delivered_kwh: float
    source: str


class SocReading(NamedTuple):
    soc: float | None
    changed_at: datetime | None


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    """Finite float from an int/float/numeric string; bools and NaN are None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _strict(value: Any, low: float, high: float) -> bool:
    """Stored-state number check: real int/float, finite, inside bounds.

    JSON gives arbitrarily large ints (``10**400``) that ``math.isfinite``
    cannot convert; only ints a float represents exactly are accepted.
    """
    if type(value) is int:
        return abs(value) <= _MAX_EXACT_INT and low <= value <= high
    return type(value) is float and math.isfinite(value) and low <= value <= high


_MAX_EXACT_INT = 2 ** 53


def _hour_key(hour: Any) -> bool:
    """A stored bucket key "0".."23" (never int() of an unbounded digit string)."""
    return (isinstance(hour, str) and 0 < len(hour) <= 2 and hour.isascii() and hour.isdigit()
            and 0 <= int(hour) <= 23)


def _strict_int(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _aware(value: Any) -> datetime | None:
    """Aware datetime from a datetime or ISO string; naive values are rejected."""
    if isinstance(value, datetime):
        return value if value.utcoffset() is not None else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.utcoffset() is not None else None
    return None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _epoch_min(value: datetime) -> int:
    return int(value.timestamp() // 60)


def _from_min(minutes: int) -> datetime:
    return datetime.fromtimestamp(minutes * 60, UTC)


def _quantile(values: Iterable[float], q: float) -> float:
    """Linear-interpolation quantile (numpy default); deterministic."""
    data = sorted(values)
    if not data:
        raise ValueError("quantile of an empty list")
    position = (len(data) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    return data[low] + (data[high] - data[low]) * (position - low)


def _median(values: Iterable[float]) -> float:
    return _quantile(values, 0.5)


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    return min(max(value, bounds[0]), bounds[1])


def _soc_value(value: Any) -> float | None:
    number = _num(value)
    return number if number is not None and 0 <= number <= 100 else None


def _minutes_of(value: Any) -> int | None:
    """Minutes after midnight from "HH:MM[:SS]", datetime.time or an int."""
    if isinstance(value, time):
        return value.hour * 60 + value.minute
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1440:
        return value
    if isinstance(value, str):
        parts = value.strip().split(":")
        if len(parts) in (2, 3) and all(0 < len(part) <= 2 and part.isascii() and part.isdigit() for part in parts):
            hour, minute = int(parts[0]), int(parts[1])
            if 0 <= hour < 24 and 0 <= minute < 60:
                return hour * 60 + minute
    return None


def _hhmm(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def local_hour_start(at: datetime, tz: tzinfo) -> datetime:
    """UTC start of the local clock hour containing ``at`` (fold-safe)."""
    local = at.astimezone(tz)
    return local.replace(minute=0, second=0, microsecond=0).astimezone(UTC)


def _hours(start: datetime, end: datetime, tz: tzinfo):
    """Yield (start, end) UTC pairs of local clock hours covering [start, end)."""
    current = local_hour_start(start, tz)
    while current < end:
        following = current + timedelta(hours=1)
        yield current, following
        current = following


def day_class(local_day: date, flags: Mapping[date, bool] | None = None) -> str:
    """"weekend" for weekend/holiday dates (price-list flags first), else "weekday"."""
    off = flags.get(local_day) if flags else None
    if off is None:
        off = local_day.weekday() >= 5
    return "weekend" if off else "weekday"


def _spans_local_minute(start: datetime, end: datetime, tz: tzinfo, minute: int) -> bool:
    """True if the first local ``minute``-of-day after ``start`` is <= ``end``."""
    local = start.astimezone(tz)
    clock = time(minute // 60, minute % 60)
    for offset in range(3):
        candidate = datetime.combine(local.date() + timedelta(days=offset), clock, tzinfo=tz)
        if candidate > start:
            return candidate <= end
    return False


def _phases(value: Any, maximum: float) -> tuple[float, float, float] | None:
    if isinstance(value, Mapping):
        value = [value.get("L1"), value.get("L2"), value.get("L3")]
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    result = []
    for item in value:
        number = _num(item)
        if number is None or not 0 <= number <= maximum:
            return None
        result.append(number)
    return tuple(result)


def _limits(max_current: Any = None, max_mains: Any = None,
            min_current: Any = None) -> tuple[float, float, float]:
    """Charger limits with the §8.2 defaults (16/16/6 A) for missing values."""
    current = _num(max_current)
    if current is None or not 1 <= current <= 80:
        current = DEFAULT_MAX_CURRENT
    mains = _num(max_mains)
    if mains is None or not 1 <= mains <= 200:
        mains = DEFAULT_MAX_MAINS
    minimum = _num(min_current)
    if minimum is None or not 0 < minimum <= current:
        minimum = min(DEFAULT_MIN_CURRENT, current)
    return current, mains, minimum


def normalize_price_entries(entries: Iterable[Any] | None, *,
                            factor: float = 1.0) -> list[dict[str, Any]]:
    """Merge price entries into a sorted, start-deduplicated list.

    Accepts Real Electricity Price ``hourly_prices`` items (``start_time``,
    ``end_time``, ``actual_price``, ``is_weekend``, ``is_holiday``) or
    ``{"start", "end", "price"}`` dicts; datetimes may be aware objects or ISO
    strings. ``factor`` converts the list's unit to currency/kWh. A known price
    wins over a null one for the same start. Result items:
    ``{"start": utc dt, "end": utc dt, "price": float|None, "off_day": bool|None}``.
    """
    scale = _num(factor)
    if scale is None or scale <= 0:
        scale = 1.0
    merged: dict[int, dict[str, Any]] = {}
    for raw in entries or ():
        if not isinstance(raw, Mapping):
            continue
        start = _aware(raw.get("start", raw.get("start_time")))
        end = _aware(raw.get("end", raw.get("end_time")))
        if start is None or end is None or end <= start:
            continue
        price = _num(raw["price"] if "price" in raw else raw.get("actual_price"))
        if price is not None:
            price *= scale
            if not math.isfinite(price) or abs(price) > PRICE_ABS_MAX:
                price = None
        weekend, holiday = raw.get("is_weekend"), raw.get("is_holiday")
        off_day = None
        if isinstance(weekend, bool) or isinstance(holiday, bool):
            off_day = weekend is True or holiday is True
        key = _epoch_min(start)
        entry = {"start": start.astimezone(UTC), "end": end.astimezone(UTC),
                 "price": price, "off_day": off_day}
        previous = merged.get(key)
        if previous is None or (previous["price"] is None and price is not None):
            merged[key] = entry
    return [merged[key] for key in sorted(merged)]


def resolve_soc(now: datetime, *, live_soc: Any = None, live_changed_at: Any = None,
                last_soc: Any = None, last_soc_at: Any = None, plugged_at: Any = None,
                charging: bool = False, energy_since_kwh: Any = None,
                kwh_per_pct: Any = None, car_limit: Any = None) -> tuple[float | None, str]:
    """§8.4 SoC and its source for one car (pure).

    * live numeric SoC -> "vehicle"; else the last SoC of this plug session ->
      "last_known"; neither -> ``(None, "unknown")``.
    * The reading is "unverified" when it last changed more than 15 min before
      the plug event (a value from before a drive looks fresh on Volvo).
    * When energy was metered since the reading and the reading is more than
      10 min old (or its age is unknown), ``soc + energy_since_kwh / kwh_per_pct``
      capped at ``car_limit`` -> "estimated" (stays "unverified" for an
      unverified base). ``energy_since_kwh`` is the exclusive energy metered
      since the reading. This holds after the charger stops too: the delivered
      energy stays in the battery until a newer reading replaces the anchor, so
      a stalled reading never pulls a full car back below its target.
      ``charging`` is kept for callers and no longer gates the estimate.
    """
    soc, changed, source = _soc_value(live_soc), _aware(live_changed_at), "vehicle"
    if soc is None:
        soc, changed, source = _soc_value(last_soc), _aware(last_soc_at), "last_known"
        if soc is None:
            return None, "unknown"
    plugged = _aware(plugged_at)
    unverified = changed is not None and plugged is not None and changed < plugged - SOC_UNVERIFIED_AGE
    energy = _num(energy_since_kwh) or 0.0
    per_pct = _num(kwh_per_pct)
    stale = changed is None or now - changed > SOC_ESTIMATE_AGE
    if stale and energy > 0 and per_pct is not None and per_pct > 0:
        limit = _soc_value(car_limit)
        ceiling = max(limit if limit is not None else 100.0, soc)
        estimate = min(soc + energy / per_pct, ceiling)
        if estimate > soc:
            return round(estimate, 1), "unverified" if unverified else "estimated"
    return soc, "unverified" if unverified else source


def reached_with_hysteresis(soc: Any, target: Any, was_reached: bool = False, *,
                            hysteresis: float = REACHED_HYSTERESIS) -> bool:
    """§8.4: once reached in this session, plan again only below target - 2."""
    value, goal = _num(soc), _num(target)
    if value is None or goal is None:
        return bool(was_reached)
    if was_reached:
        return value >= goal - hysteresis
    return value >= goal


# ---------------------------------------------------------------------------
# State: defaults and per-field validation
# ---------------------------------------------------------------------------

def default_state() -> dict[str, Any]:
    """Cold-start state (schema 1)."""
    return {
        "schema": SCHEMA_VERSION,
        "household": {"buckets": {dc: {} for dc in DAY_CLASSES}, "days": []},
        "power": {"kw_per_phase_amp": [], "calibration": [],
                  "car_cap": {vehicle: [] for vehicle in VEHICLES}},
        "energy": {vehicle: [] for vehicle in VEHICLES},
        "budget": {vehicle: [] for vehicle in VEHICLES},
        "departures": {vehicle: {dc: [] for dc in DAY_CLASSES} for vehicle in VEHICLES},
        "prices": {dc: {} for dc in DAY_CLASSES},
        "last_good": None,
        "realised": [],
    }


def _list_of(value: Any, limit: int, item_ok: Callable[[Any], bool]) -> bool:
    return isinstance(value, list) and len(value) <= limit and all(item_ok(item) for item in value)


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _valid_household_item(item: Any) -> bool:
    return (isinstance(item, list) and len(item) == 2
            and all(_strict(v, 0.0, MAX_MAINS_PHASE_A) for v in item))


def _valid_day(item: Any) -> bool:
    return (isinstance(item, list) and len(item) == 2 and _valid_date(item[0])
            and _strict_int(item[1], 1, 25))


def _valid_budget_item(item: Any) -> bool:
    return (isinstance(item, list) and len(item) == 3
            and isinstance(item[0], str) and 0 < len(item[0]) <= 64
            and _strict(item[1], BUDGET_MIN_RECORD_KWH, BUDGET_MAX_KWH)
            and _strict_int(item[2], 0, 10**9))


def _valid_price_item(item: Any) -> bool:
    return (isinstance(item, list) and len(item) == 2
            and _strict_int(item[0], 0, 10**9) and _strict(item[1], -PRICE_ABS_MAX, PRICE_ABS_MAX))


def _valid_slot(item: Any) -> bool:
    return (isinstance(item, list) and len(item) == 3
            and _strict_int(item[0], 0, 10**9) and _strict_int(item[1], 0, 10**9)
            and item[1] > item[0] and _strict(item[2], -PRICE_ABS_MAX, PRICE_ABS_MAX))


def _valid_time_text(value: Any, nullable: bool = True) -> bool:
    if value is None:
        return nullable
    parsed = _aware(value) if isinstance(value, str) else None
    if parsed is None:
        return False
    try:
        return _TIME_RANGE[0] <= parsed <= _TIME_RANGE[1]
    except (OverflowError, ValueError):
        return False


def _valid_parts(value: Any, fields: tuple[str, ...]) -> bool:
    return (isinstance(value, dict) and set(value) == set(fields)
            and all(_strict(value[f], -_MONEY_MAX, _MONEY_MAX) for f in fields)
            and all(value[f] >= 0 for f in fields if f != "cost"))


def _valid_snapshot(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"t0", "prices", "kinds", "kw"}:
        return False
    prices, kinds, powers = value["prices"], value["kinds"], value["kw"]
    return (_strict_int(value["t0"], 0, 10**9)
            and isinstance(prices, list) and isinstance(powers, list) and isinstance(kinds, str)
            and len(prices) == len(powers) == len(kinds) <= REALISED_SNAPSHOT_MAX
            and all(p is None or _strict(p, -PRICE_ABS_MAX, PRICE_ABS_MAX) for p in prices)
            and all(_strict(kw, 0.0, 100.0) for kw in powers)
            and all(code in _KIND_NAMES for code in kinds))


_REALISED_KEYS = frozenset({
    "id", "outlet", "vehicle", "ledger_id", "plugged_at", "start", "deadline", "closed_at",
    "base", "last", "intelligent", "top_up", "other", "unpriced_kwh",
    "unobserved_seconds", "mixed", "reached", "snapshot", "baseline_cost", "final",
})
# Added in review round 3; records written before it have no baseline start.
_REALISED_OPTIONAL_KEYS = frozenset({"baseline_start"})


def _valid_realised(record: Any) -> bool:
    return (isinstance(record, dict) and _REALISED_KEYS <= set(record)
            and set(record) <= _REALISED_KEYS | _REALISED_OPTIONAL_KEYS
            and _valid_time_text(record.get("baseline_start"))
            and isinstance(record["id"], str) and 0 < len(record["id"]) <= 64
            and record["outlet"] in OUTLETS
            and (record["vehicle"] is None or record["vehicle"] in VEHICLES)
            and (record["ledger_id"] is None
                 or isinstance(record["ledger_id"], str) and 0 < len(record["ledger_id"]) <= 64)
            and _valid_time_text(record["plugged_at"])
            and _valid_time_text(record["start"], nullable=False)
            and _valid_time_text(record["deadline"])
            and _valid_time_text(record["closed_at"])
            and _valid_parts(record["base"], _LEDGER_FIELDS)
            and _valid_parts(record["last"], _LEDGER_FIELDS)
            and _valid_parts(record["intelligent"], _PART_FIELDS)
            and _valid_parts(record["top_up"], _PART_FIELDS)
            and _valid_parts(record["other"], _PART_FIELDS)
            and all(_strict(record[f], 0.0, _MONEY_MAX)
                    for f in ("unpriced_kwh", "unobserved_seconds"))
            and type(record["mixed"]) is bool and type(record["reached"]) is bool
            and type(record["final"]) is bool
            and _valid_snapshot(record["snapshot"])
            and (record["baseline_cost"] is None
                 or _strict(record["baseline_cost"], -_MONEY_MAX, _MONEY_MAX)))


def load_state(data: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate stored state field by field.

    Returns ``(state, issues)``. A bad field (wrong type, non-finite, out of
    bounds, oversized list) resets only itself to its default and its path is
    listed in ``issues``; ``None`` (no file) gives defaults without issues.
    """
    state = default_state()
    issues: list[str] = []
    if data is None:
        return state, issues
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        return state, ["schema"]

    def take(path: str, value: Any, valid: bool, assign: Callable[[Any], None]) -> None:
        if valid:
            assign(deepcopy(value))
        else:
            issues.append(path)

    def section(name: str) -> dict:
        value = data.get(name)
        if isinstance(value, dict):
            return value
        if name in data:
            issues.append(name)
        return {}

    household = section("household")
    buckets = household.get("buckets") if isinstance(household.get("buckets"), dict) else {}
    if "buckets" in household and not isinstance(household.get("buckets"), dict):
        issues.append("household.buckets")
    for dc in DAY_CLASSES:
        hours = buckets.get(dc)
        if hours is None:
            continue
        if not isinstance(hours, dict):
            issues.append(f"household.buckets.{dc}")
            continue
        for hour, items in hours.items():
            path = f"household.buckets.{dc}.{hour}"
            if not _hour_key(hour):
                issues.append(path)
                continue
            take(path, items, _list_of(items, HOUSEHOLD_BUCKET_LIMIT, _valid_household_item),
                 lambda v, dc=dc, hour=hour: state["household"]["buckets"][dc].__setitem__(str(int(hour)), v))
    if "days" in household:
        days = household.get("days")
        take("household.days", days,
             _list_of(days, HOUSEHOLD_DAYS_LIMIT, _valid_day)
             and len({item[0] for item in days}) == len(days),
             lambda v: state["household"].__setitem__("days", v))

    power = section("power")
    if "kw_per_phase_amp" in power:
        value = power["kw_per_phase_amp"]
        take("power.kw_per_phase_amp", value,
             _list_of(value, KW_PER_PHASE_AMP_LIMIT,
                      lambda x: _strict(x, *KW_PER_PHASE_AMP_SAMPLE_BOUNDS)),
             lambda v: state["power"].__setitem__("kw_per_phase_amp", v))
    if "calibration" in power:
        value = power["calibration"]
        take("power.calibration", value,
             _list_of(value, CALIBRATION_LIMIT, lambda x: _strict(x, *CALIBRATION_SAMPLE_BOUNDS)),
             lambda v: state["power"].__setitem__("calibration", v))
    caps = power.get("car_cap")
    if "car_cap" in power and not isinstance(caps, dict):
        issues.append("power.car_cap")
        caps = {}
    for vehicle in VEHICLES:
        if isinstance(caps, dict) and vehicle in caps:
            value = caps[vehicle]
            take(f"power.car_cap.{vehicle}", value,
                 _list_of(value, CAR_CAP_LIMIT, lambda x: _strict(x, *CAR_CAP_BOUNDS)),
                 lambda v, vehicle=vehicle: state["power"]["car_cap"].__setitem__(vehicle, v))

    energy = section("energy")
    for vehicle in VEHICLES:
        if vehicle in energy:
            value = energy[vehicle]
            take(f"energy.{vehicle}", value,
                 _list_of(value, KWH_LIMIT, lambda x: _strict(x, *KWH_SAMPLE_BOUNDS)),
                 lambda v, vehicle=vehicle: state["energy"].__setitem__(vehicle, v))

    budget = section("budget")
    for vehicle in VEHICLES:
        if vehicle in budget:
            value = budget[vehicle]
            take(f"budget.{vehicle}", value,
                 _list_of(value, BUDGET_LIMIT, _valid_budget_item)
                 and len({item[0] for item in value}) == len(value),
                 lambda v, vehicle=vehicle: state["budget"].__setitem__(vehicle, v))

    departures = section("departures")
    for vehicle in VEHICLES:
        per_vehicle = departures.get(vehicle)
        if per_vehicle is None:
            continue
        if not isinstance(per_vehicle, dict):
            issues.append(f"departures.{vehicle}")
            continue
        for dc in DAY_CLASSES:
            if dc in per_vehicle:
                value = per_vehicle[dc]
                take(f"departures.{vehicle}.{dc}", value,
                     _list_of(value, DEPARTURE_LIMIT, lambda x: _strict_int(x, 0, 1439)),
                     lambda v, vehicle=vehicle, dc=dc: state["departures"][vehicle].__setitem__(dc, v))

    prices = section("prices")
    for dc in DAY_CLASSES:
        hours = prices.get(dc)
        if hours is None:
            continue
        if not isinstance(hours, dict):
            issues.append(f"prices.{dc}")
            continue
        for hour, items in hours.items():
            path = f"prices.{dc}.{hour}"
            if not _hour_key(hour):
                issues.append(path)
                continue
            take(path, items,
                 _list_of(items, PRICE_BUCKET_LIMIT, _valid_price_item)
                 and len({item[0] for item in items}) == len(items),
                 lambda v, dc=dc, hour=hour: state["prices"][dc].__setitem__(str(int(hour)), v))

    if data.get("last_good") is not None:
        value = data["last_good"]
        take("last_good", value,
             isinstance(value, dict) and set(value) == {"fetched_at", "currency", "slots"}
             and _valid_time_text(value["fetched_at"], nullable=False)
             and (value["currency"] is None
                  or isinstance(value["currency"], str) and 0 < len(value["currency"]) <= 8)
             and _list_of(value["slots"], LAST_GOOD_LIMIT, _valid_slot),
             lambda v: state.__setitem__("last_good", v))

    if "realised" in data:
        value = data["realised"]
        if not isinstance(value, list) or len(value) > REALISED_LIMIT + len(OUTLETS):
            issues.append("realised")
        else:
            seen: set[str] = set()
            open_outlets: set[str] = set()
            for index, record in enumerate(value):
                ok = _valid_realised(record) and record["id"] not in seen
                if ok and record["closed_at"] is None:
                    ok = record["outlet"] not in open_outlets
                    open_outlets.add(record["outlet"])
                if ok:
                    seen.add(record["id"])
                    record = deepcopy(record)
                    record.setdefault("baseline_start", None)
                    state["realised"].append(record)
                else:
                    issues.append(f"realised.{index}")
    return state, issues


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

def _norm_outlet(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, Mapping) else {}
    connected = raw.get("connected") is True
    vehicle = raw.get("vehicle")
    return {
        "available": raw.get("available") is True,
        "connected": connected,
        "charging": connected and raw.get("charging") is True,
        "vehicle": vehicle if vehicle in VEHICLES else None,
        "offered": _num(raw.get("offered_current", raw.get("charge_current"))),
        "max_current": _num(raw.get("max_current")),
        "max_mains": _num(raw.get("max_mains")),
        "min_current": _num(raw.get("min_current")),
        "plugged_at": _aware(raw.get("plugged_at")),
        "deadline": _aware(raw.get("deadline")),
        "reached": raw.get("reached") is True,
    }


def _norm_socs(socs: Any) -> dict[str, SocReading]:
    result = {}
    for vehicle in VEHICLES:
        raw = socs.get(vehicle) if isinstance(socs, Mapping) else None
        if isinstance(raw, Mapping):
            soc = raw.get("soc", raw.get("value"))
            changed = raw.get("changed_at", raw.get("last_changed"))
        elif isinstance(raw, (tuple, list)) and len(raw) == 2:
            soc, changed = raw
        else:
            soc, changed = raw, None
        result[vehicle] = SocReading(_soc_value(soc), _aware(changed))
    return result


def _ledger_values(record: Any) -> dict[str, float] | None:
    if not isinstance(record, Mapping):
        return None
    values = {}
    for field in _LEDGER_FIELDS:
        number = _num(record.get(field))
        if number is None or abs(number) > _MONEY_MAX or (field != "cost" and number < 0):
            return None
        values[field] = number
    return values


def _ledger_outlet_records(ledger: Mapping, outlet: str) -> list[Mapping]:
    """Newest-first records of one outlet from a snapshot or raw ledger data."""
    chargers = ledger.get("chargers")
    if isinstance(chargers, Mapping):
        charger = chargers.get(outlet)
        if not isinstance(charger, Mapping):
            return []
        history = charger.get("history")
        if isinstance(history, list) and history:
            return [r for r in history if isinstance(r, Mapping)]
        session = charger.get("session")
        return [session] if isinstance(session, Mapping) else []
    records = ledger.get("records")
    if isinstance(records, list):
        return [r for r in reversed(records) if isinstance(r, Mapping) and r.get("charger") == outlet]
    return []


def _zero_parts(fields: tuple[str, ...]) -> dict[str, float]:
    return {field: 0.0 for field in fields}


class ChargeModel:
    """Learned model state plus per-cycle observation; see the module docstring."""

    def __init__(self, stored: Any = None, *, vehicle_names: Mapping[str, str] | None = None,
                 baseline: Callable[..., Mapping[str, Any]] | None = None) -> None:
        self._state, issues = load_state(stored)
        self.load_issues: tuple[str, ...] = tuple(issues)
        self.vehicle_names: dict[str, str] = {
            k: v for k, v in (vehicle_names or {}).items() if k in VEHICLES and isinstance(v, str)}
        self.baseline = baseline
        self.last_events: frozenset[str] = frozenset()
        self.last_error: str | None = None
        self._dirty = False
        self._last_save_at: datetime | None = None
        self._events: set[str] = set()
        self._errors: list[str] = []
        # Transient per-cycle state; never persisted (§8.10).
        self._hour_acc: dict[str, Any] | None = None
        # Max-phase household base of the last hour, (time, amps) per cycle (§8.1
        # persistence: what the house draws right now).
        self._recent_base: list[tuple[datetime, float]] = []
        self._recent_snapshot: tuple[datetime, tuple[datetime, float, float] | None] | None = None
        self._run: dict[str, Any] | None = None
        self._sessions: dict[str, dict[str, Any]] = {}
        self._connected: dict[str, bool] = {}
        self._pending_departures: list[dict[str, Any]] = []
        self._day_flags: dict[date, bool] = {}
        self._known: list[dict[str, Any]] = []
        self._ingested: set[int] = {
            item[0] for hours in self._state["prices"].values()
            for items in hours.values() for item in items}
        self._budget_checked: set[str] = set()
        self._ledger_ok = False
        self._ledger_active: dict[str, tuple[str, dict[str, float]]] = {}
        self._ledger_seen: dict[str, dict[str, float]] = {}
        self._outlets: dict[str, dict[str, Any]] = {k: _norm_outlet(None) for k in OUTLETS}
        self._socs: dict[str, SocReading] = _norm_socs(None)
        self._prev_state: str | None = None
        self._prev_reason: str | None = None
        self._prev_shared = False
        self._last_observed_at: datetime | None = None

    # -- persistence -------------------------------------------------------

    @property
    def dirty(self) -> bool:
        """Persisted state changed since the last successful save."""
        return self._dirty

    def should_save(self, now: datetime, *, min_interval: float = SAVE_INTERVAL_SECONDS) -> bool:
        """Dirty and at least ``min_interval`` seconds since the last save attempt."""
        if not self._dirty:
            return False
        return (self._last_save_at is None
                or (now - self._last_save_at).total_seconds() >= min_interval
                or now < self._last_save_at)

    def mark_saved(self, now: datetime) -> None:
        self._dirty = False
        self._last_save_at = now

    def mark_save_failed(self, now: datetime) -> None:
        """Keep the state dirty but throttle the next attempt."""
        self._last_save_at = now

    def to_dict(self) -> dict[str, Any]:
        """Detached deep copy of the persisted state (JSON-friendly)."""
        return deepcopy(self._state)

    def _commit(self, event: str) -> None:
        self._events.add(event)
        self._dirty = True

    # -- observation ---------------------------------------------------------

    def observe_cycle(self, now: datetime, tz: tzinfo, *, outlets: Mapping[str, Any] | None = None,
                      ev_power_kw: Any = None, mains_phase_currents: Any = None,
                      ev_phase_currents: Any = None, socs: Any = None,
                      capacities: Mapping[str, Any] | None = None, ledger: Any = None,
                      prices: Iterable[Any] | None = None, price_currency: str | None = None,
                      controller_state: Any = None, charge_reason: Any = None) -> frozenset[str]:
        """Observe one controller cycle; returns this cycle's commit events."""
        if _aware(now) is None:
            raise ValueError("now must be timezone-aware")
        self._events = set()
        self._errors = []
        raw_outlets = outlets if isinstance(outlets, Mapping) else {}
        norm = {k: _norm_outlet(raw_outlets.get(k)) for k in OUTLETS}
        soc_map = _norm_socs(socs)
        caps = {}
        for vehicle in VEHICLES:
            value = _num(capacities.get(vehicle)) if isinstance(capacities, Mapping) else None
            caps[vehicle] = value if value is not None and CAPACITY_BOUNDS[0] <= value <= CAPACITY_BOUNDS[1] else None
        power = _num(ev_power_kw)
        power = power if power is not None and power >= 0 else None
        mains = _phases(mains_phase_currents, MAX_MAINS_PHASE_A)
        ev_phases = _phases(ev_phase_currents, MAX_EV_PHASE_A)
        state = str(controller_state).lower() if controller_state is not None else None
        reason = str(charge_reason).lower() if charge_reason is not None else None
        previous = self._last_observed_at
        gap = previous is not None and (
            now < previous or (now - previous).total_seconds() > MAX_GAP_SECONDS)
        charging = [k for k in OUTLETS if norm[k]["available"] and norm[k]["charging"]]
        exclusive = (charging[0] if len(charging) == 1
                     and all(norm[k]["available"] for k in OUTLETS) else None)
        unplugged: set[str] = set()

        def run(name: str, step: Callable[[], Any]) -> Any:
            try:
                return step()
            except Exception as err:  # noqa: BLE001 - model steps must never break the cycle
                self._errors.append(f"{name}: {type(err).__name__}: {err}")
                return None

        run("prices", lambda: self._observe_prices(now, tz, prices, price_currency))
        run("ledger", lambda: self._observe_ledger(ledger))
        result = run("sessions", lambda: self._observe_sessions(
            now, tz, norm, soc_map, caps, exclusive, gap))
        unplugged = result or set()
        run("household", lambda: self._observe_household(now, tz, mains, ev_phases, bool(charging)))
        run("power", lambda: self._observe_power(now, tz, norm, power, ev_phases, soc_map,
                                                  exclusive, gap, mains))
        run("realised", lambda: self._observe_realised(now, tz, norm, state, reason,
                                                        unplugged, len(charging) >= 2))

        self._outlets = norm
        self._socs = soc_map
        self._prev_state = state
        self._prev_reason = reason
        self._prev_shared = len(charging) >= 2
        self._last_observed_at = now
        self.last_events = frozenset(self._events)
        self.last_error = "; ".join(self._errors) or None
        return self.last_events

    # -- prices (§8.7, §8.9) -------------------------------------------------

    def _observe_prices(self, now: datetime, tz: tzinfo, prices: Any, currency: Any) -> None:
        entries = normalize_price_entries(prices)
        flags: dict[date, bool] = {}
        for entry in entries:
            local = entry["start"].astimezone(tz)
            # The local 00:00 hour belongs to the previous CET delivery day's list
            # and carries that day's weekend/holiday flags; never use it.
            if entry["off_day"] is not None and local.hour != 0:
                flags[local.date()] = flags.get(local.date(), False) or entry["off_day"]
        self._day_flags.update(flags)
        if len(self._day_flags) > 14:
            for old in sorted(self._day_flags)[:-14]:
                del self._day_flags[old]

        known = [e for e in entries if e["price"] is not None]
        changed = False
        for entry in known:
            start_min = _epoch_min(entry["start"])
            if start_min in self._ingested:
                continue
            self._ingested.add(start_min)
            local = entry["start"].astimezone(tz)
            bucket = self._state["prices"][self.day_class(local.date())].setdefault(str(local.hour), [])
            bucket.append([start_min, round(entry["price"], 6)])
            bucket.sort()
            del bucket[:-PRICE_BUCKET_LIMIT]
            changed = changed or any(item[0] == start_min for item in bucket)
        if len(self._ingested) > 2000:
            newest = max(self._ingested)
            self._ingested = {m for m in self._ingested if m >= newest - 14 * 1440}
        if changed:
            self._commit("price_forecast")

        if any(entry["end"] > now for entry in known):
            slots = [[_epoch_min(e["start"]), _epoch_min(e["end"]), round(e["price"], 6)]
                     for e in known][-LAST_GOOD_LIMIT:]
            currency = currency if isinstance(currency, str) and 0 < len(currency) <= 8 else None
            last_good = self._state["last_good"]
            if last_good is None or last_good["slots"] != slots or last_good["currency"] != currency:
                self._state["last_good"] = {"fetched_at": _iso(now), "currency": currency,
                                            "slots": slots}
                self._commit("price_list")
        self._known = known

    def day_class(self, local_day: date) -> str:
        """Day class using the price lists' weekend/holiday flags when known."""
        return day_class(local_day, self._day_flags)

    def _known_entries(self, entries: Iterable[Any] | None = None) -> list[dict[str, Any]]:
        """Live known entries merged with the last good list (live wins)."""
        live = ([e for e in normalize_price_entries(entries) if e["price"] is not None]
                if entries is not None else list(self._known))
        merged = {_epoch_min(e["start"]): e for e in live}
        last_good = self._state["last_good"]
        if last_good:
            for start_min, end_min, price in last_good["slots"]:
                if start_min not in merged:
                    merged[start_min] = {"start": _from_min(start_min), "end": _from_min(end_min),
                                         "price": price, "off_day": None}
        result: list[dict[str, Any]] = []
        for key in sorted(merged):
            entry = merged[key]
            if result and entry["start"] < result[-1]["end"]:
                continue  # drop overlaps; earlier (live-first) entries stay
            result.append(entry)
        return result

    def last_good_prices(self) -> tuple[datetime | None, str | None, list[dict[str, Any]]]:
        """The persisted last good list: (fetched_at, currency, entries)."""
        last_good = self._state["last_good"]
        if not last_good:
            return None, None, []
        return (_aware(last_good["fetched_at"]), last_good["currency"],
                [{"start": _from_min(s), "end": _from_min(e), "price": p}
                 for s, e, p in last_good["slots"]])

    def price_forecast(self, at: datetime, tz: tzinfo) -> tuple[float | None, str]:
        """§8.7: bucket median (n >= 3), else the same local hour on the most recent known day."""
        local = at.astimezone(tz)
        hour = str(local.hour)
        items = self._state["prices"][self.day_class(local.date())].get(hour, [])
        if len(items) >= PRICE_MIN_BUCKET:
            return round(_median(p for _, p in items), 6), "forecast"
        target = _epoch_min(at)
        candidates = [(s, p) for dc in DAY_CLASSES
                      for s, p in self._state["prices"][dc].get(hour, []) if s < target]
        for entry in self._known_entries():
            start = entry["start"]
            if start < at and start.astimezone(tz).hour == local.hour:
                candidates.append((_epoch_min(start), entry["price"]))
        if candidates:
            return round(max(candidates)[1], 6), "forecast"
        return None, "unknown"

    def price_slots(self, start: datetime, end: datetime, tz: tzinfo,
                    entries: Iterable[Any] | None = None) -> list[dict[str, Any]]:
        """Price slots covering [start, end): known entries as-is (live, else last
        good), then hourly forecast/unknown slots for local hours without any."""
        known = [e for e in self._known_entries(entries) if e["end"] > start and e["start"] < end]
        result: list[dict[str, Any]] = []
        emitted: set[int] = set()
        index = 0
        for hour_start, hour_end in _hours(start, end, tz):
            covering = []
            while index < len(known) and known[index]["end"] <= hour_start:
                index += 1
            probe = index
            while probe < len(known) and known[probe]["start"] < hour_end:
                covering.append(known[probe])
                probe += 1
            if covering:
                for entry in covering:
                    key = _epoch_min(entry["start"])
                    if key not in emitted:
                        emitted.add(key)
                        result.append({"start": entry["start"], "end": entry["end"],
                                       "price": entry["price"], "kind": "known"})
            else:
                price, kind = self.price_forecast(hour_start, tz)
                result.append({"start": hour_start, "end": hour_end, "price": price, "kind": kind})
        return result

    # -- ledger (§8.5) -------------------------------------------------------

    def _observe_ledger(self, ledger: Any) -> None:
        self._ledger_active = {}
        self._ledger_seen = {}
        self._ledger_ok = isinstance(ledger, Mapping)
        if not self._ledger_ok:
            return
        raw_active = ledger.get("active") if isinstance(ledger.get("active"), Mapping) else None
        added = False
        for outlet in OUTLETS:
            records = _ledger_outlet_records(ledger, outlet)
            for index, record in enumerate(records[:6]):
                ident = record.get("id")
                values = _ledger_values(record)
                if not isinstance(ident, str) or values is None:
                    continue
                self._ledger_seen[ident] = values
                active = (raw_active.get(outlet) == ident if raw_active is not None
                          else index == 0 and record.get("status") != "finished")
                if active and outlet not in self._ledger_active:
                    self._ledger_active[outlet] = (ident, values)
                added = self._ingest_budget_record(record) or added
        if added:
            self._commit("budget")

    def _vehicle_key(self, label: Any) -> str | None:
        if label in VEHICLES:
            return label
        if not isinstance(label, str):
            return None
        wanted = label.strip().casefold()
        matches = [k for k, name in self.vehicle_names.items() if name.strip().casefold() == wanted]
        return matches[0] if len(matches) == 1 else None

    def _ingest_budget_record(self, record: Mapping) -> bool:
        ident = record.get("id")
        if not isinstance(ident, str) or not 0 < len(ident) <= 64 or ident in self._budget_checked:
            return False
        if record.get("status") != "finished":
            return False  # re-check once it finishes
        self._budget_checked.add(ident)
        values = _ledger_values(record)
        issues = record.get("issues")
        vehicle = self._vehicle_key(record.get("vehicle"))
        if (values is None or vehicle is None or not isinstance(issues, list)
                or values["energy_kwh"] < BUDGET_MIN_RECORD_KWH
                or values["energy_kwh"] > BUDGET_MAX_KWH
                or values["unobserved_seconds"] > BUDGET_MAX_UNOBSERVED_S
                or any(issue in BUDGET_DIRTY_ISSUES for issue in issues)):
            return False
        if any(item[0] == ident for v in VEHICLES for item in self._state["budget"][v]):
            return False
        started = _aware(record.get("started_at"))
        items = self._state["budget"][vehicle]
        items.append([ident, round(values["energy_kwh"], 3), _epoch_min(started) if started else 0])
        items.sort(key=lambda item: (item[2], item[0]))
        del items[:-BUDGET_LIMIT]
        return any(item[0] == ident for item in items)

    def seed_from_ledger(self, ledger: Any) -> int:
        """§8.5 seeding: ingest every clean finished record of the ledger history."""
        if not isinstance(ledger, Mapping):
            return 0
        added = 0
        for outlet in OUTLETS:
            for record in _ledger_outlet_records(ledger, outlet):
                added += bool(self._ingest_budget_record(record))
        if added:
            self._dirty = True
        return added

    def budget_kwh(self, vehicle: str | None) -> Estimate:
        """max(30 kWh, P80 of the last 10 clean records); None = max of both vehicles."""
        if vehicle not in VEHICLES:
            estimates = [self.budget_kwh(v) for v in VEHICLES]
            best = max(estimates, key=lambda e: e.value)
            source = (SOURCE_LEARNED if any(e.source == SOURCE_LEARNED for e in estimates)
                      else SOURCE_DEFAULT)
            return Estimate(best.value, source)
        values = [item[1] for item in self._state["budget"][vehicle]]
        if not values:
            return Estimate(BUDGET_FLOOR_KWH, SOURCE_DEFAULT)
        return Estimate(round(max(BUDGET_FLOOR_KWH, _quantile(values, BUDGET_QUANTILE)), 3),
                        SOURCE_LEARNED)

    def delivered_kwh(self, outlet: str) -> float:
        """Energy metered on the outlet since plug-in (the active ledger record)."""
        active = self._ledger_active.get(outlet)
        return active[1]["energy_kwh"] if active else 0.0

    def session_budget(self, outlet: str, vehicle: str | None) -> Budget:
        """Remaining unknown-SoC energy = budget - energy metered since plug-in."""
        budget = self.budget_kwh(vehicle)
        delivered = self.delivered_kwh(outlet)
        return Budget(round(max(0.0, budget.value - delivered), 3), budget.value,
                      round(delivered, 3), budget.source)

    # -- plug sessions, departures, kWh/% (§8.3, §8.6) -----------------------

    def _new_session(self, plugged_at: datetime | None, observed: bool) -> dict[str, Any]:
        return {"plugged_at": plugged_at, "observed": observed, "vehicle": None,
                "remapped": False, "last_available_at": None, "energy": None,
                "anchor": None, "soc_prev": None, "kwh": None, "cap_samples": [],
                "power_windows": []}

    def _observe_sessions(self, now: datetime, tz: tzinfo, outlets: dict[str, dict[str, Any]],
                          soc_map: dict[str, SocReading], capacities: dict[str, float | None],
                          exclusive: str | None, gap: bool) -> set[str]:
        unplugged: set[str] = set()
        for outlet in OUTLETS:
            info = outlets[outlet]
            if not info["available"]:
                continue  # API blip: never an unplug (§3.2)
            previous = self._connected.get(outlet)
            self._connected[outlet] = info["connected"]
            session = self._sessions.get(outlet)
            if info["connected"]:
                if session is None:
                    observed = previous is False
                    session = self._new_session(now if observed else info["plugged_at"], observed)
                    self._sessions[outlet] = session
                    if observed:
                        for pending in self._pending_departures:
                            if now - pending["unplug_at"] <= DEPARTURE_RECONNECT:
                                pending["reconnects"].setdefault(outlet, None)
                self._track_session(outlet, session, info, now, soc_map, exclusive == outlet, gap)
            elif session is not None:
                self._end_session(outlet, session, now, tz, capacities)
                del self._sessions[outlet]
                unplugged.add(outlet)
        self._confirm_departures(now, outlets)
        return unplugged

    def _track_session(self, outlet: str, session: dict[str, Any], info: dict[str, Any],
                       now: datetime, soc_map: dict[str, SocReading], exclusive: bool,
                       gap: bool) -> None:
        session["last_available_at"] = now
        mapped = info["vehicle"]
        if mapped is not None:
            if session["vehicle"] is not None and mapped != session["vehicle"]:
                self._drop_session_samples(outlet, session)
            session["vehicle"] = mapped
        vehicle = session["vehicle"]
        active = self._ledger_active.get(outlet)
        energy = active[1]["energy_kwh"] if active else (0.0 if self._ledger_ok else None)
        unobserved = active[1]["unobserved_seconds"] if active else 0.0
        ledger_id = active[0] if active else None
        session["energy"] = energy
        segment = session["kwh"]
        if gap and segment is not None:
            segment["frozen"] = True
        if vehicle is None:
            return
        reading = soc_map.get(vehicle, SocReading(None, None))
        if reading.soc is None:
            if segment is not None:
                segment["dropped"] = True  # battery unavailable (§8.3)
            return
        if session["anchor"] is None:
            plugged = session["plugged_at"]
            before_plug = (reading.changed_at is not None and plugged is not None
                           and reading.changed_at <= plugged)
            session["anchor"] = {"soc": reading.soc, "at": reading.changed_at or now,
                                 "energy": 0.0 if before_plug else (energy or 0.0)}
        elif session["soc_prev"] is not None and reading.soc != session["soc_prev"]:
            session["anchor"] = {"soc": reading.soc, "at": reading.changed_at or now,
                                 "energy": energy or 0.0}
            self._kwh_change(session, vehicle, reading.soc, energy, unobserved, ledger_id,
                             exclusive and mapped == vehicle)
        session["soc_prev"] = reading.soc

    def _kwh_change(self, session: dict[str, Any], vehicle: str, soc: float,
                    energy: float | None, unobserved: float, ledger_id: str | None,
                    exclusive: bool) -> None:
        """Anchor/extend the per-session kWh/% segment at an observed SoC change."""
        segment = session["kwh"]
        if session["remapped"] or (segment is not None and (segment["dropped"] or segment["frozen"])):
            return
        if not exclusive or energy is None or ledger_id is None:
            return
        if soc > KWH_MAX_SOC:
            if segment is not None:
                segment["frozen"] = True
            return
        if segment is None:
            session["kwh"] = {"vehicle": vehicle, "id": ledger_id, "soc0": soc, "e0": energy,
                              "u0": unobserved, "soc1": soc, "e1": energy,
                              "frozen": False, "dropped": False}
            return
        if ledger_id != segment["id"] or unobserved - segment["u0"] > KWH_UNOBSERVED_TOLERANCE_S:
            segment["frozen"] = True
            return
        if soc > segment["soc1"]:
            segment["soc1"], segment["e1"] = soc, energy

    def _drop_session_samples(self, outlet: str, session: dict[str, Any]) -> None:
        """Remap or cross-check swap: drop this session's vehicle-specific samples."""
        session["remapped"] = True
        if session["kwh"] is not None:
            session["kwh"]["dropped"] = True
        else:
            session["kwh"] = {"vehicle": None, "id": None, "soc0": 0.0, "e0": 0.0, "u0": 0.0,
                              "soc1": 0.0, "e1": 0.0, "frozen": True, "dropped": True}
        removed = False
        for vehicle, value in session["cap_samples"]:
            items = self._state["power"]["car_cap"][vehicle]
            for index in range(len(items) - 1, -1, -1):
                if items[index] == value:
                    del items[index]
                    removed = True
                    break
        session["cap_samples"] = []
        session["power_windows"] = []
        if removed:
            self._dirty = True
        if self._run is not None and self._run["outlet"] == outlet:
            self._run = None
        session["anchor"] = None
        session["soc_prev"] = None

    def discard_session(self, outlet: str) -> None:
        """§8.11 swap: discard this plug session's samples (kWh/%, departure, car cap)."""
        session = self._sessions.get(outlet)
        if session is not None:
            self._drop_session_samples(outlet, session)
        for pending in self._pending_departures:
            if pending["outlet"] == outlet:
                pending["discarded"] = True

    def _end_session(self, outlet: str, session: dict[str, Any], now: datetime, tz: tzinfo,
                     capacities: dict[str, float | None]) -> None:
        segment = session["kwh"]
        if (segment is not None and not segment["dropped"] and not session["remapped"]
                and segment["soc1"] - segment["soc0"] >= KWH_MIN_POINTS):
            vehicle = segment["vehicle"]
            capacity = capacities.get(vehicle) or DEFAULT_CAPACITY_KWH
            ratio = (segment["e1"] - segment["e0"]) / (segment["soc1"] - segment["soc0"])
            low, high = (factor * capacity / 100 for factor in KWH_BAND)
            if low <= ratio <= high and KWH_SAMPLE_BOUNDS[0] <= ratio <= KWH_SAMPLE_BOUNDS[1]:
                items = self._state["energy"][vehicle]
                items.append(round(ratio, 4))
                del items[:-KWH_LIMIT]
                self._commit("kwh_sample")
        vehicle = session["vehicle"]
        plugged = session["plugged_at"]
        seen = session["last_available_at"]
        if (vehicle is None or session["remapped"] or plugged is None or seen is None
                or (now - seen).total_seconds() > MAX_GAP_SECONDS):
            return
        local = now.astimezone(tz)
        minute = local.hour * 60 + local.minute
        if (DEPARTURE_WINDOW[0] <= minute <= DEPARTURE_WINDOW[1]
                and _spans_local_minute(plugged, now, tz, DEPARTURE_SPAN_MINUTE)):
            self._pending_departures.append({
                "outlet": outlet, "vehicle": vehicle, "day_class": self.day_class(local.date()),
                "minute": minute, "unplug_at": now, "reconnects": {}, "discarded": False})

    def _confirm_departures(self, now: datetime, outlets: dict[str, dict[str, Any]]) -> None:
        for pending in list(self._pending_departures):
            for outlet in pending["reconnects"]:
                info = outlets[outlet]
                if info["available"] and info["connected"] and info["vehicle"] is not None:
                    pending["reconnects"][outlet] = info["vehicle"]
            if now - pending["unplug_at"] < DEPARTURE_RECONNECT:
                continue
            self._pending_departures.remove(pending)
            # A reconnect within 60 min cancels unless it is clearly the other car.
            if pending["discarded"] or any(
                    vehicle is None or vehicle == pending["vehicle"]
                    for vehicle in pending["reconnects"].values()):
                continue
            items = self._state["departures"][pending["vehicle"]][pending["day_class"]]
            items.append(pending["minute"])
            del items[:-DEPARTURE_LIMIT]
            self._commit("departure")

    def departure_suggestion(self, vehicle: str, ready_by: Any = None) -> dict[str, Any] | None:
        """§8.6 suggestion: earliest across day classes; None when hidden or unlearned."""
        if vehicle not in VEHICLES:
            return None
        best = None
        for dc in DAY_CLASSES:
            samples = self._state["departures"][vehicle][dc]
            if len(samples) < DEPARTURE_MIN_SAMPLES:
                continue
            middle = _median(samples)
            kept = [m for m in samples if abs(m - middle) <= DEPARTURE_OUTLIER_MINUTES]
            if len(kept) < DEPARTURE_MIN_SAMPLES:
                continue
            suggested = int(math.floor((_quantile(kept, DEPARTURE_QUANTILE) - DEPARTURE_LEAD_MINUTES)
                                       / DEPARTURE_STEP_MINUTES) * DEPARTURE_STEP_MINUTES)
            usual = int(round(_median(kept) / DEPARTURE_STEP_MINUTES) * DEPARTURE_STEP_MINUTES)
            candidate = (suggested, dc, usual, len(kept))
            if best is None or candidate[0] < best[0]:
                best = candidate
        if best is None:
            return None
        current = _minutes_of(ready_by)
        if current is not None and abs(best[0] - current) <= DEPARTURE_HIDE_MINUTES:
            return None
        return {"suggested_ready_by": _hhmm(best[0]), "usually_leaves": _hhmm(best[2]),
                "day_class": best[1], "samples": best[3]}

    def suggested_ready_by(self, vehicle: str, ready_by: Any = None) -> str | None:
        suggestion = self.departure_suggestion(vehicle, ready_by)
        return suggestion["suggested_ready_by"] if suggestion else None

    def departure_samples(self, vehicle: str) -> int:
        if vehicle not in VEHICLES:
            return 0
        return sum(len(self._state["departures"][vehicle][dc]) for dc in DAY_CLASSES)

    def session_info(self, outlet: str) -> dict[str, Any] | None:
        """Current plug session as tracked by the model (no raw departure data)."""
        session = self._sessions.get(outlet)
        if session is None:
            return None
        return {"plugged_at": session["plugged_at"], "vehicle": session["vehicle"],
                "remapped": session["remapped"], "delivered_kwh": self.delivered_kwh(outlet)}

    # -- energy per percent (§8.3) and SoC (§8.4) -----------------------------

    def kwh_per_pct(self, vehicle: str | None, capacity_kwh: Any = None) -> KwhPerPct:
        """Learned (>= 3 sessions): median of last 7 / max of last 5; else capacity/100 x 1.05."""
        samples = self._state["energy"][vehicle] if vehicle in VEHICLES else []
        if len(samples) >= KWH_MIN_SAMPLES:
            expected = _median(samples[-KWH_LIMIT:])
            conservative = max(max(samples[-KWH_CONSERVATIVE_LAST:]), expected)
            return KwhPerPct(round(expected, 4), round(conservative, 4), SOURCE_LEARNED, len(samples))
        capacity = _num(capacity_kwh)
        if capacity is not None and CAPACITY_BOUNDS[0] <= capacity <= CAPACITY_BOUNDS[1]:
            value = round(capacity / 100 * KWH_DEFAULT_FACTOR, 4)
            return KwhPerPct(value, value, SOURCE_CAPACITY, len(samples))
        value = round(DEFAULT_CAPACITY_KWH / 100 * KWH_DEFAULT_FACTOR, 4)
        return KwhPerPct(value, value, SOURCE_DEFAULT, len(samples))

    def learned_sessions(self, vehicle: str) -> int:
        """Number of accepted kWh/% session samples ("Learned from N charges")."""
        return len(self._state["energy"][vehicle]) if vehicle in VEHICLES else 0

    def energy_since_reading(self, outlet: str) -> float:
        """kWh metered on the outlet since the battery reading the SoC comes from."""
        session = self._sessions.get(outlet)
        anchor = session["anchor"] if session else None
        energy = session["energy"] if session else None
        if anchor is None or energy is None:
            return 0.0
        return max(0.0, energy - anchor["energy"])

    def soc_estimate(self, outlet: str, now: datetime, *, car_limit: Any = None,
                     kwh_per_pct: Any = None, capacity_kwh: Any = None,
                     last_soc: Any = None, last_soc_at: Any = None) -> tuple[float | None, str]:
        """§8.4 for an outlet using the last observed readings (see resolve_soc)."""
        session = self._sessions.get(outlet)
        info = self._outlets.get(outlet) or _norm_outlet(None)
        vehicle = session["vehicle"] if session else info["vehicle"]
        reading = self._socs.get(vehicle) if vehicle else None
        anchor = session["anchor"] if session else None
        energy = session["energy"] if session else None
        since = (max(0.0, energy - anchor["energy"])
                 if anchor is not None and energy is not None else 0.0)
        if _soc_value(last_soc) is None and anchor is not None:
            last_soc, last_soc_at = anchor["soc"], anchor["at"]
        if kwh_per_pct is None:
            kwh_per_pct = self.kwh_per_pct(vehicle, capacity_kwh).expected
        return resolve_soc(
            now, live_soc=reading.soc if reading else None,
            live_changed_at=reading.changed_at if reading else None,
            last_soc=last_soc, last_soc_at=last_soc_at,
            plugged_at=session["plugged_at"] if session else info["plugged_at"],
            charging=info["available"] and info["charging"], energy_since_kwh=since,
            kwh_per_pct=kwh_per_pct, car_limit=car_limit)

    # -- household load (§8.1) ------------------------------------------------

    def _observe_household(self, now: datetime, tz: tzinfo, mains: tuple | None,
                           ev_phases: tuple | None, any_charging: bool) -> None:
        local = now.astimezone(tz)
        key = (local.date(), local.hour, local.utcoffset())
        accumulator = self._hour_acc
        if accumulator is not None and accumulator["key"] != key:
            self._commit_hour(accumulator)
            accumulator = self._hour_acc = None
        if mains is None:
            return
        if ev_phases is None:
            if any_charging:
                return  # EV share unknown while a car charges
            ev_phases = (0.0, 0.0, 0.0)
        base = max(max(0.0, m - e) for m, e in zip(mains, ev_phases))
        if accumulator is None:
            accumulator = self._hour_acc = {"key": key, "samples": []}
        accumulator["samples"].append(base)
        recent = [(at, value) for at, value in self._recent_base
                  if timedelta(0) <= now - at <= HOUSEHOLD_RECENT_WINDOW]
        recent.append((now, base))
        self._recent_base = recent
        snapshot = self._recent_snapshot
        if (snapshot is None or snapshot[1] is None or now < snapshot[0]
                or _unit_start(now) != _unit_start(snapshot[0])
                or now - snapshot[0] >= HOUSEHOLD_RECENT_REFRESH):
            self._recent_snapshot = (now, self._recent_stats())

    def recent_base(self) -> tuple[datetime, float, float] | None:
        """(sample time, mean, P90) of the household base over the last hour, when it
        covers at least half an hour; else None. Re-read once per 15 min, in 0.5 A
        steps rounded up."""
        snapshot = self._recent_snapshot
        if snapshot is None or snapshot[1] is None or not self._recent_base:
            return None
        # Values from the snapshot; the time of the newest sample, so the "current and
        # next hour" window follows the clock and not the snapshot.
        _, mean, p90 = snapshot[1]
        return self._recent_base[-1][0], mean, p90

    def _recent_stats(self) -> tuple[datetime, float, float] | None:
        samples = self._recent_base
        if len(samples) < HOUSEHOLD_MIN_SAMPLES:
            return None
        if samples[-1][0] - samples[0][0] < HOUSEHOLD_RECENT_MIN_SPAN:
            return None
        values = [value for _, value in samples]
        step = HOUSEHOLD_RECENT_STEP_A
        # Rounded up: the floor only ever raises the household load.
        mean = math.ceil(sum(values) / len(values) / step - 1e-9) * step
        p90 = math.ceil(_quantile(values, HOUR_P90) / step - 1e-9) * step
        return samples[-1][0], mean, max(p90, mean)

    def _commit_hour(self, accumulator: dict[str, Any]) -> None:
        samples = accumulator["samples"]
        if len(samples) < HOUSEHOLD_MIN_SAMPLES:
            return
        local_day, hour, _offset = accumulator["key"]
        mean = sum(samples) / len(samples)
        p90 = _quantile(samples, HOUR_P90)
        bucket = self._state["household"]["buckets"][self.day_class(local_day)].setdefault(str(hour), [])
        bucket.append([round(mean, 3), round(p90, 3)])
        del bucket[:-HOUSEHOLD_BUCKET_LIMIT]
        days = self._state["household"]["days"]
        text = local_day.isoformat()
        for item in days:
            if item[0] == text:
                item[1] = min(25, item[1] + 1)
                break
        else:
            days.append([text, 1])
            days.sort()
            del days[:-HOUSEHOLD_DAYS_LIMIT]
        self._commit("household_hour")

    def household_learned_days(self) -> int:
        """Committed days (>= 12 committed hours) for the cold-start rule and card."""
        return sum(1 for _, hours in self._state["household"]["days"]
                   if hours >= HOUSEHOLD_DAY_MIN_HOURS)

    def base_load(self, at: datetime, tz: tzinfo) -> BaseLoad:
        """Max-phase household base for the local hour: median of means / max(P80 of P90s, +1 A).

        Persistence (what the house draws now) raises it: for the current and the
        next local hour the last hour's mean/P90 are a floor; an hour with no learned
        bucket uses them too (bounded, so an evening peak does not empty the night)
        instead of the 3 A / 5 A defaults alone.
        """
        local = at.astimezone(tz)
        items = self._state["household"]["buckets"][self.day_class(local.date())].get(str(local.hour), [])
        learned = len(items) >= HOUSEHOLD_MIN_BUCKET
        if learned:
            expected = _median(mean for mean, _ in items)
            conservative = max(_quantile((p90 for _, p90 in items), BASE_CONSERVATIVE_QUANTILE),
                               expected + BASE_CONSERVATIVE_MARGIN_A)
        else:
            expected, conservative = DEFAULT_BASE_EXPECTED_A, DEFAULT_BASE_CONSERVATIVE_A
        recent = self.recent_base()
        if recent is not None:
            last, mean, p90 = recent
            near = at < local_hour_start(last, tz) + HOUSEHOLD_RECENT_NEAR
            if near:
                expected = max(expected, mean)
                conservative = max(conservative, p90, expected + BASE_CONSERVATIVE_MARGIN_A)
            elif not learned:
                expected = max(expected, min(mean, HOUSEHOLD_RECENT_MAX_EXPECTED_A))
                conservative = max(conservative, min(p90, HOUSEHOLD_RECENT_MAX_CONSERVATIVE_A),
                                   expected + BASE_CONSERVATIVE_MARGIN_A)
        return BaseLoad(round(expected, 3), round(conservative, 3),
                        SOURCE_LEARNED if learned else SOURCE_DEFAULT)

    # -- power (§8.2) -----------------------------------------------------------

    def kw_per_phase_amp(self) -> Estimate:
        samples = self._state["power"]["kw_per_phase_amp"]
        if len(samples) < KW_PER_PHASE_AMP_MIN_SAMPLES:
            return Estimate(DEFAULT_KW_PER_PHASE_AMP, SOURCE_DEFAULT)
        return Estimate(round(_clamp(_median(samples), KW_PER_PHASE_AMP_BOUNDS), 5), SOURCE_LEARNED)

    def calibration(self) -> Estimate:
        samples = self._state["power"]["calibration"][-CALIBRATION_LIMIT:]
        if len(samples) < CALIBRATION_MIN_SAMPLES:
            return Estimate(1.0, SOURCE_DEFAULT)
        return Estimate(round(_clamp(_median(samples), CALIBRATION_BOUNDS), 4), SOURCE_LEARNED)

    def car_current_cap(self, vehicle: str | None, max_current: Any = None) -> Estimate:
        """P90 of the car's max phase current at full offer; default max_current - 0.8 A."""
        limit, _, minimum = _limits(max_current)
        samples = self._state["power"]["car_cap"][vehicle] if vehicle in VEHICLES else []
        if len(samples) >= CAR_CAP_MIN_SAMPLES:
            value = min(max(_quantile(samples, CAR_CAP_QUANTILE), minimum), limit)
            return Estimate(round(value, 3), SOURCE_LEARNED)
        return Estimate(round(limit - CAR_CAP_OFFSET_A, 3), SOURCE_DEFAULT)

    def session_current_cap(self, outlet: str | None, vehicle: str | None,
                            now: datetime | None = None) -> float | None:
        """What the car accepted in this plug session while it drew clearly less than
        offered: the median max-phase current of its last six car-limited windows, or
        None. The median is right for a car with a fixed limit and safe when its
        shortfall grows with a lower offer (heat-pump hours). It lasts the plug session,
        also while the car waits for a cheaper slot; ``now`` only drops windows stamped in
        the future."""
        session = self._sessions.get(outlet) if outlet else None
        if session is None or vehicle is None:
            return None
        windows = [w for w in session.get("power_windows", ())
                   if w[1] == vehicle and (now is None or w[0] <= now)]
        limited = [w[2] for w in windows if w[3]]
        if len(limited) < SESSION_CAP_MIN_LIMITED:
            return None
        return round(_median(limited), 3)

    def car_power(self, vehicle: str | None, max_current: Any = None, *, outlet: str | None = None,
                  now: datetime | None = None) -> CarPower:
        """Planner cap_kw / cap_low_kw = 3 x kW/phase-amp x car current cap (low x 0.95).

        With ``outlet``, a car that keeps drawing clearly less than offered in this plug
        session is planned at what it accepted (source ``session``).
        """
        kpa = self.kw_per_phase_amp().value
        cap = self.car_current_cap(vehicle, max_current)
        value, source = cap.value, cap.source
        accepted = self.session_current_cap(outlet, vehicle, now)
        if accepted is not None and accepted < value:
            value, source = round(max(accepted, CAR_CAP_BOUNDS[0]), 3), SOURCE_SESSION
        cap_kw = PHASES * kpa * value
        return CarPower(round(cap_kw, 3), round(cap_kw * CAP_LOW_FACTOR, 3), value, source)

    def hour_power(self, at: datetime, tz: tzinfo, *, max_current: Any = None,
                   max_mains: Any = None, min_current: Any = None, vehicle: str | None = None,
                   include_car_cap: bool = False, calibrated: bool = True) -> HourPower:
        """Smart-mode charging power for the local hour containing ``at``.

        I_set = min(max_current, max_mains - B); 0 kW below min_current; with
        ``include_car_cap`` I = min(I_set, car cap). kW = I x 3 x kW/phase-amp;
        expected x calibration, conservative x min(calibration, 1), conservative
        <= 8.5 kW until 7 committed household days, and never above expected.
        """
        limit, mains, minimum = _limits(max_current, max_mains, min_current)
        base = self.base_load(at, tz)
        kpa = self.kw_per_phase_amp().value
        ratio = self.calibration().value if calibrated else 1.0
        cap = self.car_current_cap(vehicle, limit).value if include_car_cap else math.inf

        def kw(base_a: float, factor: float) -> float:
            setpoint = min(limit, mains - base_a)
            if setpoint < minimum:
                return 0.0
            return max(0.0, min(setpoint, cap)) * PHASES * kpa * factor

        expected = kw(base.expected_a, ratio)
        conservative = kw(base.conservative_a, min(ratio, 1.0))
        if self.household_learned_days() < COLD_START_DAYS:
            conservative = min(conservative, COLD_START_MAX_CONSERVATIVE_KW)
        conservative = min(conservative, expected)
        return HourPower(round(expected, 3), round(conservative, 3), base.source)

    def power_slots(self, start: datetime, end: datetime, tz: tzinfo, *, max_current: Any = None,
                    max_mains: Any = None, min_current: Any = None, vehicle: str | None = None,
                    include_car_cap: bool = False) -> list[dict[str, Any]]:
        """Hourly (local clock hours) power slots covering [start, end)."""
        slots = []
        for hour_start, hour_end in _hours(start, end, tz):
            power = self.hour_power(hour_start, tz, max_current=max_current, max_mains=max_mains,
                                    min_current=min_current, vehicle=vehicle,
                                    include_car_cap=include_car_cap)
            slots.append({"start": hour_start, "end": hour_end, "expected_kw": power.expected_kw,
                          "conservative_kw": power.conservative_kw, "source": power.source})
        return slots

    def household_summary(self, now: datetime, tz: tzinfo, *, max_current: Any = None,
                          max_mains: Any = None, min_current: Any = None) -> dict[str, Any]:
        """Card learning status: committed days and the coming night's expected kW."""
        local = now.astimezone(tz)
        night = local.date() if local.hour < 6 else local.date() + timedelta(days=1)
        learned = []
        for hour in range(6):
            at = datetime.combine(night, time(hour), tzinfo=tz)
            power = self.hour_power(at, tz, max_current=max_current, max_mains=max_mains,
                                    min_current=min_current)
            if power.source == SOURCE_LEARNED:
                learned.append(power.expected_kw)
        return {"learned_days": self.household_learned_days(),
                "night_expected_kw": round(_median(learned), 1) if learned else None}

    def _observe_power(self, now: datetime, tz: tzinfo, outlets: dict[str, dict[str, Any]],
                       power: float | None, ev_phases: tuple | None,
                       soc_map: dict[str, SocReading], exclusive: str | None, gap: bool,
                       mains: tuple | None = None) -> None:
        sample = None
        if exclusive is not None:
            info = outlets[exclusive]
            vehicle = info["vehicle"]
            soc = soc_map[vehicle].soc if vehicle else None
            if soc is None and vehicle and exclusive in self._sessions:
                anchor = self._sessions[exclusive]["anchor"]
                soc = anchor["soc"] if anchor else None
            valid = (vehicle is not None and power is not None and power >= POWER_MIN_KW
                     and soc is not None and soc < POWER_MAX_SOC)
            offered = info["offered"]
            if valid and offered is not None and offered > 0:
                valid = power <= (POWER_SANITY_FACTOR * offered * PHASES
                                  * KW_PER_PHASE_AMP_SAMPLE_BOUNDS[1])
            if valid:
                # The household base measured right now (max phase of mains - EV):
                # calibration compares against it, never against the hour's forecast.
                base = (max(max(0.0, m - e) for m, e in zip(mains, ev_phases))
                        if mains is not None and ev_phases is not None else None)
                sample = {"at": now, "outlet": exclusive, "vehicle": vehicle, "power": power,
                          "phases": ev_phases, "offered": offered, "base": base,
                          "limits": (info["max_current"], info["max_mains"], info["min_current"])}
        current = self._run
        if (sample is None or gap or current is None or current["outlet"] != sample["outlet"]
                or current["vehicle"] != sample["vehicle"]
                or (now - current["last_at"]).total_seconds() > MAX_GAP_SECONDS):
            # Any rejected sample breaks the run: the next window is a "first window".
            self._run = None if sample is None else {
                "outlet": sample["outlet"], "vehicle": sample["vehicle"], "last_at": now,
                "index": 0, "window_start": now, "samples": [sample]}
            return
        current["samples"].append(sample)
        current["last_at"] = now
        if (now - current["window_start"]).total_seconds() < POWER_WINDOW_SECONDS:
            return
        samples, index = current["samples"], current["index"]
        current["index"] += 1
        current["window_start"] = now
        current["samples"] = []
        if index > 0 and len(samples) >= POWER_WINDOW_MIN_SAMPLES:
            self._accept_window(tz, samples)

    def _accept_window(self, tz: tzinfo, samples: list[dict[str, Any]]) -> None:
        vehicle = samples[0]["vehicle"]
        outlet = samples[0]["outlet"]
        limits = _limits(*samples[-1]["limits"])
        power = sum(s["power"] for s in samples) / len(samples)
        # Calibration covers the charger/car physics only: the prediction uses the base
        # load measured in this window, so an evening's household-forecast error (or a
        # Force charge at another hour) does not rescale every hour tonight.
        predicted = None
        setpoint = None
        if all(s.get("base") is not None for s in samples):
            base = sum(s["base"] for s in samples) / len(samples)
            setpoint = min(limits[0], limits[1] - base)
            cap = self.car_current_cap(vehicle, limits[0]).value
            predicted = (0.0 if setpoint < limits[2]
                         else max(0.0, min(setpoint, cap)) * PHASES * self.kw_per_phase_amp().value)
            setpoint = min(setpoint, cap)
        state = self._state["power"]
        car_limited = False
        if all(s["phases"] is not None for s in samples):
            per_phase = [sum(s["phases"][i] for s in samples) / len(samples) for i in range(PHASES)]
            total = sum(per_phase)
            if total > 0:
                ratio = power / total
                if KW_PER_PHASE_AMP_SAMPLE_BOUNDS[0] <= ratio <= KW_PER_PHASE_AMP_SAMPLE_BOUNDS[1]:
                    state["kw_per_phase_amp"].append(round(ratio, 5))
                    del state["kw_per_phase_amp"][:-KW_PER_PHASE_AMP_LIMIT]
            offers = [s["offered"] for s in samples]
            current_max = max(per_phase)
            # The car drew clearly less than it was offered (cold battery, taper, its
            # own limit): that says nothing about Smart mode's power.
            car_limited = setpoint is not None and current_max < setpoint - CALIBRATION_CAR_LIMITED_A
            # Phase currents must agree with the metered power (a zero or missing phase
            # reading is no evidence of what the car accepts).
            consistent = (total > 0 and CAR_CAP_BOUNDS[0] <= current_max <= CAR_CAP_BOUNDS[1]
                          and KW_PER_PHASE_AMP_SAMPLE_BOUNDS[0] <= power / total
                          <= KW_PER_PHASE_AMP_SAMPLE_BOUNDS[1])
            if consistent and setpoint is not None and outlet in self._sessions:
                windows = self._sessions[outlet].setdefault("power_windows", [])
                if car_limited:
                    windows.append((samples[-1]["at"], vehicle, round(current_max, 3), True))
                    del windows[:-SESSION_CAP_WINDOWS]
                else:
                    held = self.session_current_cap(outlet, vehicle)
                    if held is not None and current_max > held + SESSION_CAP_LIFT_A:
                        windows.clear()  # it takes clearly more now: warmed up
            if (all(o is not None for o in offers)
                    and min(offers) >= limits[0] - CAR_CAP_FULL_OFFER_MARGIN_A
                    and CAR_CAP_BOUNDS[0] <= current_max <= CAR_CAP_BOUNDS[1]):
                value = round(current_max, 3)
                state["car_cap"][vehicle].append(value)
                del state["car_cap"][vehicle][:-CAR_CAP_LIMIT]
                if outlet in self._sessions:
                    self._sessions[outlet]["cap_samples"].append((vehicle, value))
        if predicted is not None and predicted > 0.5 and not car_limited:
            ratio = power / predicted
            if CALIBRATION_SAMPLE_BOUNDS[0] <= ratio <= CALIBRATION_SAMPLE_BOUNDS[1]:
                state["calibration"].append(round(ratio, 4))
                del state["calibration"][:-CALIBRATION_LIMIT]
        self._commit("power_window")

    # -- realised saving windows (§8.8) ---------------------------------------

    def _open_window(self, outlet: str) -> dict[str, Any] | None:
        for record in self._state["realised"]:
            if record["outlet"] == outlet and record["closed_at"] is None:
                return record
        return None

    def _observe_realised(self, now: datetime, tz: tzinfo, outlets: dict[str, dict[str, Any]],
                          state: str | None, reason: str | None, unplugged: set[str],
                          shared_now: bool) -> None:
        intelligent_now = state == INTELLIGENT_STATE
        for outlet in OUTLETS:
            info = outlets[outlet]
            window = self._open_window(outlet)
            # An observed unplug, or a disconnected outlet whose persisted window
            # outlived an HA restart during which the car left.
            gone = outlet in unplugged or (info["available"] and not info["connected"])
            if window is not None and (info["available"] or gone):
                self._accumulate(window, outlet, info, state, reason, shared_now)
            if window is not None and gone:
                self._close_window(window, now)
                window = None
            rolled = False
            if window is not None and info["deadline"] is not None:
                old = _aware(window["deadline"])
                if old is not None and info["deadline"] != old and now >= old:
                    self._close_window(window, now)  # deadline roll
                    window = None
                    rolled = True
                elif old is None or info["deadline"] != old:
                    window["deadline"] = _iso(info["deadline"])
                    self._commit("realised_update")
            if window is None and intelligent_now and info["available"] and not gone:
                self._open_new_window(now, tz, outlet, info, rolled=rolled)

    def _baseline_start(self, now: datetime, outlet: str, plugged: datetime | None,
                        deadline: datetime | None, base_energy: float, rolled: bool) -> datetime:
        """Where this window's "charging at plug-in" baseline starts (the same anchor
        the planned saving uses): the plug-in, at most a day (+ the late plug-in hour)
        before the deadline; the window start when a deadline roll opened it, when an
        earlier window of the same plug session exists, or when the session already
        charged in another mode (that charge used the plug-in moment)."""
        if plugged is None or rolled or base_energy >= REALISED_MIN_KWH:
            return now
        if any(r["outlet"] == outlet and r["plugged_at"] == _iso(plugged) for r in self._state["realised"]):
            return now
        start = plugged
        if deadline is not None:
            start = max(start, deadline - REALISED_BASELINE_LOOKBACK)
        return min(start, now)

    @staticmethod
    def _hour_price(slots: list[dict[str, Any]], start: datetime, end: datetime) -> tuple[float | None, str]:
        """Time-weighted price of [start, end) and its kind: unknown when any part is
        unpriced, forecast when any part is a forecast (15-minute price lists)."""
        covered, total, kind = 0.0, 0.0, "known"
        for slot in slots:
            overlap = (min(slot["end"], end) - max(slot["start"], start)).total_seconds()
            if overlap <= 0:
                continue
            if slot["price"] is None or slot["kind"] == "unknown":
                return None, "unknown"
            if slot["kind"] == "forecast":
                kind = "forecast"
            covered += overlap
            total += slot["price"] * overlap
        if covered < (end - start).total_seconds() - 1:
            return None, "unknown"
        return total / covered, kind

    def _open_new_window(self, now: datetime, tz: tzinfo, outlet: str, info: dict[str, Any],
                         *, rolled: bool = False) -> None:
        session = self._sessions.get(outlet)
        vehicle = (session["vehicle"] if session else None) or info["vehicle"]
        plugged = (session["plugged_at"] if session else None) or info["plugged_at"]
        active = self._ledger_active.get(outlet)
        values = dict(active[1]) if active else _zero_parts(_LEDGER_FIELDS)
        baseline_start = self._baseline_start(now, outlet, plugged, info["deadline"],
                                              values.get("energy_kwh", 0.0), rolled)
        end = now + timedelta(hours=REALISED_SNAPSHOT_HOURS)
        hours = list(_hours(baseline_start, end, tz))[:REALISED_SNAPSHOT_MAX]
        slots = self.price_slots(local_hour_start(baseline_start, tz), end, tz)
        snapshot_prices, kinds, powers = [], "", []
        for hour_start, hour_end in hours:
            price, kind = self._hour_price(slots, hour_start, hour_end)
            snapshot_prices.append(None if price is None else round(price, 6))
            kinds += _KIND_CODES[kind]
            powers.append(self.hour_power(hour_start, tz, max_current=info["max_current"],
                                          max_mains=info["max_mains"],
                                          min_current=info["min_current"], vehicle=vehicle,
                                          include_car_cap=vehicle is not None).expected_kw)
        record = {
            "id": f"{outlet}-{_epoch_min(now)}",
            "outlet": outlet, "vehicle": vehicle,
            "ledger_id": active[0] if active else None,
            "plugged_at": _iso(plugged) if plugged else None,
            "start": _iso(now),
            "deadline": _iso(info["deadline"]) if info["deadline"] else None,
            "closed_at": None,
            "base": dict(values), "last": dict(values),
            "intelligent": _zero_parts(_PART_FIELDS), "top_up": _zero_parts(_PART_FIELDS),
            "other": _zero_parts(_PART_FIELDS), "unpriced_kwh": 0.0, "unobserved_seconds": 0.0,
            "mixed": False, "reached": info["reached"],
            "snapshot": {"t0": _epoch_min(hours[0][0]) if hours else _epoch_min(now),
                         "prices": snapshot_prices, "kinds": kinds, "kw": powers},
            "baseline_cost": None, "final": False,
            "baseline_start": _iso(baseline_start),
        }
        if any(r["id"] == record["id"] for r in self._state["realised"]):
            record["id"] += "-b"
        self._state["realised"].append(record)
        self._trim_realised()
        self._commit("realised")

    @staticmethod
    def _covering(slots: dict[int, dict[str, Any]], at: datetime) -> dict[str, Any] | None:
        for slot in slots.values():
            if slot["start"] <= at < slot["end"]:
                return slot
        return None

    def _accumulate(self, window: dict[str, Any], outlet: str, info: dict[str, Any],
                    state: str | None, reason: str | None, shared_now: bool) -> None:
        changed = False
        session = self._sessions.get(outlet)
        if session is not None and session["vehicle"] and session["vehicle"] != window["vehicle"]:
            window["vehicle"] = session["vehicle"]
            changed = True
        if info["reached"] and not window["reached"]:
            window["reached"] = True
            changed = True
        ident = window["ledger_id"]
        current = self._ledger_seen.get(ident) if ident else None
        if current is None:
            active = self._ledger_active.get(outlet)
            if active is not None and active[0] != ident:
                # A record created after the window opened: all its energy is in the window.
                window["ledger_id"], current = active[0], active[1]
                window["last"] = _zero_parts(_LEDGER_FIELDS)
                changed = True
        if current is not None:
            delta = {f: current[f] - window["last"][f] for f in _LEDGER_FIELDS}
            if any(delta[f] < -1e-9 for f in _LEDGER_FIELDS if f != "cost"):
                window["last"] = dict(current)  # ledger went backwards: resync, count nothing
                changed = True
            elif any(abs(v) > 1e-12 for v in delta.values()):
                window["last"] = dict(current)
                charged_state = self._prev_state if self._prev_state is not None else state
                charged_reason = self._prev_reason if self._prev_state is not None else reason
                if delta["energy_kwh"] > 0 or delta["cost"] != 0:
                    if charged_state == INTELLIGENT_STATE:
                        key = "top_up" if charged_reason == TOP_UP_REASON else "intelligent"
                    else:
                        key = "other"  # Force, Schedule or any other controller state
                    for field in _PART_FIELDS:
                        window[key][field] += delta[field]
                    if window["other"]["energy_kwh"] > MIXED_TOLERANCE_KWH:
                        window["mixed"] = True
                window["unpriced_kwh"] += max(0.0, delta["unpriced_kwh"])
                overlap = ((shared_now or self._prev_shared)
                           and delta["unobserved_seconds"] <= SHARED_OVERLAP_TOLERANCE_S)
                if not overlap:
                    window["unobserved_seconds"] += max(0.0, delta["unobserved_seconds"])
                changed = True
        if changed:
            self._commit("realised_update")

    def _close_window(self, window: dict[str, Any], now: datetime) -> None:
        window["closed_at"] = _iso(now)
        self._trim_realised()
        self._commit("realised")

    def _trim_realised(self) -> None:
        records = self._state["realised"]
        closed = [r for r in records if r["closed_at"] is not None]
        excess = len(closed) - REALISED_LIMIT
        if excess > 0:
            drop = {r["id"] for r in sorted(closed, key=lambda r: (r["start"], r["id"]))[:excess]}
            self._state["realised"] = [r for r in records if r["id"] not in drop]

    @staticmethod
    def _window_partial(record: dict[str, Any]) -> bool:
        return (record["unpriced_kwh"] > UNPRICED_TOLERANCE_KWH
                or record["unobserved_seconds"] > REALISED_MAX_UNOBSERVED_S)

    def realised_sessions(self, tz: tzinfo, *, limit: int = 10,
                          baseline: Callable[..., Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Realised saving per (outlet, deadline window), newest first.

        Closed windows get a final baseline once every overlapping window that
        was plugged in earlier is closed (sequential one-charger baseline);
        open windows show a provisional value once the car reached its
        minimum. saving = baseline(priced Intelligent kWh, top-up excluded) -
        Intelligent ledger cost; None when mixed, partial, unpriceable or when
        less than 0.1 kWh was charged under Intelligent.
        Finalising marks the model dirty.
        """
        callback = baseline or self.baseline
        records = self._state["realised"]

        def start_of(record):
            return _aware(record["start"])

        def order_of(record):
            plugged = _aware(record["plugged_at"])
            start = start_of(record)
            return (min(plugged, start) if plugged else start, record["id"])

        ordered = sorted(records, key=lambda r: (start_of(r), r["id"]))
        groups: list[list[dict[str, Any]]] = []
        group_end = None
        for record in ordered:
            closed = _aware(record["closed_at"])
            end = closed if closed else datetime.max.replace(tzinfo=UTC)
            if groups and group_end is not None and start_of(record) < group_end:
                groups[-1].append(record)
                group_end = max(group_end, end)
            else:
                groups.append([record])
                group_end = end
        provisional: dict[str, float | None] = {}
        for group in groups:
            needed = any((r["closed_at"] is not None and not r["final"])
                         or (r["closed_at"] is None and r["reached"]) for r in group)
            results = None
            if needed and callback is not None:
                results = self._call_baseline(sorted(group, key=order_of), callback)
            earlier_open = False
            for record in sorted(group, key=order_of):
                if record["closed_at"] is None:
                    earlier_open = True
                    if results is not None:
                        provisional[record["id"]] = results.get(record["id"])
                    continue
                if not record["final"] and results is not None and not earlier_open:
                    cost = results.get(record["id"])
                    record["baseline_cost"] = round(cost, 2) if cost is not None else None
                    record["final"] = True
                    self._dirty = True
                elif not record["final"] and results is not None:
                    provisional[record["id"]] = results.get(record["id"])
        output = []
        for record in sorted(records, key=lambda r: (start_of(r), r["id"]), reverse=True)[:max(0, limit)]:
            closed = record["closed_at"] is not None
            if record["final"]:
                baseline_cost = record["baseline_cost"]
            else:
                value = provisional.get(record["id"])
                baseline_cost = round(value, 2) if value is not None else None
            partial = self._window_partial(record)
            saving = None
            if (baseline_cost is not None and not record["mixed"] and not partial
                    and (closed or record["reached"])
                    and record["intelligent"]["priced_kwh"] >= REALISED_MIN_KWH):
                saving = round(baseline_cost - record["intelligent"]["cost"], 2)
            parts = (record["intelligent"], record["top_up"], record["other"])
            energy = sum(part["energy_kwh"] for part in parts)
            cost = sum(part["cost"] for part in parts)
            output.append({
                "ledger_id": record["ledger_id"], "outlet": record["outlet"],
                "vehicle": record["vehicle"],
                "vehicle_name": self.vehicle_names.get(record["vehicle"]) if record["vehicle"] else None,
                "date": start_of(record).astimezone(tz).date().isoformat(),
                "closed": closed, "energy_kwh": round(energy, 2), "cost": round(cost, 2),
                "baseline_cost": baseline_cost if (closed or record["reached"]) else None,
                "saving": saving, "mixed": record["mixed"], "partial": partial,
                "top_up_kwh": round(record["top_up"]["energy_kwh"], 2),
                # For the adapter's planned saving (not published as such).
                "id": record["id"], "deadline": record["deadline"],
                "baseline_start": record.get("baseline_start"),
                "intelligent_kwh": round(record["intelligent"]["energy_kwh"], 4),
                "intelligent_priced_kwh": round(record["intelligent"]["priced_kwh"], 4),
                "intelligent_cost": round(record["intelligent"]["cost"], 4),
                "top_up_cost": round(record["top_up"]["cost"], 4),
            })
        return output

    def realised_digest(self) -> list[list[Any]]:
        """What the published realised windows look like, without computing any
        baseline (for the payload's change detection): id, open/closed, reached,
        final baseline, deadline and mixed per window, and the energy of a closed
        window (an open window's energy grows every cycle while charging; the
        payload follows it at the next unit or SoC change)."""
        rows = []
        for record in self._state["realised"]:
            closed = record["closed_at"] is not None
            parts = (record["intelligent"], record["top_up"], record["other"])
            energy = round(sum(part["energy_kwh"] for part in parts), 1) if closed else None
            rows.append([record["id"], closed, energy, record["reached"], record["final"],
                         record["baseline_cost"], record["deadline"], record["mixed"]])
        return rows

    def _call_baseline(self, group: list[dict[str, Any]],
                       callback: Callable[..., Mapping[str, Any]]) -> dict[str, float | None] | None:
        sessions = []
        prices: dict[int, dict[str, Any]] = {}
        powers: dict[int, dict[str, Any]] = {}
        for record in group:
            start = _aware(record["start"])
            plugged = _aware(record["plugged_at"])
            anchor = _aware(record.get("baseline_start"))
            if anchor is None:  # a record written before the baseline start was stored
                anchor = min(plugged, start) if plugged else start
            sessions.append({"key": record["id"], "outlet": record["outlet"],
                             "vehicle": record["vehicle"],
                             "plugged_at": anchor,
                             "start": start,
                             "energy_kwh": round(record["intelligent"]["priced_kwh"], 4)})
            snapshot = record["snapshot"]
            for index, kw in enumerate(snapshot["kw"]):
                minute = snapshot["t0"] + 60 * index
                kind = _KIND_NAMES[snapshot["kinds"][index]]
                existing = prices.get(minute)
                if existing is not None and (existing["kind"] == "known" or kind != "known"):
                    continue  # a published price is a fact: known wins over forecast/unknown
                slot_start, slot_end = _from_min(minute), _from_min(minute + 60)
                prices[minute] = {"start": slot_start, "end": slot_end,
                                  "price": snapshot["prices"][index], "kind": kind}
                if minute not in powers:
                    powers[minute] = {"start": slot_start, "end": slot_end,
                                      "expected_kw": kw, "conservative_kw": kw}
        try:
            raw = callback(sessions, [prices[m] for m in sorted(prices)],
                           [powers[m] for m in sorted(powers)])
        except Exception as err:  # noqa: BLE001 - a baseline failure only hides savings
            self.last_error = f"baseline: {type(err).__name__}: {err}"
            return None
        if not isinstance(raw, Mapping):
            return None
        results: dict[str, float | None] = {}
        for record in group:
            value = _num(raw.get(record["id"]))
            results[record["id"]] = value if value is not None and abs(value) <= _MONEY_MAX else None
        return results

    # -- diagnostics ------------------------------------------------------------

    def learning_summary(self) -> dict[str, Any]:
        """Counts only (no raw departure minutes, no timestamps) for diagnostics."""
        power = self._state["power"]
        return {
            "household_days": self.household_learned_days(),
            "power_windows": len(power["kw_per_phase_amp"]),
            "calibration_windows": len(power["calibration"]),
            "car_cap_windows": {v: len(power["car_cap"][v]) for v in VEHICLES},
            "kwh_samples": {v: len(self._state["energy"][v]) for v in VEHICLES},
            "budget_records": {v: len(self._state["budget"][v]) for v in VEHICLES},
            "departure_samples": {v: self.departure_samples(v) for v in VEHICLES},
            "realised_windows": len(self._state["realised"]),
            "load_issues": list(self.load_issues),
        }


__all__ = [
    "Budget", "BaseLoad", "CarPower", "ChargeModel", "Estimate", "HourPower", "KwhPerPct",
    "SocReading", "day_class", "default_state", "load_state", "local_hour_start",
    "normalize_price_entries", "reached_with_hysteresis", "resolve_soc",
]
