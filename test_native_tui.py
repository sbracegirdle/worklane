"""Finite core groups 11–12: installed Rust frontend in real PTYs, controlled external tools."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import tempfile
import termios
import time
import unittest
import uuid
import worklane as w

ROOT = Path(__file__).resolve().parent
BINARY = Path(os.environ.get('WORKLANE_TUI_BIN', ROOT/'target/debug/worklane-tui'))
PROBE = ROOT/'target/debug/examples/tui-controller-probe'

@unittest.skipUnless(os.environ.get('WORKLANE_TEST_RUST') == '1', 'native Rust tests are opt-in')
class NativeTuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(['cargo', 'build', '--locked', '--offline', '--bin', 'worklane-tui', '--example', 'tui-controller-probe'], cwd=ROOT, check=True)
        if BINARY.read_bytes()[:4] != b'\x7fELF':
            raise AssertionError('PTY candidate must be the native ELF executable')

    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp); self.tree = self.root/'checkout'; self.tree.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.tree)], check=True)
        subprocess.run(['git', '-C', str(self.tree), 'remote', 'add', 'origin', 'https://github.com/owner/repo.git'], check=True)
        self.state = w.State('owner/repo', self.root)
        self.task = {'number': 1, 'phase': 'building', 'issue': {'title': 'Task 界 é'}, 'pr_url': 'https://github.com/owner/repo/pull/12'}
        self.run_id = self.state.begin_run(self.task, 'build', self.tree)
        self.session_id = str(uuid.uuid4())
        self.state.record_event(self.run_id, {'type': 'thread.started', 'thread_id': self.session_id}, w.MODEL)
        self.state.append_log(self.run_id, 'agent', 'Initial transcript 界 é')
        self.bin = self.root/'bin'; self.bin.mkdir()
        gh = self.bin/'gh'; gh.write_bytes((ROOT/'tools/fixtures/tui_gh.py').read_bytes()); gh.chmod(0o755)
        codex = self.bin/'codex'; codex.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TUI_FIXTURE/resume-args"\nstty -a > "$TUI_FIXTURE/resume-terminal"\n'); codex.chmod(0o755)
        self.issue = {'number': 1, 'title': 'Task 界 é', 'body': 'Description line', 'state': 'OPEN', 'url': 'https://github.com/owner/repo/issues/1'}
        (self.root/'issue.json').write_text(json.dumps(self.issue))
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.root), TUI_FIXTURE=str(self.root),
                        PATH=str(self.bin)+os.pathsep+os.environ['PATH'], TERM='xterm-256color')
        self.env.pop('NO_COLOR', None)
        self.process = None
        self.output = b''

    def launch(self, *args):
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master); self.addCleanup(os.close, self.slave)
        self.resize(28, 110)
        self.original_terminal = termios.tcgetattr(self.slave)
        self.process = subprocess.Popen([str(BINARY), '--repo', str(self.tree), '--project', 'owner/2', '--refresh', '.05', *args], stdin=self.slave, stdout=self.slave, stderr=self.slave, env=self.env)
        process = self.process
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        self.addCleanup(cleanup)
        self.until('Task 界')

    def resize(self, rows, cols):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    def send(self, text):
        os.write(self.master, text.encode() if isinstance(text, str) else text)

    def until(self, text, timeout=6):
        token = text.encode() if isinstance(text, str) else text
        output = b''; deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], .05)[0]:
                output += os.read(self.master, 65536)
                self.output = (self.output + output[-65536:])[-200000:]
                if token in output:
                    return output
            if self.process.poll() is not None:
                break
        self.fail(f'PTY missing {token!r}, exit={self.process.poll()}: {output[-7000:]!r}')

    def wait_for(self, predicate):
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.02)
        self.fail('condition did not become true')

    def inputs(self):
        with self.state.connect() as db:
            return [v["text"] for v in self.state._get(db, "input:"+self.session_id, [])]

    def calls(self):
        path=self.root/'gh-calls.jsonl'
        return [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []

    def quit(self, key='\x03'):
        self.send(key); self.process.wait(timeout=8)
        self.assertEqual(self.process.returncode, 0)
        self.assertEqual(termios.tcgetattr(self.slave), self.original_terminal)
        self.assertEqual(self.state.session(1, 'worker')['session_id'], self.session_id)

    def logs(self):
        self.send('\nj\n'); self.until('Logs #1'); self.until('Initial transcript')

    def test_group11_live_refresh_unicode_navigation_resize_search_details_quit(self):
        with w.locked(self.state.path/'runner.lock'):
            self.launch()
            self.assertIn(b'\x1b[1;36m', self.output)
            self.logs()
            self.state.append_log(self.run_id, 'agent', 'Fresh output after refresh')
            self.until('Fresh output after refresh')
            self.send('\x1b[5~\x1b[6~\x1b[F')
            self.resize(5, 30); self.until('Resize terminal')
            self.resize(12, 50); self.until('Fresh output')
            self.resize(28, 110); self.until('Fresh output')
            self.send('b/initial\n'); self.until('matches')
            self.send('\n'); self.until('Initial transcript')
            self.send('bi'); self.until('Second paginated comment')
            self.assertEqual(len([c for c in self.calls() if 'comments(first:100' in c['stdin']]), 2)
            self.quit()
            with self.assertRaises(RuntimeError):
                with w.locked(self.state.path/'runner.lock', blocking=False): pass
        self.assertEqual(self.state.load(1)['phase'], 'building')

    def test_group12_config_cancel_validation_save_no_controller(self):
        self.launch('--no-color')
        self.send('c'); self.until('completion_gates:')
        self.send('\t'*7+'\x15[53]'); self.send('\x1b'); self.until('Cancelled')
        self.assertFalse((self.tree/'.worklane.toml').exists())
        self.send('c'); self.until('completion_gates:')
        self.send('\t'*7+'\x15[53]\x13'); self.until('Configuration saved')
        self.assertEqual(w.load_config(self.tree)['completion_gates'], [53])
        self.send('c'); self.until('completion_gates:')
        self.send('\t'*7+'\x15bad\x13'); self.until('strings need quotes')
        self.assertEqual(w.load_config(self.tree)['completion_gates'], [53])
        self.send('\x1b'); self.until('Cancelled'); self.quit('q')
        self.assertFalse(self.state.running())
        self.assertEqual(self.calls(), [])

    def test_group12_issue_editor_cancel_edit_conflict_and_revocation(self):
        self.launch(); self.send('e'); self.until('Title: Task')
        self.send('\x15Cancelled title\x1b'); self.until('Cancelled')
        self.assertFalse(any(c['args'][:2] == ['issue', 'edit'] for c in self.calls()))
        self.send('e'); self.until('Title: Task')
        self.send('\x15Updated 界\t\x15Line one\nLiteral $HOME `code`\x13'); self.until('Saved https://')
        saved=json.loads((self.root/'issue.json').read_text())
        self.assertEqual(saved['title'], 'Updated 界'); self.assertEqual(saved['body'], 'Line one\nLiteral $HOME `code`')
        self.send('e'); self.until('Title: Updated')
        changed=dict(saved, title='Concurrent change'); (self.root/'issue.json').write_text(json.dumps(changed))
        self.send('!\x13'); self.until('Issue changed on GitHub')
        self.assertEqual(json.loads((self.root/'issue.json').read_text()), changed)
        self.send('\x1b'); self.until('Cancelled')
        self.send('e'); self.until('Title: Concurrent')
        before=len([c for c in self.calls() if c['args'][:2] == ['issue', 'edit']])
        (self.root/'mode').write_text('revoked'); self.send('!\x13'); self.until('security preflight refused')
        self.assertEqual(len([c for c in self.calls() if c['args'][:2] == ['issue', 'edit']]), before)
        self.send('\x1b'); self.until('Cancelled'); self.quit()

    def test_group12_create_issue_board_browser_pr(self):
        self.launch(); self.send('n'); self.until('Title:')
        self.send('\x13'); self.until('Title is required')
        self.send('Created 界\tBody\x13'); self.until('Saved https://')
        self.assertEqual(json.loads((self.root/'issue.json').read_text())['title'], 'Created 界')
        self.state.save({'number':2,'phase':'blocked','issue':{'title':'Other issue'},'pr_url':'https://github.com/owner/repo/pull/99'})
        self.send('B'); self.until('Board ·'); self.until('Created 界')
        self.send('p'); self.until('Opened PR')
        opened=[c for c in self.calls() if c['args'][:2]==['pr','view'] and '--web' in c['args']]
        self.assertEqual(opened[-1]['args'][2], 'https://github.com/owner/repo/pull/12')
        self.send('\n'); self.until('Second paginated comment')
        self.send('o'); self.until('Opened issue')
        self.send('bp'); self.until('Opened PR')
        self.assertTrue(any('--web' in c['args'] and c['args'][:2]==['pr','view'] for c in self.calls()))
        self.quit()

    def test_group12_input_cancel_queue_exact_session_and_stopped_refusal(self):
        with w.locked(self.state.path/'runner.lock'):
            self.launch(); self.logs()
            self.send('mdo not send\x1b'); self.until('Cancelled')
            self.assertEqual(self.inputs(), [])
            self.send('mFollow up 界\n'); self.until('Input queued')
            self.assertEqual(self.inputs(), ['Follow up 界'])
        self.send('mRetain this\n'); self.until('Worklane is stopped')
        self.until('Retain this'); self.send('\x1b'); self.until('Cancelled')
        self.quit()

    def test_group12_interactive_resume_exact_role_terminal_and_missing_worktree(self):
        self.launch(); self.logs(); self.send('a'); self.until('Session returned')
        args=(self.root/'resume-args').read_text().splitlines()
        self.assertEqual(args[:2], ['resume', self.session_id]); self.assertIn('workspace-write', args)
        self.assertNotIn('-icanon', (self.root/'resume-terminal').read_text())
        other=self.root/'missing'
        with self.state.connect() as db:
            db.execute('UPDATE sessions SET worktree=?', (str(other),))
        self.send('a'); self.until('worktree is missing')
        self.quit()

    def test_group12_native_lifecycle_detached_start_pause_resume_drain_immediate(self):
        self.launch('--controller', str(PROBE))
        self.send('s'); self.until('Controller started'); self.assertTrue(self.state.running())
        runtime=self.state.get_meta('runtime', {}); pid=runtime['pid']
        self.addCleanup(lambda: self.state.request_stop(True) if self.state.running() else None)
        self.send('z'); self.until('Dispatch control updated'); self.assertTrue(self.state.paused())
        self.send('u'); self.until('Dispatch control updated'); self.assertFalse(self.state.paused())
        self.quit('q'); self.assertTrue(self.state.running()); os.kill(pid, 0)
        self.output=b''; self.launch('--controller', str(PROBE))
        self.send('x'); self.until('Stop requested'); self.wait_for(lambda:not self.state.running())
        self.send('s'); self.until('Controller started')
        self.send('X'); self.until('Stop requested'); self.wait_for(lambda:not self.state.running())
        self.quit()

    def test_group11_slow_network_stop_quit_cancels_only_owned_requests(self):
        unrelated=subprocess.Popen(['sleep','30']); self.addCleanup(unrelated.wait); self.addCleanup(unrelated.terminate)
        self.launch(); (self.root/'mode').write_text('slow')
        self.send('i'); self.wait_for(lambda:(self.root/'slow-entered').exists())
        self.send('z'); self.until('Dispatch control updated'); self.assertTrue(self.state.paused())
        self.send('x'); self.until('Stop requested')
        started=time.monotonic(); self.quit(); self.assertLess(time.monotonic()-started, 3)
        self.assertIsNone(unrelated.poll())

    def test_group11_held_github_budget_lock_still_quits_and_restores_terminal(self):
        budget=w.State('_github', self.root)
        with w.locked(budget.path/'budget.lock'):
            self.launch(); self.send('e'); self.until('Working')
            self.send('z'); self.until('Dispatch control updated')
            started=time.monotonic(); self.quit(); self.assertLess(time.monotonic()-started, 3)
        self.assertEqual(self.calls(), [])

    def test_group12_reviewer_input_and_resume_keep_exact_role(self):
        review=self.state.begin_run(self.state.load(1), 'review', self.tree)
        session=str(uuid.uuid4())
        self.state.record_event(review, {'type':'thread.started','thread_id':session}, w.MODEL)
        self.state.append_log(review, 'agent', 'Reviewer transcript')
        with w.locked(self.state.path/'runner.lock'):
            self.launch(); self.send('\njj\n'); self.until('Reviewer transcript')
            self.send('mReviewer follow-up\n'); self.until('Input queued')
            with self.state.connect() as db:
                self.assertEqual(self.state._get(db,'input:'+session,[]), [{'text':'Reviewer follow-up','status':'pending'}])
                self.assertEqual(self.state._get(db,'input:'+self.session_id,[]), [])
            self.send('a'); self.until('already running')
            self.assertFalse((self.root/'resume-args').exists())
        self.send('a'); self.until('Session returned')
        self.assertEqual((self.root/'resume-args').read_text().splitlines()[:2], ['resume',session])
        self.assertEqual(self.state.session(1,'reviewer')['session_id'],session)
        self.quit()

    def test_group12_public_guard_retry_and_signal_restore(self):
        self.launch(); (self.root/'mode').write_text('public')
        self.send('e'); self.until('security preflight refused')
        self.send('u'); self.until('security preflight refused')
        self.send('s'); self.until('No such file')  # Missing native CLI never falls back to Python.
        self.task['phase']='blocked'; self.task['blocker']='example'; self.state.save(self.task)
        (self.root/'mode').write_text(''); self.send('t'); self.until('Retry saved')
        self.assertEqual(self.state.load(1)['phase'], 'retry')
        os.kill(self.process.pid, signal.SIGTERM); self.process.wait(timeout=8)
        self.assertEqual(self.process.returncode,0)
        self.assertEqual(termios.tcgetattr(self.slave),self.original_terminal)

    def test_group11_redirected_and_invalid_refresh_refused(self):
        for args in [[], ['--refresh','nan'], ['--refresh','0']]:
            result=subprocess.run([str(BINARY), '--repo', str(self.tree), *args], env=self.env, capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertNotIn(b'Traceback',result.stderr)
        self.assertFalse(self.calls())

if __name__ == '__main__':
    unittest.main()
