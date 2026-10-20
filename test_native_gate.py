"""Native acceptance evidence must bind the tested binary and unchanged assertions."""
import copy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from tools.native_validate import acceptance_checks, runtime_failures, upstream_changes
from tools.port_gate import contract_hash


class NativeEvidenceTests(unittest.TestCase):
    def test_pinned_parent_accepts_its_merged_tree_but_rejects_upstream_drift_and_unrelated_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            def git(*args):
                return subprocess.check_output(['git',*args],cwd=root,text=True,stderr=subprocess.PIPE).strip()
            git('init','-q','-b','main');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
            (root/'reference').write_text('oracle');git('add','.');git('commit','-qm','oracle')
            oracle=git('rev-parse','HEAD')
            (root/'native').write_text('reviewed runtime');git('add','.');git('commit','-qm','parent')
            parent=git('rev-parse','HEAD');git('checkout','-qb','candidate')
            (root/'cli').write_text('CLI');git('add','.');git('commit','-qm','candidate')
            with patch('tools.native_validate.ROOT',root):
                self.assertEqual(upstream_changes(parent,parent),[])
                self.assertEqual(upstream_changes(oracle,parent),['native'])
                git('checkout','-q','main');(root/'unreviewed').write_text('change');git('add','.');git('commit','-qm','unreviewed')
                unrelated=git('rev-parse','HEAD');git('checkout','-q','candidate')
                self.assertEqual(upstream_changes(parent,unrelated),['unreviewed'])
                with self.assertRaises(subprocess.CalledProcessError):upstream_changes(unrelated,unrelated)
                with self.assertRaises(ValueError):upstream_changes('main',parent)

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
