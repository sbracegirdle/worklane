"""Real subprocess contract tests for harness routing, OpenCode events and recovery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import worklane as w
import worklane_tui as tui
from test_sessions import FAKE_CODEX

FAKE_OPENCODE = r'''
import json, os, pathlib, sys, time, uuid
root = pathlib.Path(os.environ['FAKE_ROOT'])
args = sys.argv[1:]
# OpenCode 2 changes directory to inherited PWD before handling any command.
if os.environ.get('PWD'):
    os.chdir(os.environ['PWD'])
with (root / 'opencode-invocations.jsonl').open('a') as handle:
    handle.write(json.dumps({'args': args, 'cwd': os.getcwd()}) + '\n')
if args[:2] == ['session', 'export']:
    exists = (root / args[-1]).exists() and not os.environ.get('FAKE_DELETED')
    print(json.dumps({'session': {'id': args[-1]}}) if exists else 'not found')
    sys.exit(0 if exists else 1)
if args[0] != 'run':
    sys.exit(0)  # Fake interactive resume; never start an actual agent.
prompt = sys.stdin.read()
with (root / 'opencode-calls.jsonl').open('a') as handle:
    handle.write(json.dumps({'args': args, 'prompt': prompt, 'cwd': os.getcwd(),
                             'config': json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])}) + '\n')
session = args[args.index('--session') + 1] if '--session' in args else 'ses_' + uuid.uuid4().hex
if os.environ.get('FAKE_MISMATCH'):
    session = 'ses_' + uuid.uuid4().hex
(root / session).touch()
def emit(kind, part=None):
    event = {'type': kind, 'part': part or {}}
    if not os.environ.get('FAKE_NO_ID'):
        event['sessionID'] = session
    print(json.dumps(event), flush=True)
emit('step_start')
time.sleep(float(os.environ.get('FAKE_DELAY', '0')))
if os.environ.get('FAKE_FAIL'):
    print('provider unavailable', file=sys.stderr, flush=True)
    sys.exit(1)
if os.environ.get('FAKE_ERROR'):
    emit('error', {'message': 'provider error'})
value = {'verdict': 'pass', 'pr_url': 'https://github.com/owner/repo/pull/9',
         'head_sha': 'a' * 40, 'summary': 'Checks passed', 'findings': []}
if os.environ.get('FAKE_TOOL_STEP'):
    emit('text', {'text': 'Inspecting files'})
    emit('tool_use', {'tool': 'bash', 'state': {'output': 'tests passed'}})
    emit('step_finish', {'reason': 'tool-calls'})
    if os.environ.get('FAKE_TOOL_ONLY'):
        sys.exit(0)
    emit('step_start')
if os.environ.get('FAKE_RESULT'):
    value = json.loads(os.environ['FAKE_RESULT'])
text = json.dumps(value) if not os.environ.get('FAKE_BAD_JSON') else 'invalid'
emit('text', {'text': text[:15]})
emit('text', {'text': text[15:]})
if not os.environ.get('FAKE_NO_FINISH'):
    emit('step_finish', {'reason': os.environ.get('FAKE_REASON', 'stop')})
'''


class HarnessTests(unittest.TestCase):
    def safe_resume(self, *args, **kwargs):
        with patch.object(w, "Board"):
            return tui.resume_session(*args, **kwargs)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tree = self.root / 'checkout'
        self.tree.mkdir()
        self.controller = self.root / 'controller'
        self.controller.mkdir()
        self.state = w.State('owner/repo', self.root)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name, script in [('codex', FAKE_CODEX), ('opencode', FAKE_OPENCODE)]:
            executable = self.bin / name
            executable.write_text(f'#!{sys.executable}\n' + script)
            executable.chmod(0o755)
        env = patch.dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                         FAKE_ROOT=str(self.root), PWD=str(self.controller))
        env.start()
        self.addCleanup(env.stop)
        self.task = {'number': 1, 'phase': 'building', 'issue': {'title': 'Harness support'}}
        self.state.save(self.task)

    def backend(self, fd, model='provider/model'):
        return w.AgentBackend(model, self.tree, self.state, fd, 'opencode')

    def calls(self):
        file = self.root / 'opencode-calls.jsonl'
        return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []

    def test_queued_input_resumes_exact_opencode_conversation_for_every_role(self):
        for stage, role in w.ROLES.items():
            with self.subTest(role=role), w.locked(self.state.path / 'runner.lock') as fd, \
                 patch.dict(os.environ, FAKE_DELAY='.2'), ThreadPoolExecutor(1) as pool:
                before = len(self.calls())
                future = pool.submit(self.backend(fd).run, stage, self.task, self.tree)
                deadline = time.monotonic() + 5
                while not self.state.session(1, role) and time.monotonic() < deadline:
                    time.sleep(.005)
                session = self.state.session(1, role)
                self.assertIsNotNone(session)
                run = self.state.load(1)['active_run_id']
                self.state.queue_input(run, 'Check Unicode 界 before handoff')
                output = future.result(timeout=5)
                calls = self.calls()[before:]
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1]['args'][calls[1]['args'].index('--session') + 1], session['session_id'])
                self.assertIn('Check Unicode 界 before handoff', calls[1]['prompt'])
                self.assertEqual(self.state.session(1, role)['session_id'], session['session_id'])
                self.assertEqual(self.state.advance_run(run, output), [])

    def test_failed_opencode_queued_input_is_retried_with_saved_session(self):
        with w.locked(self.state.path / 'runner.lock') as fd:
            self.backend(fd).run('build', self.task, self.tree)
            session = self.state.session(1, 'worker')
            run = self.state.begin_run(self.task, 'build', self.tree)
            self.state.queue_input(run, 'Retry this input')
            self.state.advance_run(run)
            self.state.finish_run(run, 'interrupted')
            with patch.dict(os.environ, FAKE_FAIL='1'), self.assertRaisesRegex(RuntimeError, 'exited'):
                self.backend(fd).run('build', self.task, self.tree)
            self.backend(fd).run('build', self.task, self.tree)
            calls = self.calls()
            for call in calls[1:]:
                self.assertEqual(call['args'][call['args'].index('--session') + 1], session['session_id'])
                self.assertIn('Retry this input', call['prompt'])

    def test_inherited_pwd_cannot_redirect_runs_exports_or_interactive_resume(self):
        for stage, role in w.ROLES.items():
            with self.subTest(role=role):
                tree = self.root / role
                tree.mkdir()
                with w.locked(self.state.path / 'runner.lock') as fd:
                    self.backend(fd).run(stage, self.task, tree)
                    self.backend(fd).run(stage, self.task, tree)
                self.safe_resume(self.state, 1, role)
                invocations = [json.loads(line) for line in
                               (self.root / 'opencode-invocations.jsonl').read_text().splitlines()][-5:]
                self.assertEqual([call['args'][0] for call in invocations],
                                 ['run', 'session', 'run', 'session', str(tree)])
                self.assertEqual([call['cwd'] for call in invocations], [str(tree)] * 5)
                self.assertEqual(os.environ['PWD'], str(self.controller))

    def test_deleted_controller_pwd_does_not_prevent_worker_start(self):
        with w.locked(self.state.path / 'runner.lock') as fd, \
             patch.dict(os.environ, PWD=str(self.root / 'deleted-controller')):
            self.assertEqual(self.backend(fd).run('build', self.task, self.tree)['verdict'], 'pass')
        self.assertEqual(self.calls()[0]['cwd'], str(self.tree))

    def test_each_role_has_separate_named_session_and_persistent_wire_events(self):
        with w.locked(self.state.path / 'runner.lock') as fd:
            backend = self.backend(fd)
            for stage in w.ROLES:
                self.assertEqual(backend.run(stage, self.task, self.tree)['verdict'], 'pass')
        reopened = w.State('owner/repo', self.root)
        sessions = reopened.load(1)['sessions']
        self.assertEqual(len({s['session_id'] for s in sessions.values()}), 3)
        for call, role in zip(self.calls(), w.ROLES.values()):
            self.assertIn(f'owner/repo #1 [{role}]: Harness support', call['args'])
            self.assertEqual(call['cwd'], str(self.tree))
            self.assertIn('provider/model', call['args'])
            self.assertIn(json.dumps(w.RESULT_SCHEMA), call['prompt'])
            self.assertIn('Do not change board status', call['prompt'])
            self.assertEqual(sessions[role]['backend'], 'opencode')
        events = reopened.events(reopened.runs(1)[0]['id'])
        self.assertTrue(any(e.get('sessionID') for e in events))
        self.assertTrue(any(e['type'] == 'turn.completed' for e in events))
        self.assertFalse([p for p in self.state.path.rglob('*') if p.suffix in {'.json', '.jsonl', '.log'}])

    def test_resume_after_failure_preserves_session_and_feedback(self):
        with w.locked(self.state.path / 'runner.lock') as fd:
            with patch.dict(os.environ, FAKE_FAIL='1'):
                with self.assertRaisesRegex(RuntimeError, 'exited 1'):
                    self.backend(fd).run('build', self.task, self.tree)
            session = self.state.session(1, 'worker')['session_id']
            self.task['human_feedback'] = {'comment': 'Fix the edge case'}
            reopened = w.State('owner/repo', self.root)
            w.AgentBackend('provider/model', self.tree, reopened, fd, 'opencode').run('build', self.task, self.tree)
        call = self.calls()[1]
        self.assertEqual(call['args'][call['args'].index('--session') + 1], session)
        self.assertIn('Fix the edge case', call['prompt'])
        self.assertIn('Continue this existing conversation', call['prompt'])
        self.assertEqual([r['status'] for r in reopened.runs(1)], ['failed', 'completed'])

    def test_saved_harness_and_model_survive_configuration_changes(self):
        with w.locked(self.state.path / 'runner.lock') as fd:
            self.backend(fd).run('build', self.task, self.tree)
            args = w.parser().parse_args(['run', '--harness', 'codex', '--model', 'different-model'])
            router = w.HarnessBackend(args, self.tree, self.state, fd)
            router.run('build', self.task, self.tree)
            router.run('review', self.task, self.tree)
        self.assertIn('provider/model', self.calls()[1]['args'])
        self.assertEqual(self.state.session(1, 'worker')['backend'], 'opencode')
        self.assertEqual(self.state.session(1, 'reviewer')['backend'], 'codex')
        self.assertEqual(self.state.session(1, 'reviewer')['model'], 'different-model')

    def test_global_default_role_overrides_and_models(self):
        args = w.parser().parse_args(['run', '--harness', 'opencode', '--worker-model', 'provider/worker',
                                     '--reviewer-harness', 'codex', '--reviewer-model', 'review-model'])
        with w.locked(self.state.path / 'runner.lock') as fd:
            router = w.HarnessBackend(args, self.tree, self.state, fd)
            for stage in w.ROLES:
                router.run(stage, self.task, self.tree)
        self.assertEqual(self.state.session(1, 'worker')['model'], 'provider/worker')
        self.assertEqual(self.state.session(1, 'reviewer')['model'], 'review-model')
        self.assertEqual(self.state.session(1, 'finalizer')['backend'], 'opencode')
        self.assertNotIn('--model', self.calls()[1]['args'])

    def test_tool_steps_do_not_complete_turn_or_pollute_result(self):
        with w.locked(self.state.path / 'runner.lock') as fd, patch.dict(os.environ, FAKE_TOOL_STEP='1'):
            self.assertEqual(self.backend(fd).run('build', self.task, self.tree)['summary'], 'Checks passed')
        self.assertTrue(any(row['content'] == 'tests passed' for row in self.state.log_entries(1)))

    def test_missing_session_does_not_fall_back_to_new_conversation(self):
        with w.locked(self.state.path / 'runner.lock') as fd:
            with patch.dict(os.environ, FAKE_NO_ID='1'):
                with self.assertRaisesRegex(RuntimeError, 'session ID'):
                    self.backend(fd).run('build', self.task, self.tree)
            with self.assertRaisesRegex(RuntimeError, 'replacement conversation'):
                self.backend(fd).run('build', self.task, self.tree)
        self.assertEqual(len(self.calls()), 1)

    def test_deleted_session_blocks_before_starting_new_turn(self):
        with w.locked(self.state.path / 'runner.lock') as fd:
            self.backend(fd).run('build', self.task, self.tree)
            with patch.dict(os.environ, FAKE_DELETED='1'):
                with self.assertRaisesRegex(RuntimeError, 'replacement conversation'):
                    self.backend(fd).run('build', self.task, self.tree)
        self.assertEqual(len(self.calls()), 1)

    def test_mismatched_session_rolls_back_raw_event_and_mapping(self):
        with w.locked(self.state.path / 'runner.lock') as fd:
            self.backend(fd).run('build', self.task, self.tree)
            original = self.state.session(1, 'worker')['session_id']
            with patch.dict(os.environ, FAKE_MISMATCH='1'):
                with self.assertRaisesRegex(RuntimeError, 'different session'):
                    self.backend(fd).run('build', self.task, self.tree)
        self.assertEqual(self.state.session(1, 'worker')['session_id'], original)
        self.assertEqual(self.state.events(self.state.runs(1)[1]['id']), [])

    def test_rejects_wrong_worktree_and_harness(self):
        other = self.root / 'other'
        other.mkdir()
        with w.locked(self.state.path / 'runner.lock') as fd:
            self.backend(fd).run('build', self.task, self.tree)
            with self.assertRaisesRegex(RuntimeError, 'another worktree'):
                self.backend(fd).run('build', self.task, other)
            with self.assertRaisesRegex(RuntimeError, 'another harness'):
                w.AgentBackend(w.MODEL, self.tree, self.state, fd).run('build', self.task, self.tree)
        self.assertEqual(len(self.calls()), 1)

    def test_signal_interruption_and_controller_restart_resume_exact_id(self):
        with w.locked(self.state.path / 'runner.lock') as fd:
            with patch.dict(os.environ, FAKE_DELAY='5'), ThreadPoolExecutor(1) as pool:
                future = pool.submit(self.backend(fd).run, 'build', self.task, self.tree)
                deadline = time.monotonic() + 3
                while not self.state.session(1, 'worker') and time.monotonic() < deadline:
                    time.sleep(.01)
                session = self.state.session(1, 'worker')['session_id']
                os.killpg(self.state.load(1)['child_pid'], signal.SIGTERM)
                with self.assertRaises(w.InterruptedRun):
                    future.result(timeout=3)
            reopened = w.State('owner/repo', self.root)
            w.AgentBackend('provider/model', self.tree, reopened, fd, 'opencode').run('build', reopened.load(1), self.tree)
        self.assertEqual(reopened.session(1, 'worker')['session_id'], session)
        self.assertEqual([r['status'] for r in reopened.runs(1)], ['interrupted', 'completed'])

    def test_timeout_retains_session_and_immediate_stop_starts_no_process(self):
        with w.locked(self.state.path / 'runner.lock') as fd:
            backend = self.backend(fd)
            backend.timeout = .1
            with patch.dict(os.environ, FAKE_DELAY='5'):
                with self.assertRaises(w.InterruptedRun):
                    backend.run('build', self.task, self.tree)
            self.assertIsNotNone(self.state.session(1, 'worker'))
            self.state.set_meta('shutdown', {'immediate': True})
            with self.assertRaises(w.InterruptedRun):
                backend.run('review', self.task, self.tree)
        self.assertEqual(len(self.calls()), 1)

    def test_incomplete_failed_and_invalid_results_never_pass(self):
        cases = [dict(FAKE_NO_FINISH='1'), dict(FAKE_TOOL_STEP='1', FAKE_TOOL_ONLY='1'),
                 dict(FAKE_ERROR='1'), dict(FAKE_REASON='length'), dict(FAKE_BAD_JSON='1'),
                 dict(FAKE_RESULT='[]'), dict(FAKE_RESULT='{"verdict":"pass"}'),
                 dict(FAKE_RESULT=json.dumps({'verdict': 'pass', 'pr_url': '', 'head_sha': '',
                                             'summary': 'invalid findings', 'findings': [1]}))]
        with w.locked(self.state.path / 'runner.lock') as fd:
            for index, env in enumerate(cases, 2):
                with self.subTest(env=env), patch.dict(os.environ, env):
                    task = {'number': index, 'phase': 'building'}
                    self.state.save(task)
                    with self.assertRaises((RuntimeError, ValueError)):
                        self.backend(fd).run('build', task, self.tree)
                    self.assertEqual(self.state.runs(index)[0]['status'], 'failed')

    def test_permissions_disable_subagents_and_preserve_user_denials(self):
        config = {'permission': {'bash': 'deny', 'task': 'allow'}, 'model': 'provider/configured'}
        with w.locked(self.state.path / 'runner.lock') as fd, patch.dict(os.environ, OPENCODE_CONFIG_CONTENT=json.dumps(config)):
            self.backend(fd, None).run('build', self.task, self.tree)
        call = self.calls()[0]
        self.assertEqual(call['config']['permission'], {'bash': 'deny', 'task': 'deny'})
        self.assertEqual(call['config']['model'], 'provider/configured')
        self.assertNotIn('--model', call['args'])

    def test_interactive_resume_uses_saved_harness_with_lock_and_checks_missing_history(self):
        with w.locked(self.state.path / 'runner.lock') as fd:
            self.backend(fd).run('build', self.task, self.tree)
        invoke = Mock(return_value=subprocess.CompletedProcess([], 0))
        self.safe_resume(self.state, 1, 'worker', invoke=invoke)
        self.assertEqual(invoke.call_args.args[0], ['opencode', str(self.tree), '--session', self.state.session(1, 'worker')['session_id']])
        self.assertEqual(invoke.call_args.kwargs['cwd'], self.tree)
        self.assertEqual(len(invoke.call_args.kwargs['pass_fds']), 1)
        self.assertEqual(json.loads(invoke.call_args.kwargs['env']['OPENCODE_CONFIG_CONTENT'])['permission']['task'], 'deny')
        with patch.dict(os.environ, FAKE_DELETED='1'):
            with self.assertRaisesRegex(RuntimeError, 'replacement conversation'):
                self.safe_resume(self.state, 1, 'worker', invoke=invoke)
        self.assertEqual(invoke.call_count, 1)

    def test_tui_restart_preserves_all_harness_options_and_explicit_overrides(self):
        saved = {'harness': 'opencode', 'model': None, 'worker_harness': None, 'worker_model': 'provider/worker',
                 'reviewer_harness': 'codex', 'reviewer_model': 'review-model', 'finalizer_harness': None, 'finalizer_model': None}
        self.state.set_meta('runtime', {'project': 'owner/2', 'model': 'harness default', 'harness_options': saved})
        dashboard = tui.Dashboard(Mock(), self.state, 'owner/repo', 1, root=self.tree, options={'harness': 'codex'})
        with patch.object(w, 'command', return_value='') as command:
            tui.controller_action('start', self.tree, dashboard.settings())
        argv = command.call_args.args[0]
        self.assertEqual(argv[argv.index('--harness') + 1], 'codex')
        self.assertIn('--reviewer-harness', argv)
        self.assertIn('provider/worker', argv)
        self.assertNotIn('harness default', argv)

    def test_check_requires_only_selected_binaries_and_reports_configuration(self):
        output = io.StringIO()
        board = Mock(project={'url': 'board'}, options={})
        board.security = {"safe": True}
        board.items.return_value = []
        with patch.object(w, 'repository', return_value=('owner/repo', self.tree, 'main')), \
             patch.object(w, 'State', return_value=self.state), patch.object(w, 'Board', return_value=board), \
             patch.object(w.shutil, 'which', side_effect=lambda name: '/mock/' + name if name != 'codex' else None), \
             redirect_stdout(output):
            self.assertEqual(w.main(['check', '--harness', 'opencode']), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['harness_options']['harness'], 'opencode')
        self.assertEqual(report['model'], 'harness default')
        board.ensure_review_status.assert_not_called()

    def test_background_start_forwards_role_options(self):
        board = Mock(owner='owner', number=2)
        child = Mock(pid=123, poll=Mock(return_value=None))
        options = ['--harness', 'opencode', '--reviewer-harness', 'codex', '--reviewer-model', 'review-model']
        self.state.set_meta('runtime', {'pid': 123})
        with patch.object(w, 'repository', return_value=('owner/repo', self.tree, 'main')), \
             patch.object(w, 'State', return_value=self.state), patch.object(w, 'Board', return_value=board), \
             patch.object(w.shutil, 'which', return_value='/mock/binary'), \
             patch.object(w.subprocess, 'Popen', return_value=child) as launch, redirect_stdout(io.StringIO()):
            self.assertEqual(w.main(['start', *options]), 0)
        argv = launch.call_args.args[0]
        for flag in options[::2]:
            self.assertEqual(argv[argv.index(flag) + 1], options[options.index(flag) + 1])


@unittest.skipUnless(os.environ.get('WORKLANE_TEST_OPENCODE') == '1',
                     'Set WORKLANE_TEST_OPENCODE=1 for the installed CLI empty-input probe')
class InstalledOpenCodeTests(unittest.TestCase):
    def test_inherited_pwd_does_not_override_subprocess_worktree(self):
        self.assertIsNotNone(shutil.which('opencode'), 'OpenCode must be installed for this opt-in check')
        with tempfile.TemporaryDirectory(prefix='worklane-opencode-pwd-') as directory:
            root = Path(directory)
            tree, controller = root / 'worktree', root / 'controller'
            tree.mkdir()
            controller.mkdir()
            for inherited_pwd in (controller, root / 'deleted-controller'):
                with self.subTest(inherited_pwd=inherited_pwd), patch.dict(os.environ, {
                    'PWD': str(inherited_pwd), 'XDG_DATA_HOME': str(root / 'data'),
                    'XDG_STATE_HOME': str(root / 'state'), 'XDG_CACHE_HOME': str(root / 'cache'),
                    'XDG_CONFIG_HOME': str(root / 'config'),
                }):
                    harness = w.HARNESSES['opencode'](None, controller, Mock(repo='owner/repo'))
                    args, _, env, _ = harness.prepare('build', {'number': 30}, tree, None, '', {}, -1, -1)
                    # Empty input stops before any provider or agent turn can run.
                    completed = subprocess.run(args, input='', cwd=tree, env=env,
                                               text=True, capture_output=True, timeout=30)
                    output = completed.stdout + completed.stderr
                    self.assertIn('You must provide a message', output)
                    self.assertNotIn('Failed to change directory', output)


if __name__ == '__main__':
    unittest.main()
