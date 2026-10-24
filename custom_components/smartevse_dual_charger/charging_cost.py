"""Persistent, observational charging ledger. Never sends device commands.

A shared meter cannot provide per-outlet billing. Integrate its measured power
only while exactly one charger reports Charging, at the observed tariff. Values
are estimates; unobserved time and missing prices are explicitly retained.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math
from uuid import uuid4

KEYS = ("smartevse_1", "smartevse_2")
MAX_GAP_SECONDS = 120
HISTORY_LIMIT = 100  # Per outlet, including the current session.


def finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def power_kw(state):
    """Require a supported meter unit; unknown is never interpreted as zero."""
    if state is None or (value := finite(state.state)) is None or value < 0:
        return None
    factor = {"W": 0.001, "kW": 1}.get(state.attributes.get("unit_of_measurement"))
    return value * factor if factor is not None else None


def tariff(state):
    """Normalize full Real Electricity Price (including its configured fees)."""
    if state is None or (value := finite(state.state)) is None:
        return None, None
    unit = str(state.attributes.get("unit_of_measurement", "")).replace(" ", "")
    numerator, _, denominator = unit.partition("/")
    currency = {"€": "EUR", "EUR": "EUR", "USD": "USD", "GBP": "GBP", "£": "GBP", "SEK": "SEK", "NOK": "NOK", "DKK": "DKK"}.get(numerator)
    factor = {"kWh": 1, "MWh": .001, "Wh": 1000}.get(denominator)
    if currency is None or factor is None:
        return None, None
    return value * factor, currency


class ChargingLedger:
    """One record per plug-in session; pauses and price gates do not split it."""

    def __init__(self, stored=None):
        if stored is not None and (not isinstance(stored, dict) or stored.get("version") != 1
                                   or not isinstance(stored.get("records"), list)
                                   or not isinstance(stored.get("active"), dict)):
            raise ValueError("Unsupported or invalid charging ledger; do not overwrite")
        self.data = deepcopy(stored) if stored is not None else {
            "version": 1, "records": [], "active": {}, "sample": None,
        }
        # Persisted samples are checkpoints, not evidence of activity while HA was off.
        self.restored = self.data.get("sample") is not None

    def _record(self, ident):
        return next((r for r in self.data["records"] if r["id"] == ident), None)

    @staticmethod
    def _issue(record, issue):
        if issue not in record["issues"]:
            record["issues"].append(issue)

    def observe(self, now: datetime, statuses: dict, power: float | None,
                price: float | None, currency: str | None, sources: list[str]):
        """Close the preceding measured interval, then retain the new sample."""
        timestamp = now.isoformat()
        previous = self.data.get("sample")
        if previous:
            seconds = (now - datetime.fromisoformat(previous["at"])).total_seconds()
            records = [r for ident in previous["sessions"] if (r := self._record(ident))]
            if seconds > 0:
                gap = self.restored or seconds > MAX_GAP_SECONDS or previous.get("sources") != sources
                for record in records:
                    if gap:
                        record["unobserved_seconds"] += seconds
                        self._issue(record, "restart_or_telemetry_gap")
                    elif not all(previous["available"].values()):
                        record["unobserved_seconds"] += seconds
                        self._issue(record, "charger_unavailable")
                    elif len(records) != 1 or not previous["exclusive"]:
                        record["unobserved_seconds"] += seconds
                        self._issue(record, "shared_meter_ambiguous")
                    elif previous["power_kw"] is None:
                        record["unobserved_seconds"] += seconds
                        self._issue(record, "power_unavailable")
                    else:
                        energy = previous["power_kw"] * seconds / 3600
                        record["energy_kwh"] += energy
                        if previous["price"] is None or previous["currency"] is None:
                            record["unpriced_kwh"] += energy
                            if energy: self._issue(record, "price_unavailable")
                        elif record["currency"] not in (None, previous["currency"]):
                            record["unpriced_kwh"] += energy
                            if energy: self._issue(record, "currency_changed")
                        else:
                            record["currency"] = previous["currency"]
                            record["cost"] += energy * previous["price"]
                            record["priced_kwh"] += energy
                if not records and gap:
                    # Keep uncertainty on any retained paused session too.
                    for ident in self.data["active"].values():
                        if (record := self._record(ident)) is not None and record["status"] != "complete":
                            record["unobserved_seconds"] += seconds
                            self._issue(record, "restart_or_telemetry_gap")
            elif seconds < 0:
                for record in records: self._issue(record, "clock_changed")
                return  # Retain the later checkpoint; never count the same time twice.
        self.restored = False
        charging = []
        for key in KEYS:
            status = statuses[key]
            record = self._record(self.data["active"].get(key))
            if not status["available"]:
                if record:
                    record["status"] = "unavailable"
                    charging.append(record["id"])
                continue
            if not status["connected"]:
                if record:
                    record["ended_at"] = record["ended_at"] or timestamp
                    record["status"] = "finished"
                    self.data["active"].pop(key, None)
                continue
            if status["charging"]:
                if record is None:
                    record = {"id": uuid4().hex, "charger": key, "vehicle": status.get("vehicle"),
                              "started_at": timestamp, "ended_at": None, "status": "charging",
                              "cost": 0.0, "currency": currency, "energy_kwh": 0.0,
                              "priced_kwh": 0.0, "unpriced_kwh": 0.0,
                              "unobserved_seconds": 0.0, "issues": [], "method": "power_integration"}
                    self.data["records"].append(record)
                    self.data["active"][key] = record["id"]
                    if previous is None or not previous.get("available", {}).get(key, False):
                        self._issue(record, "start_not_observed")
                record["status"] = "charging"
                record["ended_at"] = None
                charging.append(record["id"])
            elif record:
                record["status"] = "complete" if status.get("complete") else "paused"
                if status.get("complete") and not record["ended_at"]:
                    record["ended_at"] = timestamp
            if record and status.get("vehicle") not in (None, "unknown"):
                record["vehicle"] = status["vehicle"]
        self.data["sample"] = {"at": timestamp, "sessions": charging,
            "exclusive": len(charging) == 1 and all(s["available"] for s in statuses.values()),
            "available": {k:s["available"] for k,s in statuses.items()},
            "power_kw": power, "price": price, "currency": currency, "sources": sources}
        keep = {r["id"] for key in KEYS for r in [r for r in self.data["records"] if r["charger"] == key][-HISTORY_LIMIT:]}
        self.data["records"] = [r for r in self.data["records"] if r["id"] in keep]

    def snapshot(self):
        """Detached, compact values for all clients; amounts are never bill claims."""
        result = {"version": 1, "method": "power_integration", "history_limit": HISTORY_LIMIT, "chargers": {}}
        for key in KEYS:
            records = deepcopy([r for r in reversed(self.data["records"]) if r["charger"] == key])
            for r in records:
                for name in ["cost", "energy_kwh", "priced_kwh", "unpriced_kwh"]:
                    r[name] = round(r[name], 6)
                r["partial"] = bool(r["issues"])
            result["chargers"][key] = {"session": records[0] if records else None, "history": records}
        return result
