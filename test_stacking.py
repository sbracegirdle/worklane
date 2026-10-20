"""Opt-in gates and merge-forward stack recovery using disposable Git repositories."""
import argparse
from contextlib import redirect_stdout
import copy
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import worklane as w
from test_worklane import FakeBoard, item, result


class StackTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = w.State('owner/repo', self.root)
        self.board = FakeBoard([item(1, w.HUMAN), item(2)])
        self.runner = w.Runner(argparse.Namespace(base='main', once=True, interval=60), 'owner/repo',
                               self.root, self.state, self.board, Mock())
        self.runner.stack_items = self.board.queue
        self.board.item_status = Mock(return_value=w.HUMAN.casefold())
        self.runner.issue = Mock(return_value={'state':'OPEN', 'labels':[], 'title':'Child'})
        self.runner.refresh_review_threads = Mock()
        self.runner.human_feedback = Mock(return_value={})
        self.parent = {'number':1, 'phase':'human_review', 'branch':'codex/1-worklane', 'base':'main',
                       'head_sha':'a'*40, 'pr_url':'https://github.com/owner/repo/pull/10',
                       'review_result':result(), 'handoff_result':result(), 'human_feedback_seen':{}}
        self.state.save(self.parent)
        self.parent_pr = {'state':'OPEN', 'isDraft':False, 'mergeable':'MERGEABLE',
                          'headRefOid':'a'*40, 'headRefName':self.parent['branch'], 'baseRefName':'main'}
        self.runner.pr = Mock(return_value=self.parent_pr)
        w.save_config(self.root, {'stack_dependencies':['2:1']})

    def dependency(self, number=1, state='open', repo='owner/repo'):
        return {'number':number, 'state':state, 'repository_url':f'https://api.github.com/repos/{repo}'}

    def transport(self, dependencies):
        count = sum(d['state'] == 'open' for d in dependencies)
        self.enterContext(patch.object(w, 'graphql', return_value={'repository':{'issue':{'issueDependenciesSummary':{'blockedBy':count}}}}))
        self.enterContext(patch.object(w, 'gh', return_value=[dependencies]))

    def test_exact_reviewed_parent_pins_child_base_and_provenance(self):
        self.transport([self.dependency()])
        task = self.runner.pick(item(2), w.WORKERS[0])
        self.assertEqual(task['base'], 'codex/1-worklane')
        self.assertEqual(task['base_sha'], 'a'*40)
        self.assertEqual(task['stack'][0]['reviewed_head'], 'a'*40)
        self.assertEqual(task['stack'][0]['pr_url'], self.parent['pr_url'])
        self.assertEqual(self.board.moves, [(2, w.PROGRESS)])

    def test_unlisted_hard_gate_and_external_dependencies_never_stack(self):
        for dependency in (self.dependency(53), self.dependency(repo='external/repo')):
            with self.subTest(dependency=dependency), patch.object(w, 'graphql', return_value={'repository':{'issue':{'issueDependenciesSummary':{'blockedBy':1}}}}), patch.object(w, 'gh', return_value=[[dependency]]):
                selected = item(2)
                self.assertFalse(self.runner.dependencies_ready(selected))
                self.assertTrue(selected['dependency_reason'])
        self.assertEqual(self.board.moves, [])

    def test_all_desktop_edges_remain_hard_completion_dependencies(self):
        w.save_config(self.root, {'stack_dependencies':['50:49', '51:50', '52:50'], 'completion_gates':[53]})
        self.transport([self.dependency(53)])
        for number in range(40,49):
            selected = item(number)
            self.assertFalse(self.runner.dependencies_ready(selected))
            self.assertIn('Hard completion gate #53', selected['dependency_reason'])
        with self.assertRaisesRegex(ValueError, 'cannot be stackable'):
            w.validate_config({'stack_dependencies':['40:53'], 'completion_gates':[53]})

    def test_closed_completion_gate_still_requires_verified_merged_handoff(self):
        w.save_config(self.root, {'completion_gates':[53]})
        self.transport([self.dependency(53, 'closed')])
        selected = item(40)
        self.assertFalse(self.runner.dependencies_ready(selected))
        self.state.save({'number':53, 'phase':'done', 'completion_source':'merged_pr', 'head_sha':'a'*40,
                         'merged_head_sha':'a'*40, 'review_result':result(), 'handoff_result':result()})
        self.assertTrue(self.runner.dependencies_ready(selected))

    def test_fan_in_blocks_visibly_instead_of_dropping_an_edge(self):
        w.save_config(self.root, {'stack_dependencies':['2:1','2:3']})
        other = {**self.parent, 'number':3}
        self.state.save(other)
        self.runner.stack_items.append(item(3, w.HUMAN))
        self.transport([self.dependency(), self.dependency(3)])
        selected = item(2)
        self.assertFalse(self.runner.dependencies_ready(selected))
        self.assertIn('Multiple open stack parents', selected['dependency_reason'])
        self.assertIn('2', self.state.dependency_blocks())

    def test_changed_heads_feedback_drafts_and_missing_review_block(self):
        self.transport([self.dependency()])
        for update in ({'phase':'rework'}, {'human_feedback_pending':True}, {'review_result':{}}, {'head_sha':'b'*40}):
            with self.subTest(update=update):
                self.state.save({**self.parent, **update})
                self.assertFalse(self.runner.dependencies_ready(item(2)))
        self.state.save(self.parent)
        self.runner.human_feedback.return_value = {'new':'feedback'}
        self.assertFalse(self.runner.dependencies_ready(item(2)))
        self.runner.human_feedback.return_value = {}
        self.parent_pr['isDraft'] = True
        self.assertFalse(self.runner.dependencies_ready(item(2)))

    def test_incomplete_native_list_fails_closed_and_cycles_are_rejected(self):
        with patch.object(w, 'graphql', return_value={'repository':{'issue':{'issueDependenciesSummary':{'blockedBy':2}}}}), patch.object(w, 'gh', return_value=[[self.dependency()]]):
            selected = item(2)
            self.assertFalse(self.runner.dependencies_ready(selected))
            self.assertIn('incomplete', selected['dependency_reason'])
        for edges in (['2:2'], ['2:1','1:2']):
            with self.assertRaisesRegex(ValueError, 'cycle'):
                w.validate_config({'stack_dependencies':edges})

    def test_interrupted_child_keeps_pinned_revision_and_original_assignment(self):
        self.transport([self.dependency()])
        task = self.runner.pick(item(2), w.WORKERS[0])
        task.update(phase='interrupted', resume_stage='build')
        self.state.save(task)
        selected = item(2, w.PROGRESS)
        resumed = self.runner.recover([selected], w.WORKERS[0])
        self.assertEqual(resumed['stack'], task['stack'])
        self.assertEqual(resumed['base_sha'], task['base_sha'])
        self.parent_pr['headRefOid'] = 'b'*40
        resumed['phase'] = 'interrupted'
        self.state.save(resumed)
        self.assertIsNone(self.runner.recover([selected], w.WORKERS[0]))

    def test_completion_gate_rejects_unreviewed_merged_head(self):
        w.save_config(self.root, {'completion_gates':[53]})
        self.transport([self.dependency(53, 'closed')])
        self.state.save({'number':53, 'phase':'done', 'completion_source':'merged_pr',
                         'head_sha':'b'*40, 'merged_head_sha':'b'*40, 'review_result':result(), 'handoff_result':result()})
        self.assertFalse(self.runner.dependencies_ready(item(40)))

    def test_active_handoff_revalidates_draft_conflict_and_board_withdrawal(self):
        edge={'number':1,'branch':self.parent['branch'],'base':'main','head_sha':'a'*40}
        task={'number':2,'stack':[edge]}
        for update in ({'isDraft':True},{'mergeable':'CONFLICTING'}):
            before=dict(self.parent_pr)
            self.parent_pr.update(update)
            with self.assertRaisesRegex(RuntimeError, 'changed, conflicted or closed'):
                self.runner.validate_stack(task)
            self.parent_pr.clear(); self.parent_pr.update(before)
        self.board.item_status.return_value=w.READY.casefold()
        with self.assertRaisesRegex(RuntimeError,'Human review'):
            self.runner.validate_stack(task)



class RestackTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo, self.remote = self.root/'repo', self.root/'remote.git'
        self.git('init', '--bare', '-q', str(self.remote), cwd=self.root)
        self.git('clone', '-q', str(self.remote), str(self.repo), cwd=self.root)
        self.git('config','user.email','test@example.invalid')
        self.git('config','user.name','Test')
        self.git('checkout','-b','main')
        (self.repo/'base').write_text('base\n')
        self.git('add','.'); self.git('commit','-qm','base')
        self.git('push','-q','origin','main')
        self.git('checkout','-b','parent')
        (self.repo/'parent').write_text('parent\n')
        self.git('add','.'); self.git('commit','-qm','parent')
        self.parent_head = self.git('rev-parse','HEAD')
        self.git('push','-q','origin','parent')
        self.git('checkout','-b','child')
        (self.repo/'child').write_text('child\n')
        self.git('add','.'); self.git('commit','-qm','child')
        self.child_head = self.git('rev-parse','HEAD')
        self.git('push','-q','origin','child')
        self.git('checkout','main')
        self.git('merge','--squash','parent'); self.git('commit','-qm','squashed parent')
        self.git('push','-q','origin','main'); self.git('checkout','child')
        self.state = w.State('owner/repo', self.root)
        self.board = FakeBoard([item(2,w.HUMAN)])
        self.board.item_status = Mock(return_value=w.HUMAN.casefold())
        self.runner = w.Runner(argparse.Namespace(base='main', once=True, interval=60), 'owner/repo', self.repo, self.state, self.board, Mock())
        self.edge = {'number':1,'pr_url':'https://github.com/owner/repo/pull/10','branch':'parent','base':'main','head_sha':self.parent_head,'reviewed_head':self.parent_head}
        self.task = {'number':2, 'phase':'human_review','stack':[self.edge], 'branch':'child','base':'parent',
                     'base_sha':self.parent_head,'head_sha':self.child_head,'default_base':'main',
                     'worker_worktree':str(self.repo),'pr_url':'https://github.com/owner/repo/pull/11', 'item':item(2,w.HUMAN),
                     'review_result':result(),'handoff_result':result(),'completed_turns':{'review':result()}}
        self.state.save(self.task)
        self.child_pr = {'state':'OPEN','headRefName':'child','headRefOid':self.child_head,'baseRefName':'parent'}
        self.runner.pr = Mock(side_effect=lambda url: dict(self.child_pr) if url.endswith('/11') else
            {'state':'MERGED','headRefOid':self.parent_head, 'headRefName':'parent','baseRefName':'main'})
        self.actual_command = w.command
        self.calls=[]

    def git(self,*args,cwd=None):
        return subprocess.run(['git',*args],cwd=cwd or self.repo,text=True,capture_output=True,check=True).stdout.strip()

    def transport(self,args,**kwargs):
        self.calls.append(args)
        if args[0]=='gh':
            self.child_pr['baseRefName']=args[-1]
            return ''
        output=self.actual_command(args,**kwargs)
        if args[:2]==['git','push']:
            self.child_pr['headRefOid']=self.git('rev-parse','HEAD')
        return output

    def test_squash_parent_merge_restacks_without_rewriting_and_requires_review(self):
        with patch.object(w,'command',side_effect=self.transport):
            self.runner.reconcile_stacks([self.task['item']])
        task=self.state.load(2)
        self.assertEqual(task['base'],'main')
        self.assertEqual(task['stack'],[])
        self.assertEqual(task['phase'],'awaiting_review')
        self.assertEqual(task['round'],1)
        self.assertEqual(self.git('diff','--name-only','origin/main...HEAD'),'child')
        self.git('merge-base','--is-ancestor',self.child_head,'HEAD')
        self.assertNotIn('review_result',task)
        self.assertEqual(len(task['stack_history']),1)
        self.assertFalse(any('--force' in arg for call in self.calls for arg in call))
        self.assertEqual(self.board.moves,[(2,w.REVIEW)])

    def test_interrupted_retarget_reuses_recorded_merge_and_push(self):
        lose_response=[True]
        def transport(args,**kwargs):
            out=self.transport(args,**kwargs)
            if args[0]=='gh' and lose_response[0]:
                lose_response[0]=False
                raise RuntimeError('retarget response lost')
            return out
        with patch.object(w,'command',side_effect=transport):
            self.runner.reconcile_stacks([self.task['item']])
            head=self.git('rev-parse','HEAD')
            self.assertIn('restack_pending',self.state.load(2))
            self.runner.reconcile_stacks([self.task['item']])
        self.assertEqual(self.git('rev-parse','HEAD'),head)
        self.assertEqual(self.state.load(2)['phase'],'awaiting_review')
        self.assertEqual(len(self.state.load(2)['stack_history']),1)

    def test_dirty_worktree_or_changed_merged_revision_preserves_child(self):
        (self.repo/'child').write_text('unfinished\n')
        self.runner.reconcile_stacks([self.task['item']])
        self.assertIn('has changes',self.state.load(2)['stack_blocked'])
        self.assertEqual((self.repo/'child').read_text(),'unfinished\n')
        self.assertEqual(self.git('rev-parse','HEAD'),self.child_head)
        self.runner.pr.side_effect=None
        self.runner.pr.return_value={'state':'MERGED','headRefOid':'b'*40}
        self.runner.reconcile_stacks([self.task['item']])
        self.assertIn('differs',self.state.load(2)['stack_blocked'])
        self.assertEqual(self.board.moves,[])

    def test_conflicting_restack_aborts_without_losing_original_work(self):
        self.git('checkout','main')
        (self.repo/'child').write_text('upstream conflict\n')
        self.git('add','.'); self.git('commit','-qm','conflict'); self.git('push','-q','origin','main')
        self.git('checkout','child')
        self.runner.reconcile_stacks([self.task['item']])
        self.assertIn('conflicts',self.state.load(2)['stack_blocked'])
        self.assertEqual(self.git('rev-parse','HEAD'),self.child_head)
        self.assertEqual(self.git('status','--porcelain'),'')
        self.assertEqual((self.repo/'child').read_text(),'child\n')

    def test_pre_pr_interruption_keeps_the_worker_stage_after_parent_merge(self):
        self.task.update(phase='interrupted', resume_stage='build', pr_url='', head_sha='')
        self.state.save(self.task)
        with patch.object(w,'command',side_effect=self.transport):
            self.runner.reconcile_stacks([self.task['item']])
        saved=self.state.load(2)
        self.assertEqual(saved['phase'],'interrupted')
        self.assertEqual(saved['resume_stage'],'build')
        self.assertEqual(saved['base'],'main')
        self.assertEqual(self.board.moves,[])

    def test_child_ancestry_rejects_unrelated_remote_branch(self):
        self.git('checkout','main')
        with self.assertRaisesRegex(RuntimeError,'does not contain pinned parent'):
            self.runner.verify_stack_ancestry(self.task,self.repo)
        self.git('checkout','child')
        self.runner.verify_stack_ancestry(self.task,self.repo)

    def test_lost_board_transition_response_recovers_without_duplicate_history(self):
        original_move=self.board.move
        lose=[True]
        def move(*args):
            original_move(*args)
            self.board.item_status.return_value=w.REVIEW.casefold()
            if lose[0]:
                lose[0]=False
                raise RuntimeError('board response lost')
        self.board.move=move
        with patch.object(w,'command',side_effect=self.transport):
            self.runner.reconcile_stacks([self.task['item']])
            self.assertIn('restack_pending',self.state.load(2))
            self.runner.reconcile_stacks([self.task['item']])
        saved=self.state.load(2)
        self.assertEqual(saved['phase'],'awaiting_review')
        self.assertEqual(len(saved['stack_history']),1)
        self.assertEqual(saved['round'],1)



if __name__ == '__main__': unittest.main()
