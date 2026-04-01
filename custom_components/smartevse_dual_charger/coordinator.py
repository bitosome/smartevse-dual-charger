"""Coordinator for SmartEVSE Dual Charger."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_EVSE_1_ERROR_ENTITY,
    CONF_EVSE_1_MODE_ENTITY,
    CONF_EVSE_1_OVERRIDE_ENTITY,
    CONF_EVSE_1_PLUG_ENTITY,
    CONF_EVSE_1_STATE_ENTITY,
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
    CONF_MAINS_L1_ENTITY,
    CONF_MAINS_L2_ENTITY,
    CONF_MAINS_L3_ENTITY,
    CONF_PRICE_SENSOR_ENTITY,
    CONF_SCHEDULE_ENTITY,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    LOGGER,
)
from .controller import SmartEVSEDualChargerController


class SmartEVSEDualChargerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinates controller refreshes."""

    def __init__(
        self,
        hass: HomeAssistant,
        controller: SmartEVSEDualChargerController,
        *,
        entry_data: dict[str, Any],
        options: dict[str, Any],
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            name="smartevse_dual_charger",
            update_interval=timedelta(seconds=int(options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))),
        )
        self._controller = controller
        self._entry_data = entry_data
        self._options = options
        self._unsub_state_changes = None
        self._unsub_stop = None
        self._setup_state_listeners()

    async def _async_update_data(self) -> dict[str, Any]:
        """Run a controller cycle."""
        try:
            return await self._controller.async_run_cycle(reason="scheduled_refresh")
        except Exception as err:  # pragma: no cover - defensive; surfaced to HA logs
            raise UpdateFailed(str(err)) from err

    @callback
    def _setup_state_listeners(self) -> None:
        """Listen to source entity changes for immediate reevaluation."""
        entity_ids = [
            self._entry_data.get(CONF_EVSE_1_STATE_ENTITY),
            self._entry_data.get(CONF_EVSE_1_PLUG_ENTITY),
            self._entry_data.get(CONF_EVSE_1_MODE_ENTITY),
            self._entry_data.get(CONF_EVSE_1_OVERRIDE_ENTITY),
            self._entry_data.get(CONF_EVSE_1_ERROR_ENTITY),
            self._entry_data.get(CONF_EVSE_2_STATE_ENTITY),
            self._entry_data.get(CONF_EVSE_2_PLUG_ENTITY),
            self._entry_data.get(CONF_EVSE_2_MODE_ENTITY),
            self._entry_data.get(CONF_EVSE_2_OVERRIDE_ENTITY),
            self._entry_data.get(CONF_EVSE_2_ERROR_ENTITY),
            self._entry_data.get(CONF_MAINS_L1_ENTITY),
            self._entry_data.get(CONF_MAINS_L2_ENTITY),
            self._entry_data.get(CONF_MAINS_L3_ENTITY),
            self._entry_data.get(CONF_EV_METER_L1_ENTITY),
            self._entry_data.get(CONF_EV_METER_L2_ENTITY),
            self._entry_data.get(CONF_EV_METER_L3_ENTITY),
            self._entry_data.get(CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY),
            self._entry_data.get(CONF_EV_METER_IMPORT_ACTIVE_ENERGY_ENTITY),
            self._entry_data.get(CONF_EV_METER_EXPORT_ACTIVE_ENERGY_ENTITY),
            self._entry_data.get(CONF_PRICE_SENSOR_ENTITY),
            self._entry_data.get(CONF_SCHEDULE_ENTITY),
        ]
        entity_ids = [entity_id for entity_id in entity_ids if entity_id]

        @callback
        def _handle_state_change(_event) -> None:
            self.hass.async_create_task(self._async_refresh_now("state_change"))

        self._unsub_state_changes = async_track_state_change_event(
            self.hass,
            entity_ids,
            _handle_state_change,
        )

        @callback
        def _handle_stop(_event) -> None:
            if self._unsub_state_changes:
                self._unsub_state_changes()
                self._unsub_state_changes = None

        self._unsub_stop = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _handle_stop)

    async def _async_refresh_now(self, reason: str) -> None:
        """Refresh immediately and propagate updated data."""
        data = await self._controller.async_run_cycle(reason=reason)
        self.async_set_updated_data(data)

