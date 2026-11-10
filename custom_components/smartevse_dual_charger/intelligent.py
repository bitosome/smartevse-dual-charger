"""Intelligent charging: Home Assistant adapter around the pure planner and model.

``charge_planner`` (cheapest plan) and ``charge_model`` (learned model) are pure
standard-library modules. This adapter is the only Intelligent code that talks
to Home Assistant: it derives the source entities (entity registry first),
reads and normalises their states, owns the model's private ``ModelStore``, turns
controller facts into planner requests, and publishes the ``intelligent``
payload plus the flat sensor values.

The controller owns the per-outlet Intelligent state in its ``_mutable`` dict
(deadlines, overrun, completion, not-accepting, commitment) and calls, once per
cycle and in this order:

1. :meth:`IntelligentCharging.observe` in every charge mode: model observation,
   SoC/settings facts per outlet and the §8.11 mapping cross-check;
2. :meth:`IntelligentCharging.plan` while Intelligent is enabled: the planner
   decision (isolated; a failure falls back to the no-price plan);
3. :meth:`IntelligentCharging.async_save_if_due` (model persistence);
4. :meth:`IntelligentCharging.build_payload` with the final gate result.

Nothing here may break the control loop: every step is isolated, and a planner
failure never means "charge now at any price".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, tzinfo
import json
import math
from typing import Any

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import charge_model as cm
from . import charge_planner as cp
from .charging_cost import finite, power_kw, price_unit, tariff
from .const import (
    CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY,
    CONF_EV_METER_L1_ENTITY,
    CONF_EV_METER_L2_ENTITY,
    CONF_EV_METER_L3_ENTITY,
    CONF_MAINS_L1_ENTITY,
    CONF_MAINS_L2_ENTITY,
    CONF_MAINS_L3_ENTITY,
    CONF_PRICE_SENSOR_ENTITY,
    CONF_VEHICLE_1_BATTERY_CAPACITY_KWH,
    CONF_VEHICLE_1_BATTERY_ENTITY,
    CONF_VEHICLE_2_BATTERY_CAPACITY_KWH,
    CONF_VEHICLE_2_BATTERY_ENTITY,
    DEFAULT_MINIMUM_CHARGE,
    DEFAULT_READY_BY,
    DOMAIN,
    ERROR_INTELLIGENT_MODEL_UNAVAILABLE,
    ERROR_INTELLIGENT_PLANNER_FAILED,
    ERROR_INTELLIGENT_PRICE_DATA_UNAVAILABLE,
    ERROR_VEHICLE_TIMER_BLOCKS_CHARGING,
    INTELLIGENT_ALLOWED_REASONS,
    INTELLIGENT_STATUS_AT_RISK,
    INTELLIGENT_STATUS_CHARGING,
    INTELLIGENT_STATUS_DISABLED,
    INTELLIGENT_STATUS_IDENTIFYING,
    INTELLIGENT_STATUS_NO_EV,
    INTELLIGENT_STATUS_NO_PRICE_DATA,
    INTELLIGENT_STATUS_NOT_ACCEPTING,
    INTELLIGENT_STATUS_OVERRIDDEN,
    INTELLIGENT_STATUS_PLANNED,
    INTELLIGENT_STATUS_REACHED,
    INTELLIGENT_STORAGE_KEY,
    LOGGER,
    MINIMUM_CHARGE_MAX,
    MINIMUM_CHARGE_MIN,
    OTHER_VEHICLE_NAME,
    PRICE_INTEGRATION_DOMAIN,
    REASON_INTELLIGENT_CHARGING_COMPLETE,
    REASON_INTELLIGENT_IDENTIFYING,
    REASON_INTELLIGENT_NO_EV,
    REASON_INTELLIGENT_NOT_ACCEPTING,
    REASON_INTELLIGENT_TARGETS_REACHED,
    REASON_ONLY_WAITING_FOR_SELECTED_EV,
    REASON_INTELLIGENT_NO_PRICE_DATA,
    REASON_INTELLIGENT_OUTLET_UNAVAILABLE,
    REASON_INTELLIGENT_WAITING,
    VEHICLE_INTEGRATION_DOMAIN,
)
from .naming import configured_vehicle_names
from .storage import ModelStore

OUTLETS = ("smartevse_1", "smartevse_2")
VEHICLES = ("vehicle_1", "vehicle_2")
PRICE_LIST_DAYS = ("yesterday", "today", "tomorrow")
INTELLIGENT_STORE_VERSION = 1

UNIT = timedelta(minutes=cp.UNIT_MINUTES)
STARTUP_GRACE = timedelta(minutes=10)  # §7.9: prices may not be loaded yet
IDENTIFYING_WINDOW = timedelta(minutes=10)  # §6.9
LATE_PLUG_WINDOW = timedelta(minutes=60)  # §6.2
READY_BY_EDIT_KEEP = timedelta(hours=2)  # §6.2
LOG_INTERVAL = timedelta(minutes=10)  # rate limit for repeated exception logs
MAX_PAYLOAD_SLOTS = 36
MAX_PAYLOAD_SESSIONS = 10
MIN_SESSION_WINDOW_KWH = 0.01  # a realised window (or a session's cost) needs at least this energy
# §8.11 SoC-response cross-check.
CROSS_CHECK_MIN_KW = 5.0
CROSS_CHECK_MIN_DURATION = timedelta(minutes=10)
CROSS_CHECK_MIN_RISE = 2.0
CROSS_CHECK_MAX_GAP = timedelta(seconds=120)
# A battery reading trails the battery (cloud delay, stalled readings): rises
# reported within this long after the vehicle's own outlet charged still
# describe that charge, never the other outlet's.
CROSS_CHECK_OWN_CHARGE_QUIET = timedelta(minutes=30)
# The vehicle on the charging outlet must stay flat (less than this rise) ...
CROSS_CHECK_OTHER_MAX_RISE = 1.0
# ... and the rise must fit the energy metered meanwhile (tolerance, plus one
# reading step for whole-percent cars).
CROSS_CHECK_ENERGY_TOLERANCE = 1.4
# §8.4 unverified SoC: one short probe instead of "reached".
PROBE_MINUTES = 10
# Fallback power when the model cannot estimate it (never used in normal runs).
FALLBACK_EXPECTED_KW = 9.0
# Real Electricity Price usually has tomorrow's day-ahead prices by 14:00-15:00 local
# time. Until then the plan waits for them where that is still safe (round 5).
PRICE_PUBLICATION_LOCAL = time(15, 0)
FALLBACK_CONSERVATIVE_KW = 7.5
FALLBACK_CAP_KW = 10.7
EMERGENCY_MARGIN = 1.10
EMERGENCY_OVERHEAD = timedelta(minutes=10)

# Unique-id keys of the Intelligent entities (§4): f"{entry_id}_{key}".
ENTITY_KEYS: dict[str, Any] = {
    "intelligent_charge": ("switch", "intelligent_charge"),
    "vehicle_1": {
        "ready_by": ("time", "vehicle_1_ready_by"),
        "minimum_charge": ("number", "vehicle_1_minimum_charge"),
    },
    "vehicle_2": {
        "ready_by": ("time", "vehicle_2_ready_by"),
        "minimum_charge": ("number", "vehicle_2_minimum_charge"),
    },
    "other": {
        "ready_by": ("time", "other_vehicle_ready_by"),
    },
    "status": ("sensor", "intelligent_status"),
    "planned_cost": ("sensor", "intelligent_planned_cost"),
    "planned_saving": ("sensor", "intelligent_planned_saving"),
    "next_charge": ("sensor", "intelligent_next_charge"),
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def default_time_zone() -> tzinfo:
    """The Home Assistant time zone (read at call time so tests can patch it)."""
    return dt_util.DEFAULT_TIME_ZONE


def parse_ready_by(value: Any) -> time | None:
    """``datetime.time`` (seconds dropped) from "HH:MM[:SS]" or a time; None if invalid."""
    if isinstance(value, datetime):
        return None
    if isinstance(value, time):
        return time(value.hour, value.minute)
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = dt_util.parse_time(value.strip())
    if parsed is None:
        return None
    return time(parsed.hour, parsed.minute)


def normalize_ready_by(value: Any, default: str = DEFAULT_READY_BY) -> str:
    """Stored "HH:MM" form of a ready-by value; ``default`` when it cannot be parsed."""
    parsed = parse_ready_by(value)
    if parsed is None:
        parsed = parse_ready_by(default) or time(7, 0)
    return parsed.strftime("%H:%M")


def normalize_minimum(value: Any, default: int = DEFAULT_MINIMUM_CHARGE) -> int:
    """Minimum charge as a whole percent clamped to 10–100; ``default`` when invalid."""
    number = finite(value) if not isinstance(value, bool) else None
    if number is None:
        number = default
    return int(min(MINIMUM_CHARGE_MAX, max(MINIMUM_CHARGE_MIN, round(number))))


def next_local_occurrence(ready_by: Any, after: datetime, tz: tzinfo | None = None) -> datetime:
    """First local wall-clock ``ready_by`` strictly after ``after`` (returned in UTC).

    Built with ``datetime.combine(local_date, t, tzinfo=tz)`` per local date
    (fold=0, so an ambiguous time is its first occurrence; a nonexistent
    spring-forward time maps forward by the gap). Never rolls by adding a UTC
    timedelta, so the deadline keeps its local clock time across DST.
    """
    tz = tz or default_time_zone()
    clock = parse_ready_by(ready_by) or parse_ready_by(DEFAULT_READY_BY)
    after = dt_util.as_utc(after)
    local_day = after.astimezone(tz).date()
    for offset in range(-1, 4):
        candidate = datetime.combine(local_day + timedelta(days=offset), clock, tzinfo=tz).astimezone(UTC)
        if candidate > after:
            return candidate
    return datetime.combine(local_day + timedelta(days=4), clock, tzinfo=tz).astimezone(UTC)


def session_deadline(ready_by: Any, now: datetime, plugged_at: datetime | None,
                     tz: tzinfo | None = None) -> datetime:
    """§6.2 deadline for a plug session: the next occurrence, or the one after it
    when the car was plugged in less than 60 min before that ready-by."""
    deadline = next_local_occurrence(ready_by, now, tz)
    if plugged_at is not None and dt_util.as_utc(plugged_at) > deadline - LATE_PLUG_WINDOW:
        deadline = next_local_occurrence(ready_by, deadline, tz)
    return deadline


def iso_minute(value: datetime | None) -> str | None:
    """UTC ISO string rounded to the minute (payload times never carry seconds)."""
    if value is None:
        return None
    value = dt_util.as_utc(value)
    rounded = value.replace(second=0, microsecond=0)
    if value - rounded >= timedelta(seconds=30):
        rounded += timedelta(minutes=1)
    return rounded.isoformat()


def floor_minute(value: datetime) -> datetime:
    return dt_util.as_utc(value).replace(second=0, microsecond=0)


def money(value: float | None) -> float | None:
    """Money rounded to cents (None stays None, -0.0 becomes 0.0)."""
    if value is None:
        return None
    number = finite(value)
    if number is None:
        return None
    result = round(number, 2)
    return 0.0 if result == 0 else result


def build_entities(lookup: Callable[[str, str], str | None]) -> dict[str, Any]:
    """``intelligent.entities`` from a registry lookup ``(domain, unique_key) -> entity_id``."""

    def resolve(spec):
        if isinstance(spec, dict):
            return {name: resolve(value) for name, value in spec.items()}
        domain, key = spec
        try:
            return lookup(domain, key)
        except Exception:  # noqa: BLE001 - a missing registry only hides the entity
            return None

    return {name: resolve(spec) for name, spec in ENTITY_KEYS.items()}


def registry_entities(hass: Any, entry_id: str) -> dict[str, Any]:
    """``intelligent.entities`` of a config entry from the entity registry (never raises).

    Also used by the controller's fallback payload, so the card keeps its
    Intelligent controls when the adapter or its payload fails.
    """
    try:
        registry = er.async_get(hass)
    except Exception:  # noqa: BLE001 - bare HomeAssistant / test harness
        registry = None

    def lookup(domain: str, key: str) -> str | None:
        if registry is None:
            return None
        entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{entry_id}_{key}")
        if entity_id is None:
            return None
        entry = registry.async_get(entity_id)
        if entry is None or entry.disabled_by:
            return None
        return entity_id

    return build_entities(lookup)


def merge_price_lists(states: Iterable[Any]) -> tuple[list[dict[str, Any]], str | None]:
    """Merge Real Electricity Price ``hourly_prices`` lists (yesterday/today/tomorrow).

    Each list's ``unit_of_measurement`` (present even while its state is
    ``unknown``) gives the factor to currency/kWh. Entries keep their raw keys
    with ``actual_price`` converted, so the model can still read the
    weekend/holiday flags. The local 00:00–01:00 hour lives in the previous
    day's list; merging is by timestamp, never by list name.
    Returns (entries, currency).
    """
    entries: list[dict[str, Any]] = []
    currency = None
    for state in states:
        attributes = getattr(state, "attributes", None)
        if not isinstance(attributes, Mapping):
            continue
        factor, list_currency = price_unit(attributes.get("unit_of_measurement"))
        if factor is None:
            continue
        items = attributes.get("hourly_prices")
        if not isinstance(items, (list, tuple)):
            continue
        currency = currency or list_currency
        for item in items:
            if not isinstance(item, Mapping):
                continue
            price = item.get("actual_price")
            value = finite(price) if price is not None and not isinstance(price, bool) else None
            entries.append({
                "start_time": item.get("start_time"),
                "end_time": item.get("end_time"),
                "actual_price": None if value is None else value * factor,
                "is_weekend": item.get("is_weekend"),
                "is_holiday": item.get("is_holiday"),
            })
    return entries, currency


def emergency_outlet(now: datetime, cars: Sequence[cp.CarRequest]) -> str | None:
    """Last-resort decision when even the no-price plan fails: charge only a car
    whose latest continuous start (need / power, +10 %, +10 min) has arrived."""
    best = None
    for car in cars:
        if car.done or car.excluded or getattr(car, "reached", False):
            continue
        cap = finite(car.cap_kw) or 0.0
        if cap <= 0:
            continue
        if car.soc is None:
            need = finite(car.budget_kwh)
            need = cp.DEFAULT_BUDGET_KWH if need is None else max(0.0, need)
        else:
            per_pct = finite(car.kwh_per_pct) or cp.DEFAULT_KWH_PER_PCT
            need = max(0.0, (finite(car.target_soc) or 0.0) - car.soc) * per_pct
        if need <= 0:
            continue
        latest = car.deadline - timedelta(hours=need / cap * EMERGENCY_MARGIN) - EMERGENCY_OVERHEAD
        if car.overrun or now >= latest:
            key = (car.deadline, car.priority, car.outlet)
            if best is None or key < best[0]:
                best = (key, car.outlet)
    return best[1] if best else None


def empty_payload(*, enabled: bool, overridden: bool = False,
                  settings: Mapping[str, Mapping[str, Any]] | None = None,
                  vehicle_names: Mapping[str, str] | None = None, top_up_enabled: bool = False,
                  top_up_price: float | None = None, entities: Mapping[str, Any] | None = None,
                  currency: str | None = None) -> dict[str, Any]:
    """The §10 payload shape with nothing planned (also used when the adapter is missing)."""
    settings = settings or {}
    names = dict(vehicle_names or {})
    vehicles: dict[str, Any] = {}
    for vehicle in VEHICLES:
        values = settings.get(vehicle) or {}
        vehicles[vehicle] = {
            "name": names.get(vehicle) or vehicle,
            "ready_by": normalize_ready_by(values.get("ready_by")),
            "minimum": normalize_minimum(values.get("minimum")),
            "suggested_ready_by": None, "usually_leaves": None, "learned_sessions": 0,
            "model": {"kwh_per_pct": None, "kwh_per_pct_source": "default", "cap_kw": None},
        }
    other = settings.get("other") or {}
    vehicles["other"] = {"name": OTHER_VEHICLE_NAME, "ready_by": normalize_ready_by(other.get("ready_by")),
                         "minimum": normalize_minimum(other.get("minimum"))}
    if not enabled:
        status = INTELLIGENT_STATUS_DISABLED
    elif overridden:
        status = INTELLIGENT_STATUS_OVERRIDDEN
    else:
        status = INTELLIGENT_STATUS_NO_EV
    return {
        "enabled": bool(enabled),
        "status": status,
        "reason": None,
        "overridden_by": "force" if enabled and overridden else None,
        "connected_count": 0,
        "currency": currency,
        "known_until": None,
        "prices_expected_at": None,
        "forecast_used": False,
        "baseline_kind": "plug_in",
        "model_error": None,
        "planner_error": None,
        "totals": {"cost": None, "saving": None, "energy_kwh": 0.0, "includes_forecast": False,
                   "partial": False},
        "next_charge": None,
        "order": [],
        "chargers": {outlet: None for outlet in OUTLETS},
        "vehicles": vehicles,
        "household": {"learned_days": 0, "night_expected_kw": None},
        "slots": [],
        "sessions": [],
        # Charging above the minimum in cheap hours. "price" is the learned limit; there
        # is no user setting since 0.0.10.0.
        "top_up": {"enabled": bool(top_up_enabled), "price": top_up_price, "source": "learned"},
        "entities": dict(entities) if entities is not None else build_entities(lambda _domain, _key: None),
    }


def flat_values(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Flat sensor values (§4) derived from a payload."""
    next_charge = payload.get("next_charge")
    return {
        "intelligent_status": payload.get("status"),
        "intelligent_planned_cost": (payload.get("totals") or {}).get("cost"),
        "intelligent_planned_saving": (payload.get("totals") or {}).get("saving"),
        "intelligent_currency": payload.get("currency"),
        "intelligent_next_charge": dt_util.parse_datetime(next_charge) if next_charge else None,
    }


# ---------------------------------------------------------------------------
# Data passed between the controller and the adapter
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OutletInput:
    """Controller facts for one outlet (polled status, mapping, Intelligent state)."""

    key: str
    available: bool
    connected: bool  # effective: kept from the last poll while the API is unavailable
    charging: bool  # polled state == "Charging"
    mode: str = "Off"
    vehicle: str | None = None
    offered_current: float | None = None
    max_current: float | None = None
    max_mains: float | None = None
    min_current: float | None = None
    plugged_at: datetime | None = None
    deadline: datetime | None = None
    reached: bool = False
    vehicle_charging_state: str = ""
    # Last poll that showed this outlet "Charging" (persisted by the controller).
    last_charging_at: datetime | None = None


@dataclass
class CarFacts:
    """Adapter facts for one connected outlet (SoC, settings, model estimates)."""

    outlet: str
    available: bool
    connected: bool
    charging: bool
    vehicle: str | None
    settings_key: str
    name: str
    soc: float | None
    soc_source: str
    live_soc: float | None
    live_changed_at: datetime | None
    minimum: int
    ready_by: str
    car_limit: float | None
    target: float
    limited_by_car: bool
    kwh_per_pct: float
    kwh_per_pct_conservative: float
    kwh_per_pct_source: str
    cap_kw: float
    cap_low_kw: float
    budget_kwh: float | None
    delivered_kwh: float
    capacity_kwh: float | None
    car_timer: bool


@dataclass
class ObserveResult:
    facts: dict[str, CarFacts] = field(default_factory=dict)
    swap: tuple[str, str] | None = None  # §8.11: outlets whose vehicles must be swapped


@dataclass(frozen=True)
class PlanInput:
    """Controller planning state for one connected outlet."""

    outlet: str
    deadline: datetime
    overrun: bool = False
    done: bool = False
    excluded: bool = False
    priority: int = 0
    drawing: bool = False
    identifying: bool = False
    # Off the API for longer than the hand-off hold: not planned until it answers
    # (its plug session, deadline and overrun are kept by the controller).
    unavailable: bool = False
    # Reached its minimum in this session (§8.4 hysteresis): no minimum need, but
    # still topped up to its car limit in cheap known hours when top-up is on.
    reached: bool = False
    # Held off because it did not draw (§6.4); part of ``done``.
    not_accepting: bool = False


@dataclass(frozen=True)
class IntelligentDecision:
    """What the Intelligent gate does this cycle."""

    allowed: bool
    reason: str
    outlet: str | None = None
    block_start: datetime | None = None
    controller_error: str | None = None  # warning level, never blocking


@dataclass(frozen=True)
class OutletPayloadInput:
    deadline: datetime | None = None
    overrun: bool = False
    not_accepting: bool = False
    identifying: bool = False
    excluded: bool = False


@dataclass(frozen=True)
class PayloadContext:
    """Final cycle result the payload is built from."""

    enabled: bool
    overridden: bool
    governing: bool
    charge_allowed: bool
    controller_state: str
    charge_reason: str | None
    active_outlet: str | None
    active_since: datetime | None
    settings: Mapping[str, Mapping[str, Any]]  # {"vehicle_1": {"ready_by", "minimum"}, ...}
    top_up_enabled: bool
    top_up_price: float | None
    outlets: Mapping[str, OutletPayloadInput]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class IntelligentCharging:
    """HA adapter for Intelligent charging (owned by the controller)."""

    def __init__(self, hass: Any, entry_data: Mapping[str, Any], options: Mapping[str, Any], *,
                 store: Any = None) -> None:
        self.hass = hass
        self._entry_data = entry_data
        self._options = options
        self._entry_id = str(entry_data.get("entry_id") or "")
        # Private, atomic; write failures raise and a corrupt file is never replaced.
        self._store = store if store is not None else ModelStore(
            hass,
            INTELLIGENT_STORE_VERSION,
            INTELLIGENT_STORAGE_KEY.format(entry_id=self._entry_id),
        )
        self.model = cm.ChargeModel(None, vehicle_names=self._vehicle_names(), baseline=self._baseline)
        self._storage_error: str | None = None
        self._save_error: str | None = None
        self._estimate_error: str | None = None
        self._read_only = False
        self._started_at: datetime | None = None
        self._log_times: dict[str, datetime] = {}
        self._cross: dict[str, dict[str, Any]] = {}
        self._outlet_charged_at: dict[str, datetime] = {}  # last cycle each outlet was Charging
        self._facts: dict[str, CarFacts] = {}
        self._inputs: dict[str, OutletInput] = {}
        self._price_entries: list[dict[str, Any]] = []
        self._currency: str | None = None
        self._known_until: datetime | None = None
        self._prices_expected_at: datetime | None = None
        self._limits: dict[str, float | None] = {}
        self._capacities: dict[str, float | None] = {}
        self._plan: cp.Plan | None = None
        # The plan the payload shows: during a boundary bridge (see _decide) the
        # last plan of the same unit that still had the running car, so the
        # payload and sensors do not switch to a "planned" view for ~1 min.
        self._display_plan: cp.Plan | None = None
        self._bridged = False
        self._plan_inputs: dict[str, PlanInput] = {}
        self._price_slots: list[cp.PriceSlot] = []
        self._power_slots: list[cp.PowerSlot] = []
        self._planner_error: str | None = None
        self._decision: IntelligentDecision | None = None
        self._display_key: str | None = None
        self._display: tuple[dict[str, Any], dict[str, Any]] | None = None

    # -- configuration -------------------------------------------------------

    def _config(self, key: str) -> Any:
        return self._options.get(key, self._entry_data.get(key))

    def _vehicle_names(self) -> dict[str, str]:
        values = {**self._entry_data, **self._options}
        first, second = configured_vehicle_names(values)
        return {"vehicle_1": first, "vehicle_2": second}

    def _battery_entity(self, vehicle: str) -> str | None:
        key = CONF_VEHICLE_1_BATTERY_ENTITY if vehicle == "vehicle_1" else CONF_VEHICLE_2_BATTERY_ENTITY
        value = self._config(key)
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()

    def _capacity_override(self, vehicle: str) -> float | None:
        key = (CONF_VEHICLE_1_BATTERY_CAPACITY_KWH if vehicle == "vehicle_1"
               else CONF_VEHICLE_2_BATTERY_CAPACITY_KWH)
        value = finite(self._options.get(key, 0))
        return value if value is not None and value > 0 else None

    @property
    def model_error(self) -> str | None:
        """Current model problem (storage, save, observation or estimation), or None."""
        parts = [self._storage_error, self._save_error, self._estimate_error,
                 getattr(self.model, "last_error", None)]
        text = "; ".join(part for part in parts if part)
        return text[:300] or None

    @property
    def planner_error(self) -> str | None:
        return self._planner_error

    # -- entity derivation (registry first, never raises) ----------------------

    def _registry(self):
        try:
            return er.async_get(self.hass)
        except Exception:  # noqa: BLE001 - bare HomeAssistant / test harness
            return None

    def price_list_entity_ids(self) -> list[str]:
        """Hourly price list sensors (yesterday, today, tomorrow) of the price integration."""
        price_entity = self._entry_data.get(CONF_PRICE_SENSOR_ENTITY)
        if not isinstance(price_entity, str) or not price_entity:
            return []
        found: dict[str, str] = {}
        registry = self._registry()
        if registry is not None:
            try:
                entry = registry.async_get(price_entity)
                config_entry_id = getattr(entry, "config_entry_id", None) if entry else None
                if config_entry_id:
                    for candidate in er.async_entries_for_config_entry(registry, config_entry_id):
                        if (candidate.platform != PRICE_INTEGRATION_DOMAIN
                                or candidate.domain != "sensor" or candidate.disabled_by):
                            continue
                        unique_id = str(candidate.unique_id or "")
                        for day in PRICE_LIST_DAYS:
                            if unique_id.endswith(f"_hourly_prices_{day}"):
                                found[day] = candidate.entity_id
            except Exception:  # noqa: BLE001 - registry shape problems fall back below
                found = {}
        if found:
            return [found[day] for day in PRICE_LIST_DAYS if day in found]
        if "current_price" in price_entity:
            return [price_entity.replace("current_price", f"hourly_prices_{day}") for day in PRICE_LIST_DAYS]
        return []

    def vehicle_entity(self, vehicle: str, suffix: str) -> str | None:
        """Volvo sensor next to the battery entity: ``battery_capacity`` or
        ``target_battery_charge_level`` (registry unique id, else name suffix)."""
        battery = self._battery_entity(vehicle)
        if battery is None:
            return None
        registry = self._registry()
        if registry is not None:
            try:
                entry = registry.async_get(battery)
                unique_id = str(getattr(entry, "unique_id", "") or "") if entry else ""
                if entry is not None and entry.platform == VEHICLE_INTEGRATION_DOMAIN and unique_id:
                    for base in ("battery_charge_level", "battery"):
                        if unique_id.endswith(base):
                            # The registry is authoritative for a known Volvo battery:
                            # a missing or disabled sibling is not guessed by name.
                            entity_id = registry.async_get_entity_id(
                                "sensor", VEHICLE_INTEGRATION_DOMAIN, unique_id[: -len(base)] + suffix)
                            target = registry.async_get(entity_id) if entity_id else None
                            return entity_id if target is not None and not target.disabled_by else None
            except Exception:  # noqa: BLE001
                pass
        if battery.endswith("_battery"):
            stem = battery[: -len("_battery")]
            return f"{stem}_{suffix}"
        return None

    def source_entity_ids(self) -> list[str]:
        """Entities whose changes should trigger an immediate cycle (coordinator listener)."""
        ids = list(self.price_list_entity_ids())
        for vehicle in VEHICLES:
            battery = self._battery_entity(vehicle)
            if battery:
                ids.append(battery)
        return [eid for eid in dict.fromkeys(ids) if isinstance(eid, str) and eid]

    def entities(self) -> dict[str, Any]:
        """``intelligent.entities``: registry lookup each cycle; None when missing or disabled."""
        return registry_entities(self.hass, self._entry_id)

    # -- state reading -----------------------------------------------------------

    def _state(self, entity_id: str | None):
        if not entity_id:
            return None
        try:
            return self.hass.states.get(entity_id)
        except Exception:  # noqa: BLE001
            return None

    def _number(self, entity_id: str | None) -> float | None:
        state = self._state(entity_id)
        if state is None or state.state in (None, "unknown", "unavailable"):
            return None
        return finite(state.state)

    def _phases(self, entity_ids: Sequence[str | None]) -> tuple[float, float, float] | None:
        values = [self._number(entity_id) for entity_id in entity_ids]
        if any(value is None for value in values):
            return None
        return values[0], values[1], values[2]

    def _soc_reading(self, vehicle: str) -> tuple[float | None, datetime | None]:
        state = self._state(self._battery_entity(vehicle))
        if state is None or state.state in (None, "unknown", "unavailable"):
            return None, None
        value = finite(state.state)
        if value is None or not 0 <= value <= 100:
            return None, None
        changed = getattr(state, "last_changed", None)
        return value, dt_util.as_utc(changed) if isinstance(changed, datetime) else None

    def _read_prices(self) -> tuple[list[dict[str, Any]], str | None]:
        states = [self._state(entity_id) for entity_id in self.price_list_entity_ids()]
        entries, currency = merge_price_lists(states)
        if currency is None:
            try:
                currency = self.model.last_good_prices()[1]
            except Exception:  # noqa: BLE001
                currency = None
        if currency is None:
            currency = tariff(self._state(self._entry_data.get(CONF_PRICE_SENSOR_ENTITY)))[1]
        return entries, currency

    # -- lifecycle ---------------------------------------------------------------

    async def async_initialize(self, ledger: Any = None) -> None:
        """Load the private model store; a load failure runs on defaults read-only."""
        stored = None
        try:
            stored = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - corrupt/newer file: never overwrite it
            self._read_only = True
            self._storage_error = f"storage_load_failed: {type(err).__name__}"
            try:
                self._store.make_read_only()
            except Exception:  # noqa: BLE001
                pass
            LOGGER.warning("Intelligent charging model could not be loaded; running on defaults "
                           "without saving: %s", err)
        try:
            self.model = cm.ChargeModel(stored, vehicle_names=self._vehicle_names(), baseline=self._baseline)
        except Exception as err:  # noqa: BLE001
            # The file holds learning this version cannot read: run on defaults but
            # never save them over it (same as a storage load failure).
            self._read_only = True
            self._storage_error = f"model_load_failed: {type(err).__name__}"
            try:
                self._store.make_read_only()
            except Exception:  # noqa: BLE001
                pass
            LOGGER.warning("Intelligent charging model could not be read; running on defaults "
                           "without saving: %s", err)
            self.model = cm.ChargeModel(None, vehicle_names=self._vehicle_names(), baseline=self._baseline)
        if "schema" in self.model.load_issues and not self._read_only:
            # The whole file was rejected (another schema, e.g. after a rollback from a
            # newer release, or not a model at all): run on defaults but never save
            # them over it.
            self._read_only = True
            self._storage_error = "model_schema_unsupported"
            try:
                self._store.make_read_only()
            except Exception:  # noqa: BLE001
                pass
            LOGGER.warning("Intelligent charging model has an unsupported format; running on defaults "
                           "without saving")
        elif self.model.load_issues:
            LOGGER.warning("Intelligent charging model reset invalid stored fields: %s",
                           ", ".join(self.model.load_issues[:20]))
        if ledger is not None:
            try:
                self.model.seed_from_ledger(ledger)
            except Exception:  # noqa: BLE001
                LOGGER.debug("Unable to seed the Intelligent session budget", exc_info=True)

    async def async_save_if_due(self, now: datetime) -> None:
        """Save on commit events at most every 5 min; errors only set model_error."""
        try:
            due = self.model.should_save(now)
        except Exception:  # noqa: BLE001
            due = False
        if due:
            await self._async_save(now)

    async def async_flush(self) -> None:
        """Save unconditionally (controller shutdown / entry unload)."""
        await self._async_save(dt_util.utcnow())

    async def _async_save(self, now: datetime) -> None:
        if self._read_only:
            return
        try:
            data = self.model.to_dict()
            await self._store.async_save(data)
        except Exception as err:  # noqa: BLE001 - disk/serialisation errors are visible, not fatal
            self._save_error = f"save_failed: {type(err).__name__}"
            try:
                self.model.mark_save_failed(now)
            except Exception:  # noqa: BLE001
                pass
            self._log_exception("save", "Unable to save the Intelligent charging model", now)
            return
        self._save_error = None
        self.model.mark_saved(now)

    def _log_exception(self, kind: str, message: str, now: datetime | None = None) -> None:
        now = now or dt_util.utcnow()
        last = self._log_times.get(kind)
        if last is None or now - last >= LOG_INTERVAL or now < last:
            self._log_times[kind] = now
            LOGGER.exception(message)
        else:
            LOGGER.debug(message, exc_info=True)

    # -- baseline callback (model realised windows, §7.8) ---------------------------

    def _baseline(self, sessions: Sequence[Mapping[str, Any]], price_slots: Sequence[Mapping[str, Any]],
                  power_slots: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
        prices = [cp.PriceSlot(s["start"], s["end"], s.get("price"), s.get("kind") or cp.PRICE_UNKNOWN)
                  for s in price_slots]
        powers = [cp.PowerSlot(s["start"], s["end"], float(s.get("expected_kw") or 0.0),
                               float(s.get("conservative_kw") or s.get("expected_kw") or 0.0))
                  for s in power_slots]
        cap = max((p.expected_kw for p in powers), default=FALLBACK_CAP_KW) or FALLBACK_CAP_KW
        baseline = [cp.BaselineSession(outlet=s["key"], plugged_at=s["plugged_at"],
                                       energy_kwh=float(s.get("energy_kwh") or 0.0), cap_kw=cap)
                    for s in sessions]
        return cp.baseline_costs(baseline, prices, powers)

    # -- 1. observation (every mode) -------------------------------------------------

    def observe(self, now: datetime, *, outlets: Mapping[str, OutletInput], settings: Mapping[str, Mapping[str, Any]],
                last_socs: Mapping[str, tuple[float | None, datetime | None]] | None = None,
                ledger: Any = None, controller_state: str | None = None,
                charge_reason: str | None = None) -> ObserveResult:
        """Observe the cycle (model learning) and return per-outlet facts."""
        tz = default_time_zone()
        if self._started_at is None:
            self._started_at = now
        self._inputs = {key: outlets[key] for key in OUTLETS if key in outlets}
        self._estimate_error = None
        try:
            self._price_entries, self._currency = self._read_prices()
        except Exception:  # noqa: BLE001
            self._price_entries, self._currency = [], None
            self._log_exception("prices", "Unable to read Intelligent charging price lists", now)
        socs: dict[str, tuple[float | None, datetime | None]] = {}
        capacities: dict[str, float | None] = {}
        limits: dict[str, float | None] = {}
        for vehicle in VEHICLES:
            try:
                socs[vehicle] = self._soc_reading(vehicle)
                capacity = self._capacity_override(vehicle)
                if capacity is None:
                    capacity = self._number(self.vehicle_entity(vehicle, "battery_capacity"))
                capacities[vehicle] = capacity if capacity is not None and capacity > 0 else None
                limit = self._number(self.vehicle_entity(vehicle, "target_battery_charge_level"))
                limits[vehicle] = limit if limit is not None and 0 < limit <= 100 else None
            except Exception:  # noqa: BLE001
                socs[vehicle], capacities[vehicle], limits[vehicle] = (None, None), None, None
        self._capacities = capacities
        self._limits = limits

        try:
            power_state = self._state(self._entry_data.get(CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY))
            ev_power = power_kw(power_state)
        except Exception:  # noqa: BLE001
            ev_power = None
        try:
            self.model.observe_cycle(
                now, tz,
                outlets={
                    key: {
                        "available": item.available,
                        "connected": item.connected,
                        "charging": item.charging,
                        "vehicle": item.vehicle,
                        "offered_current": item.offered_current,
                        "max_current": item.max_current,
                        "max_mains": item.max_mains,
                        "min_current": item.min_current,
                        "plugged_at": item.plugged_at,
                        "deadline": item.deadline,
                        "reached": item.reached,
                    }
                    for key, item in self._inputs.items()
                },
                ev_power_kw=ev_power,
                mains_phase_currents=self._phases((
                    self._entry_data.get(CONF_MAINS_L1_ENTITY),
                    self._entry_data.get(CONF_MAINS_L2_ENTITY),
                    self._entry_data.get(CONF_MAINS_L3_ENTITY),
                )),
                ev_phase_currents=self._phases((
                    self._entry_data.get(CONF_EV_METER_L1_ENTITY),
                    self._entry_data.get(CONF_EV_METER_L2_ENTITY),
                    self._entry_data.get(CONF_EV_METER_L3_ENTITY),
                )),
                socs={vehicle: {"soc": value, "changed_at": changed} for vehicle, (value, changed) in socs.items()},
                capacities=capacities,
                ledger=ledger,
                prices=self._price_entries,
                price_currency=self._currency,
                controller_state=controller_state,
                charge_reason=charge_reason,
            )
        except Exception as err:  # noqa: BLE001 - learning never breaks control
            self._estimate_error = f"observe_failed: {type(err).__name__}"
            self._log_exception("observe", "Intelligent charging model observation failed", now)
        self._known_until = self._price_horizon()

        result = ObserveResult()
        for key, item in self._inputs.items():
            if not item.connected:
                continue
            try:
                result.facts[key] = self._car_facts(now, item, settings, socs, capacities, limits,
                                                    last_socs or {})
            except Exception as err:  # noqa: BLE001
                self._estimate_error = f"estimate_failed: {type(err).__name__}"
                self._log_exception("facts", "Intelligent charging estimation failed", now)
                result.facts[key] = self._default_facts(item, settings)
        self._facts = result.facts
        try:
            result.swap = self._cross_check(now, ev_power, socs)
        except Exception:  # noqa: BLE001
            result.swap = None
            self._log_exception("cross_check", "Intelligent mapping cross-check failed", now)
        return result

    def _price_horizon(self) -> datetime | None:
        """End of the known prices: live lists, else the persisted last good list."""
        ends = [e["end"] for e in cm.normalize_price_entries(self._price_entries) if e["price"] is not None]
        try:
            ends += [e["end"] for e in self.model.last_good_prices()[2]]
        except Exception:  # noqa: BLE001
            pass
        return max(ends) if ends else None

    def _settings_for(self, settings: Mapping[str, Mapping[str, Any]], key: str) -> tuple[str, int]:
        values = settings.get(key) or {}
        return (normalize_ready_by(values.get("ready_by")),
                normalize_minimum(values.get("minimum")))

    def _default_facts(self, item: OutletInput, settings: Mapping[str, Mapping[str, Any]]) -> CarFacts:
        settings_key = item.vehicle if item.vehicle in VEHICLES else "other"
        ready_by, minimum = self._settings_for(settings, settings_key)
        default = cm.ChargeModel(None)
        per_pct = default.kwh_per_pct(None)
        power = default.car_power(None, item.max_current)
        return CarFacts(
            outlet=item.key, available=item.available, connected=item.connected, charging=item.charging,
            vehicle=item.vehicle if item.vehicle in VEHICLES else None, settings_key=settings_key,
            name=self._vehicle_names().get(item.vehicle, OTHER_VEHICLE_NAME) if item.vehicle else OTHER_VEHICLE_NAME,
            soc=None, soc_source="unknown", live_soc=None, live_changed_at=None,
            minimum=minimum, ready_by=ready_by, car_limit=None, target=float(minimum), limited_by_car=False,
            kwh_per_pct=per_pct.expected, kwh_per_pct_conservative=per_pct.conservative,
            kwh_per_pct_source=per_pct.source, cap_kw=power.cap_kw, cap_low_kw=power.cap_low_kw,
            budget_kwh=default.budget_kwh(None).value, delivered_kwh=0.0, capacity_kwh=None,
            car_timer=item.vehicle_charging_state == "scheduled",
        )

    def _car_facts(self, now: datetime, item: OutletInput, settings, socs, capacities, limits,
                   last_socs) -> CarFacts:
        vehicle = item.vehicle if item.vehicle in VEHICLES else None
        settings_key = vehicle or "other"
        ready_by, minimum = self._settings_for(settings, settings_key)
        names = self._vehicle_names()
        capacity = capacities.get(vehicle) if vehicle else None
        car_limit = limits.get(vehicle) if vehicle else None
        target = float(minimum) if car_limit is None else float(min(minimum, car_limit))
        per_pct = self.model.kwh_per_pct(vehicle, capacity)
        power = self.model.car_power(vehicle, item.max_current, outlet=item.key, now=now)
        live_soc, live_changed = socs.get(vehicle, (None, None)) if vehicle else (None, None)
        last_soc, last_soc_at = last_socs.get(vehicle, (None, None)) if vehicle else (None, None)
        try:
            soc, source = self.model.soc_estimate(
                item.key, now, car_limit=car_limit, kwh_per_pct=per_pct.expected, capacity_kwh=capacity,
                last_soc=last_soc, last_soc_at=last_soc_at)
        except Exception:  # noqa: BLE001
            soc, source = cm.resolve_soc(now, live_soc=live_soc, live_changed_at=live_changed,
                                         last_soc=last_soc, last_soc_at=last_soc_at,
                                         plugged_at=item.plugged_at)
        if vehicle is None:
            soc, source = None, "unknown"
        budget = self.model.session_budget(item.key, vehicle)
        return CarFacts(
            outlet=item.key, available=item.available, connected=item.connected, charging=item.charging,
            vehicle=vehicle, settings_key=settings_key,
            name=names.get(vehicle, OTHER_VEHICLE_NAME) if vehicle else OTHER_VEHICLE_NAME,
            soc=soc, soc_source=source, live_soc=live_soc, live_changed_at=live_changed,
            minimum=minimum, ready_by=ready_by, car_limit=car_limit, target=target,
            limited_by_car=car_limit is not None and car_limit < minimum,
            kwh_per_pct=per_pct.expected, kwh_per_pct_conservative=per_pct.conservative,
            kwh_per_pct_source=per_pct.source, cap_kw=power.cap_kw, cap_low_kw=power.cap_low_kw,
            budget_kwh=budget.remaining_kwh, delivered_kwh=budget.delivered_kwh, capacity_kwh=capacity,
            car_timer=vehicle is not None and item.vehicle_charging_state == "scheduled",
        )

    def _cross_check(self, now: datetime, ev_power: float | None,
                     socs: Mapping[str, tuple[float | None, datetime | None]]) -> tuple[str, str] | None:
        """§8.11: vehicle V's SoC rises >= 2 points while V's mapped outlet is Off and the
        other outlet is the only one charging (>= 5 kW for >= 10 min) -> swap."""
        inputs = self._inputs
        charged_at = self._outlet_charged_at
        for key, item in inputs.items():
            if item.available and item.charging:
                charged_at[key] = now
        charging = [key for key, item in inputs.items() if item.available and item.charging]
        for vehicle in VEHICLES:
            mapped = next((key for key, item in inputs.items() if item.vehicle == vehicle), None)
            soc = socs.get(vehicle, (None, None))[0]
            track = self._cross.get(vehicle)
            own_charged = None
            if mapped is not None:
                times = [t for t in (inputs[mapped].last_charging_at, charged_at.get(mapped)) if t is not None]
                own_charged = max(times) if times else None
            holds = (
                mapped is not None
                and len(charging) == 1 and charging[0] != mapped
                and inputs[mapped].available and inputs[mapped].connected
                and not inputs[mapped].charging and inputs[mapped].mode in ("Off", "Pause")
                and ev_power is not None and ev_power >= CROSS_CHECK_MIN_KW
                and soc is not None
                # A rise reported soon after the car's own outlet charged is that
                # charge arriving late (Volvo lag, a stalled reading), not evidence.
                and (own_charged is None or now - own_charged >= CROSS_CHECK_OWN_CHARGE_QUIET)
            )
            if not holds:
                self._cross.pop(vehicle, None)
                continue
            other = charging[0]
            other_vehicle = inputs[other].vehicle if inputs[other].vehicle in VEHICLES else None
            other_soc = socs.get(other_vehicle, (None, None))[0] if other_vehicle else None
            if (track is None or track["outlet"] != mapped or track["other"] != other
                    or track["other_vehicle"] != other_vehicle
                    or now - track["last_at"] > CROSS_CHECK_MAX_GAP or now < track["last_at"]):
                self._cross[vehicle] = {"outlet": mapped, "other": other, "other_vehicle": other_vehicle,
                                        "since": now, "soc": soc, "other_soc": other_soc,
                                        "last_at": now, "energy": 0.0}
                continue
            track["energy"] += ev_power * (now - track["last_at"]).total_seconds() / 3600.0
            track["last_at"] = now
            track["soc"] = min(track["soc"], soc)
            if other_soc is not None:
                track["other_soc"] = other_soc if track["other_soc"] is None else min(track["other_soc"], other_soc)
            rise = soc - track["soc"]
            if now - track["since"] < CROSS_CHECK_MIN_DURATION or rise < CROSS_CHECK_MIN_RISE:
                continue
            if other_vehicle is not None:
                # Corroboration: the car mapped to the charging outlet must not be
                # rising too (then it really is the one charging there).
                if other_soc is None or track["other_soc"] is None:
                    continue
                if other_soc - track["other_soc"] >= CROSS_CHECK_OTHER_MAX_RISE:
                    continue
            per_pct = self.model.kwh_per_pct(vehicle, self._capacities.get(vehicle)).expected
            if rise * per_pct > CROSS_CHECK_ENERGY_TOLERANCE * track["energy"] + per_pct:
                # More rise than the metered energy can explain: a stalled reading
                # catching up, not this charge.
                continue
            self._cross.clear()
            return mapped, other
        return None

    def discard_sessions(self, outlets: Iterable[str]) -> None:
        """§8.11 swap: drop the swapped outlets' session samples in the model."""
        for outlet in outlets:
            try:
                self.model.discard_session(outlet)
            except Exception:  # noqa: BLE001
                LOGGER.debug("Unable to discard Intelligent session samples", exc_info=True)

    def facts(self, outlet: str) -> CarFacts | None:
        """This cycle's facts for a connected outlet (None when not connected)."""
        return self._facts.get(outlet)

    def plan_input(self, outlet: str) -> PlanInput | None:
        """The planning state the controller passed for an outlet this cycle."""
        return self._plan_inputs.get(outlet)

    @property
    def last_decision(self) -> IntelligentDecision | None:
        """The most recent planner decision (reused for one cycle after a mapping swap)."""
        return self._decision

    # -- 2. planning ---------------------------------------------------------------------

    def _charger_limits(self) -> dict[str, float | None]:
        for key in OUTLETS:
            item = self._inputs.get(key)
            if item is not None and item.available and item.max_current:
                return {"max_current": item.max_current, "max_mains": item.max_mains,
                        "min_current": item.min_current}
        return {"max_current": None, "max_mains": None, "min_current": None}

    def _slots(self, now: datetime, end: datetime) -> tuple[list[cp.PriceSlot], list[cp.PowerSlot]]:
        tz = default_time_zone()
        start = cm.local_hour_start(now, tz)
        end = max(end, start + timedelta(hours=1))
        try:
            prices = [cp.PriceSlot(s["start"], s["end"], s["price"], s["kind"])
                      for s in self.model.price_slots(start, end, tz, entries=self._price_entries)]
        except Exception:  # noqa: BLE001 - fall back to the live known prices only
            self._estimate_error = "price_slots_failed"
            prices = [cp.PriceSlot(e["start"], e["end"], e["price"], cp.PRICE_KNOWN)
                      for e in cm.normalize_price_entries(self._price_entries) if e["price"] is not None]
        try:
            limits = self._charger_limits()
            powers = [cp.PowerSlot(s["start"], s["end"], s["expected_kw"], s["conservative_kw"])
                      for s in self.model.power_slots(start, end, tz, **limits)]
        except Exception:  # noqa: BLE001
            self._estimate_error = "power_slots_failed"
            powers = [cp.PowerSlot(start, end + timedelta(hours=1), FALLBACK_EXPECTED_KW, FALLBACK_CONSERVATIVE_KW)]
        return prices, powers

    def _requests(self, inputs: Mapping[str, PlanInput]) -> list[cp.CarRequest]:
        requests = []
        for outlet in OUTLETS:
            plan_input = inputs.get(outlet)
            facts = self._facts.get(outlet)
            if plan_input is None or facts is None:
                continue
            soc, budget = facts.soc, facts.budget_kwh
            # An outlet off the API cannot charge now: the other car gets the time.
            done = plan_input.done or plan_input.unavailable
            if (facts.soc_source == "unverified" and soc is not None and soc >= facts.target
                    and not done):
                # §8.4: a SoC from before the drive cannot prove "reached"; probe
                # with one short charge in the cheapest unit (unknown-SoC budget).
                probe = facts.cap_kw * PROBE_MINUTES / 60.0
                soc, budget = None, max(0.0, probe - facts.delivered_kwh)
            requests.append(cp.CarRequest(
                outlet=outlet, vehicle=facts.vehicle, settings_key=facts.settings_key, name=facts.name,
                soc=soc, soc_source=facts.soc_source, target_soc=facts.target, car_limit=facts.car_limit,
                deadline=plan_input.deadline, kwh_per_pct=facts.kwh_per_pct,
                kwh_per_pct_conservative=facts.kwh_per_pct_conservative, cap_kw=facts.cap_kw,
                cap_low_kw=facts.cap_low_kw, budget_kwh=budget, done=done,
                overrun=plan_input.overrun, drawing=plan_input.drawing, priority=plan_input.priority,
                excluded=plan_input.excluded, reached=plan_input.reached and not done,
            ))
        return requests

    def plan(self, now: datetime, inputs: Mapping[str, PlanInput], *, commitment: tuple[str, datetime] | None = None,
             top_up_price: float | None = None, seed: IntelligentDecision | None = None) -> IntelligentDecision:
        """Run the planner for the connected outlets and return the gate decision.

        ``seed`` stands in for the previous decision when this process has none yet
        (first cycle after a restart or reload): the controller rebuilds it from its
        persisted state, so a car charging into the last minute of a unit is bridged
        through the boundary instead of being switched Off.
        """
        self._plan_inputs = dict(inputs)
        self._planner_error = None
        cars = self._requests(inputs)
        horizon = max((car.deadline for car in cars if not car.excluded), default=now + timedelta(hours=1))
        horizon = min(max(horizon, now + timedelta(hours=1)), now + timedelta(hours=cp.MAX_HORIZON_HOURS))
        # Slots run to the plan horizon or the end of the known prices, whichever is
        # later (the payload shows the known prices too); the planner ignores the rest.
        slots_end = horizon
        if self._known_until is not None and self._known_until > slots_end:
            slots_end = min(self._known_until, now + timedelta(hours=MAX_PAYLOAD_SLOTS))
        self._price_slots, self._power_slots = self._slots(now, slots_end)
        commit = None
        if commitment is not None and commitment[1] > now:
            commit = cp.Commitment(commitment[0], commitment[1])
        self._prices_expected_at = self._expected_prices(now, cars)
        config = cp.PlannerConfig(top_up_price=top_up_price, prices_expected_at=self._prices_expected_at)
        plan = None
        failed = False
        try:
            plan = cp.build_plan(now, self._price_slots, self._power_slots, cars, config, commit)
        except Exception as err:  # noqa: BLE001
            failed = True
            self._planner_error = f"planner_failed: {type(err).__name__}"
            self._log_exception("planner", "Intelligent charging planner failed; using the no-price plan", now)
            try:
                plan = cp.build_plan(now, self._price_slots, self._power_slots, cars,
                                     cp.PlannerConfig(no_price_mode=True), commit)
            except Exception:  # noqa: BLE001
                plan = None
                self._log_exception("planner_fallback", "Intelligent no-price fallback plan failed", now)
        self._plan = plan
        if self._decision is None and seed is not None:
            self._decision = seed
        decision = self._decide(now, plan, cars, inputs, failed)
        self._decision = decision
        shown = self._display_plan
        if not (self._bridged and plan is not None and shown is not None
                and cp.floor15(shown.now) == cp.floor15(now)):
            self._display_plan = plan
        return decision

    def extra_price(self) -> float | None:
        """The learned price limit for charging above the minimum, or None."""
        reference = self.model.cheap_price_reference(default_time_zone())
        return None if reference is None else float(reference.value)

    def _expected_prices(self, now: datetime, cars: Sequence[cp.CarRequest]) -> datetime | None:
        """When the first missing price should be published, or None when the plan must
        not wait for it.

        Only cars that still need their minimum count (a car that is done, reached or
        left out by policy never makes another car wait). If any of them has a fully
        priced window (its ready-by is not after the first missing hour), nothing
        waits: its cheapest hours may well be before the publication. Day-ahead lists
        run 01:00 -> 01:00 local and arrive by 15:00 the day before they start; when
        that time has passed the prices are late and the plan no longer waits.
        """
        tz = default_time_zone()
        deadlines = [cp._utc(car.deadline) for car in cars
                     if not (car.excluded or car.done or car.reached)
                     and (car.soc is None or car.soc < car.target_soc)]
        if not deadlines:
            return None
        horizon = max(deadlines)
        missing = [s.start for s in self._price_slots if s.kind != cp.PRICE_KNOWN and s.start < horizon]
        covered = max((s.end for s in self._price_slots), default=None)
        if covered is None or covered < horizon:
            missing.append(covered or now)
        if not missing:
            return None
        first = min(missing)
        if min(deadlines) <= first:
            return None
        day = (first - timedelta(hours=1)).astimezone(tz).date() - timedelta(days=1)
        expected = datetime.combine(day, PRICE_PUBLICATION_LOCAL, tzinfo=tz).astimezone(UTC)
        return expected if expected > now else None

    def _decide(self, now: datetime, plan: cp.Plan | None, cars: Sequence[cp.CarRequest],
                inputs: Mapping[str, PlanInput], failed: bool) -> IntelligentDecision:
        identifying = {outlet for outlet, item in inputs.items() if item.identifying}
        self._bridged = False
        if plan is None:
            outlet = emergency_outlet(now, cars)
            return IntelligentDecision(
                allowed=outlet is not None,
                reason=REASON_INTELLIGENT_NO_PRICE_DATA if outlet else (
                    REASON_INTELLIGENT_WAITING if cars else REASON_INTELLIGENT_NO_EV),
                outlet=outlet, block_start=floor_minute(now) if outlet else None,
                controller_error=ERROR_INTELLIGENT_PLANNER_FAILED,
            )
        outlet = plan.current_outlet
        reason = plan.reason
        previous = self._decision
        bridged = False
        if outlet is None and previous is not None and previous.allowed and previous.outlet is not None:
            # The tail of a unit is too short to plan (§7.1: < 1 min even for the
            # committed car). A car whose plan resumes within the next unit keeps
            # charging through the boundary instead of an Off/Smart restart every
            # 15 min; once it is drawing again the planner places that unit's
            # minutes at its start (a running block). A car still below its
            # minimum whose deadline falls inside this unit (which it cannot be
            # planned in) keeps charging until the deadline, where the roll makes
            # it a catch-up car or ends it, instead of an Off write ~1 min early.
            boundary = cp.floor15(now) + UNIT
            car = plan.car(previous.outlet)
            item = inputs.get(previous.outlet)
            if (car is not None and item is not None and not item.done and not item.excluded
                    and not item.unavailable):
                resumes = (boundary - now < timedelta(minutes=cp.MIN_START_MINUTES)
                           and bool(car.blocks) and car.blocks[0].start < boundary + UNIT)
                deadline_tail = (now < item.deadline <= boundary and car.status not in (
                    cp.STATUS_REACHED, cp.STATUS_DONE, cp.STATUS_EXCLUDED))
                if resumes or deadline_tail:
                    outlet, reason, bridged = previous.outlet, previous.reason, True
        self._bridged = bridged
        if outlet is not None and outlet in identifying and not bridged:
            car = plan.car(outlet)
            latest = car.latest_start if car is not None else None
            if latest is None or latest > now + UNIT:
                # §6.9: wait for the mapping unless a unit must start now.
                outlet, reason = None, REASON_INTELLIGENT_IDENTIFYING
        elif outlet is None and identifying and reason in (REASON_INTELLIGENT_WAITING, REASON_INTELLIGENT_NO_EV):
            reason = REASON_INTELLIGENT_IDENTIFYING
        if outlet is None and any(item.unavailable and not item.done and not item.excluded and not item.reached
                                  for item in inputs.values()):
            # A car that may need charge sits on a charger that is off the API.
            reason = REASON_INTELLIGENT_OUTLET_UNAVAILABLE
        if outlet is None and reason in INTELLIGENT_ALLOWED_REASONS:
            reason = REASON_INTELLIGENT_WAITING
        if outlet is None and reason == REASON_INTELLIGENT_TARGETS_REACHED:
            # "Minimum charge reached" only when every planned car really has it.
            short = [item for key, item in inputs.items()
                     if not item.excluded and (plan.car(key) is None or plan.car(key).status != cp.STATUS_REACHED)]
            if any(item.not_accepting for item in short):
                reason = REASON_INTELLIGENT_NOT_ACCEPTING
            elif short:
                reason = REASON_INTELLIGENT_CHARGING_COMPLETE
        if outlet is None and cars and all(car.excluded for car in cars):
            # The only connected car is kept out by a "SmartEVSE N only" policy.
            selected = next((key for key in OUTLETS if key not in inputs), None)
            if selected is not None:
                reason = REASON_ONLY_WAITING_FOR_SELECTED_EV.format(smartevse_key=selected)
        block_start = previous.block_start if bridged and previous is not None else None
        if outlet is not None and block_start is None:
            car = plan.car(outlet)
            for block in (car.blocks if car else ()):
                if block.start <= now + timedelta(seconds=1) and now < block.end:
                    block_start = floor_minute(block.start)
                    break
            block_start = block_start or floor_minute(now)
        error = None
        if failed:
            error = ERROR_INTELLIGENT_PLANNER_FAILED
        elif outlet is not None and self._facts.get(outlet) is not None and self._facts[outlet].car_timer:
            error = ERROR_VEHICLE_TIMER_BLOCKS_CHARGING.format(smartevse_key=outlet)
        elif (plan.no_price_mode and not self._in_startup_grace(now)
              and any(not car.excluded and not car.done for car in cars)):
            error = ERROR_INTELLIGENT_PRICE_DATA_UNAVAILABLE
        elif self.model_error:
            error = ERROR_INTELLIGENT_MODEL_UNAVAILABLE
        if not cars:
            reason = REASON_INTELLIGENT_NO_EV
        return IntelligentDecision(allowed=outlet is not None, reason=reason, outlet=outlet,
                                   block_start=block_start, controller_error=error)

    def _in_startup_grace(self, now: datetime) -> bool:
        return self._started_at is not None and now - self._started_at < STARTUP_GRACE

    def clear_plan(self) -> None:
        """Forget this cycle's plan (Intelligent disabled)."""
        self._plan = None
        self._display_plan = None
        self._bridged = False
        self._plan_inputs = {}
        self._decision = None
        self._planner_error = None

    # -- 4. payload ------------------------------------------------------------------------

    def build_payload(self, now: datetime, ctx: PayloadContext) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return (``intelligent`` payload, flat sensor values); stable between cycles.

        The payload is recomputed only when its inputs change (unit boundary,
        SoC, price, power, settings, status, decision); otherwise the previous
        payload is returned unchanged, so the card does not re-render every cycle.
        """
        tz = default_time_zone()
        static = self._static_sections(now, tz, ctx)
        key = self._display_signature(now, ctx, static)
        if key == self._display_key and self._display is not None:
            return self._display
        payload = self._payload(now, tz, ctx, static)
        flat = flat_values(payload)
        self._display_key = key
        self._display = (payload, flat)
        return self._display

    def _static_sections(self, now: datetime, tz: tzinfo, ctx: PayloadContext) -> dict[str, Any]:
        names = self._vehicle_names()
        limits = self._charger_limits()
        vehicles: dict[str, Any] = {}
        for vehicle in VEHICLES:
            settings = ctx.settings.get(vehicle) or {}
            ready_by = normalize_ready_by(settings.get("ready_by"))
            minimum = normalize_minimum(settings.get("minimum"))
            entry = {"name": names[vehicle], "ready_by": ready_by, "minimum": minimum,
                     "suggested_ready_by": None, "usually_leaves": None, "learned_sessions": 0,
                     "model": {"kwh_per_pct": None, "kwh_per_pct_source": "default", "cap_kw": None}}
            try:
                suggestion = self.model.departure_suggestion(vehicle, ready_by)
                per_pct = self.model.kwh_per_pct(vehicle, self._capacities.get(vehicle))
                power = self.model.car_power(vehicle, limits["max_current"])
                entry.update({
                    "suggested_ready_by": suggestion["suggested_ready_by"] if suggestion else None,
                    "usually_leaves": suggestion["usually_leaves"] if suggestion else None,
                    "learned_sessions": int(self.model.learned_sessions(vehicle)),
                    "model": {"kwh_per_pct": round(per_pct.expected, 3), "kwh_per_pct_source": per_pct.source,
                              "cap_kw": round(power.cap_kw, 1)},
                })
            except Exception:  # noqa: BLE001
                self._log_exception("vehicles", "Unable to summarise the Intelligent vehicle model", now)
            vehicles[vehicle] = entry
        other = ctx.settings.get("other") or {}
        vehicles["other"] = {"name": OTHER_VEHICLE_NAME, "ready_by": normalize_ready_by(other.get("ready_by")),
                             "minimum": normalize_minimum(other.get("minimum"))}
        try:
            household = self.model.household_summary(now, tz, **limits)
            household = {"learned_days": int(household.get("learned_days") or 0),
                         "night_expected_kw": household.get("night_expected_kw")}
        except Exception:  # noqa: BLE001
            household = {"learned_days": 0, "night_expected_kw": None}
        try:
            entities = self.entities()
        except Exception:  # noqa: BLE001
            entities = build_entities(lambda _domain, _key: None)
        return {"vehicles": vehicles, "household": household, "entities": entities}

    def _display_signature(self, now: datetime, ctx: PayloadContext, static: Mapping[str, Any]) -> str:
        plan = self._display_plan
        outlets = {}
        for outlet in OUTLETS:
            facts = self._facts.get(outlet)
            item = ctx.outlets.get(outlet) or OutletPayloadInput()
            car = plan.car(outlet) if plan is not None else None
            soc = None
            if facts is not None and facts.soc is not None:
                soc = round(facts.soc) if facts.soc_source == "estimated" else round(facts.soc, 1)
            plan_input = self._plan_inputs.get(outlet)
            outlets[outlet] = None if facts is None else [
                facts.available, facts.charging, facts.vehicle, facts.settings_key, soc, facts.soc_source,
                facts.target, facts.car_limit, facts.minimum, facts.ready_by, facts.car_timer,
                iso_minute(item.deadline), item.overrun, item.not_accepting, item.identifying, item.excluded,
                car.status if car else None, car.feasible if car else None,
                # Every planner input the payload depends on (a charge-policy change
                # moves the order and blocks without changing anything above).
                None if plan_input is None else [plan_input.priority, plan_input.drawing, plan_input.done,
                                                 plan_input.reached, plan_input.unavailable,
                                                 plan_input.excluded, plan_input.overrun],
            ]
        data = {
            "unit": cp.floor15(now).isoformat(),
            "ctx": [ctx.enabled, ctx.overridden, ctx.governing, ctx.charge_allowed, ctx.controller_state,
                    ctx.charge_reason, ctx.active_outlet, iso_minute(ctx.active_since),
                    ctx.top_up_enabled, ctx.top_up_price],
            "settings": {key: dict(value) for key, value in ctx.settings.items()},
            "outlets": outlets,
            "decision": [plan.current_outlet, plan.reason, plan.no_price_mode, list(plan.order)]
            if plan is not None else None,
            # Realised windows: opening or closing one (e.g. at the deadline roll) and
            # their energy/saving change what the payload shows.
            "realised": self._realised_digest(now),
            "prices": [[s.start.isoformat(), s.price, s.kind] for s in self._price_slots] if plan else None,
            "power": [[s.start.isoformat(), s.expected_kw, s.conservative_kw] for s in self._power_slots]
            if plan else None,
            "errors": [self.model_error, self._planner_error, self._currency],
            "static": static,
        }
        return json.dumps(data, sort_keys=True, default=str)

    def _realised_digest(self, now: datetime) -> list[Any]:
        try:
            return self.model.realised_digest()
        except Exception:  # noqa: BLE001 - the payload build logs realised problems
            return []

    def _payload(self, now: datetime, tz: tzinfo, ctx: PayloadContext, static: Mapping[str, Any]) -> dict[str, Any]:
        plan = self._display_plan if ctx.enabled else None
        connected = [outlet for outlet in OUTLETS if outlet in self._facts]
        payload = empty_payload(enabled=ctx.enabled, overridden=ctx.overridden, settings=ctx.settings,
                                vehicle_names=self._vehicle_names(), top_up_enabled=ctx.top_up_enabled,
                                top_up_price=ctx.top_up_price, entities=static["entities"],
                                currency=self._currency)
        payload.update({
            "connected_count": len(connected),
            "known_until": iso_minute(self._known_until) if plan is not None else None,
            "prices_expected_at": iso_minute(self._prices_expected_at) if plan is not None else None,
            "model_error": self.model_error,
            "planner_error": self._planner_error if ctx.enabled else None,
            "vehicles": static["vehicles"],
            "household": static["household"],
        })
        realised = self._realised(tz, now)
        payload["sessions"] = [
            {
                "ledger_id": record.get("ledger_id"),
                "outlet": record.get("outlet"),
                "vehicle_name": record.get("vehicle_name"),
                "closed": bool(record.get("closed")),
                "energy_kwh": record.get("energy_kwh"),
                "cost": money(record.get("cost")),
                "baseline_cost": money(record.get("baseline_cost")),
                "saving": money(record.get("saving")),
                "mixed": bool(record.get("mixed")),
                "partial": bool(record.get("partial")),
            }
            for record in realised
            # A window without energy, open or closed (the one a deadline roll opens
            # for a car that stays plugged in until it leaves), says nothing and
            # would shadow the charging window of the same ledger id in the card.
            if (finite(record.get("energy_kwh")) or 0.0) >= MIN_SESSION_WINDOW_KWH
        ][:MAX_PAYLOAD_SESSIONS]
        if not ctx.enabled:
            return payload
        if ctx.governing:
            payload["reason"] = ctx.charge_reason
        if plan is None and not self._facts:
            payload["status"] = INTELLIGENT_STATUS_OVERRIDDEN if ctx.overridden else INTELLIGENT_STATUS_NO_EV
            return payload

        windows = {}
        for record in realised:
            if not record.get("closed") and record.get("outlet") not in windows:
                windows[record.get("outlet")] = record
        savings = self._planned_savings(now, tz, plan, windows, ctx)
        chargers: dict[str, Any] = {}
        order = list(plan.order) if plan is not None else []
        next_starts = []
        totals_cost, totals_saving, saving_ok, totals_ok = [], [], True, True
        energy = 0.0
        for outlet in OUTLETS:
            facts = self._facts.get(outlet)
            if facts is None:
                chargers[outlet] = None
                continue
            item = ctx.outlets.get(outlet) or OutletPayloadInput()
            car = plan.car(outlet) if plan is not None else None
            entry = self._charger_entry(now, outlet, facts, car, item, ctx, order, savings.get(outlet))
            chargers[outlet] = entry
            if entry["next_start"]:
                next_starts.append(entry["next_start"])
            plan_input = self._plan_inputs.get(outlet)
            if plan_input is not None and plan_input.unavailable:
                # Its plan is unknown until the charger answers: no partial totals.
                if not plan_input.done and not plan_input.reached:
                    saving_ok = totals_ok = False
                continue
            if car is not None and not item.excluded and not item.identifying:
                energy += (car.energy_kwh or 0.0) + (car.top_up_kwh or 0.0)
                if entry["session_cost"] is not None:
                    totals_cost.append(entry["session_cost"])
                has_energy = (car.energy_kwh or 0.0) > 0 or (savings.get(outlet) or {}).get("delivered", 0) > 0
                if has_energy:
                    if entry["saving"] is None:
                        saving_ok = False
                    else:
                        totals_saving.append(entry["saving"])
        payload["chargers"] = chargers
        payload["order"] = order
        payload["next_charge"] = min(next_starts) if next_starts else None
        payload["totals"] = {
            "cost": money(sum(totals_cost)) if totals_cost and totals_ok else None,
            "saving": money(sum(totals_saving)) if totals_saving and saving_ok else None,
            "energy_kwh": round(energy, 2),
            "includes_forecast": bool(plan.includes_forecast) if plan is not None else False,
            "partial": bool(plan.partial) if plan is not None else False,
        }
        payload["slots"] = self._payload_slots(now, plan)
        payload["forecast_used"] = any(slot["kind"] == cp.PRICE_FORECAST for slot in payload["slots"])
        payload["status"] = self._overall_status(ctx, plan, chargers)
        return payload

    def _realised(self, tz: tzinfo, now: datetime) -> list[dict[str, Any]]:
        try:
            # Twice the published count: empty open windows are skipped in the payload.
            return list(self.model.realised_sessions(tz, limit=2 * MAX_PAYLOAD_SESSIONS))
        except Exception:  # noqa: BLE001
            self._log_exception("realised", "Unable to summarise realised Intelligent savings", now)
            return []

    def _charger_entry(self, now: datetime, outlet: str, facts: CarFacts, car: cp.CarPlan | None,
                       item: OutletPayloadInput, ctx: PayloadContext, order: list[str],
                       saving: Mapping[str, Any] | None) -> dict[str, Any]:
        if item.identifying:
            status = INTELLIGENT_STATUS_IDENTIFYING
        elif item.not_accepting:
            status = INTELLIGENT_STATUS_NOT_ACCEPTING
        elif car is not None:
            status = car.status
        else:
            status = cp.STATUS_PLANNED
        active = ctx.governing and ctx.charge_allowed and ctx.active_outlet == outlet
        if status == cp.STATUS_CHARGING and not active:
            status = cp.STATUS_PLANNED
        elif active and self._bridged and status in (cp.STATUS_PLANNED, cp.STATUS_TIGHT):
            # Kept charging through a unit tail the planner cannot use (see _decide).
            status = cp.STATUS_CHARGING
        plan_input = self._plan_inputs.get(outlet)
        unavailable = plan_input is not None and plan_input.unavailable
        if unavailable and status == cp.STATUS_DONE:
            # Held out only while its charger is off the API; planned again when it answers.
            status = cp.STATUS_PLANNED
        blocks = []
        for index, block in enumerate(car.blocks if car is not None else ()):
            start = block.start
            if (index == 0 and active and ctx.active_since is not None and start <= now
                    and ctx.active_since <= start):
                start = ctx.active_since  # the running block started at the Smart write
            blocks.append({"start": iso_minute(start), "end": iso_minute(block.end), "top_up": bool(block.top_up)})
        # A car whose charger is off the API has no plan to price until it answers
        # (its done-car numbers would read as a finished, cheaper charge).
        hide_money = item.identifying or item.excluded or unavailable
        session_cost = None if hide_money or saving is None else saving.get("session_cost")
        position = order.index(outlet) if outlet in order else -1
        return {
            "vehicle": facts.vehicle,
            "vehicle_name": self._vehicle_names().get(facts.vehicle) if facts.vehicle else None,
            "settings_key": facts.settings_key,
            "soc": None if facts.soc is None else round(facts.soc, 1),
            "soc_source": facts.soc_source,
            "minimum": int(facts.minimum),
            "target": round(float(facts.target), 1),
            "car_limit": facts.car_limit,
            "limited_by_car": bool(facts.limited_by_car),
            "ready_by": facts.ready_by,
            "deadline": iso_minute(item.deadline),
            "status": status,
            "feasible": bool(car.feasible) if car is not None else False,
            "estimated": bool(car.estimated) if car is not None else facts.soc is None,
            "projected_soc": car.projected_soc if car is not None and not unavailable else None,
            "need_kwh": round(float(car.need_kwh), 1) if car is not None else 0.0,
            "blocks": blocks,
            "next_start": blocks[0]["start"] if blocks else None,
            "session_cost": session_cost,
            "saving": None if hide_money or saving is None else saving.get("saving"),
            "includes_forecast": bool(car.includes_forecast) if car is not None else False,
            "partial": bool(car.partial) if car is not None else False,
            "overrun": bool(item.overrun),
            "not_accepting": bool(item.not_accepting),
            "car_timer": bool(facts.car_timer),
            "queue_after": order[position - 1] if position > 0 else None,
            "top_up": bool(car.top_up) if car is not None else False,
            "top_up_cost": None if hide_money or car is None else car.top_up_cost,
            # Level the plan reaches with the extra charging in cheap hours (None when
            # nothing extra is planned or the battery level is unknown).
            "extra_soc": self._extra_soc(facts, car) if not unavailable else None,
        }

    @staticmethod
    def _extra_soc(facts: CarFacts, car: cp.CarPlan | None) -> float | None:
        if car is None or not car.top_up or facts.soc is None or not facts.kwh_per_pct:
            return None
        base = max(float(facts.soc), float(car.projected_soc) if car.projected_soc is not None else float(facts.soc))
        limit = float(facts.car_limit) if facts.car_limit is not None else 100.0
        return round(min(limit, base + float(car.top_up_kwh or 0.0) / float(facts.kwh_per_pct)), 1)

    def _planned_savings(self, now: datetime, tz: tzinfo, plan: cp.Plan | None,
                         windows: Mapping[str, Mapping[str, Any]], ctx: PayloadContext) -> dict[str, dict[str, Any]]:
        """§7.8 planned saving per car vs charging at plug-in on one charger.

        The baseline starts where the open realised window's baseline starts (its
        plug-in, or the deadline roll that opened it), so the planned and the
        realised figures describe the same comparison. Top-up is excluded from
        both sides: the baseline energy is the Intelligent minimum energy delivered
        in the window plus the remaining planned minimum, and it is compared with
        the Intelligent (non-top-up) cost so far plus the remaining planned cost.
        """
        if plan is None:
            return {}
        result: dict[str, dict[str, Any]] = {}
        sessions = []
        for outlet in OUTLETS:
            car = plan.car(outlet)
            facts = self._facts.get(outlet)
            item = ctx.outlets.get(outlet) or OutletPayloadInput()
            if car is None or facts is None or item.excluded or item.identifying:
                continue
            window = dict(windows.get(outlet) or {})
            window_deadline = dt_util.parse_datetime(window["deadline"]) if window.get("deadline") else None
            rolled = False
            if window and item.deadline is not None and window_deadline is not None \
                    and dt_util.as_utc(window_deadline) != dt_util.as_utc(item.deadline):
                # The window of the previous deadline is closed in this cycle (the
                # deadline rolled before the model saw it): nothing of it belongs here,
                # and the next window's baseline starts at the roll (now).
                window, rolled = {}, True
            if "intelligent_kwh" in window:
                delivered = max(0.0, float(window.get("intelligent_kwh") or 0.0))
                intelligent_so_far = float(window.get("intelligent_cost") or 0.0)
            else:
                delivered = max(0.0, float(window.get("energy_kwh") or 0.0) - float(window.get("top_up_kwh") or 0.0))
                intelligent_so_far = float(window.get("cost") or 0.0)
            cost_so_far = float(window.get("cost") or 0.0)
            if facts.soc is not None and facts.soc_source in ("vehicle", "last_known") and window:
                # The plan starts from the battery reading, which does not include the
                # energy metered since it: count that energy once (in the plan), so
                # the totals do not depend on when the payload is rebuilt.
                since = min(self._energy_since_reading(outlet), delivered)
                price = self._price_now(now)
                if since > 0 and price is not None:
                    delivered -= since
                    cost_so_far -= since * price
                    intelligent_so_far -= since * price
            remaining = (car.cost or 0.0) + (car.top_up_cost or 0.0)
            session_cost = money(cost_so_far + remaining) if car.cost is not None else None
            planned = (car.energy_kwh or 0.0) + (car.top_up_kwh or 0.0)
            if float(window.get("energy_kwh") or 0.0) < MIN_SESSION_WINDOW_KWH and planned <= 0.0:
                # Nothing charged in this deadline window and nothing planned (e.g. a
                # reached car after its ready-by roll): no cost, rather than "≈ €0.00".
                session_cost = None
            anchor = dt_util.parse_datetime(window["baseline_start"]) if window.get("baseline_start") else None
            if rolled:
                plugged = now
            elif anchor is not None:
                plugged = dt_util.as_utc(anchor)
            else:
                plugged = self._inputs[outlet].plugged_at if outlet in self._inputs else None
                plugged = plugged or now
                if item.deadline is not None:
                    # A car left plugged over several nights is compared per deadline
                    # window; a car plugged in within the late plug-in hour before its
                    # ready-by keeps its own plug-in time.
                    plugged = max(plugged, item.deadline - cm.REALISED_BASELINE_LOOKBACK)
            plugged = min(plugged, now)
            result[outlet] = {"session_cost": session_cost, "saving": None, "delivered": delivered,
                              "cost_so_far": intelligent_so_far, "window": window, "planned": planned}
            sessions.append(cp.BaselineSession(outlet=outlet, plugged_at=plugged,
                                               energy_kwh=delivered + (car.energy_kwh or 0.0),
                                               cap_kw=facts.cap_kw))
        if not sessions:
            return result
        try:
            start = cm.local_hour_start(min(session.plugged_at for session in sessions), tz)
            end = start + timedelta(hours=cp.MAX_HORIZON_HOURS)
            prices = [cp.PriceSlot(s["start"], s["end"], s["price"], s["kind"])
                      for s in self.model.price_slots(start, end, tz, entries=self._price_entries)]
            powers = [cp.PowerSlot(s["start"], s["end"], s["expected_kw"], s["conservative_kw"])
                      for s in self.model.power_slots(start, end, tz, **self._charger_limits())]
            baseline = cp.baseline_costs(sessions, prices, powers)
        except Exception:  # noqa: BLE001 - a baseline failure only hides the saving
            self._log_exception("baseline", "Unable to compute the Intelligent planned saving", now)
            return result
        for outlet, entry in result.items():
            car = plan.car(outlet)
            window = entry["window"]
            if entry["planned"] <= 0.0 and window.get("saving") is not None:
                # Nothing left to plan (reached/done): the window's own provisional
                # realised saving is the figure, the same one History shows.
                entry["saving"] = money(window.get("saving"))
                continue
            cost = baseline.get(outlet)
            if (cost is None or car.cost is None or entry["session_cost"] is None
                    or car.partial or car.includes_forecast
                    or window.get("mixed") or window.get("partial")):
                continue
            entry["saving"] = money(cost - (entry["cost_so_far"] + car.cost))
        return result

    def _energy_since_reading(self, outlet: str) -> float:
        try:
            return max(0.0, float(self.model.energy_since_reading(outlet)))
        except Exception:  # noqa: BLE001
            return 0.0

    def _price_now(self, now: datetime) -> float | None:
        for slot in self._price_slots:
            if slot.start <= now < slot.end and slot.kind == cp.PRICE_KNOWN:
                return slot.price
        return None

    def _payload_slots(self, now: datetime, plan: cp.Plan | None) -> list[dict[str, Any]]:
        if plan is None:
            return []
        deadlines = [item.deadline for item in self._plan_inputs.values() if not item.excluded]
        end = max([*deadlines, *([plan.known_until] if plan.known_until else [])], default=now)
        slots = []
        for price in self._price_slots:
            if price.end <= now or price.start >= end:
                continue
            power = next((p for p in self._power_slots if p.start <= price.start < p.end), None)
            slots.append({
                "start": iso_minute(price.start),
                "end": iso_minute(price.end),
                "price": None if price.price is None else round(price.price, 4),
                "kind": price.kind,
                "smartevse_1_minutes": int(round(plan.minutes_between("smartevse_1", price.start, price.end))),
                "smartevse_2_minutes": int(round(plan.minutes_between("smartevse_2", price.start, price.end))),
                "expected_kw": None if power is None else round(power.expected_kw, 1),
            })
            if len(slots) >= MAX_PAYLOAD_SLOTS:
                break
        return slots

    def _overall_status(self, ctx: PayloadContext, plan: cp.Plan | None, chargers: Mapping[str, Any]) -> str:
        if not ctx.enabled:
            return INTELLIGENT_STATUS_DISABLED
        if ctx.overridden:
            return INTELLIGENT_STATUS_OVERRIDDEN
        entries = [entry for entry in chargers.values() if entry is not None]
        if not entries or all(entry["status"] == cp.STATUS_EXCLUDED for entry in entries):
            # No car Intelligent may charge (only one kept out by charge policy).
            return INTELLIGENT_STATUS_NO_EV
        statuses = {entry["status"] for entry in entries}
        if statuses & {cp.STATUS_AT_RISK, cp.STATUS_CATCH_UP}:
            return INTELLIGENT_STATUS_AT_RISK
        if INTELLIGENT_STATUS_NOT_ACCEPTING in statuses:
            return INTELLIGENT_STATUS_NOT_ACCEPTING
        if plan is not None and plan.no_price_mode:
            return INTELLIGENT_STATUS_NO_PRICE_DATA
        if ctx.governing and ctx.charge_allowed and ctx.charge_reason in INTELLIGENT_ALLOWED_REASONS:
            return INTELLIGENT_STATUS_CHARGING
        if INTELLIGENT_STATUS_IDENTIFYING in statuses:
            return INTELLIGENT_STATUS_IDENTIFYING
        if statuses & {cp.STATUS_PLANNED, cp.STATUS_TIGHT, cp.STATUS_CHARGING} or any(
                entry["blocks"] for entry in entries):
            return INTELLIGENT_STATUS_PLANNED
        return INTELLIGENT_STATUS_REACHED


__all__ = [
    "CarFacts", "IntelligentCharging", "IntelligentDecision", "ObserveResult", "OutletInput",
    "OutletPayloadInput", "PayloadContext", "PlanInput", "build_entities", "emergency_outlet",
    "empty_payload", "flat_values", "iso_minute", "merge_price_lists", "money",
    "next_local_occurrence", "normalize_minimum", "normalize_ready_by", "parse_ready_by",
    "registry_entities", "session_deadline",
]
