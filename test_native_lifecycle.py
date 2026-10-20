"""Real Git worktrees and saved turns through both complete workflow implementations."""
import argparse
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import worklane as w
from test_worklane import connection, result, review_thread
from test_native_workflow import Runtime, CLOCK, URL
from tools.native_test_bridge import ROOT, invoke_rpc, acceptance_report

RAW_COMMAND = w.raw_command
EVIDENCE = []


class Lifecycle(Runtime):
    def __init__(self, root, native):
        super().__init__(root, native)
        self.posts, self.errors, self.promotions = [], [], 0
        self.threads, self.thread_reads = [], 0
        self.pr_reads = 0
        self.on_pr, self.on_threads, self.on_post = None, None, None
        self.on_security,self.security_reads=None,0
        self.sync_remote,self.fail_retarget=False,False
        self.promotion = 'success'
        self.cached = {stage:result() for stage in ('build','review','finalize')}
        self.git('init','-q','-b','main')
        self.git('config','user.name','Fixture')
        self.git('config','user.email','fixture@example.invalid')
        (root/'tracked').write_text('initial\n')
        self.git('add','tracked')
        self.git('commit','-qm','initial')
        self.head = self.git('rev-parse','HEAD')
        self.pr['headRefOid']=self.head
        self.git('clone','-q','--bare',str(root),str(root/'remote.git'))
        self.git('remote','add','origin',str(root/'remote.git'))
        (root/'saved-worktree').rmdir()
        self.git('worktree','add','-q','-b','codex/1-worklane',str(root/'saved-worktree'))
        self.git('push','-q','origin','codex/1-worklane')
        self.git('worktree','add','-q','--detach',str(root/'review-tree'),self.head)
        for value in self.cached.values():value['head_sha']=self.head
        self.task=self.state.load(1)
        self.task.update(head_sha=self.head,question=False,round=1,phase='building',
                         review_worktree=str(root/'review-tree'),completed_turns=self.cached)
        self.status['item-1']=w.PROGRESS
        self.items[0]['status']=w.PROGRESS
        self.task['item']=copy.deepcopy(self.items[0])
        self.save(self.task)
        self.initial_sessions=copy.deepcopy(self.state.load(1)['sessions'])

    def git(self, *args, cwd=None):
        env={**os.environ,'GIT_AUTHOR_DATE':CLOCK,'GIT_COMMITTER_DATE':CLOCK,
             'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'}
        return subprocess.check_output(['git',*args],cwd=cwd or self.root,env=env,
                                       text=True,stderr=subprocess.PIPE).strip()

    def transport(self, argv, *, stdin=None, **kwargs):
        if argv[0]!='gh':return RAW_COMMAND(argv,stdin=stdin,**kwargs)
        args=list(argv[1:])
        if args[:2]==['pr','view'] and args[-1]!='comments':
            if self.sync_remote:self.pr['headRefOid']=self.git('rev-parse','origin/codex/1-worklane')
            self.pr_reads+=1
            if self.on_pr:self.on_pr(self)
        if args[:2] in (['pr','view'],['issue','view']) and args[-1]=='comments':
            return json.dumps({'comments':[{'body':body} for body in self.posts]})
        if args[:2] in (['pr','comment'],['issue','comment']):
            self.posts.append(stdin)
            if self.on_post:self.on_post(self)
            return ''
        if args[:2]==['pr','ready']:
            self.promotions+=1
            if self.promotion=='failed':raise RuntimeError('fixture promotion failed')
            if self.promotion=='success':self.pr['isDraft']=False
            return ''
        if args[:2]==['pr','edit']:
            self.pr['baseRefName']=args[-1]
            if self.fail_retarget:
                self.fail_retarget=False
                raise RuntimeError('retarget response lost')
            return ''
        if 'graphql' in args:
            body=json.loads(stdin)
            if 'viewerCanUpdate' in body['query']:
                self.security_reads+=1
                if self.on_security:self.on_security(self)
            if 'reviewThreads' in body['query']:
                self.thread_reads+=1
                if self.on_threads:self.on_threads(self)
                return json.dumps({'data':{'repository':{'pullRequest':{'reviewThreads':connection([
                    {**thread,'comments':connection(thread['comments'])} for thread in self.threads])}}}})
        return super().transport(argv,stdin=stdin,**kwargs)

    def stage(self, method, *, review=False):
        # Cached turns must suffice; a new provider invocation is always a failure.
        backend=Mock(model=w.MODEL)
        backend.run.side_effect=AssertionError('Saved turn was launched twice')
        with patch.object(w,'now',return_value=self.clock),redirect_stdout(io.StringIO()):
            if self.native:
                def command(event):
                    return {'stdout':self.transport(event['argv'],stdin=event['stdin']),
                            'stderr':'','exit':0}
                output=invoke_rpc({'op':'workflow.call','method':method,'repo':'owner/repo',
                    'root':str(self.root),'state_root':str(self.root),'clock':self.clock,
                    'board':{'options':self.board.options},'task':self.task,
                    'head':self.task['head_sha'],'review':review,
                    'item':self.task['item'],'remaining':[]},command)
                self.task=output['task']
                error=output.get('error')
            else:
                with patch.object(w,'raw_command',side_effect=self.transport):
                    runner=w.Runner(argparse.Namespace(base='main'),'owner/repo',self.root,
                                    self.state,self.board,backend)
                    try:
                        if method=='execute':runner.execute(self.task,review)
                        elif method=='worktree':runner.worktree(self.task,review)
                        elif method=='verify_pr':runner.verify_pr(self.task,self.task['head_sha'])
                        elif method=='restack':runner.restack(self.task,[],copy.deepcopy(self.task['item']))
                        else:getattr(runner,method)(self.task)
                        error=None
                    except (RuntimeError,w.InterruptedRun) as exc:error=str(exc)
            if method=='execute':error=self.task.get('error')
            self.errors.append(error)
            return error

    def reviewing(self):
        self.task['phase']='reviewing'
        self.items[0]['status']=w.REVIEW
        self.task['item']=copy.deepcopy(self.items[0])
        self.status['item-1']=w.REVIEW
        self.save(self.task)

    def observe(self):
        value=super().observe()
        value.update(posts=self.posts,errors=self.errors,promotions=self.promotions)
        return json.loads(json.dumps(value).replace(str(self.root),'<root>'))

    def stack(self):
        self.git('checkout','-qb','parent')
        (self.root/'parent').write_text('parent\n')
        self.git('add','parent');self.git('commit','-qm','parent')
        parent=self.git('rev-parse','HEAD')
        self.git('push','-q','origin','parent')
        worker=self.root/'saved-worktree'
        self.git('merge','--ff-only','parent',cwd=worker)
        (worker/'child').write_text('child\n')
        self.git('add','child',cwd=worker);self.git('commit','-qm','child',cwd=worker)
        self.git('push','-q','origin','codex/1-worklane',cwd=worker)
        self.head=self.git('rev-parse','HEAD',cwd=worker)
        self.git('checkout','-q','main')
        self.git('merge','--squash','parent');self.git('commit','-qm','squashed parent')
        self.git('push','-q','origin','main')
        self.task.update(phase='human_review',base='parent',base_sha=parent,head_sha=self.head,
            default_base='main',stack=[{'number':2,'pr_url':'https://github.com/owner/repo/pull/10',
            'branch':'parent','base':'main','head_sha':parent,'reviewed_head':parent}],
            review_result=result(sha=self.head),handoff_result=result(sha=self.head))
        self.status['item-1']=self.items[0]['status']=w.HUMAN
        self.task['item']=copy.deepcopy(self.items[0])
        self.pr.update(headRefOid=self.head,baseRefName='parent')
        self.sync_remote=True
        self.save(self.task)


@unittest.skipUnless(os.environ.get('WORKLANE_TEST_RUST')=='1','opt-in native workflow lifecycle parity')
class NativeLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        EVIDENCE.clear()
        subprocess.run(['cargo','build','--locked','--offline'],cwd=ROOT,check=True,capture_output=True)
        pin=json.loads((ROOT/'parity/inventory.json').read_text())['revision']
        for name in ('worklane.py','worklane_harness.py'):
            if subprocess.check_output(['git','hash-object',name],cwd=ROOT)!=subprocess.check_output(['git','rev-parse',pin+':'+name],cwd=ROOT):
                raise AssertionError('Frozen oracle drift: '+name)

    @classmethod
    def tearDownClass(cls):
        if path:=os.environ.get('WORKLANE_NATIVE_LIFECYCLE_EVIDENCE'):
            Path(path).write_text(json.dumps(acceptance_report(EVIDENCE,'test_native_lifecycle.py'),indent=2)+'\n')

    def pair(self, scenario):
        observed=[]
        for native in (False,True):
            with tempfile.TemporaryDirectory() as directory:
                r=Lifecycle(Path(directory),native)
                with patch.dict(os.environ,{'GIT_AUTHOR_DATE':CLOCK,'GIT_COMMITTER_DATE':CLOCK}):scenario(r)
                # No acceptance check may replace saved role ownership/history.
                self.assertEqual(r.state.load(1)['sessions'],r.initial_sessions)
                self.assertEqual(len(r.state.runs(1)),1)
                observed.append(r.observe())
        self.assertEqual(observed[1],observed[0])
        EVIDENCE.append({'id':self.id()+(' '+str(self._subtest) if self._subtest else ''),
                         'status':'pass','comparison':'equal','observations':observed[0]})

    def test_complete_build_review_and_handoff_promotes_only_drafts(self):
        for draft in (False,True):
            def scenario(r):
                r.pr['isDraft']=draft
                self.assertIsNone(r.stage('build'))
                self.assertEqual(r.task['phase'],'awaiting_review')
                r.reviewing()
                self.assertIsNone(r.stage('review'))
                self.assertEqual(r.task['phase'],'human_review')
                self.assertEqual(r.status['item-1'],w.HUMAN)
                self.assertEqual(len(r.posts),3)
                self.assertEqual(r.promotions,int(draft))
            with self.subTest(draft=draft):self.pair(scenario)

    def test_worker_rejects_invalid_or_unverified_results(self):
        for case in ('blocked','findings','invalid-sha','wrong-head','dirty','closed','retargeted','cross-repo','unresolved','outdated','question-pr'):
            def scenario(r):
                value=r.task['completed_turns']['build']
                if case=='blocked':value['verdict']='blocked'
                elif case=='findings':value['findings']=['Unresolved']
                elif case=='invalid-sha':value['head_sha']='short'
                elif case=='wrong-head':
                    value['head_sha']='b'*40;r.pr['headRefOid']='b'*40
                elif case=='dirty':(r.root/'saved-worktree'/'untracked').write_text('must remain')
                elif case=='closed':r.pr['state']='CLOSED'
                elif case=='retargeted':r.pr['baseRefName']='other'
                elif case=='cross-repo':r.pr['isCrossRepository']=True
                elif case=='question-pr':r.task['question']=True
                else:r.threads=[review_thread(outdated=case=='outdated')]
                self.assertIsNotNone(r.stage('build'))
                self.assertEqual(r.status['item-1'],w.PROGRESS)
                self.assertEqual(r.posts,[])
            with self.subTest(case=case):self.pair(scenario)

    def test_questions_complete_without_pr_threads_finalizer_or_promotion(self):
        def scenario(r):
            r.task.update(question=True,pr_url='',head_sha='')
            for value in r.task['completed_turns'].values():value.update(pr_url='',head_sha='')
            r.task['completed_turns'].pop('finalize')
            self.assertIsNone(r.stage('build'))
            r.reviewing()
            self.assertIsNone(r.stage('review'))
            self.assertEqual(r.task['phase'],'human_review')
            self.assertEqual(r.thread_reads,0)
            self.assertEqual(r.promotions,0)
            self.assertEqual(r.pr_reads,0)
            self.assertEqual(len(r.posts),2)
        self.pair(scenario)

    def test_resolved_threads_remain_in_saved_review_context_and_do_not_block(self):
        for stage in ('build','review'):
            def scenario(r):
                if stage=='review':r.reviewing()
                r.threads=[review_thread(resolved=True,outdated=True)]
                self.assertIsNone(r.stage(stage))
                self.assertEqual(r.task['review_threads'],r.threads)
            with self.subTest(stage=stage):self.pair(scenario)

    def test_immediate_stop_prevents_workflow_and_keeps_exact_session(self):
        def scenario(r):
            r.state.set_meta('shutdown',{'immediate':True})
            self.assertIsNotNone(r.stage('execute'))
            self.assertEqual(r.task['phase'],'interrupted')
            self.assertEqual(r.task['resume_stage'],'build')
            self.assertEqual(r.pr_reads,0)
            self.assertEqual(r.thread_reads,0)
            self.assertEqual(r.posts,[])
        self.pair(scenario)

    def test_review_and_finalizer_failures_cannot_handoff(self):
        for case in ('blocked','changes','findings','wrong-result-head','dirty-reviewer',
                     'handoff-blocked','handoff-findings','handoff-head','stale-pr','open-thread','outdated-thread',
                     'late-thread','promotion-failed','promotion-still-draft','changed-before-promotion'):
            def scenario(r):
                r.reviewing()
                value=r.task['completed_turns']['review']
                if case=='blocked':value['verdict']='blocked'
                elif case=='changes':value['verdict']='changes_requested'
                elif case=='findings':value['findings']=['Unresolved']
                elif case=='wrong-result-head':value['head_sha']='b'*40
                elif case=='dirty-reviewer':(r.root/'review-tree'/'tracked').write_text('edited')
                elif case.startswith('handoff-'):
                    value=r.task['completed_turns']['finalize']
                    if case=='handoff-blocked':value['verdict']='blocked'
                    elif case=='handoff-findings':value['findings']=['Unresolved']
                    else:value['head_sha']='b'*40
                elif case=='stale-pr':r.pr['headRefOid']='b'*40
                elif case in ('open-thread','outdated-thread'):r.threads=[review_thread(outdated=case=='outdated-thread')]
                elif case=='late-thread':
                    def late(current):
                        if current.thread_reads==3:current.threads=[review_thread()]
                    r.on_threads=late
                elif case.startswith('promotion-'):
                    r.pr['isDraft']=True;r.promotion='failed' if case=='promotion-failed' else 'no-op'
                else:
                    r.pr['isDraft']=True
                    def changed(current):
                        if current.pr_reads==4:current.pr['headRefOid']='b'*40
                    r.on_pr=changed
                error=r.stage('review')
                self.assertEqual(error is None,case=='changes')
                self.assertNotEqual(r.status['item-1'],w.HUMAN)
                self.assertEqual(r.task['phase']=='rework',case=='changes')
                if case=='changed-before-promotion':self.assertEqual(r.promotions,0)
            with self.subTest(case=case):self.pair(scenario)

    def test_saved_handoff_retries_without_duplicate_turns_or_comments(self):
        def scenario(r):
            r.reviewing();r.pr['mergeable']='UNKNOWN'
            self.assertIsNotNone(r.stage('review'))
            self.assertEqual(r.task['phase'],'finalizing')
            self.assertEqual(len(r.posts),2)
            r.task.update(resume_stage='finalize')
            r.task['completed_turns'].pop('review')
            r.pr['mergeable']='MERGEABLE'
            self.assertIsNone(r.stage('review'))
            self.assertEqual(r.task['phase'],'human_review')
            self.assertEqual(len(r.posts),2)
        self.pair(scenario)

    def test_quota_retry_reuses_saved_turn_and_records_each_failure_once(self):
        for stage in ('build','review'):
            def scenario(r):
                if stage=='review':r.reviewing()
                def exhausted(current):
                    current.on_pr=None
                    raise w.GitHubRateLimit('fixture verification quota',retry_at=0.)
                r.on_pr=exhausted
                with patch.object(w.time,'time',return_value=1792454400.):
                    self.assertIsNotNone(r.stage('execute',review=stage=='review'))
                saved=r.state.load(1)
                self.assertEqual(saved['phase'],'interrupted')
                self.assertEqual(saved['resume_stage'],stage)
                budget=w.State('_github',r.root).get_meta('github_backoff')
                self.assertEqual(budget['delay'],60)
                r.advance(61)
                r.task=r.call('recover',slot='reviewer' if stage=='review' else 'worker-1')
                self.assertIsNotNone(r.task)
                self.assertIsNone(r.stage('execute',review=stage=='review'))
                self.assertEqual(r.task['phase'],'awaiting_review' if stage=='build' else 'human_review')
            with self.subTest(stage=stage):self.pair(scenario)

    def test_conflicts_requeue_and_respect_round_limit_and_status_override(self):
        for late in (False,True):
            for change in ('none','limit','human'):
                def scenario(r):
                    r.reviewing()
                    if change=='limit':r.task['round']=3
                    elif change=='human':r.status['item-1']=w.READY
                    if late:
                        def conflict(current):
                            if current.pr_reads==4:current.pr['mergeable']='CONFLICTING'
                        r.on_pr=conflict
                    else:r.pr['mergeable']='CONFLICTING'
                    self.assertEqual(r.stage('review') is None,change=='none')
                    self.assertNotEqual(r.status['item-1'],w.HUMAN)
                    self.assertEqual(r.task['review_result']['verdict'],'changes_requested')
                with self.subTest(late=late,change=change):self.pair(scenario)

    def test_saved_turn_rechecks_privacy_and_handoff_write_permissions(self):
        for stage in ('build','review'):
            for timing in ('before','after-post'):
                def scenario(r):
                    if stage=='review':r.reviewing()
                    if timing=='before':r.public=True
                    else:r.on_post=lambda current:setattr(current,'public',True)
                    self.assertIsNotNone(r.stage(stage))
                    self.assertNotEqual(r.status['item-1'],w.HUMAN)
                    self.assertEqual(r.promotions,0)
                    self.assertEqual(len(r.posts),int(timing=='after-post'))
                with self.subTest(stage=stage,timing=timing):self.pair(scenario)

    def test_worktree_guards_and_reviewer_revision_reuse(self):
        for case in ('missing','wrong-branch','dirty-revision','clean-revision'):
            def scenario(r):
                if case=='missing':r.task['worker_worktree']=str(r.root/'missing')
                elif case=='wrong-branch':r.task['branch']='other'
                else:
                    r.git('commit','-qm','new revision','--allow-empty')
                    r.git('push','-q','origin','main')
                    r.task['head_sha']=r.git('rev-parse','HEAD')
                    if case=='dirty-revision':(r.root/'review-tree'/'tracked').write_text('preserve')
                error=r.stage('worktree',review=case.endswith('revision'))
                self.assertEqual(error is None,case=='clean-revision')
                if case=='clean-revision':self.assertEqual(r.git('rev-parse','HEAD',cwd=r.root/'review-tree'),r.task['head_sha'])
            with self.subTest(case=case):self.pair(scenario)

    def test_new_worktree_respects_existing_branches_and_pinned_parent(self):
        for case in ('local-collision','moved-parent','remote-existing','new-branch'):
            def scenario(r):
                r.task={'number':2,'branch':'codex/2-worklane','base':'main','head_sha':r.head,'question':False,
                        'item':{'id':'item-2'},'phase':'building'}
                if case=='local-collision':r.git('branch','codex/2-worklane')
                elif case=='moved-parent':r.task.update(base_sha='b'*40,stack=[{'head_sha':'b'*40}])
                elif case=='remote-existing':
                    r.git('push','-q','origin','main:refs/heads/codex/2-worklane')
                    r.git('fetch','-q','origin')
                error=r.stage('worktree')
                self.assertEqual(error is None,case in ('remote-existing','new-branch'))
                if error is None:
                    worker=Path(r.task['worker_worktree'])
                    self.assertEqual(r.git('rev-parse','HEAD',cwd=worker),r.head)
                    self.assertEqual(r.git('branch','--show-current',cwd=worker),'codex/2-worklane')
            with self.subTest(case=case):self.pair(scenario)

    def test_restack_merge_forward_recovery_preserves_history_and_fresh_review(self):
        for interruption in ('none','retarget','board','merge-record'):
            def scenario(r):
                r.stack()
                original=r.head;worker=r.root/'saved-worktree'
                if interruption=='retarget':r.fail_retarget=True
                elif interruption=='board':r.fail_after_move=True
                elif interruption=='merge-record':
                    target=r.git('rev-parse','origin/main')
                    r.task['restack_pending']={'old_base':'parent','old_head':original,'target':'main',
                        'target_sha':target,'stack':[],'started_at':r.clock}
                    r.save(r.task)
                    r.git('merge','--no-ff','--no-edit',target,cwd=worker)
                first=r.stage('restack')
                self.assertEqual(first is None,interruption in ('none','merge-record'))
                merged=r.git('rev-parse','HEAD',cwd=worker)
                if first:
                    self.assertIn('restack_pending',r.state.load(1))
                    r.task=r.state.load(1)
                    self.assertIsNone(r.stage('restack'))
                    self.assertEqual(r.git('rev-parse','HEAD',cwd=worker),merged)
                self.assertEqual(r.git('diff','--name-only','origin/main...HEAD',cwd=worker),'child')
                self.assertEqual(r.git('merge-base','--is-ancestor',original,'HEAD',cwd=worker),'')
                self.assertEqual(r.task['base'],'main')
                self.assertEqual(r.task['phase'],'awaiting_review')
                self.assertEqual(r.task['round'],2)
                self.assertEqual(len(r.task['stack_history']),1)
                self.assertEqual(r.task['stack_history'][0]['head_sha'],original)
                self.assertEqual(r.status['item-1'],w.REVIEW)
                for key in ('review_result','handoff_result','completed_turns','restack_pending'):
                    self.assertNotIn(key,r.task)
            with self.subTest(interruption=interruption):self.pair(scenario)

    def test_restack_live_privacy_changes_preserve_pending_intent_and_remote_state(self):
        for change in (1,2,3):
            def scenario(r):
                r.stack();r.security_reads=0
                def revoke(current):
                    if current.security_reads==change:current.public=True
                r.on_security=revoke
                self.assertIsNotNone(r.stage('restack'))
                self.assertEqual(r.pr['baseRefName'],'parent')
                self.assertEqual(r.status['item-1'],w.HUMAN)
                remote=r.git('rev-parse','origin/codex/1-worklane')
                self.assertEqual(remote==r.head,change<3)
                self.assertEqual('restack_pending' in r.state.load(1),change>1)
                # Restoring permission resumes the recorded merge in the same worktree.
                r.public=False;r.on_security=None
                self.assertIsNone(r.stage('restack'))
                self.assertEqual(r.task['phase'],'awaiting_review')
                self.assertEqual(len(r.task['stack_history']),1)
            with self.subTest(change=change):self.pair(scenario)

    def test_restack_dirty_conflicting_or_unrelated_changes_are_preserved(self):
        for case in ('dirty','conflict','changed-local','wrong-branch'):
            def scenario(r):
                r.stack();worker=r.root/'saved-worktree'
                if case=='dirty':(worker/'child').write_text('unfinished\n')
                elif case=='conflict':
                    (r.root/'child').write_text('conflict\n');r.git('add','child')
                    r.git('commit','-qm','conflicting upstream');r.git('push','-q','origin','main')
                elif case=='changed-local':r.git('commit','--allow-empty','-qm','outside change',cwd=worker)
                else:r.task['branch']='other'
                local=r.git('rev-parse','HEAD',cwd=worker)
                contents=(worker/'child').read_text()
                self.assertIsNotNone(r.stage('restack'))
                self.assertEqual(r.git('rev-parse','HEAD',cwd=worker),local)
                self.assertEqual((worker/'child').read_text(),contents)
                self.assertEqual(r.git('rev-parse','origin/codex/1-worklane'),r.head)
                self.assertEqual(r.status['item-1'],w.HUMAN)
                self.assertFalse(Path(r.git('rev-parse','--git-path','MERGE_HEAD',cwd=worker)).exists())
            with self.subTest(case=case):self.pair(scenario)
