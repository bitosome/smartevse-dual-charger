"""Accounting behavior only; no device/network/service calls."""
import importlib.util
import json
from pathlib import Path
from datetime import datetime, UTC, timedelta
from types import SimpleNamespace
from copy import deepcopy
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
    def test_parts_of_a_charge_are_recorded_by_what_made_it_charge(self):
        # 0.0.14.0: history shows the parts a charge went through. Each metered interval
        # goes to the part named for it; consecutive intervals of one kind are one part,
        # a pause or another kind starts a new one.
        def run(minutes, kind, price=.2):
            for _ in range(minutes):
                self.now += timedelta(seconds=60)
                self.ledger.observe(self.now, self.states, 7.2, price, 'EUR', self.sources, kind=kind)
        self.start()
        t0 = self.now
        at = lambda minutes: (t0 + timedelta(minutes=minutes)).isoformat()
        run(20, 'guaranteed')
        run(10, 'extra')
        self.states['smartevse_1']['charging'] = False
        run(1, 'extra')                # the last metered minute before the pause
        run(30, None)                  # paused: nothing metered, no part
        self.states['smartevse_1']['charging'] = True
        run(1, None)                   # charging again from here
        run(10, 'force')
        run(5, 'nonsense')
        run(5, None, price=None)       # the price of the next minutes is unknown
        run(5, None)
        record = self.record()
        parts = record['parts']
        self.assertEqual([(part['kind'], part['start'], part['end']) for part in parts], [
            ('guaranteed', at(0), at(20)), ('extra', at(20), at(31)), ('force', at(62), at(72)), ('other', at(72), at(87))])
        kwh = lambda minutes: 7.2 * minutes / 60
        self.assertAlmostEqual(parts[0]['energy_kwh'], kwh(20), places=4); self.assertAlmostEqual(parts[0]['cost'], kwh(20) * .2, places=4)
        self.assertAlmostEqual(parts[1]['energy_kwh'], kwh(11), places=4)
        self.assertAlmostEqual(parts[2]['energy_kwh'], kwh(10), places=4)
        # An unknown kind and no kind are "other"; an unpriced interval leaves the part without a cost.
        self.assertAlmostEqual(parts[3]['energy_kwh'], kwh(15), places=4); self.assertIsNone(parts[3]['cost'])
        # The parts add up to the session.
        self.assertAlmostEqual(sum(part['energy_kwh'] for part in parts), record['energy_kwh'], places=3)
        # They survive a restart, and a session recorded before 0.0.14.0 has an empty list.
        restored = m.ChargingLedger(json.loads(json.dumps(self.ledger.data)))
        self.assertEqual(restored.snapshot()['chargers']['smartevse_1']['session']['parts'], parts)
        old = json.loads(json.dumps(self.ledger.data)); old['records'][0].pop('parts')
        self.assertEqual(m.ChargingLedger(old).snapshot()['chargers']['smartevse_1']['session']['parts'], [])
        # Damaged parts are left out of the snapshot and do not stop the accounting.
        old['records'][0]['parts'] = [{'kind': 'extra'}, 'x', {'kind': 'extra', 'start': at(0), 'end': at(1), 'energy_kwh': 1, 'cost': None}]
        damaged = m.ChargingLedger(old)
        self.assertEqual(len(damaged.snapshot()['chargers']['smartevse_1']['session']['parts']), 1)
        damaged.observe(self.now + timedelta(seconds=60), self.states, 7.2, .2, 'EUR', self.sources, kind='extra')

    def test_parts_are_bounded_per_session(self):
        self.start()
        for index in range(m.PARTS_LIMIT * 2 + 6):
            self.now += timedelta(seconds=60)
            self.ledger.observe(self.now, self.states, 7.2, .2, 'EUR', self.sources, kind='guaranteed' if index % 2 else 'extra')
        record = self.record()
        self.assertEqual(len(record['parts']), m.PARTS_LIMIT)
        self.assertAlmostEqual(sum(part['energy_kwh'] for part in record['parts']), record['energy_kwh'], places=3)
        self.assertAlmostEqual(sum(part['cost'] for part in record['parts']), record['cost'], places=3)

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
    def test_charge_started_during_restart_or_long_gap_is_partial(self):
        for restart in (True, False):
            with self.subTest(restart=restart):
                self.setUp()
                if restart:self.ledger=m.ChargingLedger(deepcopy(self.ledger.data))
                self.states['smartevse_1'].update(connected=True,charging=True)
                self.observe(30 if restart else 600)
                self.assertIn('start_not_observed',self.record()['issues'])
                self.assertEqual(self.record()['energy_kwh'],0)
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
    def test_damaged_nested_history_is_rejected_without_mutation(self):
        self.start();self.observe(60)
        for section, key, value in [
            ('record','cost',float('nan')), ('record','priced_kwh',-1),
            ('record','issues',None), ('record','status',[]),
            ('record','currency',[]), ('record','started_at','2026-10-24T12:00:00'),
            ('sample','at','invalid'), ('sample','sessions',['missing-session']),
            ('sample','available',{'smartevse_1':True}), ('sample','power_kw',-1),
            ('sample','price',float('inf')), ('sample','sources',None),
        ]:
            with self.subTest(section=section,key=key):
                stored=deepcopy(self.ledger.data)
                target=stored['records'][0] if section=='record' else stored['sample']
                target[key]=value
                before=repr(stored)
                with self.assertRaises(ValueError): m.ChargingLedger(stored)
                self.assertEqual(repr(stored),before)
    def test_missing_checkpoint_fields_and_orphan_active_ids_are_rejected(self):
        self.start()
        for key in ('power_kw','price','currency'):
            stored=deepcopy(self.ledger.data);del stored['sample'][key]
            with self.assertRaises(ValueError): m.ChargingLedger(stored)
        stored=deepcopy(self.ledger.data);stored['active']['smartevse_1']='missing'
        with self.assertRaises(ValueError): m.ChargingLedger(stored)
        stored=deepcopy(self.ledger.data);stored['records'].append(deepcopy(stored['records'][0]))
        with self.assertRaises(ValueError): m.ChargingLedger(stored)
    def test_valid_additive_fields_and_paused_sessions_restore(self):
        self.start();self.observe(60);self.states['smartevse_1']['charging']=False;self.observe(30)
        stored=deepcopy(self.ledger.data);stored['future_metadata']={'example':1}
        self.ledger=m.ChargingLedger(stored);self.observe(30)
        self.assertEqual(self.record()['cost'],.036)
        self.assertIn('restart_or_telemetry_gap',self.record()['issues'])
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
