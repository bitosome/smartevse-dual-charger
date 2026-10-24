"""Exercise accounting wiring with HA State objects and mocked storage only."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock
from homeassistant.core import State
from custom_components.smartevse_dual_charger.charging_cost import ChargingLedger
from custom_components.smartevse_dual_charger.controller import SmartEVSEDualChargerController, SmartEVSEStatus, MUTABLE_DEFAULTS
from custom_components.smartevse_dual_charger.sensor import ChargingCostSensor, ControllerSensor, SENSOR_DESCRIPTIONS

class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c=SmartEVSEDualChargerController.__new__(SmartEVSEDualChargerController)
        self.c._entry_data={'ev_meter_import_active_power_entity':'sensor.power','price_sensor_entity':'sensor.price'}
        self.states={'sensor.power':State('sensor.power','7200',{'unit_of_measurement':'W'}),'sensor.price':State('sensor.price','.2',{'unit_of_measurement':'€/kWh'})}
        self.c.hass=SimpleNamespace(states=SimpleNamespace(get=self.states.get))
        self.c._mutable=deepcopy(MUTABLE_DEFAULTS);self.c._cost_ledger=ChargingLedger();self.c._cost_error=None
        self.c._cost_store=SimpleNamespace(async_save=AsyncMock())
        self.now=datetime(2026,10,24,tzinfo=UTC)
        self.statuses=[SmartEVSEStatus(key=k,base_url='http://example.invalid',available=True,connected=True,plug_state='Connected',state='Charging' if i==0 else 'Connected to EV',mode='Normal',charge_current=16,max_current=16,override_current=0,error='None') for i,k in enumerate(['smartevse_1','smartevse_2'])]
    async def sample(self,seconds=0):
        self.now+=timedelta(seconds=seconds)
        await self.c._observe_charging_costs(*self.statuses,now=self.now)
    async def test_native_sources_produce_saved_cost_and_sensor(self):
        await self.sample();await self.sample(60)
        data=self.c.charging_costs();self.assertEqual(data['chargers']['smartevse_1']['session']['cost'],.024)
        self.assertEqual(data['chargers']['smartevse_2']['history'],[])
        self.assertEqual(self.c._cost_store.async_save.await_count,2)
        coord=MagicMock();coord.data={'charging_costs':data}
        entry=SimpleNamespace(entry_id='test',title='Test chargers',runtime_data=SimpleNamespace(coordinator=coord))
        sensor=ChargingCostSensor(entry,'smartevse_1')
        self.assertEqual(sensor.native_value,.024);self.assertEqual(sensor.native_unit_of_measurement,'EUR')
        self.assertTrue(sensor.extra_state_attributes['estimated'])
        status=ControllerSensor(entry,SENSOR_DESCRIPTIONS[0])
        self.assertEqual(status.extra_state_attributes['charging_costs'],data)
        self.assertIn('charging_costs',status._unrecorded_attributes)
    async def test_save_failure_visible_and_does_not_stop_controller(self):
        self.c._cost_store.async_save.side_effect=OSError('test failure')
        with self.assertLogs('custom_components.smartevse_dual_charger',level='ERROR'):await self.sample()
        self.assertIn('could not be updated',self.c.charging_costs()['error'])
        self.c._cost_store.async_save.side_effect=None;await self.sample(60)
        self.assertIsNone(self.c.charging_costs()['error'])
        self.assertEqual(self.c.charging_costs()['chargers']['smartevse_1']['session']['cost'],.024)
    async def test_missing_ledger_does_not_overwrite_failed_load(self):
        self.c._cost_ledger=None;self.c._cost_error='Load failed'
        await self.sample();self.c._cost_store.async_save.assert_not_awaited()
        self.assertEqual(self.c.charging_costs()['error'],'Load failed')
    async def test_reset_control_sessions_does_not_erase_accounting(self):
        await self.sample();await self.sample(60)
        before=deepcopy(self.c._cost_ledger.data)
        self.c._store=SimpleNamespace(async_save=AsyncMock())
        await self.c.async_reset_sessions()
        self.assertEqual(self.c._cost_ledger.data,before)
