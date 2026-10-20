"""Both runtimes reject malformed saved ancestry before Git or provider execution."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import worklane as w
from test_worklane import FakeBoard
from tools.native_test_bridge import ROOT, invoke_rpc, acceptance_report

EVIDENCE=[]


@unittest.skipUnless(os.environ.get('WORKLANE_TEST_RUST')=='1','opt-in saved ancestry shape parity')
class NativeStackShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        EVIDENCE.clear()
        subprocess.run(['cargo','build','--locked','--offline'],cwd=ROOT,check=True,capture_output=True)
        pin=json.loads((ROOT/'parity/inventory.json').read_text())['revision']
        if subprocess.check_output(['git','hash-object','worklane.py'],cwd=ROOT)!=subprocess.check_output(['git','rev-parse',pin+':worklane.py'],cwd=ROOT):
            raise AssertionError('Frozen oracle drift')

    @classmethod
    def tearDownClass(cls):
        if path:=os.environ.get('WORKLANE_NATIVE_STACK_SHAPE_EVIDENCE'):
            Path(path).write_text(json.dumps(acceptance_report(EVIDENCE,'test_native_stack_shape.py'),indent=2)+'\n')

    def test_saved_stack_types_cannot_silently_remove_ancestry_checks(self):
        for value in (None,False,17,'bad',{'number':2}):
            for method in ('validate_stack','verify_stack_ancestry'):
                with self.subTest(stack=value,method=method),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory);state=w.State('owner/repo',root)
                    runner=w.Runner(argparse.Namespace(base='main'),'owner/repo',root,state,FakeBoard(),Mock())
                    task={'number':1,'stack':value}
                    with patch.object(w,'command',side_effect=AssertionError('Git must not run')):
                        with self.assertRaises((TypeError,KeyError)):
                            if method=='validate_stack':runner.validate_stack(task)
                            else:runner.verify_stack_ancestry(task,root)
                    native=invoke_rpc({'op':'workflow.call','method':method,'repo':'owner/repo',
                        'root':directory,'state_root':directory,'task':task,'tree':directory},
                        lambda event: (_ for _ in ()).throw(AssertionError(event)))
                    self.assertIn('error',native)
                    self.assertIn('must be a list',native['error'])
                    EVIDENCE.append({'id':self.id()+' '+repr((value,method)),'status':'pass',
                        'comparison':'equal','observations':{'saved_stack':value,'source_rejected':True,
                        'native_rejected':True,'git_or_provider_commands':0}})
