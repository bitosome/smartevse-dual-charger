"""Config flow for SmartEVSE Dual Charger."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CHARGE_POLICY_DEFAULT,
    CONF_CURRENTS_PUSH_INTERVAL,
    CONF_DUTY_CYCLE_MINUTES,
    CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY,
    CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY,
    CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY,
    CONF_EV_METER_L1_ENTITY,
    CONF_EV_METER_L2_ENTITY,
    CONF_EV_METER_L3_ENTITY,
    CONF_EV_METER_PUSH_INTERVAL,
    CONF_MAINS_L1_ENTITY,
    CONF_MAINS_L2_ENTITY,
    CONF_MAINS_L3_ENTITY,
    CONF_NOTIFY_ON_SCHEDULE_WINDOW,
    CONF_PRICE_SENSOR_ENTITY,
    CONF_PUSH_CURRENTS,
    CONF_PUSH_EV_METER,
    CONF_PUSH_WLED,
    CONF_RECREATE_WLED_PRESETS,
    CONF_SCHEDULE_ENTITY,
    CONF_SMARTEVSE_1_BASE_URL,
    CONF_SMARTEVSE_2_BASE_URL,
    CONF_UPDATE_INTERVAL,
    CONF_WLED_URL,
    CONF_WLED_LED_COUNT,
    CONF_WLED_LED_OFFSET,
    CONF_WLED_PRESETS_JSON,
    DEFAULT_CHARGE_POLICY,
    DEFAULT_CURRENTS_PUSH_INTERVAL,
    DEFAULT_DUTY_CYCLE_MINUTES,
    DEFAULT_EV_METER_PUSH_INTERVAL,
    DEFAULT_NAME,
    DEFAULT_NOTIFY_ON_SCHEDULE_WINDOW,
    DEFAULT_PUSH_CURRENTS,
    DEFAULT_PUSH_EV_METER,
    DEFAULT_PUSH_WLED,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_WLED_LED_COUNT,
    DEFAULT_WLED_LED_OFFSET,
    DOMAIN,
    LOGGER,
    ChargePolicy,
)
from .wled import (
    WLEDPresetError,
    async_recreate_wled_assets,
    build_default_presets_json,
    normalize_wled_base_url,
)

CONF_SETUP_WLED = "setup_wled"

CHARGE_POLICY_OPTIONS = [policy.value for policy in ChargePolicy]
LEGACY_DEFAULTS: dict[str, Any] = {
    CONF_NAME: DEFAULT_NAME,
    CONF_SMARTEVSE_1_BASE_URL: "192.168.0.234",
    CONF_SMARTEVSE_2_BASE_URL: "192.168.0.44",
    CONF_WLED_URL: "192.168.0.81",
    CONF_MAINS_L1_ENTITY: "sensor.shelly_pro_3em_1_phase_a_current",
    CONF_MAINS_L2_ENTITY: "sensor.shelly_pro_3em_1_phase_b_current",
    CONF_MAINS_L3_ENTITY: "sensor.shelly_pro_3em_1_phase_c_current",
    CONF_EV_METER_L1_ENTITY: "sensor.shelly_pro_3em_2_phase_a_current",
    CONF_EV_METER_L2_ENTITY: "sensor.shelly_pro_3em_2_phase_b_current",
    CONF_EV_METER_L3_ENTITY: "sensor.shelly_pro_3em_2_phase_c_current",
    CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY: "sensor.shelly_pro_3em_2_total_active_power",
    CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY: "sensor.shelly_pro_3em_2_total_active_energy",
    CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY: "sensor.shelly_pro_3em_2_total_active_returned_energy",
    CONF_PRICE_SENSOR_ENTITY: "sensor.real_electricity_price_current_price",
    CONF_SCHEDULE_ENTITY: "schedule.charge_schedule",
}


def _entity_selector(domain: str) -> selector.EntitySelector:
    """Return an entity selector for one domain."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain=domain,
            multiple=False,
        )
    )


def _url_or_host(value: str) -> str:
    """Validate a URL/IP or host-like input."""
    normalized = value.strip()
    if not normalized:
        raise vol.Invalid("empty")
    parsed = urlparse(normalized if "://" in normalized else f"http://{normalized}")
    if not parsed.hostname:
        raise vol.Invalid("invalid_url")
    return normalized


def _wled_url_or_host(value: str) -> str:
    """Validate and normalize a WLED URL/IP to its base URL."""
    return normalize_wled_base_url(_url_or_host(value)).removesuffix("/")


def _parse_presets_json(value: str) -> dict[str, Any]:
    """Validate presets.json text and return parsed JSON."""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as err:
        raise vol.Invalid("invalid_json") from err
    if not isinstance(payload, dict):
        raise vol.Invalid("invalid_json")
    return payload


def _optional_entity_default(values: dict[str, Any], key: str) -> str | None:
    """Return a selector-friendly default for optional entity fields."""
    value = values.get(key)
    return value or None


class SmartEVSEDualChargerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a SmartEVSE Dual Charger config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize config flow state."""
        self._progress_task = None
        self._pending_user_input: dict[str, Any] | None = None
        self._pending_wled_input: dict[str, Any] | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                form_data = dict(user_input)
                setup_wled = bool(form_data.pop(CONF_SETUP_WLED, False))
                form_data[CONF_SMARTEVSE_1_BASE_URL] = _url_or_host(form_data[CONF_SMARTEVSE_1_BASE_URL])
                form_data[CONF_SMARTEVSE_2_BASE_URL] = _url_or_host(form_data[CONF_SMARTEVSE_2_BASE_URL])
                if setup_wled:
                    self._pending_user_input = form_data
                    self._pending_wled_input = {
                        CONF_WLED_URL: LEGACY_DEFAULTS[CONF_WLED_URL],
                        CONF_WLED_LED_COUNT: DEFAULT_WLED_LED_COUNT,
                        CONF_WLED_LED_OFFSET: DEFAULT_WLED_LED_OFFSET,
                        CONF_WLED_PRESETS_JSON: build_default_presets_json(),
                    }
                    return await self.async_step_wled()
                if not errors:
                    await self.async_set_unique_id(DOMAIN)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=form_data[CONF_NAME],
                        data=form_data,
                    )
            except vol.Invalid:
                errors["base"] = "invalid_url"

        return self.async_show_form(
            step_id="user",
            data_schema=self._build_user_schema(user_input),
            errors=errors,
        )

    async def async_step_wled(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Configure and optionally rebuild WLED assets."""
        if self._progress_task is not None:
            if not self._progress_task.done():
                return self.async_show_progress(
                    step_id="wled",
                    progress_action=CONF_RECREATE_WLED_PRESETS,
                    progress_task=self._progress_task,
                )
            return self.async_show_progress_done(next_step_id="finish_user_wled")

        if self._pending_user_input is None:
            return await self.async_step_user()

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                wled_data = self._normalize_wled_input(user_input)
                self._pending_wled_input = dict(user_input)
                presets_payload = _parse_presets_json(wled_data[CONF_WLED_PRESETS_JSON])
                self._progress_task = self.hass.async_create_task(
                    async_recreate_wled_assets(
                        self.hass,
                        wled_data[CONF_WLED_URL],
                        led_count=wled_data[CONF_WLED_LED_COUNT],
                        led_offset=wled_data[CONF_WLED_LED_OFFSET],
                        presets_payload=presets_payload,
                    )
                )
                return self.async_show_progress(
                    step_id="wled",
                    progress_action=CONF_RECREATE_WLED_PRESETS,
                    progress_task=self._progress_task,
                )
            except vol.Invalid as err:
                errors["base"] = str(err) if str(err) else "invalid_url"

        return self.async_show_form(
            step_id="wled",
            data_schema=self._build_wled_schema(user_input or self._pending_wled_input),
            errors=errors,
        )

    async def async_step_finish_user_wled(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Finish the WLED preset recreation task before creating the entry."""
        assert user_input is None
        progress_task = self._progress_task
        pending_user_input = self._pending_user_input
        pending_wled_input = self._pending_wled_input
        self._progress_task = None

        if progress_task is None or pending_user_input is None:
            return await self.async_step_user()

        try:
            await progress_task
        except WLEDPresetError as err:
            LOGGER.warning("WLED preset recreation failed during initial setup: %s", err)
            values = dict(pending_wled_input or {})
            return self.async_show_form(
                step_id="wled",
                data_schema=self._build_wled_schema(values),
                errors={"base": "wled_preset_setup_failed"},
            )
        except Exception:
            LOGGER.exception("Unexpected error during initial WLED preset recreation")
            values = dict(pending_wled_input or {})
            return self.async_show_form(
                step_id="wled",
                data_schema=self._build_wled_schema(values),
                errors={"base": "wled_preset_setup_failed"},
            )

        if pending_wled_input is not None:
            pending_user_input = {
                **pending_user_input,
                **self._normalize_wled_input(pending_wled_input),
            }

        self._pending_user_input = None
        self._pending_wled_input = None
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=pending_user_input[CONF_NAME],
            data=pending_user_input,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Return the options flow."""
        return SmartEVSEDualChargerOptionsFlow(config_entry)

    def _build_user_schema(self, user_input: dict[str, Any] | None) -> vol.Schema:
        """Build the user step schema."""
        user_input = {**LEGACY_DEFAULTS, **(user_input or {})}
        return vol.Schema(
            {
                vol.Required(CONF_NAME, default=user_input.get(CONF_NAME, DEFAULT_NAME)): selector.TextSelector(),
                vol.Required(CONF_SMARTEVSE_1_BASE_URL, default=user_input.get(CONF_SMARTEVSE_1_BASE_URL, "")): selector.TextSelector(),
                vol.Required(CONF_SMARTEVSE_2_BASE_URL, default=user_input.get(CONF_SMARTEVSE_2_BASE_URL, "")): selector.TextSelector(),
                vol.Required(
                    CONF_SETUP_WLED,
                    default=bool(user_input.get(CONF_SETUP_WLED, False)),
                ): selector.BooleanSelector(),
                vol.Required(CONF_MAINS_L1_ENTITY, default=user_input.get(CONF_MAINS_L1_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_MAINS_L2_ENTITY, default=user_input.get(CONF_MAINS_L2_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_MAINS_L3_ENTITY, default=user_input.get(CONF_MAINS_L3_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_EV_METER_L1_ENTITY, default=user_input.get(CONF_EV_METER_L1_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_EV_METER_L2_ENTITY, default=user_input.get(CONF_EV_METER_L2_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(CONF_EV_METER_L3_ENTITY, default=user_input.get(CONF_EV_METER_L3_ENTITY, "")): _entity_selector("sensor"),
                vol.Required(
                    CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY,
                    default=user_input.get(CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY, ""),
                ): _entity_selector("sensor"),
                vol.Optional(
                    CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY,
                    default=_optional_entity_default(user_input, CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY),
                ): _entity_selector("sensor"),
                vol.Optional(
                    CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY,
                    default=_optional_entity_default(user_input, CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY),
                ): _entity_selector("sensor"),
                vol.Optional(
                    CONF_PRICE_SENSOR_ENTITY,
                    default=_optional_entity_default(user_input, CONF_PRICE_SENSOR_ENTITY),
                ): _entity_selector("sensor"),
                vol.Optional(
                    CONF_SCHEDULE_ENTITY,
                    default=_optional_entity_default(user_input, CONF_SCHEDULE_ENTITY),
                ): _entity_selector("schedule"),
            }
        )

    def _build_wled_schema(self, user_input: dict[str, Any] | None) -> vol.Schema:
        """Build the WLED setup step schema."""
        user_input = user_input or {}
        return vol.Schema(
            {
                vol.Required(CONF_WLED_URL, default=user_input.get(CONF_WLED_URL, LEGACY_DEFAULTS[CONF_WLED_URL])): selector.TextSelector(),
                vol.Required(
                    CONF_WLED_LED_COUNT,
                    default=int(user_input.get(CONF_WLED_LED_COUNT, DEFAULT_WLED_LED_COUNT)),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=2000,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_WLED_LED_OFFSET,
                    default=int(user_input.get(CONF_WLED_LED_OFFSET, DEFAULT_WLED_LED_OFFSET)),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=2000,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_WLED_PRESETS_JSON,
                    default=user_input.get(CONF_WLED_PRESETS_JSON, build_default_presets_json()),
                ): selector.TextSelector(
                    selector.TextSelectorConfig(
                        multiline=True,
                    )
                ),
            }
        )

    def _normalize_wled_input(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalize WLED step input."""
        return {
            CONF_WLED_URL: _wled_url_or_host(str(user_input[CONF_WLED_URL])),
            CONF_WLED_LED_COUNT: max(1, int(float(user_input[CONF_WLED_LED_COUNT]))),
            CONF_WLED_LED_OFFSET: max(0, int(float(user_input[CONF_WLED_LED_OFFSET]))),
            CONF_WLED_PRESETS_JSON: str(user_input[CONF_WLED_PRESETS_JSON]).strip(),
        }


class SmartEVSEDualChargerOptionsFlow(config_entries.OptionsFlowWithReload):
    """Handle options for SmartEVSE Dual Charger."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._progress_task = None
        self._pending_options: dict[str, Any] | None = None

    def _current_value(self, key: str, fallback: Any) -> Any:
        """Prefer live runtime values when the entry is loaded."""
        if hasattr(self._config_entry, "runtime_data"):
            return self._config_entry.runtime_data.coordinator.data.get(key, fallback)
        return fallback

    def _build_options_schema(self, values: dict[str, Any]) -> vol.Schema:
        """Build the options step schema."""
        return vol.Schema(
            {
                vol.Required(
                    CONF_CHARGE_POLICY_DEFAULT,
                    default=values.get(
                        CONF_CHARGE_POLICY_DEFAULT,
                        self._current_value(
                            "charge_policy",
                            DEFAULT_CHARGE_POLICY,
                        ),
                    ),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=CHARGE_POLICY_OPTIONS,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_CHARGE_POLICY_DEFAULT,
                    )
                ),
                vol.Required(
                    CONF_DUTY_CYCLE_MINUTES,
                    default=values.get(
                        CONF_DUTY_CYCLE_MINUTES,
                        self._current_value(
                            CONF_DUTY_CYCLE_MINUTES,
                            DEFAULT_DUTY_CYCLE_MINUTES,
                        ),
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=720,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=values.get(
                        CONF_UPDATE_INTERVAL,
                        self._current_value(
                            CONF_UPDATE_INTERVAL,
                            DEFAULT_UPDATE_INTERVAL,
                        ),
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=60,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                vol.Required(
                    CONF_PUSH_CURRENTS,
                    default=values.get(CONF_PUSH_CURRENTS, DEFAULT_PUSH_CURRENTS),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_CURRENTS_PUSH_INTERVAL,
                    default=values.get(
                        CONF_CURRENTS_PUSH_INTERVAL,
                        self._current_value(
                            CONF_CURRENTS_PUSH_INTERVAL,
                            DEFAULT_CURRENTS_PUSH_INTERVAL,
                        ),
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=300,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                vol.Required(
                    CONF_PUSH_EV_METER,
                    default=values.get(CONF_PUSH_EV_METER, DEFAULT_PUSH_EV_METER),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_EV_METER_PUSH_INTERVAL,
                    default=values.get(
                        CONF_EV_METER_PUSH_INTERVAL,
                        self._current_value(
                            CONF_EV_METER_PUSH_INTERVAL,
                            DEFAULT_EV_METER_PUSH_INTERVAL,
                        ),
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=300,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                vol.Required(
                    CONF_PUSH_WLED,
                    default=values.get(CONF_PUSH_WLED, DEFAULT_PUSH_WLED),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_RECREATE_WLED_PRESETS,
                    default=bool(values.get(CONF_RECREATE_WLED_PRESETS, False)),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_NOTIFY_ON_SCHEDULE_WINDOW,
                    default=values.get(CONF_NOTIFY_ON_SCHEDULE_WINDOW, DEFAULT_NOTIFY_ON_SCHEDULE_WINDOW),
                ): selector.BooleanSelector(),
            }
        )

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Manage options."""
        if self._progress_task is not None:
            if not self._progress_task.done():
                return self.async_show_progress(
                    step_id="init",
                    progress_action=CONF_RECREATE_WLED_PRESETS,
                    progress_task=self._progress_task,
                )
            return self.async_show_progress_done(next_step_id="finish_wled")

        base_values = {**self._config_entry.data, **self._config_entry.options}
        errors: dict[str, str] = {}

        if user_input is not None:
            options_data = dict(user_input)
            recreate_wled_presets = bool(options_data.pop(CONF_RECREATE_WLED_PRESETS, False))
            if recreate_wled_presets:
                wled_url = self._config_entry.data.get(CONF_WLED_URL)
                if not wled_url:
                    errors["base"] = "wled_url_required"
                else:
                    presets_json = str(self._config_entry.data.get(CONF_WLED_PRESETS_JSON, "")).strip()
                    try:
                        presets_payload = _parse_presets_json(presets_json) if presets_json else None
                    except vol.Invalid:
                        presets_payload = None
                    self._pending_options = options_data
                    self._progress_task = self.hass.async_create_task(
                        async_recreate_wled_assets(
                            self.hass,
                            wled_url,
                            led_count=int(self._config_entry.data.get(CONF_WLED_LED_COUNT, DEFAULT_WLED_LED_COUNT)),
                            led_offset=int(self._config_entry.data.get(CONF_WLED_LED_OFFSET, DEFAULT_WLED_LED_OFFSET)),
                            presets_payload=presets_payload,
                        )
                    )
                    return self.async_show_progress(
                        step_id="init",
                        progress_action=CONF_RECREATE_WLED_PRESETS,
                        progress_task=self._progress_task,
                    )

            if not errors:
                return self.async_create_entry(title="", data=options_data)

            base_values.update(user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self._build_options_schema(base_values),
            errors=errors,
        )

    async def async_step_finish_wled(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Finish the WLED preset recreation task before saving options."""
        assert user_input is None
        progress_task = self._progress_task
        pending_options = self._pending_options or {**self._config_entry.options}
        self._progress_task = None
        self._pending_options = None

        if progress_task is None:
            return await self.async_step_init()

        try:
            await progress_task
        except WLEDPresetError as err:
            LOGGER.warning("WLED preset recreation failed: %s", err)
            return self.async_show_form(
                step_id="init",
                data_schema=self._build_options_schema(pending_options),
                errors={"base": "wled_preset_setup_failed"},
            )
        except Exception:
            LOGGER.exception("Unexpected error during WLED preset recreation")
            return self.async_show_form(
                step_id="init",
                data_schema=self._build_options_schema(pending_options),
                errors={"base": "wled_preset_setup_failed"},
            )

        return self.async_create_entry(title="", data=pending_options)
