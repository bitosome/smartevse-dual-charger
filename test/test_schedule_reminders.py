"""Exercise schedule reminders with native HA State/schema and controlled I/O."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import logging
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from homeassistant.core import State, HomeAssistant, CoreState, SupportsResponse
from homeassistant.components.schedule import Schedule, async_get_schedule_service
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers import entity_registry, device_registry, area_registry, floor_registry, label_registry
from homeassistant.util import dt as dt_util

from custom_components.smartevse_dual_charger.const import (
    CONF_NOTIFY_ON_SCHEDULE_WINDOW,
    CONF_SCHEDULE_NOTIFICATION_SERVICE,
    CONF_SCHEDULE_NOTIFICATION_TARGETS,
    CONF_SCHEDULE_REMINDER_MINUTES,
)
from custom_components.smartevse_dual_charger.schedule_reminders import (
    ScheduleReminders, active_window_start, schedule_intervals, validate_notification_options,
)
from custom_components.smartevse_dual_charger.config_flow import SmartEVSEDualChargerOptionsFlow

ENTITY = 'schedule.charge_schedule'


class ScheduleWindowTests(unittest.TestCase):
    def setUp(self):
        self.tz = patch.object(dt_util, 'DEFAULT_TIME_ZONE', ZoneInfo('Europe/Tallinn'))
        self.tz.start()
        self.addCleanup(self.tz.stop)

    def test_intervals_of_the_weekly_schedule_for_the_saving_comparison(self):
        tz = ZoneInfo('Europe/Tallinn')
        at = lambda day, hour, minute=0: datetime(2026, 11, day, hour, minute, tzinfo=tz)
        nightly = {name: [{'from': time(0), 'to': time(7)}] for name in
                   ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')}
        # Monday 18:00 to Wednesday 12:00: two nights, in UTC.
        self.assertEqual(schedule_intervals(nightly, at(9, 18), at(11, 12), tz),
                         [(at(10, 0).astimezone(UTC), at(10, 7).astimezone(UTC)),
                          (at(11, 0).astimezone(UTC), at(11, 7).astimezone(UTC))])
        # A window that is open at the start is cut there.
        self.assertEqual(schedule_intervals(nightly, at(10, 3), at(10, 5), tz), [(at(10, 3).astimezone(UTC), at(10, 5).astimezone(UTC))])
        # Blocks that touch at midnight are one window; the end of the day is time.max or "24:00:00".
        evening = {'monday': [{'from': time(22), 'to': time.max}], 'tuesday': [{'from': '00:00:00', 'to': '06:00:00'},
                                                                                {'from': '23:00:00', 'to': '24:00:00'}]}
        self.assertEqual(schedule_intervals(evening, at(9, 12), at(11, 12), tz),
                         [(at(9, 22).astimezone(UTC), at(10, 6).astimezone(UTC)),
                          (at(10, 23).astimezone(UTC), at(11, 0).astimezone(UTC))])
        self.assertEqual(schedule_intervals({}, at(9, 12), at(11, 12), tz), [])
        with self.assertRaises(ValueError):
            schedule_intervals({'monday': [{'from': time(8), 'to': time(7)}]}, at(9, 12), at(11, 12), tz)
        with self.assertRaises(ValueError):
            schedule_intervals(None, at(9, 12), at(11, 12), tz)

    def test_native_ranges_merge_touching_blocks_across_week_boundary(self):
        # schedule.get_schedule returns native datetime.time objects in process.
        definition = {
            'sunday': [{'from': time(23), 'to': time.max}],
            'monday': [{'from': time(), 'to': time(2)}, {'from': time(2), 'to': time(7)}],
        }
        sunday = datetime(2026, 11, 1, 23, 30, tzinfo=ZoneInfo('Europe/Tallinn'))
        monday = sunday + timedelta(hours=4)
        expected = '2026-11-01T21:00:00+00:00'
        self.assertEqual(active_window_start(definition, sunday), expected)
        self.assertEqual(active_window_start(definition, monday), expected)
        self.assertIsNone(active_window_start(definition, monday + timedelta(hours=4)))

    def test_service_serialized_times_and_continuous_week_are_supported(self):
        definition = {day: [{'from': '00:00:00', 'to': '23:59:59.999999'}]
            for day in ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')}
        now = datetime(2026, 10, 31, tzinfo=UTC)
        self.assertEqual(active_window_start(definition, now), 'continuous')
        self.assertEqual(active_window_start(definition, now + timedelta(days=8)), 'continuous')

    def test_active_window_start_follows_native_local_daylight_saving_time(self):
        definition = {'sunday': [{'from': time(0), 'to': time(5)}]}
        self.assertEqual(active_window_start(definition, datetime(2026, 3, 29, 0, 30, tzinfo=UTC)),
            '2026-03-28T22:00:00+00:00')
        self.assertEqual(active_window_start(definition, datetime(2026, 10, 25, 0, 30, tzinfo=UTC)),
            '2026-10-24T21:00:00+00:00')

    def test_options_accept_legacy_and_native_notify_actions_only(self):
        validate_notification_options({})
        validate_notification_options({CONF_SCHEDULE_NOTIFICATION_SERVICE: 'notify.notify'})
        validate_notification_options({CONF_SCHEDULE_NOTIFICATION_SERVICE: 'notify.send_message',
            CONF_SCHEDULE_NOTIFICATION_TARGETS: ['notify.phone']})
        for options in (
            {CONF_SCHEDULE_NOTIFICATION_SERVICE: 'switch.turn_off'},
            {CONF_SCHEDULE_NOTIFICATION_SERVICE: 'notify.send_message'},
            {CONF_SCHEDULE_NOTIFICATION_SERVICE: 'notify.send_message', CONF_SCHEDULE_NOTIFICATION_TARGETS: ['light.test']},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                validate_notification_options(options)

    def test_native_options_schema_exposes_defaults_and_accepts_reminder_fields(self):
        entry = SimpleNamespace(data={}, options={})
        flow = SmartEVSEDualChargerOptionsFlow(entry)
        schema = flow._build_options_schema({})
        markers = {key.schema: key for key in schema.schema}
        self.assertEqual(markers[CONF_SCHEDULE_REMINDER_MINUTES].default(), 60)
        self.assertEqual(markers[CONF_SCHEDULE_NOTIFICATION_SERVICE].default(), '')
        selector = schema.schema[markers[CONF_SCHEDULE_NOTIFICATION_TARGETS]]
        self.assertEqual(selector(['notify.phone']), ['notify.phone'])


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tz = patch.object(dt_util, 'DEFAULT_TIME_ZONE', ZoneInfo('Europe/Tallinn'))
        self.tz.start()
        self.addCleanup(self.tz.stop)
        self.now = datetime(2026, 10, 31, 4, 30, tzinfo=UTC)
        self.time_patch = patch('custom_components.smartevse_dual_charger.schedule_reminders.dt_util.utcnow', side_effect=lambda: self.now)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.options = {CONF_SCHEDULE_NOTIFICATION_SERVICE: 'notify.notify'}
        self.schedule_enabled = False
        self.state = State(ENTITY, 'off', {'next_event': '2026-10-31T07:20:00+02:00'})
        self.definition = {'saturday': [{'from': time(7, 20), 'to': time(9)}]}
        self.notifications = []
        self.disk = None
        self.saved = []

        async def call(domain, action, payload, **kwargs):
            if domain == 'schedule':
                self.assertEqual(action, 'get_schedule')
                self.assertTrue(kwargs.get('return_response'))
                return {ENTITY: deepcopy(self.definition)}
            self.notifications.append((domain, action, payload))

        async def save(data):
            self.saved.append(deepcopy(data))
            self.disk = deepcopy(data)

        self.call = AsyncMock(side_effect=call)
        self.store = SimpleNamespace(async_load=AsyncMock(side_effect=lambda: deepcopy(self.disk)),
            async_save=AsyncMock(side_effect=save))
        self.hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda entity: self.state if entity == ENTITY else None),
            services=SimpleNamespace(async_call=self.call),
            async_create_background_task=lambda coro, name: asyncio.create_task(coro, name=name),
        )
        self.store_patch = patch('custom_components.smartevse_dual_charger.schedule_reminders.StrictStore', return_value=self.store)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.manager = await self.new_manager()

    async def new_manager(self):
        manager = ScheduleReminders(self.hass, 'test', ENTITY, self.options, lambda: self.schedule_enabled)
        await manager.async_initialize()
        self.addAsyncCleanup(manager.async_shutdown)
        return manager

    async def check(self, manager=None):
        manager = manager or self.manager
        manager.async_check()
        if manager._task is not None:
            await manager._task

    def open_window(self):
        self.now = datetime(2026, 10, 31, 5, 20, tzinfo=UTC)
        self.state = State(ENTITY, 'on', {'next_event': datetime(2026, 10, 31, 9, tzinfo=ZoneInfo('Europe/Tallinn'))})

    async def test_actual_schedule_advance_and_start_each_once_across_restarts(self):
        self.now -= timedelta(hours=1)
        await self.check()
        self.assertEqual(self.notifications, [])
        self.now += timedelta(hours=1)
        await self.check()
        self.assertEqual(len(self.notifications), 1)
        self.assertIn('50 minutes (Sat 07:20)', self.notifications[0][2]['message'])
        self.assertEqual(self.call.await_count, 1)  # next_event needs no calendar request
        await self.check()
        restarted = await self.new_manager()
        await self.check(restarted)
        self.assertEqual(len(self.notifications), 1)
        self.open_window()
        await self.check(restarted)
        self.assertEqual(len(self.notifications), 2)
        self.assertIn('window is open', self.notifications[-1][2]['message'])
        restarted_again = await self.new_manager()
        await self.check(restarted_again)
        self.assertEqual(len(self.notifications), 2)
        self.assertEqual(len(self.disk['delivered']), 2)

    async def test_missing_options_preserve_persistent_only_default(self):
        self.options.clear()
        await self.check()
        self.open_window()
        await self.check()
        self.call.assert_not_awaited()

    async def test_master_disabled_schedule_enabled_unknown_or_missing_never_notifies(self):
        self.options[CONF_NOTIFY_ON_SCHEDULE_WINDOW] = False
        await self.check()
        self.options[CONF_NOTIFY_ON_SCHEDULE_WINDOW] = True
        self.schedule_enabled = True
        await self.check()
        self.schedule_enabled = False
        for value in (None, State(ENTITY, 'unavailable'), State(ENTITY, 'unknown'), State(ENTITY, 'off')):
            self.state = value
            await self.check()
        self.call.assert_not_awaited()

    async def test_lead_zero_disables_advance_only_and_modern_notify_targets_work(self):
        self.options.update({CONF_SCHEDULE_REMINDER_MINUTES: 0,
            CONF_SCHEDULE_NOTIFICATION_SERVICE: 'notify.send_message',
            CONF_SCHEDULE_NOTIFICATION_TARGETS: ['notify.phone']})
        await self.check()
        self.assertEqual(self.notifications, [])
        self.open_window()
        await self.check()
        self.assertEqual(self.notifications[0][1], 'send_message')
        self.assertEqual(self.notifications[0][2]['entity_id'], ['notify.phone'])
        self.assertNotIn('target', self.notifications[0][2])

    async def test_legacy_targets_are_service_data(self):
        self.options[CONF_SCHEDULE_NOTIFICATION_TARGETS] = ['device_target']
        await self.check()
        self.assertEqual(self.notifications[0][2]['target'], ['device_target'])

    async def test_off_next_event_respects_dst_offset_and_schedule_edits(self):
        self.now = datetime(2026, 3, 29, 0, 45, tzinfo=UTC)
        self.state = State(ENTITY, 'off', {'next_event': '2026-03-29T04:30:00+03:00'})
        await self.check()
        self.assertIn('45 minutes (Sun 04:30)', self.notifications[0][2]['message'])
        self.state = State(ENTITY, 'off', {'next_event': '2026-03-29T04:40:00+03:00'})
        await self.check()
        self.assertEqual(len(self.notifications), 2)
        self.assertIn('55 minutes (Sun 04:40)', self.notifications[1][2]['message'])

    async def test_contiguous_native_blocks_do_not_repeat_start_notice(self):
        self.definition = {'saturday': [{'from': time(7), 'to': time(8)}, {'from': time(8), 'to': time(9)}]}
        self.open_window()
        self.state = State(ENTITY, 'on', {'next_event': '2026-10-31T08:00:00+02:00'})
        await self.check()
        self.now = datetime(2026, 10, 31, 6, 1, tzinfo=UTC)
        self.state = State(ENTITY, 'on', {'next_event': '2026-10-31T09:00:00+02:00'})
        await self.check()
        self.assertEqual(len(self.notifications), 1)

    async def test_extending_current_window_does_not_repeat_start_reminder(self):
        self.open_window()
        await self.check()
        self.definition['saturday'][0]['to'] = time(10)
        self.state = State(ENTITY, 'on', {'next_event': '2026-10-31T10:00:00+02:00'})
        await self.check()
        self.assertEqual(len(self.notifications), 1)

    async def test_next_day_has_an_independent_receipt(self):
        await self.check()
        self.now += timedelta(days=1)
        self.state = State(ENTITY, 'off', {'next_event': '2026-11-01T07:20:00+02:00'})
        await self.check()
        self.assertEqual(len(self.notifications), 2)

    async def test_restart_inside_window_sends_only_missed_start_notice(self):
        self.open_window()
        self.now += timedelta(minutes=25)
        await self.check()
        self.assertEqual(len(self.notifications), 1)
        self.assertIn('window is open', self.notifications[0][2]['message'])

    async def test_definite_service_failure_retries_after_backoff_without_charge_effect(self):
        self.call.side_effect = RuntimeError('notify provider unavailable')
        with self.assertLogs('custom_components.smartevse_dual_charger', level='ERROR'):
            await self.check()
        self.assertEqual(self.disk['delivered'], [])
        await self.check()
        self.assertEqual(self.call.await_count, 1)
        self.now += timedelta(minutes=5)
        self.call.side_effect = None
        await self.check()
        self.assertEqual(self.call.await_count, 2)
        self.assertEqual(len(self.disk['delivered']), 1)
        restarted = await self.new_manager()
        await self.check(restarted)
        self.assertEqual(self.call.await_count, 2)

    async def test_failed_advance_is_not_delivered_after_window_opens(self):
        real_call = self.call.side_effect
        self.call.side_effect = RuntimeError('notify failed')
        with self.assertLogs('custom_components.smartevse_dual_charger', level='ERROR'):
            await self.check()
        self.call.side_effect = real_call
        self.open_window()
        await self.check()
        self.assertEqual(len(self.notifications), 1)
        self.assertIn('window is open', self.notifications[0][2]['message'])

    async def test_timeout_claim_survives_restart_and_does_not_duplicate_uncertain_send(self):
        self.call.side_effect = TimeoutError()
        with self.assertLogs('custom_components.smartevse_dual_charger', level='WARNING'):
            await self.check()
        restarted = await self.new_manager()
        await self.check(restarted)
        self.assertEqual(self.call.await_count, 1)

    async def test_storage_failure_prevents_send_and_does_not_escape_to_charging(self):
        self.store.async_save.side_effect = OSError('full')
        with self.assertLogs('custom_components.smartevse_dual_charger', level='ERROR'):
            await self.check()
        self.call.assert_not_awaited()
        self.assertEqual(self.manager._data['delivered'], [])

    async def test_malformed_storage_is_preserved_and_phone_reminders_stay_off(self):
        self.disk = {'delivered': 'invalid'}
        with self.assertLogs('custom_components.smartevse_dual_charger', level='ERROR'):
            restarted = await self.new_manager()
        await self.check(restarted)
        self.store.async_save.assert_not_awaited()
        self.call.assert_not_awaited()
        self.assertEqual(self.disk, {'delivered': 'invalid'})

    async def test_enabling_schedule_during_disk_write_suppresses_stale_notice(self):
        real_save = self.store.async_save.side_effect
        async def enable(data):
            await real_save(data)
            self.schedule_enabled = True
        self.store.async_save.side_effect = enable
        await self.check()
        self.call.assert_not_awaited()
        self.assertEqual(self.disk['delivered'], [])

    async def test_delivery_is_independent_and_shutdown_cancels_it_without_duplicate(self):
        started = asyncio.Event()
        finish = asyncio.Event()
        async def slow(*args, **kwargs):
            started.set()
            await finish.wait()
        self.call.side_effect = slow
        self.manager.async_check()  # synchronous return; charging work can continue
        await asyncio.wait_for(started.wait(), 1)
        self.assertFalse(self.manager._task.done())
        self.manager.async_check()
        self.assertEqual(self.call.await_count, 1)
        await self.manager.async_shutdown()
        restarted = await self.new_manager()
        await self.check(restarted)
        self.assertEqual(self.call.await_count, 1)
        self.assertEqual(len(self.disk['delivered']), 1)


class ControllerReminderTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_persistent_warning_kept_and_phone_check_only_scheduled(self):
        from custom_components.smartevse_dual_charger.controller import SmartEVSEDualChargerController
        controller = SmartEVSEDualChargerController.__new__(SmartEVSEDualChargerController)
        controller.hass = object()
        controller._mutable = {'last_schedule_window_active': False}
        controller._options = {}
        controller._schedule_reminders = MagicMock()
        with patch('custom_components.smartevse_dual_charger.controller.persistent_notification.async_create') as create, \
             patch('custom_components.smartevse_dual_charger.controller.persistent_notification.async_dismiss') as dismiss:
            await controller._handle_schedule_notification(schedule_window_active=True, schedule_enabled=False)
            create.assert_called_once()
            dismiss.assert_not_called()
            controller._schedule_reminders.async_check.assert_called_once()
            self.assertTrue(controller._mutable['last_notification'])
            controller._options[CONF_NOTIFY_ON_SCHEDULE_WINDOW] = False
            await controller._handle_schedule_notification(schedule_window_active=True, schedule_enabled=False)
            dismiss.assert_called_once()
            create.assert_called_once()


class NativeHASmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_schedule_service_storage_and_background_lifecycle(self):
        # Actual HA entities, service registry, background task API and private
        # Store writes; the only fake is the outbound notification receiver.
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            hass.state = CoreState.running
            device_registry.async_setup(hass)
            for registry in (floor_registry, area_registry, label_registry, device_registry, entity_registry):
                await registry.async_load(hass)
            component = EntityComponent(logging.getLogger(__name__), 'schedule', hass)
            definition = {day: [{'from': '00:00:00', 'to': '24:00:00'}]
                for day in ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')}
            entity = Schedule.from_yaml({'id': 'charge_schedule', 'name': 'Charging', **definition})
            await component.async_add_entities([entity])
            component.async_register_entity_service('get_schedule', {}, async_get_schedule_service,
                supports_response=SupportsResponse.ONLY)
            deliveries = []
            async def receive(call):
                deliveries.append(dict(call.data))
            hass.services.async_register('notify', 'test_phone', receive)
            options = {CONF_SCHEDULE_NOTIFICATION_SERVICE: 'notify.test_phone'}
            first = ScheduleReminders(hass, 'native_test', ENTITY, options, lambda: False)
            second = None
            try:
                await first.async_initialize()
                first.async_check()
                await first._task
                self.assertEqual(len(deliveries), 1)
                self.assertEqual(deliveries[0]['title'], 'EV charge schedule')
                await first.async_shutdown()
                second = ScheduleReminders(hass, 'native_test', ENTITY, options, lambda: False)
                await second.async_initialize()
                second.async_check()
                await second._task
                self.assertEqual(len(deliveries), 1)
                self.assertEqual(len(second._data['delivered']), 1)
            finally:
                await first.async_shutdown()
                if second is not None:
                    await second.async_shutdown()
                await entity.async_remove()
                await hass.async_stop()


if __name__ == '__main__':
    unittest.main()
