"""Controlled runtime guard scenarios used by language-neutral acceptance fixtures."""
import argparse
from contextlib import ExitStack
import ctypes
import errno
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from unittest.mock import Mock, patch


def invoke(request):
    import worklane as w
    import worklane_tui as tui
    from test_worklane import FakeBoard, item, result
    root = Path.cwd()
    state = w.State('owner/repo')
    name = request['case']
    effects = {}
    try:
        with ExitStack() as stack:
            if name.startswith(('build.', 'review.', 'worktree.', 'stack.', 'pick.', 'workflow.', 'feedback.', 'completion.')):
                task = {'number':1,'item':item(1,w.PROGRESS),'phase':'building','question':False,
                        'round':1,'head_sha':'a'*40,'branch':'codex/1-worklane','base':'main',
                        'pr_url':'https://github.com/owner/repo/pull/9'}
                board = FakeBoard([task['item']])
                backend = Mock(model=w.MODEL)
                backend.run.return_value = result()
                runner = w.Runner(argparse.Namespace(base='main',once=True,interval=60), 'owner/repo', root, state, board, backend)
                runner.worktree = Mock(return_value=root)
                runner.post = Mock()
                runner.verify_pr = Mock(return_value={'isDraft':False})
                runner.review_threads = Mock(return_value=[])
                runner.human_feedback = Mock(return_value={})
                runner.pr = Mock(return_value={'state':'OPEN','headRefOid':'a'*40,'headRefName':task['branch'],'baseRefName':'main','isDraft':False})
                def command(args, **kwargs):
                    if args[:2] == ['git','rev-parse']: return 'b'*40 if name in ('build.wrong-head','stack.unpushed-head') else 'a'*40
                    if args[:2] == ['git','status']: return ' M tracked' if name == 'build.dirty-worktree' else ''
                    if args[:2] == ['git','diff']: return 'tracked changes' if name in ('review.modified-worktree','worktree.dirty-reviewer') else ''
                    if args[:2] == ['git','branch']: return 'other'
                    return ''
                stack.enter_context(patch.object(w,'command',side_effect=command))
                state.save(task)
                if name.startswith('build.'):
                    if name == 'build.blocked-result': backend.run.return_value = result('blocked')
                    if name == 'build.pass-with-findings': backend.run.return_value = result(findings=['Unresolved'])
                    if name == 'build.question-created-pr': task['question'] = True
                    if name == 'build.invalid-sha': backend.run.return_value = result(sha='short')
                    runner.build(task)
                elif name.startswith('review.'):
                    task.update(phase='reviewing',item=item(1,w.REVIEW))
                    board.queue = [task['item']]
                    if name == 'review.blocked-result': backend.run.return_value = result('blocked')
                    if name == 'review.wrong-result-head': backend.run.return_value = result(sha='b'*40)
                    if name == 'review.pass-with-findings': backend.run.return_value = result(findings=['Unresolved'])
                    runner.review(task)
                elif name == 'workflow.immediate-stop':
                    state.set_meta('shutdown',{'immediate':True})
                    runner.execute(task,False)
                    effects['task'] = state.load(1)
                elif name == 'stack.parent-missing-handoff':
                    task.update(phase='human_review',item=item(1,w.HUMAN))
                    board.queue=[task['item']]; runner.stack_items=board.queue; state.save(task)
                    runner.reviewed_parent(1)
                elif name == 'stack.closed-parent':
                    task.update(phase='human_review',item=item(1,w.HUMAN),stack=[{'number':2,'pr_url':'https://github.com/owner/repo/pull/2','head_sha':'a'*40}])
                    runner.pr.return_value['state']='CLOSED'; state.save(task)
                    runner.reconcile_stacks([task['item']])
                    effects['task']=state.load(1)
                elif name == 'feedback.changed-pr':
                    runner.pr.return_value['state']='CLOSED'
                    w.Runner.human_feedback(runner,task)
                elif name == 'completion.retargeted-pr':
                    task.update(phase='human_review',item=item(1,w.HUMAN))
                    runner.pr.return_value['baseRefName']='other'; state.save(task)
                    runner.reconcile_completed([task['item']])
                    effects['task']=state.load(1)
                elif name == 'stack.parent-quota':
                    runner.stack_policy=Mock(return_value=({(1,2)},set()))
                    runner.reviewed_parent=Mock(side_effect=w.GitHubRateLimit('fixture parent quota'))
                    stack.enter_context(patch.object(w,'graphql',return_value={'repository':{'issue':{'issueDependenciesSummary':{'blockedBy':1}}}}))
                    stack.enter_context(patch.object(w,'gh',return_value=[[{'number':2,'state':'open','repository_url':'https://api.github.com/repos/owner/repo'}]]))
                    runner.dependencies_ready(item(1))
                elif name == 'stack.restack-quota':
                    task.update(phase='human_review',item=item(1,w.HUMAN),stack=[{'number':2,'pr_url':'https://github.com/owner/repo/pull/2'}])
                    state.save(task); runner.pr.side_effect=w.GitHubRateLimit('fixture restack quota')
                    runner.reconcile_stacks([task['item']])
                elif name == 'stack.pending-restack':
                    runner.validate_stack(dict(task,restack_pending={'target':'main'}))
                elif name == 'stack.changed-prerequisite':
                    runner.reviewed_parent = Mock(return_value=({},runner.pr.return_value))
                    runner.validate_stack(dict(task,stack=[{'number':2,'branch':'parent','base':'main','head_sha':'b'*40}]))
                elif name == 'stack.unpushed-head':
                    runner.validate_stack = Mock()
                    task['stack'] = [{'number':2,'head_sha':'a'*40}]
                    w.Runner.verify_pr(runner,task,'a'*40)
                elif name.startswith('worktree.'):
                    if name == 'worktree.missing-saved': task['worker_worktree'] = str(root / 'missing')
                    if name == 'worktree.wrong-worker-branch': task['worker_worktree'] = str(root)
                    if name == 'worktree.dirty-reviewer': task.update(review_worktree=str(root),head_sha='b'*40)
                    if name == 'worktree.existing-local-branch':
                        stack.enter_context(patch.object(w.subprocess,'run',return_value=Mock(returncode=0)))
                    if name == 'worktree.moved-parent':
                        task.update(base_sha='b'*40,stack=[{'head_sha':'b'*40}])
                        stack.enter_context(patch.object(w.subprocess,'run',return_value=Mock(returncode=1)))
                    w.Runner.worktree(runner,task,review=name == 'worktree.dirty-reviewer')
                elif name == 'pick.missing-linked-pr':
                    runner.issue = Mock(return_value={'state':'OPEN','labels':[]})
                    board.dependencies_ready = Mock(return_value=True)
                    runner.pick(item(2,w.REVIEW),w.REVIEWER)
                elif name == 'pick.closed-retry-pr':
                    state.save(dict(task,number=2,phase='retry'))
                    board.dependencies_ready = Mock(return_value=True)
                    runner.issue = Mock(return_value={'state':'OPEN','labels':[]})
                    runner.pr.return_value['state'] = 'CLOSED'
                    runner.pick(item(2,w.REVIEW),w.REVIEWER)
                else:
                    raise ValueError('Unknown guard scenario: ' + name)
                effects.update(board_moves=board.moves, posts=runner.post.call_count)
            elif name.startswith('agent.'):
                task={'number':1,'phase':'building'}
                backend=w.AgentBackend(None,root,state,-1,harness='opencode')
                if name == 'agent.missing-history':
                    old=state.begin_run(task,'build',root); state.finish_run(old,'failed')
                counter=[0]
                stop_read={'agent.before-turn':1,'agent.next-turn':2,'agent.before-launch':3,'agent.after-launch':4,'agent.during-turn':5}.get(name)
                original=state.get_meta
                def get(key,default=None):
                    if key == 'shutdown':
                        counter[0]+=1
                        if stop_read and counter[0]>=stop_read: return {'immediate':True}
                    return original(key,default)
                stack.enter_context(patch.object(state,'get_meta',side_effect=get))
                value=result()
                if name == 'agent.invalid-result-shape': value={'verdict':'pass'}
                if name == 'agent.invalid-verdict': value['verdict']='unknown'
                if name == 'agent.invalid-result-types': value['summary']=7
                if name == 'agent.invalid-findings': value['findings']=[7]
                events=[{'type':'step_start','sessionID':'ses_guard42'},
                        {'type':'text','sessionID':'ses_guard42','part':{'text':json.dumps(value)}},
                        {'type':'step_finish','sessionID':'ses_guard42','part':{'reason':'stop'}}]
                if name == 'agent.invalid-event-shape': events=[[]]
                if name == 'agent.conflicting-session-ids':
                    events=[{'type':'step_start','sessionID':'ses_first'},{'type':'step_start','sessionID':'ses_second'}]
                    stack.enter_context(patch.object(state,'record_events'))
                wire=''.join(json.dumps(event)+'\n' for event in events)
                real_popen=w.subprocess.Popen
                launches=[]
                def launch(args,**kwargs):
                    script='import sys; sys.stdin.read(); sys.stdout.write('+repr(wire)+')'
                    process=real_popen([sys.executable,'-c',script],**kwargs)
                    launches.append(True)
                    if name == 'agent.exit-timeout':
                        wait=process.wait; waited=[False]
                        def wait_once(*args,**kwargs):
                            if not waited[0]:
                                waited[0]=True; raise subprocess.TimeoutExpired('fixture',1)
                            return wait(*args,**kwargs)
                        process.wait=wait_once
                    return process
                stack.enter_context(patch.object(w.subprocess,'Popen',side_effect=launch))
                with w.locked(state.path/'runner.lock') as fd:
                    backend.runner_fd=fd
                    backend.run('build',task,root)
            elif name.startswith('restack.'):
                task = {'number':1,'item':item(1,w.HUMAN),'phase':'human_review','branch':'child','base':'parent',
                        'default_base':'main','base_sha':'b'*40,'head_sha':'a'*40,'worker_worktree':str(root),
                        'pr_url':'https://github.com/owner/repo/pull/9','stack':[{'number':2,'branch':'parent','base':'main','head_sha':'b'*40}]}
                board = FakeBoard([task['item']])
                runner = w.Runner(argparse.Namespace(base='main',once=True,interval=60),'owner/repo',root,state,board,None)
                board.item_status = Mock(return_value=w.READY.casefold() if name == 'restack.wrong-board-status' else w.HUMAN.casefold())
                pr = {'state':'OPEN','headRefName':'child','baseRefName':'parent','headRefOid':'a'*40}
                runner.pr = Mock(side_effect=lambda *_:dict(pr))
                head = ['a'*40]
                def command(args, **kwargs):
                    if args[:2] == ['git','rev-parse']:
                        return 'c'*40 if args[-1] == 'origin/main' else head[0]
                    if args[:2] == ['git','status']: return ''
                    if args[:2] == ['git','branch']: return 'other' if name == 'restack.wrong-branch' else 'child'
                    if args[:2] == ['git','show']: return 'unrelated parents'
                    if args[:2] == ['git','merge']: head[0]='d'*40
                    if args[:2] == ['git','push'] and name != 'restack.unverified-retarget': pr['headRefOid']=head[0]
                    if args[:3] == ['gh','pr','edit'] and name != 'restack.unverified-retarget': pr['baseRefName']='main'
                    return ''
                stack.enter_context(patch.object(w,'command',side_effect=command))
                remaining=[]
                if name == 'restack.moved-target': remaining=[{'branch':'main','head_sha':'b'*40}]
                if name == 'restack.missing-worktree': task['worker_worktree']=str(root/'missing')
                if name == 'restack.changed-child-head': task['head_sha']='f'*40
                pending={'old_base':'parent','old_head':'a'*40,'target':'main','target_sha':'c'*40,'stack':[],'started_at':w.now()}
                if name == 'restack.changed-intent':
                    pending['target_sha']='f'*40; task['restack_pending']=pending
                if name == 'restack.changed-local-head':
                    task['restack_pending']=pending; head[0]='f'*40
                if name == 'restack.changed-pr': pr['headRefOid']='f'*40
                state.save(task)
                runner.restack(task,remaining,task['item'])
            elif name.startswith('cli.'):
                board=Mock(owner='owner',number=2,project={'url':'board'})
                stack.enter_context(patch.object(w,'repository',return_value=('owner/repo',root,'main')))
                stack.enter_context(patch.object(w,'State',return_value=state))
                stack.enter_context(patch.object(w,'Board',return_value=board))
                stack.enter_context(patch.object(w.shutil,'which',return_value='/fake/binary'))
                stack.enter_context(patch.object(w,'installation_info',return_value={}))
                stack.enter_context(patch.object(w,'signal_owned'))
                stack.enter_context(patch.object(w,'owned',return_value={}))
                stack.enter_context(patch.object(w,'terminate_owned',return_value=[]))
                stack.enter_context(patch.object(w.time,'sleep'))
                child=Mock(pid=123); child.poll.return_value=None
                stack.enter_context(patch.object(w.subprocess,'Popen',return_value=child))
                args=['start']
                if name == 'cli.start-exited': child.poll.return_value=1
                elif name == 'cli.start-timeout': pass
                elif name == 'cli.missing-binary':
                    args=['check']; stack.enter_context(patch.object(w.shutil,'which',return_value=None))
                elif name == 'cli.retry-invalid-phase':
                    state.save({'number':1,'phase':'human_review'}); args=['retry','1']
                elif name == 'cli.resume-during-stop':
                    state.set_meta('shutdown',{'requested':True}); args=['resume']
                    stack.enter_context(patch.object(state,'running',return_value=True))
                elif name == 'cli.stop-incomplete':
                    stack.enter_context(patch.object(w,'terminate_owned',return_value=[123])); args=['stop','--now']
                elif name in ('cli.start-cancelled','cli.run-cancelled'):
                    original=state._get; reads=[0]
                    def get(db,owner,default=None):
                        if owner == 'meta:shutdown':
                            reads[0]+=1
                            return {'requested_at':'later'} if reads[0]>1 else {}
                        return original(db,owner,default)
                    stack.enter_context(patch.object(state,'_get',side_effect=get))
                    args=['run','--once'] if name == 'cli.run-cancelled' else ['start']
                else: raise ValueError('Unknown guard scenario: '+name)
                from contextlib import redirect_stdout,redirect_stderr
                stdout,stderr=io.StringIO(),io.StringIO()
                with redirect_stdout(stdout),redirect_stderr(stderr): code=w.main(args)
                effects.update(exit=code,stdout=stdout.getvalue(),stderr=stderr.getvalue(),launches=child.poll.call_count)
            elif name == 'repository.unsupported-origin':
                stack.enter_context(patch.object(w,'command',side_effect=[str(root),'https://example.invalid/owner/repo.git']))
                w.repository(root)
            elif name == 'budget.propagates-quota':
                def quota(): raise w.GitHubRateLimit('fixture transport quota')
                w.GitHubBudget(state).call(quota)
            elif name == 'github.failed-owner-probe':
                responses=[subprocess.CompletedProcess([],1,'','unknown owner type'),subprocess.TimeoutExpired('fixture-probe',1)]
                stack.enter_context(patch.object(w.subprocess,'run',side_effect=responses))
                w.raw_command(['gh','project','view','2'])
            elif name == 'github.successful-quota-error':
                response=json.dumps({'errors':[{'type':'RATE_LIMITED','message':'API rate limit exceeded'}]})
                stack.enter_context(patch.object(w.subprocess,'run',return_value=subprocess.CompletedProcess([],0,response,'')))
                w.raw_command(['gh','api','graphql'])
            elif name.startswith('board.'):
                board=object.__new__(w.Board)
                board.repo='owner/repo'; board.owner='owner'; board.number=2
                board.dangerously_allow_public_board=False
                security={'node':{'public':False,'viewerCanUpdate':True},'repository':{'viewerPermission':'ADMIN'}}
                stack.enter_context(patch.object(w,'graphql',return_value=security))
                board.project={'id':'project'}; board.field={'id':'status'}
                board.required_statuses=(w.READY,); board.options={w.READY.casefold():'ready',w.PROGRESS.casefold():'progress'}
                if name == 'board.add-missing-destination':
                    board.options={}; board.add_issue('Title','Description',w.READY)
                elif name == 'board.missing-status':
                    stack.enter_context(patch.object(w,'gh',return_value={'fields':[{'id':'status','name':'Status','options':[]}]}))
                    board.refresh_fields()
                elif name == 'board.missing-field':
                    stack.enter_context(patch.object(w,'gh',return_value={'fields':[]})); board.refresh_fields()
                elif name == 'board.failed-review-status':
                    stack.enter_context(patch.object(w,'graphql',return_value={**security,'node':{**security['node'],'options':[{'id':'human','name':w.HUMAN}]}}))
                    board.refresh_fields=Mock(); board.ensure_review_status()
                elif name == 'board.status-drift':
                    board.item_status=Mock(return_value=w.HUMAN.casefold())
                    board.move(item(1),w.PROGRESS,w.READY)
                elif name == 'board.unverified-transition':
                    board.item_status=Mock(return_value=w.READY.casefold())
                    stack.enter_context(patch.object(w,'command',return_value=''))
                    board.move(item(1),w.PROGRESS,w.READY)
                elif name == 'board.repeated-cursor':
                    w.Board.next_cursor({'hasNextPage':True,'endCursor':'repeat'},{'repeat'})
                elif name == 'board.incomplete-items':
                    stack.enter_context(patch.object(w,'graphql',return_value={'node':{'items':{'nodes':[],'totalCount':1,'pageInfo':{'hasNextPage':False}}}}))
                    board.items()
                else: raise ValueError('Unknown guard scenario: '+name)
            elif name == 'metadata.oversized-response':
                import worklane_harness as harness
                read_fd,write_fd=os.pipe()
                os.write(write_fd,b'x')
                stack.callback(os.close,write_fd)
                stream=stack.enter_context(os.fdopen(read_fd,'rb'))
                server=harness.CodexAppServer(root)
                server.process=Mock(stdin=io.BytesIO(),stdout=stream)
                server.request_id=0; server.pending=b''; server.deadline=time.monotonic()+2
                stack.enter_context(patch.object(harness.os,'read',return_value=b' '*(4*1024*1024+1)+b'\n'))
                server.request('project/list',{})
            elif name == 'metadata.repeated-project-cursor':
                import worklane_harness as harness
                task={'number':1,'phase':'building'}
                run=state.begin_run(task,'build',root)
                server=Mock(); pages=iter(['repeat','repeat',None])
                def request(method,params):
                    if method == 'project/list': return {'data':[],'nextCursor':next(pages)}
                    if method == 'project/create': return {'project':{'id':'project'}}
                    return {}
                server.request.side_effect=request
                context=Mock(); context.__enter__=Mock(return_value=server); context.__exit__=Mock(return_value=False)
                stack.enter_context(patch.object(harness,'CodexAppServer',return_value=context))
                harness.CodexHarness(None,root,state).name_session(run,'00000000-0000-0000-0000-000000000042','build',task)
                effects['logs']=[{'stream':entry['stream'],'content':entry['content']} for entry in state.log_entries()]
            elif name == 'memory.unavailable':
                stack.enter_context(patch.object(w.os,'memfd_create',None,create=True))
                stack.enter_context(patch.object(w.ctypes,'CDLL',return_value=object()))
                w.memory_fd('fixture')
            elif name == 'memory.syscall-error':
                stack.enter_context(patch.object(w.os,'memfd_create',None,create=True))
                stack.enter_context(patch.object(w.ctypes,'CDLL',return_value=Mock(memfd_create=Mock(return_value=-1))))
                stack.enter_context(patch.object(w.ctypes,'get_errno',return_value=errno.ENOSYS))
                w.memory_fd('fixture')
            elif name == 'state.newer-schema':
                with state.connect() as db: db.execute('PRAGMA user_version=999')
                w.State('owner/repo')
            elif name == 'state.unsupported-value':
                state.set_meta('invalid',{'unsupported':set()})
            elif name == 'state.foreign-key-migration':
                # Supply the validation result at the SQLite boundary, not an exception from the guard itself.
                other = state.path.parent / 'new'
                original = w.sqlite3.connect
                class Connection:
                    def __init__(self,*args,**kwargs): self.db=original(*args,**kwargs)
                    def __getattr__(self,key): return getattr(self.db,key)
                    def __setattr__(self,key,value):
                        if key == 'db': object.__setattr__(self,key,value)
                        else: setattr(self.db,key,value)
                    def __enter__(self): self.db.__enter__(); return self
                    def __exit__(self,*args): return self.db.__exit__(*args)
                    def execute(self,query,*args):
                        if query == 'PRAGMA foreign_key_check': return Mock(fetchone=lambda:('broken',))
                        return self.db.execute(query,*args)
                stack.enter_context(patch.object(w.sqlite3,'connect',side_effect=Connection))
                w.State('owner/new')
            elif name == 'state.changed-legacy-file':
                path=state.path/'controller.log'; path.write_bytes(b'changed')
                with state.connect() as db: db.execute('INSERT INTO migration_archive VALUES (?,?)',('controller.log',b'original'))
                w.State('owner/repo')
            elif name == 'editor.existing-save-error':
                dashboard=tui.Dashboard(Mock(),state,'owner/repo',1,root=root)
                dashboard.editor={'title':'Title','body':'Body','item':{'id':'existing'},'saving':False}
                dashboard.github=Mock(side_effect=RuntimeError('fixture edit denied'))
                dashboard.submit=lambda loader,done: loader()
                dashboard.save_issue()
            elif name == 'editor.empty-title':
                tui.save_editor('owner/repo',{'board':Mock(),'item':None,'title':' ','body':'text','status':w.READY})
            elif name == 'resume.missing-command':
                task={'number':1,'phase':'interrupted'}
                run=state.begin_run(task,'build',root)
                state.attach_session(run,'00000000-0000-0000-0000-000000000042',w.MODEL)
                stack.enter_context(patch.object(tui.shutil,'which',return_value=None))
                tui.resume_session(state,1,'worker')
            else:
                raise ValueError('Unknown guard scenario: ' + name)
        return {'ok':True,'effects':effects}, 0
    except Exception as error:
        if name.startswith('agent.'):
            effects.update(launches=len(launches),runs=[{key:run.get(key) for key in ('role','stage','status','session_id','error')} for run in state.runs()])
        if isinstance(locals().get('board'),FakeBoard):
            effects.update(board_moves=board.moves,posts=getattr(runner.post,'call_count',0))
        return {'error':str(error),'type':type(error).__name__,'effects':effects}, 1
