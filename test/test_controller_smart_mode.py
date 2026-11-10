"""Smart-mode controller fixes: /settings parsing, sequential hand-off, pause guard, mapping.

The controller is built with __new__ (no Home Assistant instance); charger I/O is
mocked with AsyncMock and entity states are native homeassistant.core.State objects.
"""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State

from custom_components.smartevse_dual_charger.const import (
    CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY,
    CONF_MAINS_L1_ENTITY,
    CONF_MAINS_L2_ENTITY,
    CONF_MAINS_L3_ENTITY,
    CONF_PRICE_SENSOR_ENTITY,
    CONF_PUSH_CURRENTS,
    CONF_PUSH_WLED,
    CONF_SCHEDULE_ENTITY,
    CONF_SMARTEVSE_1_BASE_URL,
    CONF_SMARTEVSE_2_BASE_URL,
    CONF_VEHICLE_1_BATTERY_ENTITY,
    CONF_VEHICLE_1_CONNECTION_STATUS_ENTITY,
    CONF_VEHICLE_2_BATTERY_ENTITY,
    CONF_VEHICLE_2_CONNECTION_STATUS_ENTITY,
)
from custom_components.smartevse_dual_charger.controller import (
    MUTABLE_DEFAULTS,
    SmartEVSEDualChargerController,
    SmartEVSEStatus,
)

CONTROLLER = 'custom_components.smartevse_dual_charger.controller'
URL_1 = 'http://evse1.invalid'
URL_2 = 'http://evse2.invalid'
SETTINGS_1 = f'{URL_1}/settings'
SETTINGS_2 = f'{URL_2}/settings'
XC40_CONNECTION = 'sensor.volvo_xc40_charging_connection_status'
XC40_CHARGING = 'sensor.volvo_xc40_charging_status'
EX30_CONNECTION = 'sensor.volvo_ex30_charging_connection_status'
EX30_CHARGING = 'sensor.volvo_ex30_charging_status'
EV_POWER = 'sensor.ev_power_total'
T0 = datetime(2026, 11, 9, 16, 0, tzinfo=UTC)
PAUSE = 'Charging Stopped - No Power Available'


def make_controller(states):
    """Return a controller with mutable defaults and native-state lookups only."""
    controller = SmartEVSEDualChargerController.__new__(SmartEVSEDualChargerController)
    controller._entry_data = {
        'entry_id': 'test',
        CONF_SMARTEVSE_1_BASE_URL: URL_1,
        CONF_SMARTEVSE_2_BASE_URL: URL_2,
        CONF_VEHICLE_1_CONNECTION_STATUS_ENTITY: XC40_CONNECTION,
        CONF_VEHICLE_2_CONNECTION_STATUS_ENTITY: EX30_CONNECTION,
        CONF_VEHICLE_1_BATTERY_ENTITY: 'sensor.volvo_xc40_battery',
        CONF_VEHICLE_2_BATTERY_ENTITY: 'sensor.volvo_ex30_battery',
        CONF_EV_METER_IMPORT_ACTIVE_POWER_ENTITY: EV_POWER,
    }
    controller._options = {}
    controller._mutable = deepcopy(MUTABLE_DEFAULTS)
    controller.hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    return controller


def put(states, entity_id, value, at):
    """Store a native State whose change/report/update times are all `at` (as Volvo reports)."""
    states[entity_id] = State(entity_id, value, {}, last_changed=at, last_reported=at, last_updated=at)


def status(key, *, connected=True, state=None, mode='Off', available=True, raw_state=None, override=0.0):
    """Build a polled SmartEVSE status snapshot."""
    if not available:
        return SmartEVSEStatus(
            key=key, base_url=URL_1 if key == 'smartevse_1' else URL_2, available=False, connected=False,
            plug_state='Unavailable', state='Unavailable', mode='Unavailable', charge_current=0.0,
            max_current=0.0, override_current=0.0, error='API unavailable',
        )
    state = state or ('Connected to EV' if connected else 'Disconnected')
    return SmartEVSEStatus(
        key=key, base_url=URL_1 if key == 'smartevse_1' else URL_2, available=True, connected=connected,
        plug_state='Connected' if connected else 'Disconnected', state=state, mode=mode,
        charge_current=16.0, max_current=16.0, override_current=override, error='None',
        max_mains=16.0, min_current=6.0, mode_id={'Off': 0, 'Smart': 3}.get(mode),
        raw_state=state if raw_state is None else raw_state,
    )


def settings_payload(*, connected=True, state='Connected to EV', mode='OFF', mode_id=0, **settings):
    """Return a GET /settings payload shaped like SmartEVSE 3.5 firmware v3.11.x."""
    return {
        'version': 'v3.11.3',
        'mode': mode,
        'mode_id': mode_id,
        'car_connected': connected,
        'evse': {'state': state, 'error': 'None', 'connected': connected, 'nrofphases': 3},
        'settings': {
            'charge_current': 160, 'override_current': 0, 'current_min': 6, 'current_max': 16,
            'current_main': 16, 'current_max_circuit': 16, **settings,
        },
    }


class SettingsParsingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = make_controller({})
        self.c._async_get_json = AsyncMock()

    async def fetch(self, payload):
        self.c._async_get_json.return_value = payload
        result = await self.c._async_fetch_status('smartevse_1', URL_1)
        self.c._async_get_json.assert_awaited_with(SETTINGS_1)
        return result

    async def test_smart_mode_limits_mode_id_and_raw_state_are_parsed(self):
        result = await self.fetch(settings_payload(state=PAUSE, mode='SMART', mode_id=3, current_main=17))
        self.assertEqual(result.max_mains, 17.0)
        self.assertEqual(result.min_current, 6.0)
        self.assertEqual(result.mode_id, 3)
        self.assertEqual(result.raw_state, PAUSE)
        # Existing fields keep their semantics (deci-amps, normalized mode).
        self.assertEqual(result.state, PAUSE)
        self.assertEqual(result.mode, 'Smart')
        self.assertEqual(result.charge_current, 16.0)
        self.assertEqual(result.max_current, 16.0)
        self.assertEqual(result.override_current, 0.0)

    async def test_raw_state_is_unmodified_when_the_plug_state_is_normalized(self):
        result = await self.fetch(settings_payload(connected=False, state='Charging Stopped'))
        self.assertEqual(result.state, 'Disconnected')
        self.assertEqual(result.raw_state, 'Charging Stopped')

    async def test_missing_or_invalid_values_become_none(self):
        payload = settings_payload(mode='OFF', mode_id='x', current_main=None, current_min='six')
        payload['evse'].pop('state')
        result = await self.fetch(payload)
        self.assertIsNone(result.max_mains)
        self.assertIsNone(result.min_current)
        self.assertIsNone(result.mode_id)
        self.assertEqual(result.raw_state, '')
        self.assertEqual(result.state, 'Connected to EV')
        for bad in (0, -16, float('nan'), True):
            with self.subTest(current_main=bad):
                self.assertIsNone((await self.fetch(settings_payload(current_main=bad))).max_mains)
        self.assertEqual((await self.fetch(settings_payload(mode_id='4'))).mode_id, 4)

    async def test_unavailable_charger_uses_defaults(self):
        result = await self.fetch(None)
        self.assertFalse(result.available)
        self.assertIsNone(result.max_mains)
        self.assertIsNone(result.min_current)
        self.assertIsNone(result.mode_id)
        self.assertEqual(result.raw_state, '')

    def test_existing_constructor_without_new_fields_still_works(self):
        result = SmartEVSEStatus(
            key='smartevse_1', base_url=URL_1, available=True, connected=True, plug_state='Connected',
            state='Charging', mode='Smart', charge_current=16, max_current=16, override_current=0, error='None',
        )
        self.assertEqual((result.max_mains, result.min_current, result.mode_id, result.raw_state), (None, None, None, ''))


class SequentialHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = make_controller({})
        self.events = []
        self.post_results = {}

        async def post(url, *, params=None, json_payload=None):
            self.events.append(('start', url, dict(params or {})))
            await asyncio.sleep(0)
            self.events.append(('end', url, dict(params or {})))
            return self.post_results.get(url, True)

        self.c._async_post = AsyncMock(side_effect=post)

    async def cycle(self, at, s1, s2, *, active='smartevse_2', allowed=True, power=None):
        self.events.clear()
        await self.c._apply_modes(
            smartevse_1=s1, smartevse_2=s2, active_smartevse=active, charge_allowed=allowed,
            now=T0 + timedelta(seconds=at), ev_meter_power=power,
        )
        return [(url, params) for kind, url, params in self.events if kind == 'start']

    async def test_outgoing_off_first_then_incoming_waits_for_low_ev_power(self):
        writes = await self.cycle(0, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'), power=11000)
        self.assertEqual(writes, [(SETTINGS_1, {'mode': 0})])
        self.assertEqual(self.c._mutable['handoff_settle_from'], 'smartevse_1')
        self.assertEqual(self.c._mutable['handoff_settle_since'], T0.isoformat())
        # Polled Off, but the outgoing car is still ramping down: incoming stays Off.
        self.assertEqual(await self.cycle(10, status('smartevse_1'), status('smartevse_2'), power=9000), [])
        # EV meter below 1 kW before the 30 s limit: incoming may start now.
        self.assertEqual(await self.cycle(20, status('smartevse_1'), status('smartevse_2'), power=600),
            [(SETTINGS_2, {'mode': 3})])
        self.assertIsNone(self.c._mutable['handoff_settle_from'])
        self.assertIsNone(self.c._mutable['handoff_settle_since'])

    async def test_incoming_starts_30_seconds_after_the_off_write_without_power_data(self):
        await self.cycle(0, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'), power=11000)
        self.assertEqual(await self.cycle(10, status('smartevse_1'), status('smartevse_2'), power=None), [])
        self.assertEqual(await self.cycle(29, status('smartevse_1'), status('smartevse_2'), power=8000), [])
        self.assertEqual(await self.cycle(30, status('smartevse_1'), status('smartevse_2'), power=8000),
            [(SETTINGS_2, {'mode': 3})])

    async def test_off_write_is_awaited_before_anything_is_written_to_the_incoming_charger(self):
        # Outgoing needs a write and the incoming may start in the same cycle:
        # the outgoing POST completes before the incoming POST begins.
        await self.cycle(0, status('smartevse_1', override=10.0), status('smartevse_2', override=10.0), power=0)
        self.assertEqual(self.events, [
            ('start', SETTINGS_1, {'disable_override_current': 1}), ('end', SETTINGS_1, {'disable_override_current': 1}),
            ('start', SETTINGS_2, {'mode': 3, 'disable_override_current': 1}),
            ('end', SETTINGS_2, {'mode': 3, 'disable_override_current': 1}),
        ])
        # Incoming in a user-set drawing mode while the outgoing is still Smart:
        # outgoing Off first, then the incoming is held Off (never sent Smart).
        await self.cycle(60, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2', mode='Normal'))
        self.assertEqual(self.events, [
            ('start', SETTINGS_1, {'mode': 0}), ('end', SETTINGS_1, {'mode': 0}),
            ('start', SETTINGS_2, {'mode': 0}), ('end', SETTINGS_2, {'mode': 0}),
        ])

    async def test_incoming_already_in_smart_is_not_restarted_while_the_other_is_switched_off(self):
        # Both polled in Smart (the outgoing reappeared after an outage): only the outgoing is written.
        writes = await self.cycle(0, status('smartevse_1', mode='Smart'), status('smartevse_2', state='Charging', mode='Smart'))
        self.assertEqual(writes, [(SETTINGS_1, {'mode': 0})])
        self.assertEqual(await self.cycle(10, status('smartevse_1'), status('smartevse_2', state='Charging', mode='Smart'), power=9000), [])

    async def test_failed_off_post_blocks_the_incoming_charger(self):
        self.post_results[SETTINGS_1] = False
        writes = await self.cycle(0, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'), power=11000)
        self.assertEqual(writes, [(SETTINGS_1, {'mode': 0})])
        # The charger ignored the write: it is still Smart, so retry Off and keep holding.
        self.post_results[SETTINGS_1] = True
        writes = await self.cycle(10, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'), power=11000)
        self.assertEqual(writes, [(SETTINGS_1, {'mode': 0})])
        self.assertEqual(await self.cycle(20, status('smartevse_1'), status('smartevse_2'), power=400),
            [(SETTINGS_2, {'mode': 3})])

    async def test_failed_off_post_that_reached_the_charger_still_waits_for_settle(self):
        self.post_results[SETTINGS_1] = False
        await self.cycle(0, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'), power=11000)
        self.assertEqual(await self.cycle(10, status('smartevse_1'), status('smartevse_2'), power=10000), [])
        self.assertEqual(await self.cycle(30, status('smartevse_1'), status('smartevse_2'), power=10000),
            [(SETTINGS_2, {'mode': 3})])

    async def test_unchanged_modes_are_never_posted(self):
        cases = (
            ('smartevse_1', True, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2')),
            ('smartevse_2', True, status('smartevse_1'), status('smartevse_2', state='Charging', mode='Smart')),
            ('none', True, status('smartevse_1'), status('smartevse_2')),
            ('smartevse_1', False, status('smartevse_1'), status('smartevse_2')),
        )
        for active, allowed, s1, s2 in cases:
            with self.subTest(active=active, allowed=allowed):
                self.assertEqual(await self.cycle(0, s1, s2, active=active, allowed=allowed, power=9000), [])
        # Only the override is cleared on a charger already in the desired mode.
        writes = await self.cycle(0, status('smartevse_1'), status('smartevse_2', mode='Smart', override=10.0))
        self.assertEqual(writes, [(SETTINGS_2, {'disable_override_current': 1})])

    async def test_switching_back_to_the_settling_charger_does_not_wait(self):
        await self.cycle(0, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'), power=11000)
        writes = await self.cycle(10, status('smartevse_1'), status('smartevse_2'), active='smartevse_1', power=9000)
        self.assertEqual(writes, [(SETTINGS_1, {'mode': 3})])
        self.assertIsNone(self.c._mutable['handoff_settle_from'])

    async def test_stopping_both_records_the_settle_for_the_next_start(self):
        await self.cycle(0, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'), active='none', power=11000)
        self.assertEqual(self.c._mutable['handoff_settle_from'], 'smartevse_1')
        self.assertEqual(await self.cycle(10, status('smartevse_1'), status('smartevse_2'), power=9000), [])
        self.assertEqual(await self.cycle(30, status('smartevse_1'), status('smartevse_2'), power=9000),
            [(SETTINGS_2, {'mode': 3})])

    async def test_unavailable_outgoing_charger_last_seen_in_smart_holds_the_incoming(self):
        # SmartEVSE 1 was last polled in Smart, then dropped off the API.
        await self.cycle(-10, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'), active='smartevse_1')
        offline = status('smartevse_1', available=False)
        self.assertEqual(await self.cycle(0, offline, status('smartevse_2'), power=11000), [])
        self.assertEqual(await self.cycle(59, offline, status('smartevse_2'), power=11000), [])
        self.assertEqual(await self.cycle(60, offline, status('smartevse_2'), power=11000), [(SETTINGS_2, {'mode': 3})])
        # Released once; neither re-held nor re-posted on the following cycles.
        self.assertEqual(await self.cycle(70, offline, status('smartevse_2', state='Charging', mode='Smart'), power=11000), [])

    async def test_unavailable_hold_scales_with_the_update_interval(self):
        self.c._mutable['update_interval'] = 45
        self.c._mutable['smartevse_1_last_observed_mode'] = 'Smart'
        offline = status('smartevse_1', available=False)
        self.assertEqual(await self.cycle(0, offline, status('smartevse_2')), [])
        self.assertEqual(await self.cycle(89, offline, status('smartevse_2')), [])
        self.assertEqual(await self.cycle(90, offline, status('smartevse_2')), [(SETTINGS_2, {'mode': 3})])

    async def test_unavailable_outgoing_charger_last_seen_off_does_not_hold(self):
        self.c._mutable['smartevse_1_last_observed_mode'] = 'Off'
        writes = await self.cycle(0, status('smartevse_1', available=False), status('smartevse_2'), power=11000)
        self.assertEqual(writes, [(SETTINGS_2, {'mode': 3})])

    async def test_start_from_idle_is_immediate(self):
        self.assertEqual(await self.cycle(0, status('smartevse_1'), status('smartevse_2'), power=0),
            [(SETTINGS_2, {'mode': 3})])
        self.assertEqual(self.c._mutable['smartevse_1_last_observed_mode'], 'Off')


class DutyCycleGraceTests(unittest.TestCase):
    def test_handoff_grace_includes_the_settle_time(self):
        c = make_controller({})
        statuses = {
            'smartevse_1': status('smartevse_1'),
            'smartevse_2': status('smartevse_2'),
        }
        for elapsed, expected in ((149, 'smartevse_2'), (150, 'smartevse_1')):
            with self.subTest(elapsed=elapsed):
                c._mutable = deepcopy(MUTABLE_DEFAULTS)
                c._mutable.update({
                    'active_smartevse': 'smartevse_2',
                    'active_smartevse_since': T0.isoformat(),
                    'handoff_target': 'smartevse_2',
                    'handoff_started_at': T0.isoformat(),
                })
                active, _since, _remaining, reason = c._resolve_active_smartevse(
                    now=T0 + timedelta(seconds=elapsed), **statuses)
                self.assertEqual((active, reason), (expected, ''))


class SmartPauseGuardTests(unittest.TestCase):
    def setUp(self):
        self.c = make_controller({})
        self.c._mutable.update({
            'active_smartevse': 'smartevse_1',
            'smartevse_1_active_seen_charging': True,
            'smartevse_1_seen_charging': True,
        })

    def observe(self, seconds, state, *, mode='Smart'):
        now = T0 + timedelta(seconds=seconds)
        current = status('smartevse_1', state=state, mode=mode)
        self.c._track_pause_state(current, now=now)
        self.c._update_session_tracking(current, now=now, ev_meter_power=3.0)
        return current

    def test_pause_and_the_90_seconds_after_it_never_complete_a_session(self):
        for second in range(0, 600, 10):
            self.observe(second, PAUSE)
            self.assertFalse(self.c._mutable['smartevse_1_session_complete'])
        self.assertEqual(self.c._mutable['smartevse_1_last_pause_at'], (T0 + timedelta(seconds=590)).isoformat())
        # Firmware ChargeDelay: plain "Charging Stopped", then "Connected to EV".
        for second in range(600, 681, 10):
            self.observe(second, 'Charging Stopped' if second < 660 else 'Connected to EV')
            self.assertIsNone(self.c._mutable['smartevse_1_non_charging_since'])
        # More than 90 s after the last pause the normal completion grace applies.
        self.observe(690, 'Connected to EV')
        self.assertEqual(self.c._mutable['smartevse_1_non_charging_since'], (T0 + timedelta(seconds=690)).isoformat())
        self.observe(860, 'Connected to EV')
        self.assertFalse(self.c._mutable['smartevse_1_session_complete'])
        self.observe(870, 'Connected to EV')
        self.assertTrue(self.c._mutable['smartevse_1_session_complete'])

    def test_guard_window_and_markers(self):
        for state in (PAUSE, 'Stop Charging - No Power Available', 'Communication Error', 'EV Meter Comm Error'):
            with self.subTest(state=state):
                self.c._mutable['smartevse_1_last_pause_at'] = None
                current = self.observe(0, state, mode='Off')  # recorded in every mode
                self.assertEqual(self.c._mutable['smartevse_1_last_pause_at'], T0.isoformat())
                self.assertTrue(self.c._status_blocks_completion_detection(current, now=T0))
        stopped = status('smartevse_1', state='Charging Stopped', mode='Smart')
        self.assertTrue(self.c._status_blocks_completion_detection(stopped, now=T0 + timedelta(seconds=90)))
        self.assertFalse(self.c._status_blocks_completion_detection(stopped, now=T0 + timedelta(seconds=91)))
        # Raw firmware state is checked even if the display state differs.
        self.assertTrue(self.c._status_blocks_completion_detection(
            status('smartevse_1', state='Charging Stopped', raw_state=PAUSE), now=T0 + timedelta(hours=1)))

    def test_unavailable_charger_does_not_record_a_pause(self):
        self.c._track_pause_state(status('smartevse_1', available=False), now=T0)
        self.assertIsNone(self.c._mutable['smartevse_1_last_pause_at'])


class VehicleMappingTests(unittest.TestCase):
    def setUp(self):
        self.states = {}
        self.c = make_controller(self.states)

    def cycle(self, at, s1, s2):
        now = at if isinstance(at, datetime) else T0 + timedelta(seconds=at)
        with patch(f'{CONTROLLER}.dt_util.utcnow', return_value=now):
            self.c._update_connected_vehicle_mapping(now=now, smartevse_1=s1, smartevse_2=s2)
        return self.c._mutable['smartevse_1_connected_ev'], self.c._mutable['smartevse_2_connected_ev']

    def both_plugged_within_a_minute(self):
        """XC40 → SmartEVSE 1 at +10 s, EX30 → SmartEVSE 2 at +60 s; cloud reports at +90 s / +150 s."""
        put(self.states, XC40_CONNECTION, 'disconnected', T0 - timedelta(minutes=5))
        put(self.states, EX30_CONNECTION, 'disconnected', T0 - timedelta(minutes=5))
        self.cycle(0, status('smartevse_1', connected=False), status('smartevse_2', connected=False))
        self.cycle(10, status('smartevse_1'), status('smartevse_2', connected=False))
        self.cycle(60, status('smartevse_1'), status('smartevse_2'))
        put(self.states, XC40_CONNECTION, 'connected', T0 + timedelta(seconds=90))
        self.cycle(90, status('smartevse_1'), status('smartevse_2'))
        put(self.states, EX30_CONNECTION, 'connected', T0 + timedelta(seconds=150))
        return self.cycle(150, status('smartevse_1'), status('smartevse_2'))

    def test_near_simultaneous_plug_leaves_both_unknown(self):
        # Before the fix the newest-plug rule mapped this as ('vehicle_2', 'vehicle_1').
        self.assertEqual(self.both_plugged_within_a_minute(), ('unknown', 'unknown'))

    def test_plugs_three_minutes_apart_still_use_the_newest_plug(self):
        put(self.states, XC40_CONNECTION, 'disconnected', T0 - timedelta(minutes=5))
        put(self.states, EX30_CONNECTION, 'disconnected', T0 - timedelta(minutes=5))
        self.cycle(0, status('smartevse_1', connected=False), status('smartevse_2', connected=False))
        self.cycle(10, status('smartevse_1'), status('smartevse_2', connected=False))
        self.cycle(190, status('smartevse_1'), status('smartevse_2'))
        put(self.states, EX30_CONNECTION, 'connected', T0 + timedelta(seconds=220))
        self.assertEqual(self.cycle(220, status('smartevse_1'), status('smartevse_2')), ('unknown', 'vehicle_2'))

    def test_stale_connected_and_fresh_charging_correct_and_eliminate(self):
        self.both_plugged_within_a_minute()
        later = T0 + timedelta(hours=6)
        # Connection sensors are now hours old; the XC40 reports a fresh "charging".
        put(self.states, XC40_CHARGING, 'charging', later + timedelta(seconds=80))
        mapping = self.cycle(later + timedelta(seconds=90), status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'))
        self.assertEqual(mapping, ('vehicle_1', 'vehicle_2'))

    def test_correction_fixes_a_swapped_mapping(self):
        put(self.states, XC40_CONNECTION, 'connected', T0 - timedelta(hours=6))
        put(self.states, EX30_CONNECTION, 'connected', T0 - timedelta(hours=6))
        self.c._mutable.update({
            'smartevse_1_connected_ev': 'vehicle_2', 'smartevse_2_connected_ev': 'vehicle_1',
            'smartevse_1_last_connected': True, 'smartevse_2_last_connected': True,
            'smartevse_1_session_complete': True, 'smartevse_2_session_complete': True,
        })
        put(self.states, XC40_CHARGING, 'charging', T0 - timedelta(seconds=30))
        mapping = self.cycle(0, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'))
        self.assertEqual(mapping, ('vehicle_1', 'vehicle_2'))
        # Both outlets changed vehicle, so both lose their session tracking.
        self.assertFalse(self.c._mutable['smartevse_1_session_complete'])
        self.assertFalse(self.c._mutable['smartevse_2_session_complete'])

    def test_correction_clears_session_tracking_only_where_the_vehicle_changed(self):
        put(self.states, XC40_CONNECTION, 'connected', T0 - timedelta(hours=6))
        put(self.states, EX30_CONNECTION, 'connected', T0 - timedelta(hours=6))
        self.c._mutable.update({
            'smartevse_1_connected_ev': 'unknown', 'smartevse_2_connected_ev': 'vehicle_2',
            'smartevse_1_last_connected': True, 'smartevse_2_last_connected': True,
            'smartevse_1_low_power_since': T0.isoformat(), 'smartevse_1_last_vehicle_charging_state': 'idle',
            'smartevse_2_session_complete': True, 'smartevse_2_seen_charging': True,
            'smartevse_2_last_vehicle_charging_state': 'done',
        })
        put(self.states, XC40_CHARGING, 'charging', T0 - timedelta(seconds=30))
        mapping = self.cycle(0, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'))
        self.assertEqual(mapping, ('vehicle_1', 'vehicle_2'))
        self.assertIsNone(self.c._mutable['smartevse_1_low_power_since'])
        self.assertIsNone(self.c._mutable['smartevse_1_last_vehicle_charging_state'])
        self.assertTrue(self.c._mutable['smartevse_2_session_complete'])
        self.assertTrue(self.c._mutable['smartevse_2_seen_charging'])
        self.assertEqual(self.c._mutable['smartevse_2_last_vehicle_charging_state'], 'done')

    def mapped_hand_off(self):
        """XC40 on SmartEVSE 1, EX30 on SmartEVSE 2, mapped right, connection reports hours old."""
        put(self.states, XC40_CONNECTION, 'connected', T0 - timedelta(hours=6))
        put(self.states, EX30_CONNECTION, 'connected', T0 - timedelta(hours=6))
        put(self.states, EX30_CHARGING, 'idle', T0 - timedelta(hours=5))
        self.c._mutable.update({
            'smartevse_1_connected_ev': 'vehicle_1', 'smartevse_2_connected_ev': 'vehicle_2',
            'smartevse_1_last_connected': True, 'smartevse_2_last_connected': True,
            'smartevse_1_session_complete': True,
        })

    def test_hand_off_does_not_read_the_outgoing_cars_late_report_as_evidence(self):
        # Review round 3: SmartEVSE 1 charged the XC40 until +0 s; its Volvo status turned
        # "charging" 4 min before the hand-off and still says so. SmartEVSE 2 charges the
        # EX30 from +40 s, whose own report is still "idle". The mapping must not change.
        self.mapped_hand_off()
        self.cycle(-360, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'))
        put(self.states, XC40_CHARGING, 'charging', T0 - timedelta(minutes=4))
        self.cycle(-10, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'))
        self.cycle(0, status('smartevse_1'), status('smartevse_2'))
        for second in range(40, 400, 10):
            mapping = self.cycle(second, status('smartevse_1'), status('smartevse_2', state='Charging', mode='Smart'))
            self.assertEqual(mapping, ('vehicle_1', 'vehicle_2'), second)
        self.assertTrue(self.c._mutable['smartevse_1_session_complete'])

    def test_short_block_report_after_the_stop_is_still_not_evidence(self):
        # The XC40 charged only 60 s; its "charging" report arrives 90 s after the stop,
        # while SmartEVSE 2 already charges the EX30. Still the outgoing car's report,
        # and the XC40 reports "not charging" a few minutes later.
        self.mapped_hand_off()
        self.cycle(-60, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'))
        self.cycle(0, status('smartevse_1'), status('smartevse_2'))
        self.cycle(40, status('smartevse_1'), status('smartevse_2', state='Charging', mode='Smart'))
        put(self.states, XC40_CHARGING, 'charging', T0 + timedelta(seconds=90))
        for second in range(90, 900, 10):
            if second == 290:
                put(self.states, XC40_CHARGING, 'idle', T0 + timedelta(seconds=290))
            mapping = self.cycle(second, status('smartevse_1'), status('smartevse_2', state='Charging', mode='Smart'))
            self.assertEqual(mapping, ('vehicle_1', 'vehicle_2'), second)

    def test_report_soon_after_a_hand_off_counts_once_it_stays_charging(self):
        # Wrong mapping (both cars swapped) and a hand-off: the car really on SmartEVSE 2
        # reports "charging" 2 min after the other charger stopped. It is not evidence at
        # once, but a car still charging 8 min after the other charger stopped is here.
        put(self.states, XC40_CONNECTION, 'connected', T0 - timedelta(hours=6))
        put(self.states, EX30_CONNECTION, 'connected', T0 - timedelta(hours=6))
        self.c._mutable.update({
            'smartevse_1_connected_ev': 'vehicle_1', 'smartevse_2_connected_ev': 'vehicle_2',
            'smartevse_1_last_connected': True, 'smartevse_2_last_connected': True,
        })
        self.cycle(-60, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'))
        self.cycle(0, status('smartevse_1'), status('smartevse_2'))
        self.cycle(30, status('smartevse_1'), status('smartevse_2', state='Charging', mode='Smart'))
        put(self.states, XC40_CHARGING, 'charging', T0 + timedelta(seconds=150))
        self.assertEqual(self.cycle(160, status('smartevse_1'), status('smartevse_2', state='Charging', mode='Smart')),
                         ('vehicle_1', 'vehicle_2'))
        # SmartEVSE 1 was last seen charging at -60 s: settled 8 min later, at +420 s.
        self.assertEqual(self.cycle(410, status('smartevse_1'), status('smartevse_2', state='Charging', mode='Smart')),
                         ('vehicle_1', 'vehicle_2'))
        self.assertEqual(self.cycle(420, status('smartevse_1'), status('smartevse_2', state='Charging', mode='Smart')),
                         ('vehicle_2', 'vehicle_1'))

    def test_report_newer_than_the_run_and_the_other_charge_still_corrects(self):
        # A wrong mapping is still fixed by a report that clearly belongs to this run.
        put(self.states, XC40_CONNECTION, 'connected', T0 - timedelta(hours=6))
        put(self.states, EX30_CONNECTION, 'connected', T0 - timedelta(hours=6))
        self.c._mutable.update({
            'smartevse_1_connected_ev': 'vehicle_2', 'smartevse_2_connected_ev': 'vehicle_1',
            'smartevse_1_last_connected': True, 'smartevse_2_last_connected': True,
        })
        self.cycle(-600, status('smartevse_1'), status('smartevse_2', state='Charging', mode='Smart'))
        self.cycle(-590, status('smartevse_1'), status('smartevse_2'))
        self.cycle(0, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'))
        put(self.states, XC40_CHARGING, 'charging', T0 + timedelta(seconds=100))
        self.assertEqual(self.cycle(110, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2')),
                         ('vehicle_1', 'vehicle_2'))

    def test_pause_keeps_the_charging_run_and_a_blip_changes_nothing(self):
        self.cycle(0, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'))
        since = self.c._mutable['smartevse_1_charging_since']
        self.cycle(10, status('smartevse_1', state=PAUSE, mode='Smart'), status('smartevse_2'))
        self.cycle(20, status('smartevse_1', available=False), status('smartevse_2'))
        self.cycle(30, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'))
        self.assertEqual(self.c._mutable['smartevse_1_charging_since'], since)
        self.assertEqual(self.c._mutable['smartevse_1_last_charging_at'], (T0 + timedelta(seconds=30)).isoformat())
        self.cycle(40, status('smartevse_1'), status('smartevse_2'))
        self.assertIsNone(self.c._mutable['smartevse_1_charging_since'])

    def test_elimination_needs_the_other_car_last_known_connected(self):
        self.both_plugged_within_a_minute()
        put(self.states, EX30_CONNECTION, 'disconnected', T0 + timedelta(hours=1))
        later = T0 + timedelta(hours=6)
        put(self.states, XC40_CHARGING, 'charging', later)
        mapping = self.cycle(later + timedelta(seconds=10), status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'))
        self.assertEqual(mapping, ('vehicle_1', 'unknown'))

    def test_charging_report_from_a_car_known_disconnected_is_ignored(self):
        put(self.states, XC40_CONNECTION, 'disconnected', T0 - timedelta(hours=6))
        put(self.states, XC40_CHARGING, 'charging', T0 - timedelta(seconds=30))
        self.c._mutable.update({'smartevse_1_last_connected': True, 'smartevse_2_last_connected': True})
        mapping = self.cycle(0, status('smartevse_1', state='Charging', mode='Smart'), status('smartevse_2'))
        self.assertEqual(mapping, ('unknown', 'unknown'))

    def test_api_blip_keeps_mapping_and_plug_time(self):
        put(self.states, XC40_CONNECTION, 'connected', T0 - timedelta(hours=1))
        put(self.states, EX30_CONNECTION, 'connected', T0 - timedelta(hours=1))
        plugged_at = (T0 - timedelta(hours=1)).isoformat()
        self.c._mutable.update({
            'smartevse_1_connected_ev': 'vehicle_1', 'smartevse_2_connected_ev': 'vehicle_2',
            'smartevse_1_last_connected': True, 'smartevse_2_last_connected': True,
            'smartevse_2_last_plug_connected': plugged_at,
        })
        self.assertEqual(self.cycle(0, status('smartevse_1'), status('smartevse_2', available=False)), ('vehicle_1', 'vehicle_2'))
        self.assertTrue(self.c._mutable['smartevse_2_last_connected'])
        self.assertEqual(self.c._mutable['smartevse_2_last_plug_connected'], plugged_at)
        # Recovery is not a plug-connect event.
        self.assertEqual(self.cycle(10, status('smartevse_1'), status('smartevse_2')), ('vehicle_1', 'vehicle_2'))
        self.assertEqual(self.c._mutable['smartevse_2_last_plug_connected'], plugged_at)
        self.assertEqual(self.cycle(1800, status('smartevse_1'), status('smartevse_2')), ('vehicle_1', 'vehicle_2'))

    def test_observed_unplug_still_clears_the_outlet(self):
        put(self.states, EX30_CONNECTION, 'connected', T0 - timedelta(hours=1))
        self.c._mutable.update({
            'smartevse_2_connected_ev': 'vehicle_2', 'smartevse_2_last_connected': True,
            'smartevse_2_last_plug_connected': T0.isoformat(),
        })
        self.assertEqual(self.cycle(0, status('smartevse_1', connected=False), status('smartevse_2', connected=False)), ('unknown', 'unknown'))
        self.assertFalse(self.c._mutable['smartevse_2_last_connected'])
        self.assertIsNone(self.c._mutable['smartevse_2_last_plug_connected'])


class ControllerCycleTests(unittest.IsolatedAsyncioTestCase):
    """Run the real async_run_cycle with mocked charger HTTP and native states."""

    async def asyncSetUp(self):
        self.states = {}
        self.now = T0
        self.c = make_controller(self.states)
        self.c._entry_data.update({
            CONF_MAINS_L1_ENTITY: 'sensor.mains_l1', CONF_MAINS_L2_ENTITY: 'sensor.mains_l2',
            CONF_MAINS_L3_ENTITY: 'sensor.mains_l3', CONF_PRICE_SENSOR_ENTITY: 'sensor.price',
            CONF_SCHEDULE_ENTITY: 'schedule.charge',
        })
        self.c._options = {CONF_PUSH_CURRENTS: False, CONF_PUSH_WLED: False}
        self.c._store = SimpleNamespace(async_save=AsyncMock())
        self.c._cost_ledger = None
        self.c._cost_error = None
        self.c._schedule_reminders = SimpleNamespace(async_check=MagicMock())
        self.payloads = {}
        self.c._async_get_json = AsyncMock(side_effect=lambda url: deepcopy(self.payloads.get(url)))
        self.c._async_post = AsyncMock(return_value=True)
        for target, kwargs in (
            (f'{CONTROLLER}.dt_util.utcnow', {'side_effect': lambda: self.now}),
            (f'{CONTROLLER}.persistent_notification', {}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def run_at(self, seconds, *, power):
        self.now = T0 + timedelta(seconds=seconds)
        put(self.states, EV_POWER, str(power), self.now)
        self.c._async_post.reset_mock()
        result = await self.c.async_run_cycle(reason='test')
        return result, [(call.args[0], call.kwargs['params']) for call in self.c._async_post.await_args_list]

    async def test_policy_switch_hands_off_sequentially(self):
        self.c._mutable.update({'force_charge': True, 'charge_policy': 'smartevse_2_only'})
        self.payloads[SETTINGS_1] = settings_payload(state='Charging', mode='SMART', mode_id=3)
        self.payloads[SETTINGS_2] = settings_payload()
        result, writes = await self.run_at(0, power=11000)
        self.assertEqual(result['active_smartevse_raw'], 'smartevse_2')
        self.assertEqual(writes, [(SETTINGS_1, {'mode': 0})])
        self.payloads[SETTINGS_1] = settings_payload()
        result, writes = await self.run_at(10, power=8000)
        self.assertEqual((result['active_smartevse_raw'], writes), ('smartevse_2', []))
        result, writes = await self.run_at(31, power=8000)
        self.assertEqual(writes, [(SETTINGS_2, {'mode': 3})])

    async def test_duty_cycle_rotation_hands_off_sequentially(self):
        self.c._mutable.update({'force_charge': True, 'charge_policy': 'smartevse_1_first', 'duty_cycle_minutes': 1})
        self.payloads[SETTINGS_1] = settings_payload()
        self.payloads[SETTINGS_2] = settings_payload()
        result, writes = await self.run_at(0, power=0)
        self.assertEqual((result['active_smartevse_raw'], writes), ('smartevse_1', [(SETTINGS_1, {'mode': 3})]))
        self.payloads[SETTINGS_1] = settings_payload(state='Charging', mode='SMART', mode_id=3)
        self.assertEqual((await self.run_at(10, power=10500))[1], [])
        # Duty cycle elapsed: rotate to SmartEVSE 2, outgoing Off first.
        result, writes = await self.run_at(60, power=10500)
        self.assertEqual((result['active_smartevse_raw'], writes), ('smartevse_2', [(SETTINGS_1, {'mode': 0})]))
        self.payloads[SETTINGS_1] = settings_payload()
        result, writes = await self.run_at(70, power=9000)
        self.assertEqual((result['active_smartevse_raw'], writes), ('smartevse_2', []))
        result, writes = await self.run_at(90, power=7000)
        self.assertEqual((result['active_smartevse_raw'], writes), ('smartevse_2', [(SETTINGS_2, {'mode': 3})]))
        self.payloads[SETTINGS_2] = settings_payload(state='Charging', mode='SMART', mode_id=3)
        result, writes = await self.run_at(100, power=4200)
        self.assertEqual((result['active_smartevse_raw'], writes), ('smartevse_2', []))
        self.assertIsNone(self.c._mutable['handoff_target'])

    async def test_pause_is_recorded_while_idle(self):
        self.payloads[SETTINGS_1] = settings_payload(state=PAUSE, mode='SMART', mode_id=3)
        self.payloads[SETTINGS_2] = settings_payload()
        await self.run_at(0, power=0)
        self.assertEqual(self.c._mutable['smartevse_1_last_pause_at'], T0.isoformat())
        self.assertIsNone(self.c._mutable['smartevse_2_last_pause_at'])

    async def test_every_no_outlet_result_clears_the_active_charger(self):
        self.c._mutable.update({
            'force_charge': True, 'last_charge_allowed': True, 'last_active_charge_reason': 'force_charge',
            'active_smartevse': 'smartevse_1', 'active_smartevse_since': T0.isoformat(),
            'smartevse_1_active_seen_charging': True,
        })
        self.payloads[SETTINGS_1] = settings_payload(state='Charging', mode='SMART', mode_id=3)
        self.payloads[SETTINGS_2] = settings_payload()
        with patch.object(self.c, '_resolve_active_smartevse', return_value=('none', None, 0, 'test_no_outlet')):
            result, writes = await self.run_at(10, power=11000)
        self.assertEqual((result['charge_reason'], result['active_smartevse_raw']), ('test_no_outlet', 'none'))
        self.assertIsNone(self.c._mutable['active_smartevse'])
        self.assertIsNone(self.c._mutable['active_smartevse_since'])
        self.assertFalse(self.c._mutable['smartevse_1_active_seen_charging'])
        self.assertEqual(writes, [(SETTINGS_1, {'mode': 0})])


if __name__ == '__main__':
    unittest.main()
