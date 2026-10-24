"""Accounting behavior only; no device/network/service calls."""
import importlib.util
import json
from pathlib import Path
from datetime import datetime, UTC, timedelta
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('charging_cost', Path(__file__).parents[1]/'custom_components/smartevse_dual_charger/charging_cost.py')
m = importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger=m.ChargingLedger();self.now=datetime(2026,10,24,tzinfo=UTC)
        self.states={k:dict(available=True,connected=False,charging=False,complete=False,vehicle='Car '+k[-1]) for k in m.KEYS}
        self.sources=['sensor.power','sensor.price'];self.observe()
    def observe(self,seconds=0,power=7.2,price=.2,currency='EUR'):
        self.now+=timedelta(seconds=seconds)
        self.ledger.observe(self.now,self.states,power,price,currency,self.sources)
    def start(self,key='smartevse_1'):
        self.states[key].update(connected=True,charging=True);self.observe()
    def record(self,key='smartevse_1'):
        return self.ledger.snapshot()['chargers'][key]['session']
    def test_tariff_changes_do_not_reprice_previous_energy(self):
        self.start();self.observe(60,price=.4);self.observe(60)
        self.assertAlmostEqual(self.record()['cost'],.072)
        self.assertAlmostEqual(self.record()['energy_kwh'],.24)
    def test_negative_and_zero_prices(self):
        self.states['smartevse_1'].update(connected=True,charging=True)
        self.observe(price=-.1);self.observe(60,price=0);self.observe(60)
        self.assertEqual(self.record()['cost'],-.012)
    def test_pause_keeps_session_and_does_not_count_idle_power(self):
        self.start();ident=self.record()['id'];self.states['smartevse_1']['charging']=False
        self.observe(60);self.observe(60);self.states['smartevse_1']['charging']=True;self.observe(60);self.observe(60)
        self.assertEqual(self.record()['id'],ident);self.assertEqual(self.record()['energy_kwh'],.24)
    def test_handoff_only_assigns_each_observed_interval_once(self):
        self.start();self.states['smartevse_1']['charging']=False
        self.states['smartevse_2'].update(connected=True,charging=True)
        self.observe(60);self.observe(60)
        self.assertEqual(self.record()['energy_kwh'],.12);self.assertEqual(self.record('smartevse_2')['energy_kwh'],.12)
    def test_simultaneous_charging_is_not_split_arbitrarily(self):
        self.start();self.start('smartevse_2');self.observe(60)
        for key in m.KEYS:
            self.assertEqual(self.record(key)['cost'],0)
            self.assertIn('shared_meter_ambiguous',self.record(key)['issues'])
    def test_missing_price_keeps_unpriced_energy(self):
        self.start();self.observe(price=None);self.observe(60,price=.2);self.observe(60)
        self.assertEqual(self.record()['unpriced_kwh'],.12)
        self.assertEqual(self.record()['cost'],.024);self.assertTrue(self.record()['partial'])
    def test_missing_power_does_not_become_zero(self):
        self.start();self.observe(power=None);self.observe(60)
        self.assertEqual(self.record()['unobserved_seconds'],60)
        self.assertIn('power_unavailable',self.record()['issues'])
    def test_offline_charger_retains_session_and_flags_gap(self):
        self.start();self.states['smartevse_1']['available']=False;self.observe(60);self.observe(60)
        self.states['smartevse_1']['available']=True;self.observe(60)
        self.assertEqual(self.record()['energy_kwh'],.12)
        self.assertIn('charger_unavailable',self.record()['issues'])
    def test_other_outlet_offline_prevents_shared_meter_assignment(self):
        self.start();self.states['smartevse_2']['available']=False;self.observe();self.observe(60)
        self.assertEqual(self.record()['cost'],0);self.assertTrue(self.record()['partial'])
    def test_restart_retains_cost_without_charging_unobserved_time(self):
        self.start();self.observe(60);ident=self.record()['id']
        self.ledger=m.ChargingLedger(json.loads(json.dumps(self.ledger.data)))
        self.observe(30);self.observe(60)
        self.assertEqual(self.record()['cost'],.048);self.assertEqual(self.record()['id'],ident)
        self.assertEqual(self.record()['unobserved_seconds'],30)
    def test_long_gap_does_not_integrate_stale_power(self):
        self.start();self.observe(600)
        self.assertEqual(self.record()['cost'],0);self.assertTrue(self.record()['partial'])
    def test_disconnection_finishes_once_reconnect_creates_new_record(self):
        self.start();self.observe(60);ident=self.record()['id']
        self.states['smartevse_1'].update(connected=False,charging=False);self.observe(10);self.observe(10)
        self.assertEqual(len(self.ledger.data['records']),1);self.assertEqual(self.record()['status'],'finished')
        self.start();self.assertNotEqual(self.record()['id'],ident)
    def test_completion_remains_visible_and_resume_keeps_same_connection(self):
        self.start();ident=self.record()['id'];self.states['smartevse_1'].update(charging=False,complete=True)
        self.observe(60);self.assertIsNotNone(self.record()['ended_at'])
        self.states['smartevse_1'].update(charging=True,complete=False);self.observe(60)
        self.assertEqual(self.record()['id'],ident);self.assertIsNone(self.record()['ended_at'])
    def test_already_charging_first_install_marks_partial(self):
        self.ledger=m.ChargingLedger();self.start()
        self.assertIn('start_not_observed',self.record()['issues'])
    def test_currency_and_source_changes_are_not_mixed(self):
        self.start();self.observe(60,currency='USD');self.observe(60)
        self.assertEqual(self.record()['cost'],.024);self.assertIn('currency_changed',self.record()['issues'])
        self.sources=['sensor.other','sensor.price'];self.observe(30)
        self.assertIn('restart_or_telemetry_gap',self.record()['issues'])
    def test_history_bounded_per_charger(self):
        for _ in range(105):
            self.start();self.states['smartevse_1'].update(connected=False,charging=False);self.observe(1)
        self.assertEqual(len(self.ledger.data['records']),100)
    def test_snapshot_cannot_mutate_saved_records(self):
        self.start();self.record()['issues'].append('oops');self.assertNotIn('oops',self.record()['issues'])
    def test_zero_elapsed_and_clock_reversal_do_not_double_count(self):
        self.start();self.observe(60);self.observe();self.assertEqual(self.record()['cost'],.024)
        self.observe(-5);self.assertEqual(self.record()['cost'],.024)
        self.observe(5);self.assertEqual(self.record()['cost'],.024)
    def test_unknown_schema_is_not_silently_reset(self):
        with self.assertRaises(ValueError): m.ChargingLedger({'version':2})
    def test_supported_units_and_invalid_inputs(self):
        def state(value,unit):return SimpleNamespace(state=value,attributes={'unit_of_measurement':unit})
        self.assertEqual(m.tariff(state('200','EUR/MWh')),(.2,'EUR'))
        self.assertEqual(m.tariff(state('0.2','€/kWh')),(.2,'EUR'))
        self.assertEqual(m.tariff(state('20','cents/kWh')),(None,None))
        for value in ['unknown','unavailable','nan','inf']:
            self.assertEqual(m.tariff(state(value,'€/kWh')),(None,None))
            self.assertIsNone(m.power_kw(state(value,'W')))
        self.assertEqual(m.power_kw(state(7200,'W')),7.2)
        self.assertEqual(m.power_kw(state(7.2,'kW')),7.2)
        self.assertIsNone(m.power_kw(state(-1,'W')))
        self.assertIsNone(m.power_kw(state(1,'A')))

if __name__=='__main__':unittest.main()
