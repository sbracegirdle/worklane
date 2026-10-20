"""Stop controls against real fake-process trees and an offline installed CLI."""
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import worklane as w
from worklane_process import OWNER_ENV, owned, terminate_owned


class StopTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = w.State('owner/repo', self.root)

    def fake(self, script, token=''):
        process = subprocess.Popen([sys.executable, '-c', script], start_new_session=True,
                                   env={**os.environ, OWNER_ENV: token},
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (terminate_owned([(process.pid, w.process_identity(process.pid))], [token], grace=0), process.wait()))
        return process

    def test_stop_bypasses_blocked_dispatch_and_repeated_requests_keep_immediate(self):
        with w.locked(self.state.path / 'dispatch.lock'), \
             patch.object(w, 'repository', return_value=('owner/repo', self.root, 'main')), \
             patch.object(w, 'State', return_value=self.state), redirect_stdout(io.StringIO()):
            started = time.monotonic()
            self.assertEqual(w.main(['stop', '--now']), 0)
            self.assertEqual(w.main(['stop']), 0)
            self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(self.state.paused())
        self.assertTrue(self.state.get_meta('shutdown')['immediate'])

    def test_detached_orphan_and_ignoring_descendant_die_without_touching_unrelated(self):
        token = 'test-' + self.root.name
        pidfile = self.root / 'child'
        script = f'''import os, signal, time
pid = os.fork()
if pid == 0:
 os.setsid()
 signal.signal(signal.SIGTERM, signal.SIG_IGN)
 open({str(pidfile)!r}, 'w').write(str(os.getpid()))
 time.sleep(60)
else:
 time.sleep(.2)
'''
        parent = self.fake(script, token)
        unrelated = self.fake('import time; time.sleep(60)')
        parent.wait(timeout=3)
        child = int(pidfile.read_text())
        self.assertIsNotNone(w.process_identity(child))
        self.assertEqual(terminate_owned([(parent.pid, 'stale')], [token], grace=.1), [])
        self.assertIsNone(w.process_identity(child))
        self.assertIsNone(unrelated.poll())

    def test_stale_pid_cannot_own_unrelated_process(self):
        process = self.fake('import time; time.sleep(60)')
        self.assertEqual(owned([(process.pid, 'wrong-birth')]), {})
        self.assertEqual(terminate_owned([(process.pid, 'wrong-birth')], grace=0), [])
        self.assertIsNone(process.poll())

    def test_immediate_stop_kills_live_worker_and_reports_stopped(self):
        process = self.fake('import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)', 'worker-token')
        self.state.save({'number': 1, 'phase': 'building', 'child_pid': process.pid,
                         'child_identity': w.process_identity(process.pid), 'ownership_token': 'worker-token'})
        with patch.object(w, 'repository', return_value=('owner/repo', self.root, 'main')), \
             patch.object(w, 'State', return_value=self.state), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(w.main(['stop', '--now']), 0)
        process.wait(timeout=2)
        self.assertIn('no owned processes remain', output.getvalue())
        self.assertEqual(self.state.get_meta('stop_result')['remaining_pids'], [])
        self.assertFalse(self.state.running())
        self.assertEqual(self.state.load(1)['phase'], 'building')  # Recovery belongs to next start.

    def test_startup_stop_is_not_overwritten_after_blocking_auth(self):
        ready, release, ran = (self.root / name for name in ('ready', 'release', 'ran'))
        script = r"""
import json, pathlib, sys, time
from unittest.mock import Mock, patch
import worklane as w
root = pathlib.Path(sys.argv[1]); state = w.State('owner/repo', root)
def command(args, **kwargs):
 if args[:3] == ['gh', 'auth', 'setup-git']:
  (root / 'ready').touch()
  while not (root / 'release').exists(): time.sleep(.01)
 return ''
def run(self, stop): (root / 'ran').touch()
board = Mock(); board.project = {'url':'fake'}
with patch.object(w, 'repository', return_value=('owner/repo', root, 'main')), patch.object(w, 'State', return_value=state), patch.object(w, 'command', side_effect=command), patch.object(w, 'Board', return_value=board), patch.object(w.shutil, 'which', return_value='/fake'), patch.object(w.Runner, 'run', run), patch.object(w, 'installation_info', return_value={}):
 raise SystemExit(w.main(['run', '--once']))
"""
        process = subprocess.Popen([sys.executable, '-c', script, str(self.root)], start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (release.touch(), terminate_owned([(process.pid, w.process_identity(process.pid))], grace=0), process.wait()))
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists())
        with patch.object(w, 'repository', return_value=('owner/repo', self.root, 'main')), patch.object(w, 'State', return_value=self.state), redirect_stdout(io.StringIO()):
            self.assertEqual(w.main(['stop', '--now']), 0)
        process.wait(timeout=3)
        self.assertFalse(ran.exists())
        self.assertTrue(self.state.get_meta('shutdown')['immediate'])

    def test_termination_wrapper_ignores_reaped_pid(self):
        from worklane_harness import terminate
        from unittest.mock import Mock
        process = Mock(pid=os.getpid())
        process.poll.return_value = 1
        with patch('worklane_harness.terminate_owned') as cleanup:
            terminate(process)
        cleanup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
