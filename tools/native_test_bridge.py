"""Test-only facades run frozen Python assertions against the Rust library driver."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / 'target/debug/runtime-fixture-adapter'


def invoke(request, *, pass_fds=(), cwd=None):
    request = {'real_dependencies': True, **request}
    process = subprocess.run([str(BINARY)], input=json.dumps(request), text=True,
                             capture_output=True, pass_fds=pass_fds, cwd=cwd, timeout=40)
    if not process.stdout:
        raise RuntimeError(f'Native driver exited {process.returncode}: {process.stderr}')
    response = json.loads(process.stdout)
    if process.returncode:
        kind, error = response.get('kind'), response['error']
        if kind == 'interrupted':
            import worklane
            raise worklane.InterruptedRun(error)
        if kind == 'sqlite':
            raise (sqlite3.IntegrityError if 'constraint failed' in error else sqlite3.OperationalError)(error)
        class NativeError(RuntimeError, ValueError):
            pass
        raise NativeError(error)
    return response['ok']


class NativeState:
    VERSION = 2

    def __init__(self, repo, root=None):
        self.repo = repo
        self.root = Path(root or os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state'))
        self.path = Path(self.call('open'))
        self.database = self.path / 'state.sqlite3'

    def call(self, method, **kwargs):
        return invoke({'op':'state.call', 'method':method, 'repo':self.repo,
                       'state_root':str(self.root), **kwargs})

    @contextmanager
    def connect(self):
        # Test assertions inspect the real shared database directly.
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, task):
        task.update(self.call('save', task=task))

    def load(self, number): return self.call('load', number=number)
    def all(self): return self.call('all')
    def get_meta(self, key, default=None): return self.call('get_meta', key=key, default=default)
    def set_meta(self, key, value): return self.call('set_meta', key=key, value=value)
    def pause(self, value): return self.call('pause', value=value)
    def paused(self): return self.call('paused')
    def running(self): return self.call('running')
    def request_stop(self, immediate=False): return self.call('request_stop', immediate=immediate)
    def session(self, number, role): return self.call('session', number=number, role=role)
    def runs(self, number=None): return self.call('runs', number=number)
    def begin_run(self, task, stage, tree):
        output = self.call('begin_run', task=task, stage=stage, tree=str(tree))
        task.update(output['task'])
        return output['id']
    def attach_process(self, run, task, pid): task.update(self.call('attach_process', run_id=run, task=task, pid=pid))
    def attach_session(self, run, session, model, backend='codex'):
        return self.call('attach_session', run_id=run, session_id=session, model=model, backend=backend)
    def record_event(self, run, event, model, backend='codex'):
        return self.record_events(run, [event], model, backend)
    def record_events(self, run, events, model, backend='codex', *, wire=None):
        return self.call('record_events', run_id=run, events=events, model=model, backend=backend,
                         **({'wire':wire} if wire is not None else {}))
    def queue_input(self, run, text): return self.call('queue_input', run_id=run, text=text)
    def advance_run(self, run, result=None):
        return self.call('advance_run', run_id=run, **({'result':result} if result is not None else {}))
    def finish_run(self, run, status, error=None, result=None):
        return self.call('finish_run', run_id=run, status=status, error=error,
                         **({'result':result} if result is not None else {}))
    def recover_runs(self): return self.call('recover_runs')
    def append_log(self, run, stream, text): return self.call('append_log', run_id=run, stream=stream, text=text)
    def events(self, run): return self.call('events', run_id=run)
    def log_entries(self, number=None):
        output, after = [], 0
        while page := self.call('logs_page', number=number, after=after, limit=1000):
            output.extend(page)
            after = page[-1]['id']
        return output


class NativeBackend:
    def __init__(self, model, root, state, runner_fd, harness='codex', effort='high'):
        self.model, self.root, self.state, self.fd = model, root, state, runner_fd
        self.backend, self.effort = harness, effort
        self.timeout = 8 * 3600

    def run(self, stage, task, worktree, extra=''):
        output = invoke({'op':'agent.run','repo':self.state.repo,'state_root':str(self.state.path.parent.parent),
                         'task':task,'stage':stage,'tree':str(worktree),'root':str(self.root),
                         'force_backend':self.backend,'model':self.model or ('gpt-6.1-sol' if self.backend=='codex' else ''),
                         'config':{'harness':self.backend,'model':self.model or '', 'effort':self.effort},
                         'timeout':self.timeout,'extra':extra,'runner_fd':self.fd,'include_task':True},
                        pass_fds=(self.fd,))
        task.update(output['task'])
        return output['result']


class NativeRouter:
    def __init__(self, args, root, state, runner_fd):
        self.args, self.root, self.state, self.fd = args, root, state, runner_fd

    def selection(self, role, session=None):
        if session: return session.get('backend','codex'), session['model'] or None
        return (getattr(self.args, role+'_harness') or self.args.harness or 'codex',
                getattr(self.args, role+'_model') or self.args.model)

    def run(self, stage, task, tree, extra=''):
        config = {k:v for k,v in vars(self.args).items() if k in {
            'harness','model','effort','worker_harness','reviewer_harness','finalizer_harness',
            'worker_model','reviewer_model','finalizer_model'} and v is not None}
        output = invoke({'op':'agent.run','repo':self.state.repo,'state_root':str(self.state.path.parent.parent),
                         'task':task,'stage':stage,'tree':str(tree),'root':str(self.root),
                         'config':config,'extra':extra,'runner_fd':self.fd,'include_task':True}, pass_fds=(self.fd,))
        task.update(output['task'])
        return output['result']


class NativeLogWriter:
    def __init__(self, state, stream, tee=None):
        self.state, self.stream, self.tee, self.chunks = state, stream, tee, []

    def write(self, text):
        self.chunks.append(text)
        if self.tee:
            self.tee.write(text)
        return len(text)

    def flush(self):
        invoke({'op':'log_writer','state_root':str(self.state.root),'repo':self.state.repo,
                'stream':self.stream,'chunks':self.chunks})
        self.chunks = []
