"""The nine frozen restack assertions through native workflow code and real Git."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import worklane as w
import test_stacking
from tools.native_test_bridge import (
    ROOT, acceptance_report, native_reconcile_stacks, native_verify_stack_ancestry,
)

EVIDENCE=[]


def case(name):
    def run(self):
        for native in (False,True):
            with ExitStack() as stack:
                if native:
                    stack.enter_context(patch.object(w.Runner,'reconcile_stacks',native_reconcile_stacks))
                    stack.enter_context(patch.object(w.Runner,'verify_stack_ancestry',native_verify_stack_ancestry))
                result=unittest.TestResult()
                test_stacking.RestackTests(name).run(result)
                self.assertEqual(result.testsRun,1)
                self.assertFalse(result.skipped)
                if result.errors or result.failures:
                    self.fail(('native' if native else 'reference')+':\n'+
                              '\n'.join(text for _,text in result.errors+result.failures))
        EVIDENCE.append({'id':'test_stacking.RestackTests.'+name,'status':'pass',
                         'comparison':'equal','observations':{'frozen_assertions':'pass'}})
    return run


@unittest.skipUnless(os.environ.get('WORKLANE_TEST_RUST')=='1','opt-in native restack parity')
class NativeRestackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        EVIDENCE.clear()
        subprocess.run(['cargo','build','--locked','--offline'],cwd=ROOT,check=True,capture_output=True)
        pin=json.loads((ROOT/'parity/inventory.json').read_text())['revision']
        for filename in ('test_stacking.py','worklane.py','worklane_process.py'):
            if subprocess.check_output(['git','hash-object',filename],cwd=ROOT)!=subprocess.check_output(['git','rev-parse',pin+':'+filename],cwd=ROOT):
                raise AssertionError('Frozen oracle drift: '+filename)

    @classmethod
    def tearDownClass(cls):
        if path:=os.environ.get('WORKLANE_NATIVE_RESTACK_EVIDENCE'):
            Path(path).write_text(json.dumps(acceptance_report(EVIDENCE,'test_native_restack.py'),indent=2)+'\n')


for name in unittest.defaultTestLoader.getTestCaseNames(test_stacking.RestackTests):
    setattr(NativeRestackTests,name,case(name))
del name
