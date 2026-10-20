"""Drained dual-command cutover preserves WAL data, symlink rollback and saved sessions."""
import fcntl
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import install_native as installer


class NativeInstallationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.root / 'source'; self.source.mkdir()
        for args in [('init', '-q'), ('config', 'user.name', 'Test'), ('config', 'user.email', 'test@example.invalid')]:
            subprocess.run(['git', *args], cwd=self.source, check=True)
        (self.source / 'tracked').write_text('preserved')
        subprocess.run(['git', 'add', '.'], cwd=self.source, check=True)
        subprocess.run(['git', 'commit', '-qm', 'native candidate'], cwd=self.source, check=True)
        self.revision = installer.git(self.source, 'rev-parse', 'HEAD')
        subprocess.run(['git', 'update-ref', 'refs/remotes/origin/main', self.revision], cwd=self.source, check=True)
        self.staged = self.root / 'native'; shutil.copy2('/bin/true', self.staged)
        self.state = self.root / 'state'; self.repo_state = self.state / 'owner--repo'; self.repo_state.mkdir(parents=True)
        self.destinations = [self.root / 'path-command', self.root / 'venv-command']
        self.old = self.root / 'old'; self.old.write_text('previous Python launcher')
        self.destinations[0].symlink_to(self.old)
        self.destinations[1].write_text('previous venv launcher')
        self.backup = self.root / 'backup'
        self.identity = {'runtime': 'native', 'revision': self.revision, 'dirty': False}
        original = subprocess.check_output
        def command(args, **kwargs):
            return json.dumps(self.identity) if args == [str(self.staged), '--version'] else original(args, **kwargs)
        self.enterContext(patch.object(installer.subprocess, 'check_output', side_effect=command))

    def cutover(self):
        return installer.cutover(self.source, self.staged, self.revision, self.state, self.backup,
                                 self.destinations, [self.source])

    def assert_originals(self):
        self.assertTrue(self.destinations[0].is_symlink())
        self.assertEqual(self.destinations[0].read_text(), 'previous Python launcher')
        self.assertEqual(self.destinations[1].read_text(), 'previous venv launcher')

    def test_drained_upgrade_backs_up_wal_config_and_both_commands_without_changing_state(self):
        database = self.repo_state / 'state.sqlite3'
        with sqlite3.connect(database) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE pending(session, role, input)')
            connection.execute("INSERT INTO pending VALUES('exact-id','reviewer','retain pending input')")
            connection.commit()
            self.assertTrue(Path(str(database) + '-wal').exists())
            (self.source / '.worklane.toml').write_text('workers = 2\n')
            manifest = self.cutover()
            self.assertFalse(manifest['production_verified'])
            for candidate in [database, self.backup / 'state/owner--repo/state.sqlite3']:
                with sqlite3.connect(candidate) as saved:
                    self.assertEqual(saved.execute('SELECT * FROM pending').fetchall(), [('exact-id', 'reviewer', 'retain pending input')])
            for destination in self.destinations:
                self.assertEqual(destination.read_bytes(), self.staged.read_bytes())
                self.assertFalse(destination.is_symlink())
            self.assertTrue((self.backup / 'command-0').is_symlink())
            self.assertEqual((self.backup / 'command-1').read_text(), 'previous venv launcher')
            self.assertEqual((self.backup / 'config-0.toml').read_text(), 'workers = 2\n')
            self.assertEqual(self.old.read_text(), 'previous Python launcher')

    def test_controller_or_pending_launch_refuses_without_backups_or_replacement(self):
        for name in ['runner.lock', 'launch.lock']:
            with (self.repo_state / name).open('a+') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with self.assertRaisesRegex(ValueError, 'Drain controllers'):
                    self.cutover()
            self.assert_originals()
            self.assertFalse(self.backup.exists())

    def test_stale_dirty_or_non_native_candidate_refuses_before_mutation(self):
        for identity in [{'runtime': 'python'}, dict(self.identity, revision='a' * 40), dict(self.identity, dirty=True)]:
            with patch.object(self, 'identity', identity), self.assertRaisesRegex(ValueError, 'Staged command'):
                self.cutover()
        (self.source / 'tracked').write_text('dirty')
        with self.assertRaisesRegex(ValueError, 'clean source'):
            self.cutover()
        self.assert_originals(); self.assertFalse(self.backup.exists())

    def test_partial_install_failure_restores_both_original_commands(self):
        original = installer.replace_file
        def replace(source, destination):
            if source == self.staged and destination == self.destinations[1]:
                raise OSError('controlled second-path failure')
            return original(source, destination)
        with patch.object(installer, 'replace_file', side_effect=replace), self.assertRaisesRegex(OSError, 'second-path'):
            self.cutover()
        self.assert_originals()
        self.assertTrue((self.backup / 'manifest.json').exists())
