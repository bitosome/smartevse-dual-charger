"""Naming helpers for SmartEVSE Dual Charger."""

from __future__ import annotations

from homeassistant.helpers import selector

from .const import (
    CONF_VEHICLE_1_NAME,
    CONF_VEHICLE_2_NAME,
    DEFAULT_VEHICLE_1_NAME,
    DEFAULT_VEHICLE_2_NAME,
    SMARTEVSE_1_NAME,
    SMARTEVSE_2_NAME,
    ChargePolicy,
)

SMARTEVSE_NAMES: dict[str, str] = {
    "smartevse_1": SMARTEVSE_1_NAME,
    "smartevse_2": SMARTEVSE_2_NAME,
}


def normalize_vehicle_name(value: str | None, fallback: str) -> str:
    """Return a clean configured vehicle name."""
    normalized = (value or "").strip()
    return normalized or fallback


def configured_vehicle_names(values: dict[str, object]) -> tuple[str, str]:
    """Return the configured known-vehicle names from entry data/options."""
    return (
        normalize_vehicle_name(values.get(CONF_VEHICLE_1_NAME), DEFAULT_VEHICLE_1_NAME),
        normalize_vehicle_name(values.get(CONF_VEHICLE_2_NAME), DEFAULT_VEHICLE_2_NAME),
    )


def configured_vehicle_name(values: dict[str, object], vehicle_key: str) -> str:
    """Return the configured display name for one known vehicle."""
    vehicle_1_name, vehicle_2_name = configured_vehicle_names(values)
    return vehicle_1_name if vehicle_key == "vehicle_1" else vehicle_2_name


def smartevse_name(smartevse_key: str) -> str:
    """Return the fixed display name for one SmartEVSE."""
    return SMARTEVSE_NAMES.get(smartevse_key, "SmartEVSE")


def charge_policy_label(policy: str) -> str:
    """Return the user-facing label for a charge policy."""
    return {
        ChargePolicy.SMARTEVSE_1_FIRST.value: "SmartEVSE 1 first",
        ChargePolicy.SMARTEVSE_2_FIRST.value: "SmartEVSE 2 first",
        ChargePolicy.SMARTEVSE_1_ONLY.value: "SmartEVSE 1 only",
        ChargePolicy.SMARTEVSE_2_ONLY.value: "SmartEVSE 2 only",
    }[policy]


def charge_policy_labels() -> dict[str, str]:
    """Return a full internal->label mapping for charge policy values."""
    return {
        policy.value: charge_policy_label(policy.value)
        for policy in ChargePolicy
    }


def charge_policy_select_options() -> list[selector.SelectOptionDict]:
    """Return options for a config-flow select."""
    return [
        selector.SelectOptionDict(value=policy.value, label=charge_policy_label(policy.value))
        for policy in ChargePolicy
    ]


def active_smartevse_label(active_smartevse: str | None) -> str:
    """Return the current active SmartEVSE as a friendly label."""
    if active_smartevse == "smartevse_1":
        return SMARTEVSE_1_NAME
    if active_smartevse == "smartevse_2":
        return SMARTEVSE_2_NAME
    return "None"
