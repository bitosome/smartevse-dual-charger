"""Config flow for SmartEVSE Dual Charger."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ACTIVE_MODE,
    CONF_CURRENTS_PUSH_INTERVAL,
    CONF_EVSE_1_BASE_URL,
    CONF_EVSE_1_ERROR_ENTITY,
    CONF_EVSE_1_MODE_ENTITY,
    CONF_EVSE_1_OVERRIDE_ENTITY,
    CONF_EVSE_1_PLUG_ENTITY,
    CONF_EVSE_1_STATE_ENTITY,
    CONF_EVSE_2_BASE_URL,
    CONF_EVSE_2_ERROR_ENTITY,
    CONF_EVSE_2_MODE_ENTITY,
    CONF_EVSE_2_OVERRIDE_ENTITY,
    CONF_EVSE_2_PLUG_ENTITY,
    CONF_EVSE_2_STATE_ENTITY,
    CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY,
    CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY,
    CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY,
    CONF_EV_METER_L1_ENTITY,
    CONF_EV_METER_L2_ENTITY,
    CONF_EV_METER_L3_ENTITY,
    CONF_EV_METER_PUSH_INTERVAL,
    CONF_LOW_BUDGET_POLICY_DEFAULT,
    CONF_MAINS_L1_ENTITY,
    CONF_MAINS_L2_ENTITY,
    CONF_MAINS_L3_ENTITY,
    CONF_NOTIFY_ON_SCHEDULE_WINDOW,
    CONF_OVERRIDE_DEADBAND,
    CONF_PRICE_SENSOR_ENTITY,
    CONF_PUSH_CURRENTS,
    CONF_PUSH_EV_METER,
    CONF_PUSH_WLED,
    CONF_SCHEDULE_ENTITY,
    CONF_TOTAL_CURRENT_LIMIT,
    CONF_UPDATE_INTERVAL,
    CONF_WLED_URL,
    DEFAULT_ACTIVE_MODE,
    DEFAULT_LOW_BUDGET_POLICY,
    DEFAULT_NAME,
    DEFAULT_NOTIFY_ON_SCHEDULE_WINDOW,
    DEFAULT_OVERRIDE_DEADBAND,
    DEFAULT_PUSH_CURRENTS,
    DEFAULT_PUSH_EV_METER,
    DEFAULT_PUSH_WLED,
    DEFAULT_TOTAL_CURRENT_LIMIT,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    LowBudgetPolicy,
)

ACTIVE_MODE_OPTIONS = ["Normal", "Smart"]
LOW_BUDGET_POLICY_OPTIONS = [policy.value for policy in LowBudgetPolicy]


def _entity_selector(domain: str) -> selector.EntitySelector:
    """Return an entity selector for one domain."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain=domain,
            multiple=False,
        )
    )


def _url_or_host(value: str) -> str:
    """Validate a URL or host-like input."""
    normalized = value.strip()
    if not normalized:
        raise vol.Invalid("empty")
    parsed = urlparse(normalized if "://" in normalized else f"http://{normalized}")
    if not parsed.hostname:
        raise vol.Invalid("invalid_url")
    return normalized


class SmartEVSEDualChargerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a SmartEVSE Dual Charger config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                user_input[CONF_EVSE_1_BASE_URL] = _url_or_host(user_input[CONF_EVSE_1_BASE_URL])
                user_input[CONF_EVSE_2_BASE_URL] = _url_or_host(user_input[CONF_EVSE_2_BASE_URL])
                if user_input.get(CONF_WLED_URL):
                    user_input[CONF_WLED_URL] = _url_or_host(user_input[CONF_WLED_URL])
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data=user_input,
                )
            except vol.Invalid:
                errors["base"] = "invalid_url"

        return self.async_show_form(
            step_id="user",
            data_schema=self._build_user_schema(user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Return the options flow."""
        return SmartEVSEDualChargerOptionsFlow(config_entry)

    def _build_user_schema(self, user_input: dict[str, Any] | None) -> vol.Schema:
        """Build the user step schema."""
        user_input = user_input or {}
        return vol.Schema(
            {
                vol.Required(CONF_NAME, default=user_input.get(CONF_NAME, DEFAULT_NAME)): selector.TextSelector(),
                vol.Required(CONF_EVSE_1_BASE_URL, default=user_input.get(CONF_EVSE_1_BASE_URL, "")): selector.TextSelector(),
                vol.Required(CONF_EVSE_2_BASE_URL, default=user_input.get(CONF_EVSE_2_BASE_URL, "")): selector.TextSelector(),
                vol.Optional(CONF_WLED_URL, default=user_input.get(CONF_WLED_URL, "")): selector.TextSelector(),
                vol.Required(CONF_EVSE_1_STATE_ENTITY, default=user_input.get(CONF_EVSE_1_STATE_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_EVSE_1_PLUG_ENTITY, default=user_input.get(CONF_EVSE_1_PLUG_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_EVSE_1_MODE_ENTITY, default=user_input.get(CONF_EVSE_1_MODE_ENTITY, "")): _entity_selector("select"),
                vol.Required(CONF_EVSE_1_OVERRIDE_ENTITY, default=user_input.get(CONF_EVSE_1_OVERRIDE_ENTITY, "")): _entity_selector("number"),
                vol.Optional(CONF_EVSE_1_ERROR_ENTITY, default=user_input.get(CONF_EVSE_1_ERROR_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_EVSE_2_STATE_ENTITY, default=user_input.get(CONF_EVSE_2_STATE_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_EVSE_2_PLUG_ENTITY, default=user_input.get(CONF_EVSE_2_PLUG_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_EVSE_2_MODE_ENTITY, default=user_input.get(CONF_EVSE_2_MODE_ENTITY, "")): _entity_selector("select"),
                vol.Required(CONF_EVSE_2_OVERRIDE_ENTITY, default=user_input.get(CONF_EVSE_2_OVERRIDE_ENTITY, "")): _entity_selector("number"),
                vol.Optional(CONF_EVSE_2_ERROR_ENTITY, default=user_input.get(CONF_EVSE_2_ERROR_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_MAINS_L1_ENTITY, default=user_input.get(CONF_MAINS_L1_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_MAINS_L2_ENTITY, default=user_input.get(CONF_MAINS_L2_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_MAINS_L3_ENTITY, default=user_input.get(CONF_MAINS_L3_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_EV_METER_L1_ENTITY, default=user_input.get(CONF_EV_METER_L1_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_EV_METER_L2_ENTITY, default=user_input.get(CONF_EV_METER_L2_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_EV_METER_L3_ENTITY, default=user_input.get(CONF_EV_METER_L3_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY, default=user_input.get(CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY, "")): _entity_selector("sensor"),
                vol.Optional(CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY, default=user_input.get(CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY, "")): _entity_selector("sensor"),
                vol.Optional(CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY, default=user_input.get(CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY, "")): _entity_selector("sensor"),
                vol.Optional(CONF_PRICE_SENSOR_ENTITY, default=user_input.get(CONF_PRICE_SENSOR_ENTITY, "")): _entity_selector("sensor"),
                vol.Optional(CONF_SCHEDULE_ENTITY, default=user_input.get(CONF_SCHEDULE_ENTITY, "")): _entity_selector("schedule"),
            }
        )


class SmartEVSEDualChargerOptionsFlow(config_entries.OptionsFlowWithReload):
    """Handle options for SmartEVSE Dual Charger."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = {**self._config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACTIVE_MODE, default=options.get(CONF_ACTIVE_MODE, DEFAULT_ACTIVE_MODE)): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=ACTIVE_MODE_OPTIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(CONF_TOTAL_CURRENT_LIMIT, default=options.get(CONF_TOTAL_CURRENT_LIMIT, DEFAULT_TOTAL_CURRENT_LIMIT)): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=6,
                            max=80,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required("min_current", default=options.get("min_current", 6)): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=6,
                            max=16,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(CONF_UPDATE_INTERVAL, default=options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5,
                            max=60,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                    vol.Required(CONF_OVERRIDE_DEADBAND, default=options.get(CONF_OVERRIDE_DEADBAND, DEFAULT_OVERRIDE_DEADBAND)): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=6,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="A",
                        )
                    ),
                    vol.Required(CONF_LOW_BUDGET_POLICY_DEFAULT, default=options.get(CONF_LOW_BUDGET_POLICY_DEFAULT, DEFAULT_LOW_BUDGET_POLICY)): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=LOW_BUDGET_POLICY_OPTIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key=CONF_LOW_BUDGET_POLICY_DEFAULT,
                        )
                    ),
                    vol.Required(CONF_PUSH_CURRENTS, default=options.get(CONF_PUSH_CURRENTS, DEFAULT_PUSH_CURRENTS)): selector.BooleanSelector(),
                    vol.Required(CONF_CURRENTS_PUSH_INTERVAL, default=options.get(CONF_CURRENTS_PUSH_INTERVAL, 10)): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5,
                            max=300,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                    vol.Required(CONF_PUSH_EV_METER, default=options.get(CONF_PUSH_EV_METER, DEFAULT_PUSH_EV_METER)): selector.BooleanSelector(),
                    vol.Required(CONF_EV_METER_PUSH_INTERVAL, default=options.get(CONF_EV_METER_PUSH_INTERVAL, 20)): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5,
                            max=300,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                    vol.Required(CONF_PUSH_WLED, default=options.get(CONF_PUSH_WLED, DEFAULT_PUSH_WLED)): selector.BooleanSelector(),
                    vol.Required(CONF_NOTIFY_ON_SCHEDULE_WINDOW, default=options.get(CONF_NOTIFY_ON_SCHEDULE_WINDOW, DEFAULT_NOTIFY_ON_SCHEDULE_WINDOW)): selector.BooleanSelector(),
                }
            ),
        )

