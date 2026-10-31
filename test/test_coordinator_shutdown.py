"""Ensure native and direct refreshes cannot outlive coordinator unload."""

import asyncio
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import frame

from custom_components.smartevse_dual_charger.coordinator import SmartEVSEDualChargerCoordinator


class CoordinatorShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        self.hass.state = CoreState.running
        frame.async_setup(self.hass)
        self.controller = MagicMock()
        self.controller.get_update_interval.return_value = 10
        self.controller.async_run_cycle = AsyncMock(return_value={'state': 'idle'})
        self.controller.async_push_currents = AsyncMock()
        self.controller.async_push_ev_meter = AsyncMock()
        self.controller.async_shutdown = AsyncMock()
        self.coordinator = SmartEVSEDualChargerCoordinator(self.hass, self.controller,
            entry_data={'schedule_entity': 'schedule.test'},
            options={'push_currents': False, 'push_ev_meter': False})
        self.addAsyncCleanup(self.hass.async_stop)
        self.addAsyncCleanup(self.coordinator.async_shutdown)

    async def test_native_timer_and_queued_direct_refreshes_are_closed(self):
        cancel_timer = MagicMock()
        self.coordinator._unsub_refresh = cancel_timer
        queued = asyncio.create_task(self.coordinator._async_refresh_now('queued_before_unload'))
        await self.coordinator.async_shutdown()
        await queued
        self.assertTrue(self.coordinator._shutdown_requested)
        self.assertTrue(self.coordinator._debounced_refresh._shutdown_requested)
        cancel_timer.assert_called_once()
        await self.coordinator._async_refresh_now('late_state_event')
        await self.coordinator._async_run_controller_cycle('late_service')
        await self.coordinator._async_push_currents()
        await self.coordinator._async_push_ev_meter()
        await self.coordinator.async_timing_updated()
        self.controller.async_run_cycle.assert_not_awaited()
        self.controller.async_push_currents.assert_not_awaited()
        self.controller.async_push_ev_meter.assert_not_awaited()
        self.assertIsNone(self.coordinator._currents_push_task)
        self.assertIsNone(self.coordinator._ev_meter_push_task)
        self.assertIsNone(self.coordinator._unsub_state_changes)

    async def test_shutdown_drains_running_cycle_and_discards_pending_followup(self):
        started = asyncio.Event()
        finish = asyncio.Event()
        writes = []
        async def cycle(**kwargs):
            started.set()
            await finish.wait()
            writes.append('equipment_write')
            return {'state': 'finished'}
        self.controller.async_run_cycle.side_effect = cycle
        self.coordinator.async_set_updated_data = MagicMock()
        active = asyncio.create_task(self.coordinator._async_refresh_now('active'))
        await started.wait()
        await self.coordinator._async_run_controller_cycle('followup')
        stopping = asyncio.create_task(self.coordinator.async_shutdown())
        await asyncio.sleep(0)
        self.assertTrue(self.coordinator._shutdown_requested)
        self.assertFalse(stopping.done())
        self.controller.async_shutdown.assert_not_awaited()
        await self.coordinator._async_refresh_now('late')
        finish.set()
        await active
        await stopping
        writes.append('unloaded')
        self.assertEqual(writes, ['equipment_write', 'unloaded'])
        self.controller.async_run_cycle.assert_awaited_once()
        self.controller.async_shutdown.assert_awaited_once()
        self.coordinator.async_set_updated_data.assert_not_called()
        self.assertIsNone(self.coordinator._pending_refresh_reason)

    async def test_home_assistant_stop_uses_same_shutdown_path(self):
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await self.hass.async_block_till_done()
        self.assertTrue(self.coordinator._shutdown_requested)
        self.controller.async_shutdown.assert_awaited()
        await self.coordinator._async_refresh_now('after_stop')
        self.controller.async_run_cycle.assert_not_awaited()

    async def test_shutdown_waits_for_cancelled_push_task_cleanup(self):
        started = asyncio.Event()
        cleaned_up = asyncio.Event()
        async def push():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned_up.set()
        task = asyncio.create_task(push())
        self.coordinator._currents_push_task = task
        await started.wait()
        await self.coordinator.async_shutdown()
        self.assertTrue(cleaned_up.is_set())
        self.assertTrue(task.done())


if __name__ == '__main__':
    unittest.main()
