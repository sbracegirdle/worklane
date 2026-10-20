"""Differential workflow transitions through real storage and a fake GitHub command boundary."""
import argparse
from contextlib import redirect_stdout
import copy
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from uuid import UUID
from unittest.mock import patch

import worklane as w
from test_worklane import item, result
from tools.native_test_bridge import ROOT, invoke_rpc, acceptance_report
from tools.port_security_cases import selected_board

CLOCK = '2026-10-20T00:00:00+00:00'
URL = 'https://github.com/owner/repo/pull/9'
EVIDENCE = []


class Runtime:
    def __init__(self, root, native):
        self.root,self.native,self.clock = root,native,CLOCK
        self.items=[item(1,w.HUMAN)]
        self.status={self.items[0]['id']:w.HUMAN}
        self.public=False
        self.blocked=0
        self.closed=False
        self.permissions={'human':'write','editor':'maintain'}
        self.entries=[]
        self.pr={'url':URL,'state':'OPEN','headRefName':'codex/1-worklane','baseRefName':'main',
                 'headRefOid':'a'*40,'isDraft':False,'mergeable':'MERGEABLE','isCrossRepository':False,'mergedAt':None}
        self.fail_after_move=False
        self.fail_read=False
        self.feedback_unavailable=set()
        self.calls=[]
        self.board=selected_board(w,{'repo':'owner/repo'})
        self.board.options={s.casefold():s for s in (w.READY,w.PROGRESS,w.REVIEW,w.HUMAN)}
        self.state=w.State('owner/repo',root)
        self.task={'number':1,'item':copy.deepcopy(self.items[0]),'phase':'human_review','round':2,
                   'branch':'codex/1-worklane','base':'main','head_sha':'a'*40,'pr_url':URL,
                   'human_feedback_seen':{},'worker_worktree':str(root/'saved-worktree')}
        self.save(self.task)
        (root/'saved-worktree').mkdir()
        with patch.object(w,'now',side_effect=lambda:self.clock),patch.object(w.uuid,'uuid4',return_value=UUID(int=1)):
            run=self.state.begin_run(self.task,'build',root/'saved-worktree')
            self.state.attach_session(run,'ses_preserved','provider/model','opencode')
            self.state.finish_run(run,'interrupted')
        self.task=self.state.load(1)

    def save(self, task):
        with patch.object(w,'now',side_effect=lambda:self.clock):self.state.save(task)

    def advance(self, seconds=301):
        self.clock=(datetime.fromisoformat(self.clock)+timedelta(seconds=seconds)).isoformat()

    def comment(self, identifier=1, body='Fix the boundary', **extra):
        entry={'id':identifier,'node_id':f'node-{identifier}','body':body,'user':{'login':'human','type':'User'},
               'created_at':self.clock,'updated_at':self.clock,'html_url':f'https://github.com/comment/{identifier}',
               **extra}
        self.entries.append(entry)
        return entry

    def transport(self, argv, *, stdin=None, **kwargs):
        argv=list(argv)
        self.calls.append((argv,stdin))
        if argv[0]!='gh':raise AssertionError('Unexpected command: '+repr(argv))
        args=argv[1:]
        if 'graphql' in args:
            q=json.loads(stdin); query,variables=q['query'],q['variables']
            if 'viewerCanUpdate' in query:
                data={'node':{'public':self.public,'viewerCanUpdate':True},'repository':{'viewerPermission':'ADMIN'}}
            elif 'issueDependenciesSummary' in query:
                data={'repository':{'issue':{'issueDependenciesSummary':{'blockedBy':self.blocked}}}}
            elif 'lastEditedAt' in query:
                entry=next(e for e in self.entries if e['node_id']==variables['id'])
                data={'node':{'body':entry['body'],'lastEditedAt':entry.get('edited'),
                              'editor':{'login':entry.get('editor'),'__typename':'User'}}}
            elif 'ProjectV2Item' in query:
                data={'node':{'project':{'id':'project'},'fieldValueByName':{'name':self.status[variables['id']]}}}
            else:raise AssertionError('Unexpected GraphQL: '+query)
            return json.dumps({'data':data})
        if args[:2]==['project','item-edit']:
            self.status[args[args.index('--id')+1]]=args[-1]
            if self.fail_after_move:
                self.fail_after_move=False
                raise RuntimeError('controller lost the response')
            return ''
        if args[:2]==['issue','view']:
            return json.dumps({'state':'CLOSED' if self.closed else 'OPEN','number':1,'title':'Title','body':'','labels':[],'comments':[],'url':'issue'})
        if args[:2]==['pr','view']:return json.dumps(self.pr)
        if args[:1]==['api']:
            if args[1].endswith('/permission'):
                return json.dumps({'permission':self.permissions.get(args[1].split('/')[-2],'read')})
            if self.fail_read:raise RuntimeError('fixture unavailable')
            entries=self.entries if '/issues/1/comments?' in args[1] else []
            return json.dumps([entries[:1],entries[1:]])
        raise AssertionError('Unexpected GitHub command: '+repr(args))

    def call(self, method, *, active=(), slot='worker-1', selected=None):
        if selected is None:selected=self.items[0]
        values={'poll':'poll_human_feedback','pick':'pick','recover':'recover','reconcile_completed':'reconcile_completed',
                'reconcile_waiting':'reconcile_waiting'}
        with patch.object(w,'now',side_effect=lambda:self.clock), patch.object(w.uuid,'uuid4',return_value=UUID(int=1)), redirect_stdout(io.StringIO()):
            if self.native:
                def command(event):
                    return {'stdout':self.transport(event['argv'],stdin=event['stdin']),'stderr':'','exit':0}
                output=invoke_rpc({'op':'workflow.call','method':method,'repo':'owner/repo','state_root':str(self.root),
                    'root':str(self.root),'clock':self.clock,'real_dependencies':False,'items':self.items,'item':selected,'active':list(active),
                    'slot':slot,'feedback_unavailable':list(self.feedback_unavailable),
                    'board':{'options':self.board.options}},command)
                self.items[:]=output['items']
                if method=='pick':
                    selected.update(output['item'])
                    for current in self.items:
                        if current['id']==selected['id']:current.update(selected)
                self.feedback_unavailable=set(output['feedback_unavailable'])
                if 'error' in output:raise RuntimeError(output['error'])
                return output['value']
            with patch.object(w,'raw_command',side_effect=self.transport):
                runner=w.Runner(argparse.Namespace(base='main'), 'owner/repo',self.root,self.state,self.board,None)
                runner.feedback_unavailable=self.feedback_unavailable
                try:
                    if method=='pick':return runner.pick(selected,slot)
                    if method=='recover':return runner.recover(self.items,slot)
                    return getattr(runner,values[method])(self.items,set(active))
                finally:self.feedback_unavailable=runner.feedback_unavailable

    def observe(self):
        tasks=self.state.all()
        return json.loads(json.dumps({'tasks':tasks,'items':self.items,'statuses':self.status,
                                      'unavailable':sorted(self.feedback_unavailable)}).replace(str(self.root),'<root>'))


@unittest.skipUnless(os.environ.get('WORKLANE_TEST_RUST')=='1','opt-in native workflow parity')
class NativeWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        EVIDENCE.clear()
        subprocess.run(['cargo','build','--locked','--offline'],cwd=ROOT,check=True,capture_output=True)
        pin=json.loads((ROOT/'parity/inventory.json').read_text())['revision']
        for name in ('worklane.py','worklane_harness.py','worklane_process.py'):
            if subprocess.check_output(['git','hash-object',name],cwd=ROOT)!=subprocess.check_output(['git','rev-parse',pin+':'+name],cwd=ROOT):
                raise AssertionError('Frozen oracle drift: '+name)

    @classmethod
    def tearDownClass(cls):
        if destination:=os.environ.get('WORKLANE_NATIVE_WORKFLOW_EVIDENCE'):
            Path(destination).write_text(json.dumps(acceptance_report(EVIDENCE,'test_native_workflow.py'),indent=2)+'\n')

    def pair(self, scenario):
        observations=[]
        for native in (False,True):
            with tempfile.TemporaryDirectory() as directory:
                runtime=Runtime(Path(directory),native)
                scenario(runtime)
                observations.append(runtime.observe())
        self.assertEqual(observations[1],observations[0])
        EVIDENCE.append({'id':self.id()+(' '+str(self._subtest) if self._subtest else ''),
                         'status':'pass','comparison':'equal','observations':observations[0]})

    def test_feedback_debounce_restart_exact_session_and_claim_once(self):
        def scenario(r):
            saved=r.state.load(1)['sessions']
            r.comment();r.call('poll');r.advance(240);r.comment(2,'Also fix validation')
            r.call('poll');r.advance(240)
            r.state=w.State('owner/repo',r.root);r.call('poll')
            self.assertEqual(r.status['item-1'],w.HUMAN)
            r.advance(61);r.call('poll')
            self.assertEqual(r.state.load(1)['phase'],'rework')
            selected=r.call('pick')
            self.assertEqual(selected['round'],0)
            self.assertEqual(selected['sessions'],saved)
            self.assertEqual(selected['worker_worktree'],str(r.root/'saved-worktree'))
            self.assertEqual(len(selected['human_feedback']),2)
            self.assertIsNone(r.call('pick'))
        self.pair(scenario)

    def test_remote_move_crash_recovers_saved_intent_and_does_not_duplicate_claim(self):
        def scenario(r):
            r.comment();r.call('poll');r.advance();r.fail_after_move=True;r.call('poll')
            self.assertEqual(r.state.load(1)['phase'],'human_review')
            self.assertTrue(r.state.load(1)['human_feedback_pending'])
            self.assertEqual(r.status['item-1'],w.READY)
            r.items[0]['status']=w.READY;r.call('poll')
            self.assertEqual(r.state.load(1)['phase'],'rework')
            self.assertIsNotNone(r.call('pick'))
            self.assertIsNone(r.call('pick'))
        self.pair(scenario)

    def test_deleted_edited_and_temporarily_inaccessible_feedback(self):
        for change in ('deleted','edited','unavailable'):
            def scenario(r):
                entry=r.comment();r.call('poll');r.advance();r.call('poll')
                if change=='deleted':r.entries.clear()
                elif change=='edited':entry['body']='Edited request';entry['updated_at']=r.clock
                else:r.fail_read=True
                r.call('poll');self.assertIsNone(r.call('pick'))
                if change=='edited':
                    r.advance();r.call('poll');self.assertIsNotNone(r.call('pick'))
            with self.subTest(change=change):self.pair(scenario)

    def test_live_revocation_blocks_dispatch_until_author_and_editor_restored(self):
        def scenario(r):
            r.comment(edited=CLOCK,editor='editor');r.call('poll');r.advance();r.call('poll')
            before=r.state.load(1)
            for user in ('human','editor'):
                r.permissions[user]='read'
                with self.assertRaisesRegex(RuntimeError,'authorization'):r.call('pick')
                self.assertEqual(r.state.load(1),before)
                r.permissions[user]='write'
            r.public=True
            with self.assertRaisesRegex(RuntimeError,'security preflight'):r.call('pick')
            self.assertEqual(r.state.load(1),before)
            r.public=False;self.assertIsNotNone(r.call('pick'))
        self.pair(scenario)

    def test_idle_polling_active_assignments_and_human_overrides(self):
        def scenario(r):
            for _ in range(60):r.call('poll');r.advance(60)
            issue_reads=[a for a,_ in r.calls if a[1:3]==['issue','view']]
            self.assertEqual(len(issue_reads),12)
            r.comment();r.call('poll',active=(1,));self.assertEqual(r.state.load(1)['human_feedback'],{})
            r.items[0]['status']='Done';r.call('poll')
            self.assertEqual(r.status['item-1'],w.HUMAN)
        self.pair(scenario)

    def test_recovery_stage_status_matrix_preserves_saved_session(self):
        for stage in ('build','review','finalize'):
            for phase in ('interrupted','retry'):
                for status in (w.READY,w.PROGRESS,w.REVIEW,w.HUMAN,'Done'):
                    def scenario(r):
                        r.items[0]['status']=r.status['item-1']=status
                        r.task.update(phase=phase,resume_stage=stage,item=copy.deepcopy(r.items[0]),question=False,
                                      build_result=result(),review_result=result(),handoff_result=result())
                        r.save(r.task)
                        r.call('recover',slot='worker-1' if stage=='build' else 'reviewer')
                        self.assertEqual(r.state.load(1)['sessions'],r.task['sessions'])
                    with self.subTest(stage=stage,phase=phase,status=status):self.pair(scenario)

    def test_blocked_dependencies_and_closed_issues_preserve_saved_work(self):
        for blocked in (0,1,101):
            def scenario(r):
                r.items[0]['status']=r.status['item-1']=w.PROGRESS
                r.task.update(phase='interrupted',resume_stage='build',item=copy.deepcopy(r.items[0]));r.save(r.task)
                r.blocked=blocked;r.closed=not blocked
                self.assertIsNone(r.call('recover'))
                self.assertEqual(r.state.load(1)['phase'],'interrupted' if blocked else 'blocked')
            with self.subTest(blocked=blocked):self.pair(scenario)

    def test_completion_and_waiting_reconciliation_preserve_board_and_feedback_cutoff(self):
        for change in ('merged','closed','retargeted','lagging','drift','active'):
            def scenario(r):
                r.task['human_feedback_cutoff']=CLOCK;r.save(r.task)
                if change in ('lagging','drift'):
                    r.task['phase']='awaiting_review';r.save(r.task)
                    r.status['item-1']=w.REVIEW if change=='lagging' else 'Done'
                    r.call('reconcile_waiting')
                    self.assertEqual(r.state.load(1)['phase'],'awaiting_review' if change=='lagging' else 'blocked')
                else:
                    r.pr['state']='MERGED' if change in ('merged','active') else 'CLOSED' if change=='closed' else 'OPEN'
                    if change=='retargeted':r.pr['baseRefName']='other'
                    r.call('reconcile_completed',active=(1,) if change=='active' else ())
                    self.assertEqual(r.state.load(1)['phase'],'done' if change=='merged' else 'human_review')
                self.assertEqual(r.state.load(1)['human_feedback_cutoff'],CLOCK)
                self.assertEqual(r.status['item-1'],w.REVIEW if change=='lagging' else 'Done' if change=='drift' else w.HUMAN)
            with self.subTest(change=change):self.pair(scenario)
