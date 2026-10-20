"""Current-main privacy invariants against Python and native shared interfaces."""
from contextlib import redirect_stderr
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_harness
import worklane as w
from tools.native_test_bridge import invoke, NativeBackend, NativeState, ROOT
from tools.port_security_cases import invoke as reference_call

CLOCK = '2026-10-20T00:00:00+00:00'
PROJECT = {'id':'project','number':2,'url':'board-url'}
PRIVATE = {'node':{'public':False,'viewerCanUpdate':True},'repository':{'viewerPermission':'ADMIN'}}
GH = r'''import json,os,sys
from pathlib import Path
p=Path(os.environ['NATIVE_SECURITY_CONTROL']); c=json.loads(p.read_text()); args=sys.argv[1:]
body=sys.stdin.read() if '--input' in args else ''
with p.with_suffix('.calls').open('a') as f: f.write(json.dumps({'args':args,'body':body})+'\n')
if args[:2]==['project','view']: out={'id':'project','number':2,'url':'board-url'}
elif args[:2]==['project','item-edit']:
 c['status']='In Progress'; p.write_text(json.dumps(c)); out=''
elif args[:2]==['pr','view']: out=c['pr']
elif args[:2]==['issue','view']: out={'state':'OPEN','number':1,'title':'Title','body':'','labels':[],'comments':[]}
elif 'graphql' in args:
 q=json.loads(body)['query']
 if 'viewerCanUpdate' in q:
  out=c['security']
  if c.get('security_sequence'):
   out=c['security_sequence'].pop(0); c['security']=out; p.write_text(json.dumps(c))
 elif 'lastEditedAt' in q: out=c['provenance']
 elif 'issueDependenciesSummary' in q: out={'repository':{'issue':{'issueDependenciesSummary':{'blockedBy':0}}}}
 elif 'ProjectV2Item' in q: out={'node':{'project':{'id':'project'},'fieldValueByName':{'name':c.get('status','Ready for work')}}}
 else: raise RuntimeError('Unexpected GraphQL '+q)
 out={'data':out}
elif args[:1]==['api'] and args[-1].endswith('/permission'):
 login=args[-1].split('/')[-2]
 if c.get('permission_error'): print(c['permission_error'],file=sys.stderr); sys.exit(1)
 out={'permission':c['permissions'].get(login,'read')}
elif args[:1]==['api'] and '--paginate' in args: out=[[],c.get('entries',[])]
else: raise RuntimeError('Unexpected mutation '+repr(args))
print(json.dumps(out) if not isinstance(out,str) else out)
'''


@unittest.skipUnless(os.environ.get('WORKLANE_TEST_RUST') == '1','opt-in native security parity')
class NativeSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(['cargo','build','--locked','--offline'],cwd=ROOT,check=True,capture_output=True)

    def setUp(self):
        self.fixture=test_harness.HarnessTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f=self.fixture
        self.control=self.f.root/'security.json'
        self.entry={'id':1,'node_id':'comment-1','body':'Fix it','author_association':'OWNER',
                    'user':{'login':'trusted','type':'User'},'created_at':CLOCK,'state':'CHANGES_REQUESTED','path':'a.py'}
        self.task={'number':1,'item':{'id':'item','content':{'number':1},'status':w.READY,'blocked_by_count':0},
                   'phase':'interrupted','resume_stage':'build','branch':'codex/1-worklane','base':'main',
                   'head_sha':'a'*40,'pr_url':'https://github.com/owner/repo/pull/9','round':0}
        self.config={'security':copy.deepcopy(PRIVATE),'permissions':{'trusted':'write','editor':'maintain'},
                     'provenance':{'node':{'body':'Fix it','lastEditedAt':None}},'entries':[self.entry],
                     'pr':{'state':'OPEN','headRefName':self.task['branch'],'baseRefName':'main','headRefOid':'a'*40,
                           'isDraft':True,'isCrossRepository':False,'url':self.task['pr_url'],'mergeable':'MERGEABLE'}}
        executable=self.f.bin/'gh'; executable.write_text('#!'+sys.executable+'\n'+GH); executable.chmod(0o755)
        self.enterContext(patch.dict(os.environ,NATIVE_SECURITY_CONTROL=str(self.control)))
        self.enterContext(patch.object(w,'now',return_value=CLOCK))
        self.write()

    def write(self): self.control.write_text(json.dumps(self.config))
    def call(self, native, method, **extra):
        request={'op':'security.call','method':method,'repo':'owner/repo','state_root':str(self.f.root),
                 'root':str(self.f.tree),'real_dependencies':False,'task':copy.deepcopy(self.task),**extra}
        return invoke(request,cwd=self.f.tree) if native else reference_call(w,request)

    def pair(self, method, **extra):
        expected=self.call(False,method,**extra)
        actual=self.call(True,method,**extra)
        self.assertEqual(actual,expected)
        return actual

    def test_visibility_and_permissions_matrix_fail_closed_in_both_runtimes(self):
        for public in (False,True,None,'false',0):
            for update in (True,False,None):
                for permission in ('WRITE','MAINTAIN','ADMIN','READ','TRIAGE',None):
                    self.config['security']={'node':{'public':public,'viewerCanUpdate':update},'repository':{'viewerPermission':permission}}
                    self.write()
                    for override in (False,True):
                        with self.subTest(public=public,update=update,permission=permission,override=override):
                            report=self.pair('report',override=override)
                            self.assertEqual(report['safe'],type(public) is bool and (not public or override) and update is True and permission in ('WRITE','MAINTAIN','ADMIN'))

    def test_private_operation_public_rejection_and_explicit_override(self):
        self.assertTrue(self.pair('open')['safe'])
        self.config['security']['node']['public']=True;self.write()
        for native in (False,True):
            with self.assertRaisesRegex(RuntimeError,'security preflight'): self.call(native,'open')
        with redirect_stderr(io.StringIO()): self.assertTrue(self.pair('open',override=True)['safe'])
        self.assertFalse(self.pair('open',check_only=True)['safe'])

    def test_visibility_changes_block_all_shared_writes_and_cached_results(self):
        before=self.f.state.all()
        self.pair('safe')
        self.config['security']['node']['public']=True;self.write()
        self.task['completed_turns']={'build':{'verdict':'pass'}}
        for method in ('add','ensure_review','move','pick','recover','poll','agent_result','post','restack','save_editor'):
            for native in (False,True):
                with self.subTest(method=method,native=native), self.assertRaisesRegex(RuntimeError,'security preflight'):
                    self.call(native,method,item=self.task['item'])
        calls=[json.loads(line) for line in self.control.with_suffix('.calls').read_text().splitlines()]
        self.assertTrue(all('graphql' in call['args'] for call in calls))
        self.assertEqual(self.f.state.all(),before)

    def test_authors_editors_bots_association_and_provenance_match(self):
        for login in ('outsider','trusted','editor'):
            for editor in (None,'outsider','trusted','bot'):
                with self.subTest(author=login,editor=editor):
                    self.entry['user']['login']=login
                    self.config['provenance']['node']={'body':'Fix it','lastEditedAt':CLOCK if editor else None,
                        'editor':{'login':editor,'__typename':'Bot' if editor=='bot' else 'User'}}
                    self.write(); feedback=self.pair('feedback')
                    self.assertEqual(bool(feedback),login!='outsider' and editor in (None,'trusted'))
                    if feedback:self.assertEqual(set(feedback),{'issue:1','pr:1','review:1','inline:1'})
        self.entry['user']['login']='trusted'
        for provenance in ({'node':None},{'node':{'body':'changed','lastEditedAt':None}}):
            self.config['provenance']=provenance;self.write()
            for native in (False,True):
                with self.assertRaisesRegex(RuntimeError,'provenance'):self.call(native,'feedback')

    def test_revoked_feedback_recovery_preserves_exact_saved_work_before_board_mutation(self):
        self.config['provenance']['node'].update(lastEditedAt=CLOCK,editor={'login':'editor','__typename':'User'})
        self.write(); snapshot=self.pair('feedback')
        for phase in ('interrupted','retry'):
            for status in (w.READY,w.PROGRESS):
                for revoked in ('trusted','editor'):
                    for native in (False,True):
                        with self.subTest(phase=phase,status=status,revoked=revoked,native=native):
                            self.task.update(phase=phase,human_feedback=snapshot)
                            self.task['item']['status']=status
                            self.config['status']=status;self.config['permissions']={'trusted':'write','editor':'maintain',revoked:'read'};self.write()
                            self.f.state.save(self.task);before=self.f.state.load(1)
                            with self.assertRaisesRegex(RuntimeError,'authorization'):
                                self.call(native,'recover',items=[self.task['item']])
                            self.assertEqual(self.f.state.load(1),before)
                            self.assertEqual(json.loads(self.control.read_text())['status'],status)

    def test_permission_lookup_failures_and_saved_consent_cannot_change_sessions(self):
        snapshot=self.pair('feedback');self.task['human_feedback']=snapshot;self.f.state.save(self.task)
        self.f.state.set_meta('runtime',{'project':'owner/2','dangerously_allow_public_board':True})
        for error in ('permission lookup failed','API rate limit exceeded'):
            for native in (False,True):
                self.config['permission_error']=error;self.write()
                # Quota metadata is deliberately repository-wide; use fresh state between runtimes.
                w.State('_github',self.f.root).set_meta('github_backoff',{})
                with self.assertRaises(RuntimeError): self.call(native,'recover',items=[self.task['item']])
        w.State('_github',self.f.root).set_meta('github_backoff',{})
        self.config.pop('permission_error'); self.config['security']['node']['public']=True;self.write()
        run=self.f.state.begin_run(self.task,'build',self.f.tree)
        self.f.state.attach_session(run,'ses_preserved','provider/model','opencode')
        self.f.state.finish_run(run,'interrupted')
        saved=self.f.state.session(1,'worker')
        for method in ('retry','resume','queue','unpause'):
            with self.subTest(method=method), self.assertRaisesRegex(RuntimeError,'security preflight'):
                self.call(True,method,run_id='missing')
        self.assertEqual(self.f.state.session(1,'worker'),saved)
        with self.assertRaises((RuntimeError,ValueError)):
            invoke({'op':'config.save','root':str(self.f.tree),'values':{'dangerously_allow_public_board':True}})
        self.assertFalse((self.f.tree/'.worklane.toml').exists())

    def test_revoked_pending_feedback_blocks_claim_and_cached_handoff(self):
        snapshot=self.pair('feedback')
        self.task.update(phase='rework',human_feedback=snapshot,human_feedback_pending=True,
                         human_feedback_snapshot=snapshot,human_feedback_observed_at='2026-10-19T00:00:00Z',
                         completed_turns={'build':{'verdict':'pass'}})
        self.f.state.save(self.task);before=self.f.state.load(1)
        self.config['permissions']['trusted']='read';self.write()
        for native in (False,True):
            for method in ('agent_result','pick'):
                with self.subTest(native=native,method=method), self.assertRaisesRegex(RuntimeError,'authorization'):
                    self.call(native,method,item=copy.deepcopy(self.task['item']))
                self.assertEqual(self.f.state.load(1),before)
        self.assertEqual(json.loads(self.control.read_text()).get('status',w.READY),w.READY)

    def test_trusted_recovery_and_revocation_then_private_resume_keep_exact_session(self):
        with w.locked(self.f.state.path/'runner.lock') as fd:
            NativeBackend('provider/model',self.f.tree,self.f.state,fd,'opencode').run('build',self.f.task,self.f.tree)
            saved=self.f.state.session(1,'worker')
            self.config['security']['node']['public']=True;self.write()
            for native in (False,True):
                with self.assertRaisesRegex(RuntimeError,'security preflight'):self.call(native,'before_turn')
            self.assertEqual(self.f.state.session(1,'worker'),saved)
            self.config['security']=copy.deepcopy(PRIVATE);self.write()
            self.pair('before_turn')
            self.f.backend(fd).run('build',self.f.task,self.f.tree)
            self.assertEqual(self.f.state.session(1,'worker'),saved)
            self.assertEqual({run['session_id'] for run in self.f.state.runs(1)},{saved['session_id']})

    def test_trusted_recovery_matches_for_interrupted_retry_ready_and_in_progress(self):
        snapshot=self.pair('feedback');self.task['human_feedback']=snapshot
        for phase in ('interrupted','retry'):
            for status in (w.READY,w.PROGRESS):
                observed=[]
                for native in (False,True):
                    self.task.update(phase=phase);self.task['item']['status']=status
                    self.config['status']=status;self.write();self.f.state.save(self.task)
                    observed.append(self.call(native,'recover',items=[copy.deepcopy(self.task['item'])]))
                    self.assertEqual(json.loads(self.control.read_text())['status'],w.PROGRESS)
                self.assertEqual(observed[0],observed[1])

    def test_queued_followup_rechecks_visibility_without_replacing_conversation(self):
        from concurrent.futures import ThreadPoolExecutor
        import itertools
        import time
        with w.locked(self.f.state.path/'runner.lock') as fd:
            for offset,(provider,native,stage) in enumerate(itertools.product(('codex','opencode'),(False,True),w.ROLES)):
                role=w.ROLES[stage]
                task={'number':10+offset,'phase':'building'};self.f.state.save(task)
                calls_path=self.f.root/('calls.jsonl' if provider=='codex' else 'opencode-calls.jsonl')
                def calls():return [json.loads(line) for line in calls_path.read_text().splitlines()] if calls_path.exists() else []
                before=len(calls())
                self.config['security']=copy.deepcopy(PRIVATE);self.write()
                if native:
                    def run():return invoke({'op':'agent.run','guarded':True,'repo':'owner/repo','state_root':str(self.f.root),
                        'root':str(self.f.tree),'tree':str(self.f.tree),'task':task,'stage':stage,'config':{'harness':provider},
                        'runner_fd':fd},pass_fds=(fd,))
                else:
                    from tools.port_security_cases import selected_board
                    board=selected_board(w,{'repo':'owner/repo'})
                    backend=w.AgentBackend(None,self.f.tree,self.f.state,fd,provider)
                    import argparse
                    runner=w.Runner(argparse.Namespace(base='main'),'owner/repo',self.f.tree,self.f.state,board,backend)
                    def run():return backend.run(stage,task,self.f.tree)
                with patch.dict(os.environ,FAKE_DELAY='.4'),ThreadPoolExecutor(1) as pool:
                    future=pool.submit(run); deadline=time.monotonic()+5
                    while not self.f.state.session(task['number'],role) and time.monotonic()<deadline:time.sleep(.01)
                    saved=self.f.state.session(task['number'],role);self.assertIsNotNone(saved)
                    active=self.f.state.runs(task['number'])[-1];self.f.state.queue_input(active['id'],'Retain this request')
                    self.config['security']['node']['public']=True;self.write()
                    with self.assertRaisesRegex(RuntimeError,'security preflight'):future.result(timeout=8)
                self.assertEqual(len(calls()),before+1)
                self.assertEqual(self.f.state.session(task['number'],role),saved)
                with self.f.state.connect() as db:
                    self.assertEqual(w.State._get(db,'input:'+saved['session_id'],[])[0]['status'],'sending')
                self.config['security']=copy.deepcopy(PRIVATE);self.write()
                w.AgentBackend(None,self.f.tree,self.f.state,fd,provider).run(stage,task,self.f.tree)
                self.assertEqual(self.f.state.session(task['number'],role),saved)
                self.assertEqual(len(calls()),before+2)
                self.assertIn('Retain this request',calls()[-1]['prompt'])
                with self.f.state.connect() as db:
                    self.assertEqual(w.State._get(db,'input:'+saved['session_id'],[]),[])
