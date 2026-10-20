"""Test-only facades run frozen Python assertions against the Rust library driver."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / 'target/debug/runtime-fixture-adapter'


def acceptance_report(rows, driver):
    import hashlib
    inventory=ROOT/'parity/inventory.json'
    digest=hashlib.sha256(BINARY.read_bytes()).hexdigest()
    return {'mode':'differential','candidate_runtime':'native','reference_verified':True,
            'reference':json.loads(inventory.read_text())['revision'],
            'inventory_sha256':hashlib.sha256(inventory.read_bytes()).hexdigest(),
            'candidate':[str(BINARY)],'candidate_hashes':{str(BINARY):digest},'release_sha256':digest,
            'driver':driver,'harness_hashes':{path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest()
                                            for path in (driver,'tools/native_test_bridge.py')},
            'results':rows}


def invoke_rpc(request, handler):
    """Inject only the command boundary; all parsing/guards execute in Rust."""
    import worklane as w
    reads, writes = os.pipe(), os.pipe()
    failures = []
    def serve():
        with os.fdopen(reads[0], 'rb') as stream, os.fdopen(writes[1], 'wb') as replies:
            while line := stream.readline(16*1024*1024+1):
                try:
                    if len(line) > 16*1024*1024:
                        raise RuntimeError('Oversized callback request')
                    reply = {'ok':handler(json.loads(line))}
                except Exception as error:
                    if isinstance(error, AssertionError): failures.append(error)
                    reply = {'error':str(error),'retry_at':getattr(error,'retry_at',None),'kind':'quota' if isinstance(error,w.GitHubRateLimit) else
                             'busy' if isinstance(error,subprocess.TimeoutExpired) else 'io' if isinstance(error,OSError) else 'github'}
                replies.write(json.dumps(reply).encode()+b'\n'); replies.flush()
    worker = threading.Thread(target=serve,daemon=True)
    worker.start()
    try:
        request = {'real_dependencies':True,**request,'callback_write_fd':reads[1],'callback_read_fd':writes[0]}
        # Popen avoids the frozen tests' subprocess.run mock at the injected boundary.
        with subprocess.Popen([str(BINARY)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              text=True,pass_fds=(reads[1],writes[0])) as process:
            stdout,stderr = process.communicate(json.dumps(request),timeout=40)
        if failures: raise failures[0]
        if not stdout: raise RuntimeError(f'Native callback driver exited {process.returncode}: {stderr}')
        response = json.loads(stdout)
        if process.returncode:
            if response.get('kind') == 'quota': raise w.GitHubRateLimit(response['error'])
            raise RuntimeError(response['error'])
        return response['ok']
    finally:
        os.close(reads[1]); os.close(writes[0]); worker.join(timeout=2)
        if worker.is_alive(): raise RuntimeError('Command callback did not close')


def native_command(argv, *, cwd=None, stdin=None, timeout=120):
    import worklane as w
    from datetime import datetime, timezone
    def execute(event):
        result = subprocess.run(event['argv'],cwd=cwd,input=event['stdin'],text=True,
                                capture_output=True,timeout=timeout)
        return {'stdout':result.stdout,'stderr':result.stderr,'exit':result.returncode}
    with tempfile.TemporaryDirectory() as state_root:
        output = invoke_rpc({'op':'transport.call','method':'raw','argv':list(map(str,argv)),'stdin':stdin,
                             'state_root':state_root,'real_dependencies':False,
                             'clock':datetime.fromtimestamp(w.time.time(),timezone.utc).isoformat()},execute)
    if 'error' in output:
        if output['kind']=='quota': raise w.GitHubRateLimit(output['error'],output.get('retry_at') or 0)
        raise RuntimeError(output['error'])
    return output['value']


def native_rate_limit(detail):
    import worklane as w
    with tempfile.TemporaryDirectory() as state_root:
        output=invoke_rpc({'op':'transport.call','method':'rate','detail':detail,'now':w.time.time(),
                           'state_root':state_root},lambda event: (_ for _ in ()).throw(AssertionError(event)))
    return None if output['value'] is None else w.GitHubRateLimit(detail,output['value'])


def native_graphql(query, variables=None):
    import worklane as w
    from datetime import datetime, timezone
    def command(event):
        return {'stdout':w.command(event['argv'],stdin=event['stdin']),'stderr':'','exit':0}
    with tempfile.TemporaryDirectory() as root:
        output=invoke_rpc({'op':'transport.call','method':'graphql','query':query,'variables':variables or {},
                           'state_root':root,'real_dependencies':False,
                           'clock':datetime.fromtimestamp(w.time.time(),timezone.utc).isoformat()},command)
    if 'error' in output:
        if output['kind']=='quota': raise w.GitHubRateLimit(output['error'],output.get('retry_at') or 0)
        raise RuntimeError(output['error'])
    return output['value']


class NativeBoard:
    """Frozen Board assertions with command responses injected into the native Board."""
    def call(self, method, **values):
        import worklane as w
        from unittest.mock import Mock
        def command(event):
            argv=event['argv'][1:]
            if 'graphql' in argv:
                body=json.loads(event['stdin'])
                if 'viewerCanUpdate' in body['query'] and 'require_safe' in self.__dict__:
                    self.require_safe()
                    result={'node':{'public':False,'viewerCanUpdate':True},'repository':{'viewerPermission':'ADMIN'}}
                else:
                    result=w.graphql(body['query'],body['variables'])
                text=json.dumps({'data':result})
            elif argv[:2]==['project','field-list'] and 'refresh_fields' in self.__dict__:
                self.refresh_fields()
                text=json.dumps({'fields':[{**self.field,'name':'Status','options':[
                    {'name':name,'id':identifier} for name,identifier in self.options.items()]}]})
            elif isinstance(w.gh,Mock): text=json.dumps(w.gh(*argv))
            else: text=w.command(['gh',*argv],stdin=event['stdin'])
            return {'stdout':text,'stderr':'','exit':0}
        attrs={key:value for key,value in self.__dict__.items() if key in {
            'repo','owner','number','project','field','options','required_statuses'}}
        with tempfile.TemporaryDirectory() as root:
            output=invoke_rpc({'op':'board.call','method':method,'board':attrs,'state_root':root,**values},command)
        self.__dict__.update(output['board'])
        return output['value']

    def items(self): return self.call('items')
    def linked_prs(self, identifier, connection): return self.call('linked_prs',id=identifier,connection=connection)
    def refresh_fields(self): return self.call('refresh_fields')
    def ensure_review_status(self): return self.call('ensure_review_status')
    def dependencies_ready(self, selected):
        output=self.call('dependencies_ready',item=selected)
        selected.update(output['item'])
        return output['ready']
    def item_status(self, selected): return self.call('item_status',item=selected)
    def move(self, selected, status, expected): return self.call('move',item=selected,status=status,expected=expected)


def native_review_threads(runner, url):
    import worklane as w
    def command(event):
        body=json.loads(event['stdin'])
        return {'stdout':json.dumps({'data':w.graphql(body['query'],body['variables'])}),'stderr':'','exit':0}
    return invoke_rpc({'op':'workflow.call','method':'review_threads','url':url,'repo':runner.repo,
                       'state_root':str(runner.state.path.parent.parent)},command)


def native_dependency_count(issue):
    return object.__new__(NativeBoard).call('dependency_count',issue=issue)


def native_assignment(runner, method, *, items=None, selected=None, slot='worker-1'):
    """Run the entire native claim/recovery guard with the frozen tests' fake board/API."""
    import worklane as w
    from unittest.mock import Mock
    board=runner.board
    statuses={value.casefold():value for value in (w.READY,w.PROGRESS,w.REVIEW,w.HUMAN)}
    attrs={key:value for key,value in board.__dict__.items() if key in {
        'repo','owner','number','project','field','options','required_statuses'}}
    attrs.setdefault('options',statuses)
    last_status={}
    def find(identifier):
        return next((item for item in [*(items or []),*getattr(board,'queue',[]),selected or {}]
                     if item.get('id')==identifier),{'id':identifier})
    def command(event):
        args=event['argv'][1:]
        if 'graphql' in args:
            body=json.loads(event['stdin']);query,variables=body['query'],body['variables']
            if 'viewerCanUpdate' in query:
                board.require_safe()
                value={'node':{'public':False,'viewerCanUpdate':True},'repository':{'viewerPermission':'ADMIN'}}
            elif 'ProjectV2Item' in query:
                current=board.item_status(find(variables['id']));last_status[variables['id']]=current
                value={'node':None if current is None else {'project':attrs.get('project',{'id':'project'}),
                       'fieldValueByName':{'name':current}}}
            elif isinstance(w.graphql,Mock):value=w.graphql(query,variables)
            elif 'issueDependenciesSummary' in query:
                ready=board.dependencies_ready(selected or find('item-'+str(variables['number'])))
                value={'repository':{'issue':{'issueDependenciesSummary':{'blockedBy':0 if ready else 1}}}}
            else:raise AssertionError('Unexpected assignment query: '+query)
            text=json.dumps({'data':value})
        elif args[:2]==['issue','view']:text=json.dumps(runner.issue(int(args[2])))
        elif args[:2]==['pr','view']:
            pr=runner.pr(args[2])
            task=runner.state.load((selected or {}).get('content',{}).get('number',1))
            text=json.dumps({'url':args[2],'isCrossRepository':False,'mergeable':'MERGEABLE','state':'OPEN',
                             'headRefName':task.get('branch','codex/1-worklane'),'baseRefName':task.get('base','main'),
                             'headRefOid':task.get('head_sha','a'*40),'isDraft':False,**pr})
        elif args[:2]==['project','item-edit']:
            identifier=args[args.index('--id')+1]
            option=args[-1]
            options=attrs.get('options',statuses)
            status=statuses[next(key for key,value in options.items() if value==option)]
            board.move(find(identifier),status,statuses[last_status[identifier]])
            text=''
        else:raise AssertionError('Unexpected assignment command: '+repr(args))
        return {'stdout':text,'stderr':'','exit':0}
    output=invoke_rpc({'op':'workflow.call','method':method,'repo':runner.repo,'root':str(runner.root),
        'state_root':str(runner.state.path.parent.parent),'board':attrs,'items':items or [],'item':selected or {},
        'slot':slot,'clock':w.now(),'feedback_unavailable':list(getattr(runner,'feedback_unavailable',set()))},command)
    if items is not None:items[:]=output['items']
    if selected is not None:selected.update(output['item'])
    if 'error' in output:
        if output['kind']=='quota':raise w.GitHubRateLimit(output['error'])
        raise RuntimeError(output['error'])
    return output['value']


def native_pick(runner, selected, slot):return native_assignment(runner,'pick',selected=selected,slot=slot)
def native_recover(runner, items, slot):return native_assignment(runner,'recover',items=items,slot=slot)


def native_owned(roots=(),tokens=(),known=None):
    output=invoke({'op':'process.owned','roots':[{'pid':pid,'birth':birth} for pid,birth in roots],
                   'tokens':list(tokens),'known':known or {}})
    return {int(pid):birth for pid,birth in output.items()}


def native_terminate_owned(roots=(),tokens=(),grace=2,known=None):
    return invoke({'op':'process.terminate','roots':[{'pid':pid,'birth':birth} for pid,birth in roots],
                   'tokens':list(tokens),'grace':grace,'known':known or {}})


def native_load_config(root):
    return invoke({'op':'config.resolve','root':str(root)})


def native_save_config(root, values):
    return invoke({'op':'config.save','root':str(root),'values':values})


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


def invoke_guarded(request, callback, *, pass_fds=()):
    if callback is None:
        return invoke(request,pass_fds=pass_fds)
    request_read,request_write=os.pipe()
    response_read,response_write=os.pipe()
    def serve():
        with os.fdopen(request_read,'rb') as stream, os.fdopen(response_write,'wb') as replies:
            while line:=stream.readline(16*1024*1024+1):
                if len(line)>16*1024*1024: raise RuntimeError('Oversized test callback request')
                event=json.loads(line)
                try:
                    callback(event['stage'],event['task']); response={'ok':True}
                except Exception as error:
                    import worklane
                    response={'error':str(error),'kind':'interrupted' if isinstance(error,worklane.InterruptedRun) else 'blocked'}
                replies.write(json.dumps(response).encode()+b'\n');replies.flush()
    worker=threading.Thread(target=serve,daemon=True);worker.start()
    try:
        return invoke({**request,'preflight_write_fd':request_write,'preflight_read_fd':response_read},
                      pass_fds=(*pass_fds,request_write,response_read))
    finally:
        os.close(request_write);os.close(response_read);worker.join(timeout=2)
        if worker.is_alive():raise RuntimeError('Test preflight callback did not close')


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
        self.preflight = None

    def run(self, stage, task, worktree, extra=''):
        output = invoke_guarded({'op':'agent.run','repo':self.state.repo,'state_root':str(self.state.path.parent.parent),
                         'task':task,'stage':stage,'tree':str(worktree),'root':str(self.root),
                         'force_backend':self.backend,'model':self.model or ('gpt-6.1-sol' if self.backend=='codex' else ''),
                         'config':{'harness':self.backend,'model':self.model or '', 'effort':self.effort},
                         'timeout':self.timeout,'extra':extra,'runner_fd':self.fd,'include_task':True},self.preflight,
                        pass_fds=(self.fd,))
        task.update(output['task'])
        return output['result']


class NativeRouter:
    def __init__(self, args, root, state, runner_fd):
        self.args, self.root, self.state, self.fd = args, root, state, runner_fd
        self.preflight = None

    def selection(self, role, session=None):
        if session: return session.get('backend','codex'), session['model'] or None
        return (getattr(self.args, role+'_harness') or self.args.harness or 'codex',
                getattr(self.args, role+'_model') or self.args.model)

    def run(self, stage, task, tree, extra=''):
        config = {k:v for k,v in vars(self.args).items() if k in {
            'harness','model','effort','worker_harness','reviewer_harness','finalizer_harness',
            'worker_model','reviewer_model','finalizer_model'} and v is not None}
        output = invoke_guarded({'op':'agent.run','repo':self.state.repo,'state_root':str(self.state.path.parent.parent),
                         'task':task,'stage':stage,'tree':str(tree),'root':str(self.root),
                         'config':config,'extra':extra,'runner_fd':self.fd,'include_task':True},self.preflight,pass_fds=(self.fd,))
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
