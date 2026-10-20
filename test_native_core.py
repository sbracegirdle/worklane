"""The owner’s finite core gate rejects missing/stale native proof without requiring exhaustive rows."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tools import native_core as core


class CoreGateTests(unittest.TestCase):
    def test_all_nine_groups_are_required_but_exhaustive_and_later_stages_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'parity/evidence').mkdir(parents=True)
            (root/'parity/inventory.json').write_text('{}')
            rust=[];reports={}
            for group in core.GROUPS.values():
                for name,ids in group.items():
                    if name=='rust':rust.extend(ids);continue
                    reports.setdefault(name,{})
                    for id in ids:reports[name][id]={'id':id,'status':'pass','comparison':'equal','observations':{}}
            for name,rows in reports.items():
                (root/f'parity/evidence/{name}.json').write_text(json.dumps({'results':list(rows.values())}))
            commands=[{'command':['cargo','test','--locked','--offline'],'exit':0,'passed_tests':rust}]
            def assess(current=True,drift=None):return core.assess_core(root,'pin','binary',commands,current,drift or [])
            with patch.object(core,'report_bound',return_value=True):
                result=assess()
                self.assertTrue(result['core_runtime_ready'])
                self.assertFalse(result['core_release_ready'])
                self.assertEqual(len(result['core_scenarios']),9)
                self.assertIn('pending',result['independent_review'])
                self.assertFalse(assess(current=False)['core_runtime_ready'])
                self.assertFalse(assess(drift=['changed.py'])['core_runtime_ready'])
                for name in reports:
                    path=root/f'parity/evidence/{name}.json';original=path.read_text()
                    path.write_text('{"results":[]}')
                    self.assertFalse(assess()['core_runtime_ready'],name)
                    path.write_text(original)
                commands[0]['passed_tests']=[]
                self.assertFalse(assess()['core_runtime_ready'])
            with patch.object(core,'report_bound',return_value=False):
                self.assertFalse(assess()['core_runtime_ready'])

    def test_reference_only_stale_binary_driver_or_fixture_cannot_attest_core(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'parity/fixtures').mkdir(parents=True)
            (root/'parity/fixtures/security.json').write_text('{}')
            evidence={'mode':'differential','candidate_runtime':'native','reference':'pin',
                'reference_verified':True,'inventory_sha256':'inventory','release_sha256':'binary',
                'candidate':['native'],'candidate_hashes':{'native':'binary'},
                'fixtures_path':'parity/fixtures/security.json',
                'fixtures_sha256':hashlib.sha256(b'{}').hexdigest()}
            def bound(value):return core.report_bound(root,value,'pin','binary','inventory')
            self.assertTrue(bound(evidence))
            for key,value in [('mode','reference'),('candidate_runtime','python'),('reference','old'),
                              ('reference_verified',False),('release_sha256','old'),('inventory_sha256','old'),
                              ('candidate_hashes',{'native':'old'}),('fixtures_sha256','old')]:
                stale=copy.deepcopy(evidence);stale[key]=value
                self.assertFalse(bound(stale),key)
            (root/'driver.py').write_text('assertions')
            evidence['driver']='driver.py';evidence['harness_hashes']={'driver.py':hashlib.sha256(b'assertions').hexdigest()}
            with patch.object(core,'acceptance_inputs',return_value=['driver.py']):
                self.assertTrue(bound(evidence))
                (root/'driver.py').write_text('changed assertions')
                self.assertFalse(bound(evidence))
