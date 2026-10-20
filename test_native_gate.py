"""Native acceptance evidence must bind the tested binary and unchanged assertions."""
import copy
import unittest
from tools.native_validate import acceptance_checks, runtime_failures
from tools.port_gate import contract_hash


class NativeEvidenceTests(unittest.TestCase):
    def test_runtime_stage_keeps_full_contract_hash_and_later_stage_obligations(self):
        inventory={'revision':'pin','language':'Rust','checks':{'runtime':{},'cli':{}},
                   'matrix':[{'id':'runtime','stage':'runtime','checks':['runtime']},
                             {'id':'cli','stage':'cli','checks':['cli']}]}
        report={'mode':'differential','candidate_runtime':'native','reference':'pin','reference_verified':True,
                'inventory_sha256':contract_hash(inventory),'candidate':['binary'],'candidate_hashes':{'binary':'release'},
                'release_sha256':'release','results':[{'id':'runtime','status':'pass','comparison':'equal','observations':{}}]}
        self.assertEqual(runtime_failures(inventory,[report]),{})
        subset={**inventory,'matrix':inventory['matrix'][:1]}
        forged={**report,'inventory_sha256':contract_hash(subset)}
        self.assertEqual(set(runtime_failures(inventory,[forged])),{'runtime'})

    def test_stale_or_unbound_acceptance_cannot_complete_a_row(self):
        manifest={'revision':'pin','checks':{'check':{}}}
        hashes={'driver.py':'driver','tools/native_test_bridge.py':'bridge'}
        report={'mode':'differential','candidate_runtime':'native','reference':'pin','reference_verified':True,
                'inventory_sha256':'inventory','candidate':['binary'],'candidate_hashes':{'binary':'release'},
                'release_sha256':'release','harness_hashes':hashes,
                'results':[{'id':'check','status':'pass','comparison':'equal','observations':{'assertions':'pass'}},
                           {'id':'invented','status':'pass','comparison':'equal','observations':{}}]}
        def assess(value):return acceptance_checks(value,manifest,'inventory','release',hashes)
        self.assertEqual(assess(report),{'check'})
        for key,value in (('mode','reference'),('candidate_runtime','interpreted'),('reference','old'),
                          ('reference_verified',False),('inventory_sha256','old'),('release_sha256','old'),
                          ('candidate',[]),('candidate_hashes',{'binary':'old'}),('harness_hashes',{})):
            with self.subTest(key=key):
                stale=copy.deepcopy(report);stale[key]=value
                self.assertEqual(assess(stale),set())
        stale=copy.deepcopy(report);stale['candidate_hashes']={'other binary':'release'}
        self.assertEqual(assess(stale),set())
        for change in ({'status':'fail'},{'comparison':'different'}):
            stale=copy.deepcopy(report);stale['results'][0].update(change)
            self.assertEqual(assess(stale),set())
        stale=copy.deepcopy(report);del stale['results'][0]['observations']
        self.assertEqual(assess(stale),set())
