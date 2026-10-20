"""Frozen direct completion/waiting assertions through the native state machine."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import worklane as w
import test_worklane
from tools.native_test_bridge import ROOT, acceptance_report, invoke_rpc

EVIDENCE=[]
NAMES=(
    'test_closed_unmerged_or_retargeted_pr_does_not_complete_task',
    'test_completion_checks_are_bounded_without_changing_feedback_cutoff',
    'test_completion_does_not_touch_live_assignments_or_completed_tasks',
    'test_completion_api_failure_preserves_task_and_rate_limit',
    'test_waiting_review_reconciliation_skips_active_assignments',
)


def reconcile(runner,method,items,active_numbers):
    def command(event):
        args=event['argv'][1:]
        if args[:2]==['pr','view']:
            saved=next(t for t in runner.state.all() if t.get('pr_url')==args[2])
            value={'url':args[2],'headRefOid':saved['head_sha'],'headRefName':saved['branch'],
                   'baseRefName':saved['base'],'isDraft':False,'isCrossRepository':False,
                   'mergeable':'MERGEABLE',**runner.pr(args[2])}
        elif 'graphql' in args:
            request=json.loads(event['stdin'])
            selected=next(i for i in items if i['id']==request['variables']['id'])
            value={'data':{'node':{'project':{'id':'project'},
                   'fieldValueByName':{'name':runner.board.item_status(selected)}}}}
        else:raise AssertionError('Unexpected reconciliation command: '+repr(args))
        return {'stdout':json.dumps(value),'stderr':'','exit':0}
    output=invoke_rpc({'op':'workflow.call','method':method,'repo':runner.repo,
        'root':str(runner.root),'state_root':str(runner.state.path.parent.parent),
        'items':items,'active':list(active_numbers),'clock':w.now()},command)
    items[:]=output['items']
    if 'error' in output:
        if output['kind']=='quota':raise w.GitHubRateLimit(output['error'])
        raise RuntimeError(output['error'])
    return output['value']


def completed(runner,items,active_numbers=()):
    return reconcile(runner,'reconcile_completed',items,active_numbers)


def waiting(runner,items,active_numbers=()):
    return reconcile(runner,'reconcile_waiting',items,active_numbers)


def case(name):
    def run(self):
        for native in (False,True):
            with ExitStack() as stack:
                if native:
                    stack.enter_context(patch.object(w.Runner,'reconcile_completed',completed))
                    stack.enter_context(patch.object(w.Runner,'reconcile_waiting',waiting))
                result=unittest.TestResult()
                test_worklane.WorklaneTests(name).run(result)
                self.assertEqual(result.testsRun,1)
                self.assertFalse(result.skipped)
                if result.errors or result.failures:
                    self.fail(('native' if native else 'reference')+':\n'+
                              '\n'.join(text for _,text in result.errors+result.failures))
        EVIDENCE.append({'id':'test_worklane.WorklaneTests.'+name,'status':'pass',
                         'comparison':'equal','observations':{'frozen_assertions':'pass'}})
    return run


@unittest.skipUnless(os.environ.get('WORKLANE_TEST_RUST')=='1','opt-in native reconciliation parity')
class NativeReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        EVIDENCE.clear()
        subprocess.run(['cargo','build','--locked','--offline'],cwd=ROOT,check=True,capture_output=True)
        pin=json.loads((ROOT/'parity/inventory.json').read_text())['revision']
        for filename in ('test_worklane.py','worklane.py'):
            if subprocess.check_output(['git','hash-object',filename],cwd=ROOT)!=subprocess.check_output(['git','rev-parse',pin+':'+filename],cwd=ROOT):
                raise AssertionError('Frozen oracle drift: '+filename)

    @classmethod
    def tearDownClass(cls):
        if path:=os.environ.get('WORKLANE_NATIVE_RECONCILIATION_EVIDENCE'):
            Path(path).write_text(json.dumps(acceptance_report(EVIDENCE,'test_native_reconciliation.py'),indent=2)+'\n')


for name in NAMES:setattr(NativeReconciliationTests,name,case(name))
del name
