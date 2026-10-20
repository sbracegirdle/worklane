"""Core group 10 exercises the installed ELF using the existing disposable command fixtures."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from tools.port_parity import ROOT, execute

FIXTURES = ROOT / 'parity/fixtures/cli.json'
SCENARIOS = {row['id']: row for row in json.loads(FIXTURES.read_text())['scenarios']}


def security(public=False, permission='ADMIN', update=True):
    return {'prefix':['api','-i','graphql'], 'stdin_contains':['viewerCanUpdate'],
            'stdout':json.dumps({'data':{'node':{'public':public,'viewerCanUpdate':update},
                                        'repository':{'viewerPermission':permission}}})}


def board_prefix(fields=False, public=False):
    rules=copy.deepcopy(SCENARIOS['cli.add-ready']['transport']['gh'][:3 if fields else 2])
    rules[1]=security(public=public)
    return rules


def scenario(name, steps, **kwargs):
    return {'id':name,'steps':steps,**kwargs}


@unittest.skipUnless(os.environ.get('WORKLANE_TEST_RUST')=='1','opt-in native installed-command scenarios')
class NativeCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary=os.environ.get('WORKLANE_TEST_CLI_BINARY')
        if not binary:
            subprocess.run(['cargo','build','--locked','--offline','--bin','worklane'],cwd=ROOT,check=True,capture_output=True)
        cls.binary=Path(binary).resolve() if binary else ROOT/'target/debug/worklane'
        if not cls.binary.read_bytes().startswith(b'\x7fELF'):
            raise AssertionError('Core CLI must be an installed native ELF, never a Python launcher')
        cls.observations=[]

    @classmethod
    def tearDownClass(cls):
        destination=os.environ.get('WORKLANE_NATIVE_CLI_EVIDENCE')
        if destination:
            paths=[Path(__file__).resolve(),FIXTURES,ROOT/'tools/port_parity.py',ROOT/'tools/parity_tool.py',ROOT/'tools/parity_git.py',
                   ROOT/'Cargo.toml',ROOT/'Cargo.lock',ROOT/'runtime/Cargo.toml',ROOT/'runtime/build.rs',*sorted((ROOT/'runtime/src').rglob('*'))]
            paths=[p for p in paths if p.is_file()]
            report={'scenario_group':'core.10.native-cli','candidate_runtime':'native','candidate':[str(cls.binary)],
                    'candidate_revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                    'reference':json.loads((ROOT/'parity/inventory.json').read_text())['revision'],
                    'parent_revision':'5cc71a5880dc77727987e4a496c53181f5e5d8f0',
                    'candidate_sha256':hashlib.sha256(cls.binary.read_bytes()).hexdigest(),
                    'driver':'test_native_cli.py','input_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
                    'results':cls.observations,'core_release_ready':False,'exhaustive_parity_complete':False,
                    'limits':['Controlled GitHub/provider responses; no live issue writes or provider turns.',
                              'Native dashboard belongs to #52; installed cutover and end-to-end backlog validation belong to #53.',
                              'Line-oriented config replaces the terminal form in this CLI layer; exact help/error presentation parity remains advisory.']}
            Path(destination).write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')

    def observe(self, fixture, assertions=None):
        try:
            result=execute([str(self.binary)],fixture)
            if assertions:assertions(result)
        except Exception as error:
            self.observations.append({'id':fixture['id'],'status':'fail','error':str(error)})
            raise
        self.observations.append({'id':fixture['id'],'status':'pass','observations':result})
        return result

    def test_existing_command_families_and_negative_arguments(self):
        excluded={'cli.tui-redirect','cli.config-redirect','tui.default-quit','tui.first-run-cancel','tui.uppercase-stop-now'}
        for name,fixture in SCENARIOS.items():
            if name in excluded:continue
            with self.subTest(scenario=name):
                if name=='cli.retry-number':
                    # Invalid input now fails before discovery; no GitHub access is needed for usage guidance.
                    fixture=copy.deepcopy(fixture);fixture.pop('transport');fixture['id']='cli.native-retry-number-before-network'
                self.observe(fixture)

    def test_offline_version_and_native_dashboard_boundary(self):
        self.observe(scenario('cli.native-identity',[
            {'argv':['--version','--repo','/missing/checkout'],'stdout_contains':['"runtime": "native"','"revision"','"executable"']},
            {'argv':['tui'],'exit':1,'stderr_contains':['Native TUI','status, logs, config or start']},
            {'argv':['logs','0'],'exit':2,'stderr_contains':['positive integer']},
        ],observe_files=['state/worklane/owner--repo/state.sqlite3']))
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([str(self.binary),'--version'],cwd=directory,text=True,capture_output=True,check=True)
            self.assertEqual(json.loads(result.stdout)['runtime'],'native')
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_config_precedence_atomic_save_cancel_and_validation(self):
        values=json.loads(subprocess.check_output([str(self.binary),'--version'],text=True))
        self.assertEqual(values['runtime'],'native')
        self.observe(scenario('cli.native-config',[
            {'argv':['config','--show'],'stdout_contains':['"workers": 4','"reviewer_harness": "opencode"']},
            {'argv':['config','--show','--workers','2'],'stdout_contains':['"workers": 2','"reviewer_harness": "opencode"']},
            {'argv':['config','--save','--workers','2','--worker-model','custom'],'stdout_contains':['Saved']},
            {'argv':['config'],'stdin':'\u001b\n','stdout_contains':['Setup cancelled']},
            {'argv':['config','--show'],'stdout_contains':['"workers": 2','"worker_model": "custom"']},
            {'argv':['config','--save','--workers','17'],'exit':1,'stderr_contains':['workers must']},
            {'argv':['config','--show'],'stdout_contains':['"workers": 2']},
        ],files={'repo/.worklane.toml':'workers = 4\nreviewer_harness = "opencode"\n'},observe_files=['repo/.worklane.toml','state/worklane/owner--repo/state.sqlite3']),
            lambda r:self.assertIsNone(r['files']['state/worklane/owner--repo/state.sqlite3']))
        # Empty responses preserve all defaults; confirmation is the only write boundary.
        self.observe(scenario('cli.native-config-confirm',[
            {'argv':['config'],'stdin':'\n'*14+'y\n','stdout_contains':['Saved']},
            {'argv':['config','--show'],'stdout_contains':['"workers": 3']},
        ],observe_files=['repo/.worklane.toml']))

    def test_migrated_retry_retains_exact_session_and_worktree(self):
        session='2d662454-7b7c-4ba8-8e9f-2a28d0b6764a'
        self.observe(scenario('cli.native-retry-migrated-session',[
            {'argv':['retry','7','--project','owner/2'],'stdout_contains':['saved session']},
            {'argv':['status'],'stdout_contains':['"phase": "retry"',session,'"resume_stage": "build"','pending followup']},
            {'argv':['stack'],'stdout_contains':['"number": 7','"base_sha": "'+'a'*40+'"']},
        ],files={
            'state/worklane/owner--repo/tasks/7.json':json.dumps({'number':7,'phase':'blocked','failed_phase':'building',
                'active_stage':'build','worker_worktree':'{repo}','base':'main','base_sha':'a'*40,'stack':[],
                'log':'{state}/worklane/owner--repo/worker.jsonl','pending_input':'pending followup'}),
            'state/worklane/owner--repo/worker.jsonl':json.dumps({'type':'thread.started','thread_id':session})+'\n',
        },transport={'gh':board_prefix()},sqlite=[
            {'path':'state/worklane/owner--repo/state.sqlite3','query':'SELECT role,session_id FROM sessions'},
            {'path':'state/worklane/owner--repo/state.sqlite3','query':'SELECT count(*) FROM migration_archive'},
        ]),lambda r:self.assertEqual(r['sqlite'][0],[['worker',session]]))

    def test_emergency_controls_do_not_open_github_budget_state(self):
        # An unusable shared transport store must not prevent stopping owned processes.
        self.observe(scenario('cli.native-emergency-budget-isolation',[
            {'argv':['pause'],'stdout_contains':['Paused']},
            {'argv':['stop'],'stdout_contains':['Stop requested']},
            {'argv':['stop','--now'],'stdout_contains':['no owned processes remain']},
            {'argv':['status'],'stdout_contains':['"paused": true','"running": false']},
        ],files={'state/worklane/_github/tasks/1.json':'invalid transport migration'},
           observe_files=['state/worklane/_github/state.sqlite3']),
           lambda r:self.assertIsNone(r['files']['state/worklane/_github/state.sqlite3']))

    def test_public_board_refusal_and_invocation_only_consent(self):
        rules=board_prefix(public=True)
        rules+=board_prefix(public=True)+[{'argv':['project','view','2','--owner','owner','--web']}]
        rules+=board_prefix(public=True)
        self.observe(scenario('cli.native-public-board',[
            {'argv':['board','--project','owner/2'],'exit':1,'stderr_contains':['public']},
            {'argv':['board','--project','owner/2','--dangerously-allow-public-board'],'stdout_contains':['Opened project board']},
            {'argv':['board','--project','owner/2'],'exit':1,'stderr_contains':['public']},
        ],transport={'gh':rules}))
        for action in ('add','run','start','resume','retry'):
            args=[action,'--project','owner/2']
            if action=='add':args+=['-t','Safe title']
            if action=='retry':args.insert(1,'7')
            self.observe(scenario('cli.native-private-guard.'+action,[
                {'argv':args,'exit':1,'stderr_contains':['public']},
            ],transport={'gh':board_prefix(public=True)}))

    def test_read_only_check_rechecks_native_blockers_and_refuses_incomplete_pages(self):
        page={'nodes':[{'id':'item','fieldValueByName':{'name':'Ready for work'},
            'links':{'pullRequests':{'nodes':[],'pageInfo':{'hasNextPage':False,'endCursor':None}}},
            'content':{'number':7,'state':'OPEN','title':'blocked','repository':{'nameWithOwner':'owner/repo'},
                       'issueDependenciesSummary':{'blockedBy':0}}}],
              'totalCount':1,'pageInfo':{'hasNextPage':False,'endCursor':None}}
        rules=board_prefix(fields=True)+[
            {'prefix':['api','-i','graphql'],'stdin_contains':['items(first:100'], 'stdout':json.dumps({'data':{'node':{'items':page}}})},
            {'prefix':['api','-i','graphql'],'stdin_contains':['issueDependenciesSummary'],
             'stdout':json.dumps({'data':{'repository':{'issue':{'issueDependenciesSummary':{'blockedBy':1}}}}})}]
        self.observe(scenario('cli.native-check-live-blocker',[
            {'argv':['check','--project','owner/2','--workers','2'],'stdout_contains':['"ready": []','"workers": 2','"reviewers": 1']},
        ],transport={'gh':rules}))
        broken=copy.deepcopy(page);broken['totalCount']=2
        self.observe(scenario('cli.native-check-incomplete-page',[
            {'argv':['check','--project','owner/2'],'exit':1,'stderr_contains':['incomplete board pagination']},
        ],transport={'gh':board_prefix(fields=True)+[{'prefix':['api','-i','graphql'],'stdin_contains':['items(first:100'],
            'stdout':json.dumps({'data':{'node':{'items':broken}}})}]}))
        self.observe(scenario('cli.native-check-public',[
            {'argv':['check','--project','owner/2'],'exit':1,'stdout_contains':['"safe": false','"visibility": "public"']},
        ],transport={'gh':board_prefix(public=True)}))

    def test_detached_controller_effective_options_controls_and_owned_stop(self):
        # A live detached controller and its caller share gh concurrently; dispatch a read-only
        # responder instead of imposing an artificial ordering on independent security checks.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);repo=root/'repo';repo.mkdir();binary=root/'bin';binary.mkdir()
            env={**os.environ,'HOME':str(root),'XDG_STATE_HOME':str(root/'state'),
                 'PATH':str(binary)+':/usr/bin:/bin','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1'}
            for args in (['init','-q','-b','main'],['remote','add','origin','https://github.com/owner/repo.git'],
                         ['symbolic-ref','refs/remotes/origin/HEAD','refs/remotes/origin/main']):
                subprocess.run(['git',*args],cwd=repo,env=env,check=True,capture_output=True)
            (repo/'.worklane.toml').write_text('workers = 4\n')
            project=json.loads(board_prefix()[0]['stdout'])
            fields=json.loads(board_prefix(fields=True)[2]['stdout'])
            script = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
body=json.load(sys.stdin) if '--input' in args else None
query=body.get('query','') if body else ''
with (Path(os.environ['HOME'])/'calls.jsonl').open('a') as log:
    log.write(json.dumps({'argv':args,'query':query})+'\\n')
if args[:2]==['project','view']: result=PROJECT
elif args[:2]==['project','field-list']: result=FIELDS
elif args[:2]==['auth','setup-git']: result={}
elif 'viewerCanUpdate' in query:
    result={'data':{'node':{'public':False,'viewerCanUpdate':True},'repository':{'viewerPermission':'ADMIN'}}}
elif 'items(first:100' in query:
    result={'data':{'node':{'items':{'nodes':[],'totalCount':0,'pageInfo':{'hasNextPage':False,'endCursor':None}}}}}
else: raise AssertionError('Unexpected command: '+str(args)+' '+query)
print(json.dumps(result))
'''.replace('PROJECT',repr(project)).replace('FIELDS',repr(fields))
            for name,source in [('gh',script),('codex','#!/bin/sh\nexit 0\n'),('opencode','#!/bin/sh\nexit 0\n')]:
                path=binary/name;path.write_text(source.replace('#!/usr/bin/env python3','#!'+__import__('sys').executable));path.chmod(0o700)
            observations=[]
            def invoke(*args, exit=0):
                process=subprocess.run([str(self.binary),*args,'--repo',str(repo)],env=env,text=True,capture_output=True,timeout=15)
                observations.append({'argv':args,'exit':process.returncode,'stdout':process.stdout,'stderr':process.stderr})
                self.assertEqual(process.returncode,exit,process.stderr)
                return process.stdout
            unrelated=subprocess.Popen(['sleep','60'],start_new_session=True)
            try:
                self.assertIn('Started owner/repo',invoke('start','--project','owner/2','--workers','2','--interval','10',
                                                         '--reviewer-harness','opencode','--reviewer-model','fixture'))
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    calls=[json.loads(line) for line in (root/'calls.jsonl').read_text().splitlines()]
                    if len(calls)>=16:break
                    time.sleep(.02)
                self.assertGreaterEqual(len(calls),16,'Detached scheduler never completed the controlled empty batch')
                status=json.loads(invoke('status'))
                self.assertTrue(status['running']);self.assertEqual(status['runtime']['workers'],2)
                self.assertEqual(status['runtime']['reviewer_model'],'fixture')
                self.assertEqual(status['runtime']['reviewer_harness'],'opencode')
                self.assertEqual(status['runtime']['cli']['runtime'],'native')
                invoke('pause');self.assertTrue(json.loads(invoke('status'))['paused'])
                invoke('resume','--project','owner/2');self.assertFalse(json.loads(invoke('status'))['paused'])
                invoke('run','--once','--project','owner/2',exit=1)
                self.assertIn('no owned processes remain',invoke('stop','--now'))
                status=json.loads(invoke('status'));self.assertFalse(status['running']);self.assertTrue(status['paused'])
                self.assertIsNone(unrelated.poll(),'Stopping the owned controller killed an unrelated process')
                self.observations.append({'id':'cli.native-detached-controller','status':'pass','observations':observations,'calls':calls})
            finally:
                subprocess.run([str(self.binary),'stop','--now','--repo',str(repo)],env=env,capture_output=True,timeout=15)
                unrelated.terminate();unrelated.wait(timeout=5)

    def test_stop_during_start_preflight_preserves_shutdown_and_does_not_launch(self):
        for checkpoint in (1,2):
            with self.subTest(project_preflight=checkpoint),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);repo=root/'repo';repo.mkdir();bins=root/'bin';bins.mkdir()
                env={**os.environ,'HOME':str(root),'XDG_STATE_HOME':str(root/'state'),
                     'PATH':str(bins)+':/usr/bin:/bin','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1',
                     'BLOCK_PREFLIGHT':str(checkpoint)}
                for args in (['init','-q','-b','main'],['remote','add','origin','https://github.com/owner/repo.git'],
                             ['symbolic-ref','refs/remotes/origin/HEAD','refs/remotes/origin/main']):
                    subprocess.run(['git',*args],cwd=repo,env=env,check=True,capture_output=True)
                script='''import json,os,sys,time
from pathlib import Path
root=Path(os.environ['HOME']);args=sys.argv[1:]
body=json.load(sys.stdin) if '--input' in args else {}
query=body.get('query','')
if args[:2]==['project','view']:
    count=int((root/'count').read_text())+1 if (root/'count').exists() else 1
    (root/'count').write_text(str(count))
    if count==int(os.environ['BLOCK_PREFLIGHT']):
        (root/'blocked').touch()
        deadline=time.monotonic()+15
        while not (root/'release').exists() and time.monotonic()<deadline:time.sleep(.01)
        assert (root/'release').exists(),'preflight barrier timed out'
    result=PROJECT
elif args[:2]==['project','field-list']:result=FIELDS
elif args[:2]==['auth','setup-git']:result={}
elif 'viewerCanUpdate' in query:result={'data':{'node':{'public':False,'viewerCanUpdate':True},'repository':{'viewerPermission':'ADMIN'}}}
elif 'items(first:100' in query:result={'data':{'node':{'items':{'nodes':[],'totalCount':0,'pageInfo':{'hasNextPage':False,'endCursor':None}}}}}
else:raise AssertionError(str(args)+' '+query)
print(json.dumps(result))
'''.replace('PROJECT',repr(json.loads(board_prefix()[0]['stdout']))).replace('FIELDS',repr(json.loads(board_prefix(fields=True)[2]['stdout'])))
                for name,source in [('gh','#!'+__import__('sys').executable+'\n'+script),('codex','#!/bin/sh\nexit 0\n')]:
                    path=bins/name;path.write_text(source);path.chmod(0o700)
                def invoke(*args):
                    result=subprocess.run([str(self.binary),*args,'--repo',str(repo)],env=env,text=True,capture_output=True,timeout=15)
                    self.assertEqual(result.returncode,0,result.stderr)
                    return result.stdout
                start=subprocess.Popen([str(self.binary),'start','--project','owner/2','--repo',str(repo)],
                                       env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                try:
                    deadline=time.monotonic()+10
                    while not (root/'blocked').exists() and time.monotonic()<deadline:time.sleep(.01)
                    self.assertTrue((root/'blocked').exists(),'Start did not reach controlled preflight')
                    self.assertIn('no owned processes remain',invoke('stop','--now'))
                    before=json.loads(invoke('status'))
                    self.assertFalse(before['running']);self.assertTrue(before['paused'])
                    (root/'release').touch()
                    out,err=start.communicate(timeout=15)
                    after=json.loads(invoke('status'))
                    self.assertEqual(start.returncode,1,(out,err))
                    self.assertIn('Start cancelled by a concurrent stop request',err)
                    self.assertFalse(after['running']);self.assertTrue(after['paused'])
                    self.assertEqual(after['shutdown'],before['shutdown'])
                    self.assertEqual(after['runtime'],before['runtime'])
                    self.observations.append({'id':'cli.native-start-stop-race.'+str(checkpoint),'status':'pass',
                        'observations':{'before':before,'after':after,'start_exit':start.returncode,'stdout':out,'stderr':err}})
                finally:
                    (root/'release').touch()
                    if start.poll() is None:start.terminate()
                    start.communicate(timeout=5)
                    invoke('stop','--now')

    def test_detached_startup_failures_remain_in_database_logs(self):
        run_rules=copy.deepcopy(SCENARIOS['cli.empty-queue-run']['transport']['gh'][:5])
        # Authentication fails after runtime ownership is recorded; the detached process persists its error.
        self.observe(scenario('cli.native-startup-error',[
            {'argv':['run','--background','--once','--project','owner/2'],'exit':1,'stderr_contains':['provider unavailable']},
            {'argv':['logs'],'stdout_contains':['controller-error','provider unavailable']},
            {'argv':['status'],'stdout_contains':['"running": false']},
        ],transport={'gh':run_rules,'codex':[{'argv':['login','status'],'exit':1,'stderr':'provider unavailable'}]}))
