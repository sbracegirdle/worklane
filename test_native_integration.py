"""Combined native entrypoint: installed PTYs and controlled saved-backlog recovery."""
import json
import os
from pathlib import Path
import socketserver
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from test_native_tui import TuiFixture
from test_native_cli import board_prefix
from test_native_lifecycle import Lifecycle
from test_worklane import connection
from test_sessions import FAKE_CODEX
import worklane as w

ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(os.environ.get('WORKLANE_TEST_RUST') == '1', 'native Rust tests are opt-in')
class NativeIntegrationTests(TuiFixture):
    binary = Path(os.environ.get('WORKLANE_TEST_CLI_BINARY', ROOT / 'target/debug/worklane'))
    refresh = '.1'

    @classmethod
    def setUpClass(cls):
        subprocess.run(['cargo', 'build', '--locked', '--offline', '--bin', 'worklane'], cwd=ROOT, check=True)
        if cls.binary.read_bytes()[:4] != b'\x7fELF':
            raise AssertionError('Combined candidate must be native')

    def test_default_and_explicit_tui_use_shared_native_guards(self):
        for command in ([], ['tui']):
            self.command = command
            self.output = b''
            self.launch('--no-color')
            self.assertNotIn(b'\x1b[1;36m', self.output)
            self.logs()
            self.send('b/initial\n'); self.until('matches')
            self.resize(12, 50); self.until('Initial transcript')
            self.send('b'); self.resize(28, 110)
            (self.root / 'mode').write_text('public')
            self.send('e'); self.until('security preflight refused')
            (self.root / 'mode').write_text('')
            self.send('c'); self.until('completion_gates:')
            self.send('\x1b'); self.until('Cancelled')
            self.quit('q')
        self.assertFalse((self.tree / '.worklane.toml').exists())
        self.native_observations = {'entrypoints': ['default', 'tui'], 'terminal_restored': True,
                                    'public_edit_refused': True, 'session_id': self.session_id}

    def test_tui_starts_same_native_controller_with_effective_overrides(self):
        # Reuse the PTY gh fixture, with an empty queue and complete controller board statuses.
        script = (self.bin / 'gh').read_text().replace(
            "elif 'items(first:100' in query:",
            "elif 'items(first:100' in query and mode == 'controller':\n        result = {'node': {'items': page([])}}\n    elif 'items(first:100' in query:")
        script = script.replace("{'id': 'done', 'name': 'Done'}", "{'id': 'done', 'name': 'Done'}, {'id':'progress','name':'In Progress'}, {'id':'review','name':'Agent review'}, {'id':'human','name':'Human review'}")
        script = script.replace("elif args[:2] == ['project', 'view']:", "elif args[:2] == ['auth', 'setup-git']:\n    pass\nelif args[:2] == ['project', 'view']:")
        (self.bin / 'gh').write_text(script)
        subprocess.run(['git', '-C', str(self.tree), 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main'], check=True)
        (self.tree / '.worklane.toml').write_text('workers = 4\n')
        self.working_directory = self.tree
        self.launch('--repo', '.', '--workers', '2', '--interval', '10', '--worker-model', 'controlled')
        (self.root / 'mode').write_text('controller')
        # Keep the existing Python-created assignment out of the disposable empty queue.
        self.task['phase'] = 'completed'; self.state.save(self.task)
        self.send('s'); self.until('Controller started')
        self.addCleanup(lambda: subprocess.run([str(self.binary), 'stop', '--now', '--repo', str(self.tree)], env=self.env, capture_output=True, timeout=15))
        self.wait_for(self.state.running)
        runtime = self.state.get_meta('runtime', {})
        identity = json.loads(subprocess.check_output([str(self.binary), '--version'], text=True))
        self.assertEqual(runtime['cli']['revision'], identity['revision'])
        self.assertEqual(runtime['cli']['executable'], str(self.binary.resolve()))
        self.assertEqual(runtime['workers'], 2)
        self.assertEqual(runtime['worker_model'], 'controlled')
        self.quit('q'); self.assertTrue(self.state.running())
        self.output = b''; self.command = ['tui']; self.launch()
        self.send('z'); self.until('Dispatch control updated'); self.assertTrue(self.state.paused())
        self.send('u'); self.until('Dispatch control updated'); self.assertFalse(self.state.paused())
        self.send('x'); self.until('Stop requested'); self.wait_for(lambda: not self.state.running())
        self.quit('q')
        self.native_observations = {'runtime': runtime, 'terminal_restored': True,
                                    'quit_preserved_controller': True, 'drained': True}

    def test_installed_controller_recovers_build_review_handoff_without_duplicate_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = Lifecycle(root, True)
            lifecycle.pr['isDraft'] = True
            lifecycle.git('remote', 'set-url', 'origin', 'https://github.com/owner/repo.git')
            lifecycle.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
            (root / 'upstream.git').symlink_to(root / 'remote.git', target_is_directory=True)
            binaries = root / 'bin'; binaries.mkdir()
            shutil.copy2(ROOT / 'tools/parity_git.py', binaries / 'git')
            (binaries / 'git').chmod(0o700)
            lock = threading.Lock()

            class Handler(socketserver.StreamRequestHandler):
                def handle(self):
                    request = json.loads(self.rfile.readline())
                    try:
                        args, stdin = request['args'], request['stdin']
                        with lock:
                            if args[:2] == ['project', 'view']:
                                result = json.dumps(dict(json.loads(board_prefix()[0]['stdout']), id='project'))
                            elif args[:2] == ['project', 'field-list']:
                                result = board_prefix(fields=True)[2]['stdout']
                            elif args[:2] == ['auth', 'setup-git']:
                                result = ''
                            elif 'items(first:100' in stdin:
                                item = dict(lifecycle.items[0])
                                item['content'] = dict(item['content'], repository={'nameWithOwner': 'owner/repo'},
                                                       state='OPEN', title='Controlled backlog', issueDependenciesSummary={'blockedBy': 0})
                                item['fieldValueByName'] = {'name': lifecycle.status[item['id']]}
                                item['links'] = {'pullRequests': connection([])}
                                result = json.dumps({'data': {'node': {'items': dict(connection([item]), totalCount=1)}}})
                            else:
                                if args[:2] == ['project', 'item-edit']:
                                    options = json.loads(board_prefix(fields=True)[2]['stdout'])['fields'][0]['options']
                                    args[-1] = next(option['name'] for option in options if option['id'] == args[-1])
                                result = lifecycle.transport(['gh', *args], stdin=stdin)
                        response = {'stdout': result, 'exit': 0}
                    except Exception as error:
                        response = {'stdout': str(error), 'exit': 1}
                    self.wfile.write(json.dumps(response).encode() + b'\n')

            server = socketserver.ThreadingUnixStreamServer(str(root / 'socket'), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
            proxy = r'''import json, socket, sys, os
s = socket.socket(socket.AF_UNIX); s.connect(os.environ['COMMAND_SOCKET'])
s.sendall(json.dumps({'args': sys.argv[1:], 'stdin': sys.stdin.read() if '--input' in sys.argv or '--body-file' in sys.argv else ''}).encode()+b'\n')
r = json.loads(s.makefile('rb').readline()); print(r['stdout']); sys.exit(r['exit'])
'''
            codex = "import sys\nif sys.argv[1:] == ['login', 'status']: sys.exit(0)\n" + FAKE_CODEX.replace('"a" * 40', 'os.environ["FIXTURE_HEAD"]')
            for name, body in [('gh', proxy),
                               ('codex', codex),
                               ('opencode', "import sys\nassert sys.argv[1:] == ['auth', 'list'], 'Cached turns must not invoke providers'\n")]:
                path = binaries / name; path.write_text('#!' + sys.executable + '\n' + body); path.chmod(0o700)
            env = dict(os.environ, HOME=str(root), XDG_STATE_HOME=str(root), COMMAND_SOCKET=str(root / 'socket'),
                       GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
                       FAKE_ROOT=str(root), FIXTURE_HEAD=lifecycle.head,
                       PARITY_ROOT=str(root), PARITY_GIT=shutil.which('git'),
                       PATH=str(binaries) + os.pathsep + os.environ['PATH'])
            outcomes = []
            for expected in ['awaiting_review', 'human_review']:
                completed = subprocess.run([str(self.binary), 'run', '--once', '--repo', str(root), '--project', 'owner/2'],
                                           env=env, text=True, capture_output=True, timeout=30)
                task = lifecycle.state.load(1)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(task['phase'], expected, task.get('error'))
                outcomes.append({'phase': task['phase'], 'head': task['head_sha'], 'runtime': lifecycle.state.get_meta('runtime', {})})
            self.assertEqual(lifecycle.status['item-1'], w.HUMAN)
            self.assertEqual(lifecycle.promotions, 1)
            self.assertEqual(len(lifecycle.posts), 3)
            self.assertEqual(task['sessions']['worker'], lifecycle.initial_sessions['worker'])
            self.assertEqual(set(task['sessions']), {'worker', 'reviewer', 'finalizer'})
            self.assertEqual(len({s['session_id'] for s in task['sessions'].values()}), 3)
            self.assertEqual(len(lifecycle.state.runs(1)), 3)
            turns = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()]
            self.assertEqual(len(turns), 2, 'Only one reviewer and one finalizer turn may run')
            self.assertFalse(lifecycle.state.running())
            self.native_observations = {'outcomes': outcomes, 'sessions': task['sessions'],
                'posts': lifecycle.posts, 'promotions': lifecycle.promotions,
                'controlled_provider_turns': turns, 'saved_build_recovery': True, 'head': lifecycle.head}


if __name__ == '__main__':
    unittest.main()
