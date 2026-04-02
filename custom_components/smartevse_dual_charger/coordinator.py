"""Coordinator for SmartEVSE Dual Charger."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_PRICE_SENSOR_ENTITY,
    CONF_PUSH_CURRENTS,
    CONF_PUSH_EV_METER,
    CONF_SCHEDULE_ENTITY,
    DEFAULT_PUSH_CURRENTS,
    DEFAULT_PUSH_EV_METER,
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
            update_interval=timedelta(seconds=controller.get_update_interval()),
        )
        self._controller = controller
        self._entry_data = entry_data
        self._options = options
        self._unsub_state_changes = None
        self._unsub_stop = None
        self._refresh_lock = asyncio.Lock()
        self._pending_refresh_reason: str | None = None
        self._currents_push_lock = asyncio.Lock()
        self._ev_meter_push_lock = asyncio.Lock()
        self._currents_push_task: asyncio.Task[None] | None = None
        self._ev_meter_push_task: asyncio.Task[None] | None = None
        self._setup_state_listeners()
        self._setup_push_loops()

    async def _async_update_data(self) -> dict[str, Any]:
        """Run a controller cycle."""
        try:
            data = await self._async_run_controller_cycle("scheduled_refresh")
        except Exception as err:  # pragma: no cover - defensive; surfaced to HA logs
            raise UpdateFailed(str(err)) from err
        return data if data is not None else self.data

    @callback
    def _setup_state_listeners(self) -> None:
        """Listen to source entity changes for immediate reevaluation."""
        entity_ids = [
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
            self._cancel_push_tasks()

        self._unsub_stop = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _handle_stop)

    @callback
    def _setup_push_loops(self) -> None:
        """Start dedicated meter-push loops independent of controller refreshes."""
        self._cancel_push_tasks()

        if self._options.get(CONF_PUSH_CURRENTS, DEFAULT_PUSH_CURRENTS):
            self._currents_push_task = self.hass.async_create_task(
                self._async_run_push_loop(
                    push_callback=self._async_push_currents,
                    interval_seconds=self._controller.get_currents_push_interval(),
                    initial_delay=0.0,
                )
            )

        if self._options.get(CONF_PUSH_EV_METER, DEFAULT_PUSH_EV_METER):
            self._ev_meter_push_task = self.hass.async_create_task(
                self._async_run_push_loop(
                    push_callback=self._async_push_ev_meter,
                    interval_seconds=self._controller.get_ev_meter_push_interval(),
                    initial_delay=self._ev_meter_initial_delay(),
                )
            )

    @callback
    def _cancel_push_tasks(self) -> None:
        """Cancel active push loops."""
        if self._currents_push_task:
            self._currents_push_task.cancel()
            self._currents_push_task = None
        if self._ev_meter_push_task:
            self._ev_meter_push_task.cancel()
            self._ev_meter_push_task = None

    def _ev_meter_initial_delay(self) -> float:
        """Offset EV meter pushes so they do not align with mains pushes."""
        if not self._options.get(CONF_PUSH_CURRENTS, DEFAULT_PUSH_CURRENTS):
            return 0.0
        interval = float(self._controller.get_ev_meter_push_interval())
        return min(2.0, max(0.5, interval / 2.0))

    async def _async_run_push_loop(
        self,
        *,
        push_callback,
        interval_seconds: int,
        initial_delay: float,
    ) -> None:
        """Run a dedicated periodic push loop."""
        try:
            if initial_delay > 0:
                await asyncio.sleep(initial_delay)
            while True:
                await push_callback()
                await asyncio.sleep(max(1, interval_seconds))
        except asyncio.CancelledError:
            raise

    async def _async_push_currents(self) -> None:
        """Push mains currents without overlapping the previous push."""
        if self._currents_push_lock.locked():
            return
        async with self._currents_push_lock:
            await self._controller.async_push_currents()

    async def _async_push_ev_meter(self) -> None:
        """Push EV meter data without overlapping the previous push."""
        if self._ev_meter_push_lock.locked():
            return
        async with self._ev_meter_push_lock:
            await self._controller.async_push_ev_meter()

    async def async_shutdown(self) -> None:
        """Release listeners on unload."""
        if self._unsub_state_changes:
            self._unsub_state_changes()
            self._unsub_state_changes = None
        self._cancel_push_tasks()
        if self._unsub_stop:
            self._unsub_stop()
            self._unsub_stop = None

    async def async_timing_updated(self) -> None:
        """Apply updated runtime timing values immediately."""
        self.update_interval = timedelta(seconds=self._controller.get_update_interval())
        self._setup_push_loops()

    async def _async_refresh_now(self, reason: str) -> None:
        """Refresh immediately and propagate updated data."""
        data = await self._async_run_controller_cycle(reason)
        if data is not None:
            self.async_set_updated_data(data)

    async def _async_run_controller_cycle(self, reason: str) -> dict[str, Any] | None:
        """Run the controller without allowing overlapping refresh storms."""
        if self._refresh_lock.locked():
            self._pending_refresh_reason = reason
            return None

        async with self._refresh_lock:
            next_reason = reason
            data: dict[str, Any] | None = None
            while True:
                data = await self._controller.async_run_cycle(reason=next_reason)
                if self._pending_refresh_reason is None:
                    return data
                next_reason = self._pending_refresh_reason
                self._pending_refresh_reason = None
