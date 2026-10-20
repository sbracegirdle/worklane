#!/usr/bin/env python3
"""Execute portable JSON fixtures against two command arrays, without a shell."""
import argparse
from contextlib import contextmanager
import base64
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import selectors
import signal
import shutil
import sqlite3
import struct
import subprocess
import sys
import tarfile
import tempfile
import termios
import time

ROOT = Path(__file__).resolve().parents[1]


def validate(value, schema, definitions=None):
    """Validate the fixture schema subset without adding runtime dependencies."""
    definitions = definitions or schema.get('$defs', {})
    if '$ref' in schema:
        return validate(value, definitions[schema['$ref'].rsplit('/', 1)[1]], definitions)
    if 'const' in schema and value != schema['const']:
        raise ValueError('Unexpected fixture schema version')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('Unknown fixture enum value')
    kind = schema.get('type')
    types = {'object':dict, 'array':list, 'string':str, 'integer':int, 'number':(int,float)}
    if kind and (not isinstance(value, types[kind]) or kind in ('integer','number') and isinstance(value,bool)):
        raise ValueError('Wrong fixture type: ' + kind)
    if kind == 'object':
        properties = schema.get('properties', {})
        if set(schema.get('required', [])) - value.keys():
            raise ValueError('Missing required fixture fields')
        for key, child in value.items():
            rule = properties.get(key, schema.get('additionalProperties', {}))
            if rule is False:
                raise ValueError('Unknown fixture field: ' + key)
            validate(child, rule, definitions)
    if kind == 'array':
        if len(value) < schema.get('minItems',0) or len(value) > schema.get('maxItems',float('inf')):
            raise ValueError('Wrong fixture array length')
        for child in value:
            validate(child, schema.get('items', {}), definitions)
    if kind == 'string' and len(value) < schema.get('minLength', 0):
        raise ValueError('Empty fixture identifier')
    if kind in ('number','integer'):
        import math
        if not math.isfinite(value) or value < schema.get('minimum',float('-inf')) or value <= schema.get('exclusiveMinimum',float('-inf')):
            raise ValueError('Invalid fixture numeric bound')


def command_array(value):
    command = json.loads(value)
    if not isinstance(command,list) or not command or any(not isinstance(v,str) for v in command):
        raise ValueError('Command must be a nonempty JSON string array')
    if not Path(command[0]).is_absolute():
        raise ValueError('Command executable must use an absolute path')
    return command


def command_hashes(command):
    return {arg:hashlib.sha256(Path(arg).read_bytes()).hexdigest() for arg in command if Path(arg).is_file()}


def expand(value, tokens):
    if isinstance(value, str):
        for key, replacement in tokens.items():
            value = value.replace('{' + key + '}', str(replacement))
    elif isinstance(value, list):
        value = [expand(v, tokens) for v in value]
    elif isinstance(value, dict):
        value = {k: expand(v, tokens) for k, v in value.items()}
    return value


def normalize(value, root):
    # Only the sandbox path is implicit. IDs, timestamps, errors and ANSI remain exact.
    if isinstance(value, str):
        return value.replace(str(root), '<SANDBOX>')
    if isinstance(value, (list, tuple)):
        return [normalize(v, root) for v in value]
    if isinstance(value, dict):
        return {k: normalize(v, root) for k, v in value.items()}
    return value


def terminal(command, cwd, env, step):
    master, slave = pty.openpty()
    rows, cols = step.get('size', [24, 100])
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
    def session():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=slave, stdout=slave,
                               stderr=slave, preexec_fn=session)
    os.close(slave)
    transcript, actions, sent = b'', step.get('terminal', []), set()
    deadline = time.monotonic() + step.get('timeout', 10)
    started = time.monotonic()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(master, selectors.EVENT_READ)
            while True:
                for i, action in enumerate(actions):
                    if i in sent or (action.get('after') and action['after'].encode() not in transcript):
                        continue
                    if time.monotonic() - started < action.get('delay', 0):
                        continue
                    if 'send' in action:
                        os.write(master, action['send'].encode())
                    if 'size' in action:
                        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', *action['size'], 0, 0))
                        os.killpg(process.pid, signal.SIGWINCH)
                    if 'signal' in action:
                        os.killpg(process.pid, getattr(signal, action['signal']))
                    sent.add(i)
                if time.monotonic() >= deadline:
                    raise TimeoutError('PTY fixture timed out')
                if selector.select(.02):
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    transcript += chunk
                elif process.poll() is not None:
                    break
        process.wait(timeout=2)
        if len(sent) != len(actions):
            raise AssertionError('Unperformed terminal actions')
        return process.returncode, transcript.decode('utf-8', errors='strict'), ''
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        os.close(master)



@contextmanager
def sandbox():
    with tempfile.TemporaryDirectory(prefix='worklane-parity-') as directory:
        root = Path(directory)
        try:
            yield root
        finally:
            # A reaped parent can leave detached descendants holding output pipes.
            # The per-sandbox inherited marker survives reparenting and setsid.
            marker = ('PARITY_ROOT=' + directory).encode()
            deadline = time.monotonic() + 2
            while True:
                found = False
                for path in Path('/proc').glob('[0-9]*/environ'):
                    pid = int(path.parent.name)
                    try:
                        if pid == os.getpid() or marker not in path.read_bytes().split(b'\0'):
                            continue
                        stat = path.parent / 'stat'
                        before = stat.read_text().rsplit(')', 1)[1].split()
                        if before[0] == 'Z':
                            continue
                        after = stat.read_text().rsplit(')', 1)[1].split()
                        if before[19] == after[19]:
                            os.kill(pid, signal.SIGKILL)
                            found = True
                    except (OSError, IndexError):
                        continue
                if not found:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('Fixture descendants survived cleanup')
                time.sleep(.02)


def execute(command, scenario):
    schema = json.loads((ROOT / 'parity/fixture-schema.json').read_text())
    validate(scenario, schema['$defs']['scenario'], schema['$defs'])
    with sandbox() as root:
        repo, home, binary = root / 'repo', root / 'home', root / 'bin'
        for path in (repo, home, binary):
            path.mkdir()
        env = {'PATH': str(binary) + ':/usr/bin:/bin', 'HOME': str(home),
               'XDG_STATE_HOME': str(root / 'state'), 'XDG_CONFIG_HOME': str(home / '.config'),
               'XDG_DATA_HOME': str(home / '.local/share'), 'XDG_CACHE_HOME': str(home / '.cache'),
               'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TERM': 'xterm-256color',
               'PARITY_ROOT': str(root), 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
               'GIT_AUTHOR_NAME': 'Parity', 'GIT_AUTHOR_EMAIL': 'parity@example.invalid',
               'GIT_COMMITTER_NAME': 'Parity', 'GIT_COMMITTER_EMAIL': 'parity@example.invalid'}
        env['PARITY_GIT'] = shutil.which('git')
        env['GIT_AUTHOR_DATE'] = env['GIT_COMMITTER_DATE'] = '2026-10-20T00:00:00+00:00'
        tokens = {'repo': repo, 'home': home, 'root': root, 'state': root / 'state'}
        env.update(expand(scenario.get('env', {}), tokens))
        for args in (['init', '-q', '-b', 'main'], ['remote', 'add', 'origin', 'https://github.com/owner/repo.git'],
                     ['symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main']):
            subprocess.run(['git', *args], cwd=repo, env=env, check=True, capture_output=True)
        for name, content in scenario.get('files', {}).items():
            target = root / name
            if not target.resolve().is_relative_to(root):
                raise ValueError('Fixture file escapes sandbox')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expand(content, tokens))
        # Seed a real disposable upstream. Preserve the GitHub origin string for
        # discovery; the PATH wrapper redirects only fetch/push to this local repo.
        (repo / 'README.fixture').write_text('Disposable parity repository\n')
        setup = [(['add','.'], repo), (['commit','-qm','Fixture baseline'], repo),
                 (['init','--bare','-q',str(root / 'upstream.git')], root),
                 (['push','-q',str(root / 'upstream.git'),'main'], repo)]
        for argv, cwd in setup:
            subprocess.run([env['PARITY_GIT'], *argv], cwd=cwd, env=env, check=True, capture_output=True)
        target = binary / 'git'
        target.write_text('#!' + sys.executable + '\n' + (ROOT / 'tools/parity_git.py').read_text())
        target.chmod(0o700)
        plan = expand(scenario.get('transport', {}), tokens)
        (root / 'transport.json').write_text(json.dumps(plan))
        for name in ('gh', 'codex', 'opencode'):
            target = binary / name
            target.write_text('#!' + sys.executable + '\n' + (ROOT / 'tools/parity_tool.py').read_text())
            target.chmod(0o700)
        results = []
        for step in scenario['steps']:
            argv = command + expand(step.get('argv', []), tokens)
            if 'terminal' in step:
                code, stdout, stderr = terminal(argv, repo, env, step)
            else:
                process = subprocess.Popen(argv, cwd=repo, env=env, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                try:
                    out, err = process.communicate(expand(step.get('stdin', ''), tokens).encode(), timeout=step.get('timeout', 10))
                    code, stdout, stderr = process.returncode, out.decode(), err.decode()
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        pipe.close()
            if code != step.get('exit', 0):
                raise AssertionError(f"{scenario['id']}: exit {code}: {stdout} {stderr}")
            for stream, text in (('stdout', stdout), ('stderr', stderr)):
                for expected in step.get(stream + '_contains', []):
                    if expand(expected, tokens) not in text:
                        raise AssertionError(f"{scenario['id']}: missing {expected!r} in {stream}: {text}")
            results.append({'exit': code, 'stdout': stdout, 'stderr': stderr})
        if (root / 'transport-errors.jsonl').exists():
            raise AssertionError('Unexpected or mismatched transport call')
        cursors = json.loads((root / 'cursor.json').read_text()) if (root / 'cursor.json').exists() else {}
        if cursors != {tool: len(rules) for tool, rules in plan.items() if rules}:
            raise AssertionError(f'Unconsumed transport responses: {cursors}')
        calls = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()] if (root / 'calls.jsonl').exists() else []
        observed = {}
        for name in scenario.get('observe_files', []):
            path = root / name
            observed[name] = base64.b64encode(path.read_bytes()).decode() if path.exists() else None
        queries = []
        for query in scenario.get('sqlite', []):
            path = root / query['path']
            if not path.exists():
                raise AssertionError(f'Missing database: {path}')
            with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as db:
                queries.append(db.execute(query['query']).fetchall())
        git_calls = [json.loads(line) for line in (root / 'git-calls.jsonl').read_text().splitlines()] if (root / 'git-calls.jsonl').exists() else []
        return normalize({'steps': results, 'calls': calls, 'git_calls':git_calls, 'files': observed, 'sqlite': queries}, root)


def reference(destination, revision):
    archive = subprocess.check_output(['git', 'archive', revision], cwd=ROOT)
    import io
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(destination, filter='data')
    return [sys.executable, str(destination / 'worklane.py')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', help='JSON argv array; default: isolated pinned git archive')
    parser.add_argument('--candidate', help='JSON argv array; omit for reference-only evidence')
    parser.add_argument('--release', type=Path, help='Release executable when the candidate uses a separate fixture adapter')
    parser.add_argument('--fixtures', type=Path, default=ROOT / 'parity/fixtures/cli.json')
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((ROOT / 'parity/inventory.json').read_text())
    fixture_set = json.loads(args.fixtures.read_text())
    validate(fixture_set, json.loads((ROOT / 'parity/fixture-schema.json').read_text()))
    identifiers = [s['id'] for s in fixture_set['scenarios']]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError('Duplicate scenario identifiers')
    report = {'inventory_sha256': hashlib.sha256((ROOT / 'parity/inventory.json').read_bytes()).hexdigest(), 'reference_verified': not bool(args.reference), 'fixtures_sha256': hashlib.sha256(args.fixtures.read_bytes()).hexdigest(), 'candidate': command_array(args.candidate) if args.candidate else None, 'reference': manifest['revision'], 'mode': 'differential' if args.candidate else 'reference-only', 'results': []}
    report['harness_hashes'] = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT / 'tools').glob('*.py'))}
    try:
        report['fixtures_path'] = str(args.fixtures.resolve().relative_to(ROOT))
    except ValueError:
        report['fixtures_path'] = str(args.fixtures.resolve())
    report['environment'] = {'python':sys.version, 'platform':sys.platform, 'locale':'C.UTF-8', 'terminal':'xterm-256color'}
    with tempfile.TemporaryDirectory(prefix='worklane-reference-') as directory:
        ref = reference(Path(directory), manifest['revision'])
        if fixture_set.get('reference_kind') == 'runtime':
            ref = [sys.executable, str(ROOT / 'tools/port_reference_driver.py'), '--source', directory]
        if args.reference:
            ref = command_array(args.reference)
        candidate = command_array(args.candidate) if args.candidate else None
        report['candidate_hashes'] = command_hashes(candidate) if candidate else {}
        if candidate:
            report['release_sha256'] = hashlib.sha256((args.release or Path(candidate[0])).read_bytes()).hexdigest()
        report['reference_command'] = ref
        report['reference_hashes'] = command_hashes(ref)
        for fixture in fixture_set['scenarios']:
            try:
                expected = execute(ref, fixture)
                actual = execute(candidate, fixture) if candidate else expected
                if actual != expected:
                    report['results'].append({'id': fixture['id'], 'status':'fail', 'reference_observations':expected, 'candidate_observations':actual})
                    continue
                report['results'].append({'id': fixture['id'], 'status': 'pass', 'comparison':'equal' if candidate else 'reference-only', 'observations': expected})
            except Exception as error:
                report['results'].append({'id': fixture['id'], 'status': 'fail', 'error': str(error)})
    args.evidence.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'mode': report['mode'], 'passed': sum(r['status'] == 'pass' for r in report['results']), 'total': len(report['results'])}))
    return int(any(r['status'] != 'pass' for r in report['results']))


if __name__ == '__main__':
    raise SystemExit(main())
