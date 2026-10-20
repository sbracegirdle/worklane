"""Run unchanged reference acceptance assertions with native storage and providers."""
import os
import json
import signal
import time
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import unittest
from unittest.mock import patch
from tools.native_test_bridge import NativeBackend, NativeRouter, NativeState, NativeLogWriter, invoke, ROOT
import test_sessions
import test_harness
import worklane as w

EVIDENCE = []

# Workflow/CLI/TUI tests stay in their owning port stages. These assertions exercise
# the storage and provider boundary through real fake-agent processes, not mocks.
SESSION_EXCLUDED = {
    'test_app_server_transport_timeout_eof_and_initialize_error',
    'test_human_feedback_dispatch_resumes_original_worker_with_batch',
    'test_conflict_rework_resumes_original_worker_with_findings',
    'test_signal_interruption_and_restart_keep_same_conversation',
    'test_cli_stop_now_then_run_resumes_saved_session',
    'test_recovery_respects_human_board_override',
    'test_interrupted_finalizer_skips_repeat_review',
    'test_reviewer_reuses_worktree_across_changed_pr_heads',
    'test_memory_backed_protocol_supports_managed_python_without_os_helper',
}
HARNESS_EXCLUDED = {
    'test_inherited_pwd_cannot_redirect_runs_exports_or_interactive_resume',
    'test_signal_interruption_and_controller_restart_resume_exact_id',
    'test_interactive_resume_uses_saved_harness_with_lock_and_checks_missing_history',
    'test_tui_restart_preserves_all_harness_options_and_explicit_overrides',
    'test_check_requires_only_selected_binaries_and_reports_configuration',
    'test_background_start_forwards_role_options',
}


def native_case(original, name):
    def run(self):
        reference = unittest.TestResult()
        original(name).run(reference)
        if reference.errors or reference.failures or reference.skipped:
            self.fail('Reference failed: '+repr(reference.errors+reference.failures+reference.skipped))
        with patch.multiple(w, State=NativeState, CodexBackend=NativeBackend,
                            AgentBackend=NativeBackend, HarnessBackend=NativeRouter, DatabaseLogWriter=NativeLogWriter):
            instance = original(name)
            result = unittest.TestResult()
            instance.run(result)
            if result.errors or result.failures:
                self.fail('\n'.join(text for _,text in result.errors + result.failures))
            if result.skipped:
                self.fail('Native parity assertion was skipped: '+repr(result.skipped))
        EVIDENCE.append({'id':original.__module__+'.'+original.__name__+'.'+name,
                         'reference':'pass','candidate':'pass'})
    return run


@unittest.skipUnless(os.environ.get('WORKLANE_TEST_RUST') == '1', 'opt-in native parity: WORKLANE_TEST_RUST=1')
class NativeAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(['cargo','build','--locked','--offline'],cwd=ROOT,check=True,capture_output=True)
        # Prevent a changed local test or oracle from masquerading as frozen parity.
        pin = json.loads((ROOT/'parity/inventory.json').read_text())['revision']
        for name in ['test_sessions.py','test_harness.py','worklane.py','worklane_harness.py','worklane_process.py']:
            actual = subprocess.check_output(['git','hash-object',name],cwd=ROOT)
            expected = subprocess.check_output(['git','rev-parse',f'{pin}:{name}'],cwd=ROOT)
            if actual != expected:
                raise AssertionError(f'Frozen oracle drift: {name}')

    @classmethod
    def tearDownClass(cls):
        if os.environ.get('WORKLANE_NATIVE_EVIDENCE'):
            from pathlib import Path
            Path(os.environ['WORKLANE_NATIVE_EVIDENCE']).write_text(json.dumps(EVIDENCE,indent=2)+'\n')


for source, exclude in [(test_sessions.SessionTests,SESSION_EXCLUDED),(test_harness.HarnessTests,HARNESS_EXCLUDED)]:
    for name in unittest.defaultTestLoader.getTestCaseNames(source):
        if name not in exclude:
            setattr(NativeAcceptanceTests,'test_'+source.__name__+'_'+name,native_case(source,name))


@unittest.skipUnless(os.environ.get('WORKLANE_TEST_RUST') == '1', 'opt-in native compatibility')
class CrossRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_harness.HarnessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def fake_github(self, response):
        executable = self.fixture.bin/'gh'
        executable.write_text(f'#!{sys.executable}\nimport os,sys\nsys.stdin.read()\nprint(os.environ["FAKE_GITHUB_RESPONSE"])\n')
        executable.chmod(0o755)
        return patch.dict(os.environ,FAKE_GITHUB_RESPONSE=json.dumps(response))

    def test_project_title_preference_uses_full_unicode_casefold(self):
        f=self.fixture
        project={'id':'preferred','number':2,'title':'ſeſſion','closed':False,'url':'board','owner':{'login':'owner'}}
        other={**project,'id':'other','number':3,'title':'Other'}
        response={'data':{'repository':{'projectsV2':{'nodes':[project,other]}}},'errors':[]}
        with self.fake_github(response):
            expected=w.Board('owner/session',f.tree,load_fields=False).project
            actual=invoke({'op':'board.project','repo':'owner/session','state_root':str(f.root)},cwd=f.tree)
        self.assertEqual(actual,expected)
        self.assertEqual(actual['id'],'preferred')

    def test_missing_reply_nodes_cannot_silently_truncate_resolved_outdated_threads(self):
        import argparse
        f=self.fixture
        response={'data':{'repository':{'pullRequest':{'reviewThreads':{
            'nodes':[{'id':'thread','isResolved':True,'isOutdated':True,'path':'file.rs',
                      'comments':{'nodes':None,'pageInfo':{'hasNextPage':False,'endCursor':None}}}],
            'pageInfo':{'hasNextPage':False,'endCursor':None}}}}}}
        runner=w.Runner(argparse.Namespace(base='main',once=True,interval=10),'owner/repo',f.tree,f.state,None,None)
        with self.fake_github(response):
            with self.assertRaises(TypeError):
                runner.review_threads('https://github.com/owner/repo/pull/1')
            with self.assertRaises(RuntimeError):
                invoke({'op':'workflow.review_threads','url':'https://github.com/owner/repo/pull/1',
                        'repo':'owner/repo','state_root':str(f.root)},cwd=f.tree)

    def test_python_native_python_exact_resume_for_both_providers_and_all_roles(self):
        f = self.fixture
        with w.locked(f.state.path / 'runner.lock') as fd:
            for backend in ('codex','opencode'):
                for offset,(stage,role) in enumerate(w.ROLES.items()):
                    task = {'number':10+offset+(100 if backend=='opencode' else 0),
                            'phase':'building','issue':{'title':'Cross-runtime 界'}}
                    f.state.save(task)
                    original = w.AgentBackend('provider/model',f.tree,f.state,fd,backend)
                    original.run(stage,task,f.tree)
                    saved = f.state.session(task['number'],role)
                    native = NativeBackend('provider/model',f.tree,f.state,fd,backend)
                    native.run(stage,task,f.tree)
                    original.run(stage,task,f.tree)
                    self.assertEqual(f.state.session(task['number'],role),saved)
                    runs = f.state.runs(task['number'])
                    self.assertEqual(len(runs),3)
                    self.assertEqual({run['session_id'] for run in runs},{saved['session_id']})
                    self.assertEqual([run['status'] for run in runs],['completed']*3)

    def test_python_sending_queue_is_consumed_once_by_native_then_python(self):
        f = self.fixture
        with w.locked(f.state.path / 'runner.lock') as fd:
            original = f.backend(fd)
            original.run('build',f.task,f.tree)
            interrupted = f.state.begin_run(f.task,'build',f.tree)
            f.state.queue_input(interrupted,'retained cross-runtime request 界')
            f.state.advance_run(interrupted)
            f.state.finish_run(interrupted,'interrupted')
            NativeBackend('provider/model',f.tree,f.state,fd,'opencode').run('build',f.task,f.tree)
            original.run('build',f.task,f.tree)
        calls = f.calls()
        self.assertIn('retained cross-runtime request',calls[1]['prompt'])
        self.assertNotIn('retained cross-runtime request',calls[2]['prompt'])
        self.assertEqual(len(calls),3)

    def test_oversized_integer_is_rejected_without_coercion_or_prior_state_loss(self):
        f = self.fixture
        f.state.set_meta('numeric',{'preserve':True})
        value = {'integer':2**100}
        with self.assertRaises(OverflowError):
            f.state.set_meta('numeric',value)
        with self.assertRaises(RuntimeError):
            NativeState('owner/repo',f.root).set_meta('numeric',value)
        self.assertEqual(f.state.get_meta('numeric'),{'preserve':True})
        f.state.set_meta('numeric',{'float':float(2**100),'boolean':True,'integer':1})
        self.assertEqual(NativeState('owner/repo',f.root).get_meta('numeric'),f.state.get_meta('numeric'))

    def test_native_read_model_matches_existing_python_history_and_unicode_search(self):
        import worklane_tui as tui
        f = self.fixture
        with w.locked(f.state.path / 'runner.lock') as fd:
            f.backend(fd).run('build',f.task,f.tree)
            first = f.state.runs(1)[0]['id']
            f.state.append_log(first,'agent','Straße 界 [literal].*')
            f.backend(fd).run('build',f.task,f.tree)
        request = {'state_root':str(f.root),'repo':'owner/repo'}
        original_snapshot = tui.snapshot(f.state)
        expected = json.loads(json.dumps(original_snapshot))
        actual = invoke({'op':'read.snapshot',**request})
        self.assertEqual(actual,expected)
        for needle in ('STRASSE','[literal].*','ses_','missing'):
            result = tui.search_sessions(f.state,needle,original_snapshot['rows'])
            found = invoke({'op':'read.search','query':needle,'rows':expected['rows'],**request})
            self.assertEqual(found,json.loads(json.dumps(result)))
            for hit in result['results']:
                self.assertEqual(invoke({'op':'read.context','result':hit,**request}),tui.search_context(f.state,hit))

    def test_fragmented_events_and_final_line_without_newline_match_both_runtimes(self):
        f = self.fixture
        executable = f.bin / 'opencode'
        executable.write_text(executable.read_text().replace(
            '    print(json.dumps(event), flush=True)',
            "    payload = (json.dumps(event) + ('\\n' if kind != 'step_finish' else '')).encode()\n"
            "    for offset in range(0,len(payload),7):\n"
            "        os.write(1,payload[offset:offset+7]); time.sleep(.0001)"))
        with w.locked(f.state.path / 'runner.lock') as fd:
            for implementation in (w.AgentBackend,NativeBackend):
                task = {'number':1 if implementation is w.AgentBackend else 2,'phase':'building'}
                f.state.save(task)
                result = implementation('provider/model',f.tree,f.state,fd,'opencode').run('build',task,f.tree)
                self.assertEqual(result['summary'],'Checks passed')
                run = f.state.runs(task['number'])[0]
                events = f.state.events(run['id'])
                self.assertEqual(sum(e.get('type')=='turn.completed' for e in events),1)

    def test_native_signal_interruption_resumes_exact_session_in_python(self):
        f = self.fixture
        with w.locked(f.state.path / 'runner.lock') as fd:
            with patch.dict(os.environ,FAKE_DELAY='5'), ThreadPoolExecutor(1) as pool:
                future = pool.submit(NativeBackend('provider/model',f.tree,f.state,fd,'opencode').run,
                                     'build',f.task,f.tree)
                deadline = time.monotonic()+5
                while not f.state.session(1,'worker') and time.monotonic()<deadline:
                    time.sleep(.01)
                session = f.state.session(1,'worker')['session_id']
                os.killpg(f.state.load(1)['child_pid'],signal.SIGTERM)
                with self.assertRaises(w.InterruptedRun):
                    future.result(timeout=5)
            f.backend(fd).run('build',f.state.load(1),f.tree)
        self.assertEqual(f.state.session(1,'worker')['session_id'],session)
        self.assertEqual([r['status'] for r in f.state.runs(1)],['interrupted','completed'])

    def test_metadata_partial_eof_error_and_size_limits_cleanup_in_both_runtimes(self):
        f = self.fixture
        executable = f.bin / 'codex'
        code = executable.read_text().replace('if args[0] == "app-server":',
            'if args[0] == "app-server":\n    (root / "server.pid").write_text(str(os.getpid()))')
        code = code.replace('        method, params = request["method"], request["params"]',
            '        method, params = request["method"], request["params"]\n'
            '        if os.environ.get("FAKE_RPC_OVERSIZE"):\n'
            '            sys.stdout.write("x" * (4*1024*1024+1)); sys.stdout.flush(); time.sleep(5)')
        executable.write_text(code)
        for env,diagnostic in [({'FAKE_RPC_PARTIAL':'1'},'timed out'),
                               ({'FAKE_RPC_EOF':'1'},'closed'),
                               ({'FAKE_RPC_ERROR':'initialize'},'unsupported method'),
                               ({'FAKE_RPC_OVERSIZE':'1'},'exceeded 4 MiB')]:
            for native in (False,True):
                with self.subTest(env=env,native=native), patch.dict(os.environ,env):
                    with self.assertRaisesRegex(RuntimeError,diagnostic):
                        if native:
                            invoke({'op':'app_server.request','timeout':.2,'method':'project/list','params':{}},cwd=f.tree)
                        else:
                            with w.CodexAppServer(f.tree,timeout=.2):
                                self.fail('metadata initialization should fail')
                    self.assertIsNone(w.process_identity(int((f.root/'server.pid').read_text())))
