#!/usr/bin/env python3
"""Operator-only, drained native cutover; staging uses cargo install separately."""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def git(source, *args):
    return subprocess.check_output(['git', *args], cwd=source, text=True).strip()


def replace_file(source, destination):
    descriptor, temporary = tempfile.mkstemp(prefix='.worklane-', dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        temporary.unlink()
        shutil.copy2(source, temporary, follow_symlinks=False)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def cutover(source, staged, revision, state, backup, destinations, repos):
    """Never stop/restart controllers or fetch/merge implicitly; fail before replacing commands."""
    if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError('Supply the exact reviewed origin/main SHA.')
    if (git(source, 'rev-parse', 'HEAD') != revision
            or git(source, 'rev-parse', 'origin/main') != revision
            or git(source, 'status', '--porcelain', '--untracked-files=no')):
        raise ValueError('Cutover requires clean source HEAD == reviewed origin/main.')
    if not staged.read_bytes().startswith(b'\x7fELF'):
        raise ValueError('Staged command must be a native ELF.')
    identity = json.loads(subprocess.check_output([str(staged), '--version'], text=True))
    if identity.get('runtime') != 'native' or identity.get('revision') != revision or identity.get('dirty') is not False:
        raise ValueError('Staged command must report the clean reviewed native revision.')
    destinations = list(dict.fromkeys(Path(os.path.abspath(p)) for p in destinations))
    if not destinations or any(not p.parent.is_dir() for p in destinations):
        raise ValueError('Every destination bin directory must already exist.')
    if backup.exists() or state.resolve() == backup.resolve() or state.resolve() in backup.resolve().parents:
        raise ValueError('Use a new backup directory outside runtime state.')
    with ExitStack() as locks:
        # Retain both locks through replacement; an in-flight detached launch cannot race cutover.
        for directory in sorted(p for p in state.glob('*') if p.is_dir()):
            for name in ('launch.lock', 'runner.lock'):
                lock = locks.enter_context((directory / name).open('a+'))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise ValueError(f'Drain controllers/agents first: {directory}') from error
        backup.mkdir(parents=True, mode=0o700)
        if state.exists():
            shutil.copytree(state, backup / 'state', symlinks=True)
            # Include committed WAL contents, even when a stopped reader holds the WAL open.
            for database in state.glob('*/state.sqlite3'):
                target = backup / 'state' / database.relative_to(state)
                target.unlink()
                for suffix in ('-wal', '-shm'):
                    Path(str(target) + suffix).unlink(missing_ok=True)
                with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as live, sqlite3.connect(target) as saved:
                    live.backup(saved)
                    if saved.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                        raise ValueError(f'Backup integrity check failed: {database}')
        for index, repo in enumerate(repos):
            config = repo / '.worklane.toml'
            if config.exists():
                shutil.copy2(config, backup / f'config-{index}.toml')
        originals = []
        for index, destination in enumerate(destinations):
            saved = backup / f'command-{index}'
            existed = destination.exists() or destination.is_symlink()
            if existed:
                shutil.copy2(destination, saved, follow_symlinks=False)
            originals.append((destination, saved, existed))
        manifest = {'revision': revision, 'staged': str(staged), 'identity': identity,
                    'sha256': hashlib.sha256(staged.read_bytes()).hexdigest(),
                    'destinations': [str(p) for p in destinations], 'repos': [str(p) for p in repos],
                    'state': str(state), 'production_verified': False}
        (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        replaced = []
        try:
            for destination, saved, existed in originals:
                replace_file(staged, destination)
                replaced.append((destination, saved, existed))
                if hashlib.sha256(destination.read_bytes()).hexdigest() != manifest['sha256']:
                    raise ValueError(f'Installed bytes differ: {destination}')
        except BaseException:
            for destination, saved, existed in reversed(replaced):
                if existed:
                    replace_file(saved, destination)
                else:
                    destination.unlink(missing_ok=True)
            raise
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--repo', type=Path, action='append', required=True, help='All managed checkouts; back up their config')
    parser.add_argument('--destination', type=Path, action='append', help='Defaults to both local command paths')
    args = parser.parse_args()
    state = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'worklane'
    destinations = args.destination or [Path.home() / '.local/bin/worklane', ROOT / '.venv/bin/worklane']
    result = cutover(ROOT, args.staged.resolve(), args.revision, state.resolve(), args.backup.resolve(), destinations,
                     [p.resolve() for p in args.repo])
    print(json.dumps(result, indent=2))
    print('Installed; operator must smoke-test CLI/TUI, restart backlog and verify runtime.cli.revision before closing #53.')


if __name__ == '__main__':
    main()
