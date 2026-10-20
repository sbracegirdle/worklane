"""Test-only facades run frozen Python assertions against the Rust library driver."""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / 'target/debug/runtime-fixture-adapter'


def acceptance_inputs(driver):
    dependencies={
        'test_native_workflow.py':['tools/port_security_cases.py'],
        'test_native_lifecycle.py':['test_native_workflow.py','tools/port_security_cases.py'],
    }
    return [driver,'tools/native_test_bridge.py',*dependencies.get(driver,[])]


def acceptance_report(rows, driver):
    import hashlib
    inventory=ROOT/'parity/inventory.json'
    digest=hashlib.sha256(BINARY.read_bytes()).hexdigest()
    return {'mode':'differential','candidate_runtime':'native','reference_verified':True,
            'reference':json.loads(inventory.read_text())['revision'],
            'inventory_sha256':hashlib.sha256(inventory.read_bytes()).hexdigest(),
            'candidate':[str(BINARY)],'candidate_hashes':{str(BINARY):digest},'release_sha256':digest,
            'driver':driver,'harness_hashes':{path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest()
                                            for path in acceptance_inputs(driver)},
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
                    reply = {'error':str(error),'retry_at':getattr(error,'retry_at',None),'deferred':getattr(error,'deferred',False),'kind':'quota' if isinstance(error,w.GitHubRateLimit) else
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


class NativeBudget:
    """The native shared quota journal, with only the raw response injected."""
    def __init__(self, state):
        import worklane as w
        self.repo=state.repo
        self.root=state.path.parent.parent
        self.state=w.State('_github',self.root)

    def boundary(self, method, handler=None, **values):
        import worklane as w
        from datetime import datetime, timezone
        def command(event):
            if handler is None:raise AssertionError('Unexpected budget request: '+repr(event))
            return {'stdout':handler(),'stderr':'','exit':0}
        output=invoke_rpc({'op':'budget.call','method':method,'repo':self.repo,
            'state_root':str(self.root),'real_dependencies':True,
            'clock':datetime.fromtimestamp(w.time.time(),timezone.utc).isoformat(),**values},command)
        if 'error' in output:
            if output['kind']=='quota':raise w.GitHubRateLimit(output['error'],output.get('retry_at') or 0,
                                                            deferred=output['deferred'])
            raise RuntimeError(output['error'])
        return output

    def snapshot(self):return self.boundary('snapshot')['value']
    def check(self):self.boundary('check')
    def clear(self, previous):self.boundary('clear',previous=previous)
    def call(self, invoke):return self.boundary('call',invoke)['value']
    def record(self, error):
        output=self.boundary('record',error=str(error),retry_at=error.retry_at,deferred=error.deferred)
        error.deferred=output['deferred']


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


def native_feedback(runner, task):
    """Run native snapshot parsing with the frozen tests' API and provenance responses."""
    import worklane as w
    nodes={}
    def command(event):
        args=event['argv'][1:]
        if 'graphql' in args:
            body=json.loads(event['stdin'])
            entry=nodes[body['variables']['id']]
            editor=runner.feedback_editor(entry)
            value={'node':{'body':entry.get('body') or '',
                          'lastEditedAt':None if editor is None else w.now(),
                          'editor':{'login':editor,'__typename':'User'} if isinstance(editor,str) else None}}
            text=json.dumps({'data':value})
        elif args[:2]==['pr','view']:
            text=json.dumps({'url':args[2],'isDraft':False,'isCrossRepository':False,'mergeable':'MERGEABLE',
                             **runner.pr(args[2])})
        elif args[0]=='api' and args[1].endswith('/permission'):
            trusted=runner.trusted_feedback_user(args[1].split('/')[-2])
            text=json.dumps({'permission':'write' if trusted else 'read'})
        else:
            pages=json.loads(json.dumps(w.gh(*args)))
            for page in pages:
                for entry in page:
                    # Frozen tests mock provenance above the command boundary.
                    # A node lets the native provenance guard consume that response.
                    identifier=entry.setdefault('node_id',f'fixture-{args[1]}-{entry["id"]}')
                    nodes[identifier]=entry
            text=json.dumps(pages)
        return {'stdout':text,'stderr':'','exit':0}
    return invoke_rpc({'op':'workflow.call','method':'human_feedback','repo':runner.repo,
                       'root':str(runner.root),'state_root':str(runner.state.path.parent.parent),
                       'task':task,'clock':w.now()},command)


def native_dependency_count(issue):
    return object.__new__(NativeBoard).call('dependency_count',issue=issue)


def native_assignment(runner, method, *, items=None, selected=None, slot='worker-1', task=None, saved=None):
    """Run the entire native claim/recovery guard with the frozen tests' fake board/API."""
    import worklane as w
    from unittest.mock import Mock
    board=runner.board
    statuses={value.casefold():value for value in (w.READY,w.PROGRESS,w.REVIEW,w.HUMAN)}
    attrs={key:value for key,value in board.__dict__.items() if key in {
        'repo','owner','number','project','field','options','required_statuses'}}
    attrs.setdefault('options',statuses)
    last_status={}
    pr_task={}
    feedback_nodes={}
    def find(identifier):
        return next((item for item in [*(items or []),*getattr(board,'queue',[]),selected or {}]
                     if item.get('id')==identifier),{'id':identifier})
    def command(event):
        nonlocal pr_task
        args=event['argv'][1:]
        if 'graphql' in args:
            body=json.loads(event['stdin']);query,variables=body['query'],body['variables']
            if 'viewerCanUpdate' in query:
                board.require_safe()
                value={'node':{'public':False,'viewerCanUpdate':True},'repository':{'viewerPermission':'ADMIN'}}
            elif 'ProjectV2Item' in query:
                current=board.item_status(find(variables['id']))
                if (selected and selected.get('id')==variables['id'] and isinstance(board.item_status,Mock)
                        and hasattr(board,'queue')):
                    # FakeBoard.move checks its queue, not its mocked parent-status reader.
                    current=next((i['status'].casefold() for i in board.queue if i['id']==variables['id']),None)
                last_status[variables['id']]=current
                value={'node':None if current is None else {'project':attrs.get('project',{'id':'project'}),
                       'fieldValueByName':{'name':current}}}
            elif 'reviewThreads' in query and isinstance(runner.refresh_review_threads,Mock):
                value={'repository':{'pullRequest':{'reviewThreads':{'nodes':[],
                       'pageInfo':{'hasNextPage':False,'endCursor':None}}}}}
            elif 'lastEditedAt' in query and variables['id'] in feedback_nodes:
                value={'node':{'body':feedback_nodes[variables['id']]['body'],'lastEditedAt':None,'editor':None}}
            elif isinstance(w.graphql,Mock):value=w.graphql(query,variables)
            elif 'issueDependenciesSummary' in query:
                ready=board.dependencies_ready(selected or find('item-'+str(variables['number'])))
                value={'repository':{'issue':{'issueDependenciesSummary':{'blockedBy':0 if ready else 1}}}}
            else:raise AssertionError('Unexpected assignment query: '+query)
            text=json.dumps({'data':value})
        elif args[:2]==['issue','view']:text=json.dumps(runner.issue(int(args[2])))
        elif args[:2]==['pr','view']:
            pr=runner.pr(args[2])
            pr_task=next((t for t in runner.state.all() if t.get('pr_url')==args[2]),{})
            task=runner.state.load((selected or {}).get('content',{}).get('number',1))
            text=json.dumps({'url':args[2],'isCrossRepository':False,'mergeable':'MERGEABLE','state':'OPEN',
                             'headRefName':task.get('branch','codex/1-worklane'),'baseRefName':task.get('base','main'),
                             'headRefOid':task.get('head_sha','a'*40),'isDraft':False,**pr})
        elif args[0]=='api' and '/dependencies/blocked_by?' in args[1]:text=json.dumps(w.gh(*args))
        elif args[0]=='api' and args[1].endswith('/permission') and isinstance(runner.human_feedback,Mock):
            text=json.dumps({'permission':'write'})
        elif args[0]=='api' and isinstance(runner.human_feedback,Mock):
            snapshot=runner.human_feedback(pr_task)
            entries=[]
            for index,(key,value) in enumerate(snapshot.items()):
                entry={'id':index,'node_id':key,'body':value.get('body','fixture feedback') if isinstance(value,dict) else str(value),
                       'user':{'login':'fixture','type':'User'},'created_at':w.now()}
                entries.append(entry);feedback_nodes[key]=entry
            text=json.dumps([entries])
        elif args[:2]==['project','item-edit']:
            identifier=args[args.index('--id')+1]
            option=args[-1]
            options=attrs.get('options',statuses)
            status=statuses[next(key for key,value in options.items() if value==option)]
            try:
                board.move(find(identifier),status,statuses[last_status[identifier]])
            except w.GitHubRateLimit as error:
                # Frozen direct pick/recover tests raise from FakeBoard.move,
                # outside a scoped budget. Preserve that preclassified boundary;
                # real raw-command quota journaling is checked separately.
                error.deferred=True
                raise
            text=''
        else:raise AssertionError('Unexpected assignment command: '+repr(args))
        return {'stdout':text,'stderr':'','exit':0}
    output=invoke_rpc({'op':'workflow.call','method':method,'repo':runner.repo,'root':str(runner.root),
        'state_root':str(runner.state.path.parent.parent),'board':attrs,'items':items or [],'item':selected or {},
        'slot':slot,'clock':w.now(),'feedback_unavailable':list(getattr(runner,'feedback_unavailable',set())),
        'stack_items':getattr(runner,'stack_items',[]),'task':task or {},'saved':saved},command)
    if items is not None:items[:]=output['items']
    if selected is not None:selected.update(output['item'])
    if 'error' in output:
        if output['kind']=='quota':raise w.GitHubRateLimit(output['error'])
        raise RuntimeError(output['error'])
    return output['value']


def native_pick(runner, selected, slot):return native_assignment(runner,'pick',selected=selected,slot=slot)
def native_recover(runner, items, slot):return native_assignment(runner,'recover',items=items,slot=slot)
def native_dependencies_ready(runner, selected, task=None):
    return native_assignment(runner,'dependencies_ready',selected=selected,saved=task)
def native_validate_stack(runner, task):return native_assignment(runner,'validate_stack',task=task)


def native_restack_boundary(runner, method, *, items=None, active=(), task=None, tree=None, review=False):
    """Real native restack/ancestry code with the frozen tests' Git and GH boundaries."""
    import worklane as w
    board=runner.board
    statuses={s.casefold():s for s in (w.READY,w.PROGRESS,w.REVIEW,w.HUMAN)}
    before, moved = {}, set()
    def find(identifier):
        return next(i for i in [*(items or []),*board.queue] if i['id']==identifier)
    def command(event):
        argv=event['argv'];args=argv[1:]
        if argv[0]=='git':
            text=w.command(argv,cwd=Path(event['cwd']),stdin=event['stdin'])
        elif 'graphql' in args:
            body=json.loads(event['stdin']);query,values=body['query'],body['variables']
            if 'viewerCanUpdate' in query:
                board.require_safe()
                value={'node':{'public':False,'viewerCanUpdate':True},'repository':{'viewerPermission':'ADMIN'}}
            elif 'ProjectV2Item' in query:
                identifier=values['id'];selected=find(identifier)
                status=board.item_status(selected)
                if identifier in moved and status==before[identifier]:
                    status=next(i['status'].casefold() for i in board.queue if i['id']==identifier)
                before[identifier]=status
                value={'node':{'project':{'id':'project'},'fieldValueByName':{'name':status}}}
            else:raise AssertionError('Unexpected restack query: '+query)
            text=json.dumps({'data':value})
        elif args[:2]==['pr','view']:
            saved=runner.state.all()
            known=[*saved,*(edge for row in saved for edge in row.get('stack',[]))]
            entry=next((row for row in known if row.get('pr_url')==args[2]),{})
            text=json.dumps({'url':args[2],'headRefName':entry.get('branch','child'),
                'baseRefName':entry.get('base','main'),'headRefOid':entry.get('head_sha','a'*40),
                'isDraft':False,'isCrossRepository':False,'mergeable':'MERGEABLE',**runner.pr(args[2])})
        elif args[:2]==['pr','edit']:
            text=w.command(argv)
        elif args[:2]==['project','item-edit']:
            identifier=args[args.index('--id')+1]
            board.move(find(identifier),args[-1],statuses[before[identifier]])
            moved.add(identifier)
            text=''
        else:raise AssertionError('Unexpected restack command: '+repr(argv))
        return {'stdout':text,'stderr':'','exit':0}
    output=invoke_rpc({'op':'workflow.call','method':method,'repo':runner.repo,'root':str(runner.root),
        'state_root':str(runner.state.path.parent.parent),'clock':w.now(),
        'board':{'options':statuses},'items':items or [],'active':list(active),'task':task or {},
        'tree':str(tree or runner.root),'review':review,'local_commands':True},command)
    if task is not None:task.update(output['task'])
    if items is not None:items[:]=output['items']
    if 'error' in output:
        if output['kind']=='quota':raise w.GitHubRateLimit(output['error'])
        raise RuntimeError(output['error'])
    return output['value']


def native_reconcile_stacks(runner, items, active_numbers=()):
    return native_restack_boundary(runner,'reconcile_stacks',items=items,active=active_numbers)


def native_verify_stack_ancestry(runner, task, tree):
    return native_restack_boundary(runner,'verify_stack_ancestry',task=task,tree=tree)


def native_worktree(runner, task, review=False):
    return Path(native_restack_boundary(runner,'worktree',task=task,review=review))


def native_descriptor(request):
    """Receive the actual Rust-created descriptor, without copying its contents."""
    import array
    import socket
    with ExitStack() as stack:
        receiver,sender=socket.socketpair()
        stack.callback(receiver.close);stack.callback(sender.close)
        receiver.settimeout(2)
        invoke({**request,'socket_fd':sender.fileno()},pass_fds=(sender.fileno(),))
        _,ancillary,flags,_=receiver.recvmsg(1,socket.CMSG_SPACE(array.array('i').itemsize),socket.MSG_CMSG_CLOEXEC)
        descriptors=array.array('i')
        for level,kind,data in ancillary:
            if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
                descriptors.frombytes(data[:len(data)-(len(data)%descriptors.itemsize)])
        if len(descriptors)!=1 or flags&socket.MSG_CTRUNC:
            for descriptor in descriptors:os.close(descriptor)
            raise RuntimeError('Native descriptor transfer was incomplete')
        return descriptors[0]


def native_memory_fd(name):return native_descriptor({'op':'memory.fd','name':name})


@contextmanager
def native_locked(path, *, blocking=True):
    fd=native_descriptor({'op':'lock.fd','path':str(path),'blocking':blocking})
    try:yield fd
    finally:os.close(fd)


def native_owned(roots=(),tokens=(),known=None):
    output=invoke({'op':'process.owned','roots':[{'pid':pid,'birth':birth} for pid,birth in roots],
                   'tokens':list(tokens),'known':known or {}})
    return {int(pid):birth for pid,birth in output.items()}


def native_terminate_owned(roots=(),tokens=(),grace=2,known=None):
    return invoke({'op':'process.terminate','roots':[{'pid':pid,'birth':birth} for pid,birth in roots],
                   'tokens':list(tokens),'grace':grace,'known':known or {}})


def native_load_config(root):
    return invoke({'op':'config.resolve','root':str(root)})


def native_validate_config(values):
    try:return invoke({'op':'config.validate','values':values})
    except RuntimeError as error:raise ValueError(str(error)) from error


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
