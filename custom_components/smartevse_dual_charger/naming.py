"""Naming helpers for SmartEVSE Dual Charger."""

from __future__ import annotations

from homeassistant.helpers import selector

from .const import (
    CONF_SMARTEVSE_1_NAME,
    CONF_SMARTEVSE_2_NAME,
    DEFAULT_SMARTEVSE_1_NAME,
    DEFAULT_SMARTEVSE_2_NAME,
    ChargePolicy,
)


def normalize_smartevse_name(value: str | None, fallback: str) -> str:
    """Return a clean user-facing SmartEVSE alias."""
    normalized = (value or "").strip()
    return normalized or fallback


def configured_smartevse_names(values: dict[str, object]) -> tuple[str, str]:
    """Return the configured SmartEVSE aliases from entry data/options."""
    return (
        normalize_smartevse_name(values.get(CONF_SMARTEVSE_1_NAME), DEFAULT_SMARTEVSE_1_NAME),
        normalize_smartevse_name(values.get(CONF_SMARTEVSE_2_NAME), DEFAULT_SMARTEVSE_2_NAME),
    )


def configured_smartevse_name(values: dict[str, object], smartevse_key: str) -> str:
    """Return the alias for one SmartEVSE."""
    smartevse_1_name, smartevse_2_name = configured_smartevse_names(values)
    return smartevse_1_name if smartevse_key == "smartevse_1" else smartevse_2_name


def charge_policy_label(policy: str, smartevse_1_name: str, smartevse_2_name: str) -> str:
    """Return the user-facing label for a charge policy."""
    return {
        ChargePolicy.SMARTEVSE_1_FIRST.value: f"{smartevse_1_name} first",
        ChargePolicy.SMARTEVSE_2_FIRST.value: f"{smartevse_2_name} first",
        ChargePolicy.SMARTEVSE_1_ONLY.value: f"{smartevse_1_name} only",
        ChargePolicy.SMARTEVSE_2_ONLY.value: f"{smartevse_2_name} only",
    }[policy]


def charge_policy_labels(smartevse_1_name: str, smartevse_2_name: str) -> dict[str, str]:
    """Return a full internal->label mapping for charge policy values."""
    return {
        policy.value: charge_policy_label(policy.value, smartevse_1_name, smartevse_2_name)
        for policy in ChargePolicy
    }


def charge_policy_select_options(smartevse_1_name: str, smartevse_2_name: str) -> list[selector.SelectOptionDict]:
    """Return options for a config-flow select with dynamic labels."""
    return [
        selector.SelectOptionDict(value=policy.value, label=charge_policy_label(policy.value, smartevse_1_name, smartevse_2_name))
        for policy in ChargePolicy
    ]


def active_smartevse_label(active_smartevse: str | None, smartevse_1_name: str, smartevse_2_name: str) -> str:
    """Return the current active SmartEVSE as a friendly label."""
    if active_smartevse == "smartevse_1":
        return smartevse_1_name
    if active_smartevse == "smartevse_2":
        return smartevse_2_name
    return "None"
