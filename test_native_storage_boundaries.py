"""Frozen descriptor ownership and worktree assertions using native-created handles."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import worklane as w
import test_worklane
import test_sessions
from tools.native_test_bridge import (
    ROOT, NativeState, acceptance_report, native_locked, native_memory_fd, native_worktree,
)

EVIDENCE=[]
CASES={
    test_worklane.WorklaneTests:[
        'test_duplicate_controller_is_locked',
        'test_repository_case_uses_same_controller_lock',
        'test_lock_is_retained_by_child_after_parent_closes',
    ],
    test_sessions.SessionTests:[
        'test_reviewer_reuses_worktree_across_changed_pr_heads',
        'test_memory_backed_protocol_supports_managed_python_without_os_helper',
    ],
}


def case(source,name):
    def run(self):
        for native in (False,True):
            with ExitStack() as stack:
                if native:
                    for owner,attr,value in ((w,'State',NativeState),(w,'locked',native_locked),
                                             (w,'memory_fd',native_memory_fd),(w.Runner,'worktree',native_worktree)):
                        stack.enter_context(patch.object(owner,attr,value))
                result=unittest.TestResult()
                source(name).run(result)
                self.assertEqual(result.testsRun,1)
                self.assertFalse(result.skipped)
                if result.errors or result.failures:
                    self.fail(('native' if native else 'reference')+':\n'+
                              '\n'.join(text for _,text in result.errors+result.failures))
        EVIDENCE.append({'id':source.__module__+'.'+source.__name__+'.'+name,'status':'pass',
                         'comparison':'equal','observations':{'frozen_assertions':'pass'}})
    return run


@unittest.skipUnless(os.environ.get('WORKLANE_TEST_RUST')=='1','opt-in native descriptor/worktree parity')
class NativeStorageBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        EVIDENCE.clear()
        subprocess.run(['cargo','build','--locked','--offline'],cwd=ROOT,check=True,capture_output=True)
        pin=json.loads((ROOT/'parity/inventory.json').read_text())['revision']
        for filename in ('test_worklane.py','test_sessions.py','worklane.py','worklane_process.py'):
            if subprocess.check_output(['git','hash-object',filename],cwd=ROOT)!=subprocess.check_output(['git','rev-parse',pin+':'+filename],cwd=ROOT):
                raise AssertionError('Frozen oracle drift: '+filename)

    @classmethod
    def tearDownClass(cls):
        if path:=os.environ.get('WORKLANE_NATIVE_STORAGE_EVIDENCE'):
            Path(path).write_text(json.dumps(acceptance_report(EVIDENCE,'test_native_storage_boundaries.py'),indent=2)+'\n')


for source,names in CASES.items():
    for name in names:setattr(NativeStorageBoundaryTests,name,case(source,name))
del source,names,name
