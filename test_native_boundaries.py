"""Unchanged frozen assertions against native GitHub, board and configuration boundaries."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import worklane as w
import test_worklane
import test_config
import test_dependencies
import test_sessions
import test_stop
import test_human_feedback
import test_stacking
from tools.native_test_bridge import (
    ROOT, NativeBoard, native_command, native_graphql, native_rate_limit,
    native_review_threads, native_load_config, native_save_config,
    acceptance_report,
    native_dependency_count, native_pick, native_recover,
    native_owned, native_terminate_owned,
    invoke, NativeBudget, native_feedback,
    native_dependencies_ready, native_validate_stack,
    native_validate_config,
)

EVIDENCE = []


def boundary_case(source, name, replacements):
    def run(self):
        for runtime in ('reference','candidate'):
            with __import__('contextlib').ExitStack() as stack:
                if runtime=='candidate':
                    for owner, attr, value in replacements:
                        stack.enter_context(patch.object(owner,attr,value))
                result=unittest.TestResult()
                source(name).run(result)
                self.assertEqual(result.testsRun,1)
                if result.errors or result.failures or result.skipped:
                    self.fail(runtime+':\n'+'\n'.join(text for _,text in result.errors+result.failures+result.skipped))
        EVIDENCE.append({'id':source.__module__+'.'+source.__name__+'.'+name,'status':'pass',
                         'comparison':'equal','observations':{'frozen_assertions':'pass'}})
    return run


@unittest.skipUnless(os.environ.get('WORKLANE_TEST_RUST')=='1','opt-in native boundary parity')
class NativeBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        EVIDENCE.clear()
        subprocess.run(['cargo','build','--locked','--offline'],cwd=ROOT,check=True,capture_output=True)
        pin=json.loads((ROOT/'parity/inventory.json').read_text())['revision']
        for filename in ('test_worklane.py','test_config.py','test_dependencies.py','test_sessions.py','test_stop.py','test_human_feedback.py','test_stacking.py','worklane.py'):
            actual=subprocess.check_output(['git','hash-object',filename],cwd=ROOT)
            expected=subprocess.check_output(['git','rev-parse',pin+':'+filename],cwd=ROOT)
            if actual!=expected:raise AssertionError('Frozen oracle drift: '+filename)

    @classmethod
    def tearDownClass(cls):
        if destination:=os.environ.get('WORKLANE_NATIVE_BOUNDARY_EVIDENCE'):
            Path(destination).write_text(json.dumps(acceptance_report(EVIDENCE,'test_native_boundaries.py'),indent=2)+'\n')

    def test_successful_probe_user_data_cannot_become_a_quota_diagnostic(self):
        for command in (w.command,native_command):
            responses=[subprocess.CompletedProcess([],1,'','unknown owner type'),
                       subprocess.CompletedProcess([],0,json.dumps({'data':{'viewer':{'login':'private rate limit detail'}}}), '')]
            with patch.object(w.subprocess,'run',side_effect=responses),self.assertRaisesRegex(RuntimeError,'unknown owner type') as error:
                command(['gh','project','view','2'])
            self.assertNotIsInstance(error.exception,w.GitHubRateLimit)

    def test_unicode_decimal_configuration_preserves_values_and_enforces_numeric_gates(self):
        import unicodedata
        for zero in (code for code in range(0x110000) if unicodedata.category(chr(code))=='Nd'
                     and unicodedata.decimal(chr(code))==0):
            with self.subTest(zero=hex(zero)):
                parent='5'+chr(zero+3)
                values={'stack_dependencies':['50:4'+chr(zero+9)],'project':'owner/2'+chr(zero+9)}
                self.assertEqual(invoke({'op':'config.validate','values':values}),w.validate_config(values))
                self.assertEqual(invoke({'op':'config.number','value':'4'+chr(zero+9)}),49)
                for invalid,message in (({'stack_dependencies':['50:'+parent],'completion_gates':[53]},'Completion gate #53'),
                                        ({'stack_dependencies':['50:'+parent,'53:50']},'contains a cycle')):
                    for validate in (w.validate_config,lambda v:invoke({'op':'config.validate','values':v})):
                        with self.assertRaisesRegex((ValueError,RuntimeError),message):validate(invalid)
                detail='Retry-After: 1'+chr(zero+2)+'\nsecondary rate limit exceeded'
                with patch.object(w.time,'time',return_value=1000):
                    self.assertEqual(native_rate_limit(detail).retry_at,w.rate_limit_error(detail).retry_at)


for name in unittest.defaultTestLoader.getTestCaseNames(test_worklane.GitHubErrorTests):
    setattr(NativeBoundaryTests,'test_transport_'+name,boundary_case(test_worklane.GitHubErrorTests,name,[
        (w,'command',native_command),(w,'graphql',native_graphql),(w,'rate_limit_error',native_rate_limit)]))
for name in ('test_rate_limit_backoff_survives_new_runner_and_is_bounded',
             'test_budget_shared_by_repositories_and_blocks_commands',
             'test_budget_serializes_calls_across_repository_instances',
             'test_success_exhausting_last_point_defers_next_request'):
    setattr(NativeBoundaryTests,'test_budget_'+name,boundary_case(test_worklane.WorklaneTests,name,[(w,'GitHubBudget',NativeBudget)]))
for name in (
    'test_board_excludes_other_repos_and_non_issues','test_incomplete_board_fails_closed',
    'test_board_paginates_with_project_id_and_retains_all_issues',
    'test_board_paginates_linked_pr_field_without_closing_reference','test_board_rejects_stuck_cursor',
    'test_board_and_link_cursor_cycles_fail_closed','test_board_status_ids_are_preserved_when_review_is_added',
):
    setattr(NativeBoundaryTests,'test_board_'+name,boundary_case(test_worklane.WorklaneTests,name,[(w,'Board',NativeBoard)]))
for name in (
    'test_review_thread_and_reply_cursor_cycles_fail_closed','test_review_threads_include_all_pages_and_replies',
    'test_review_thread_api_errors_fail_closed','test_review_thread_pagination_cannot_silently_truncate',
    'test_review_thread_lookup_rejects_other_repositories',
):
    setattr(NativeBoundaryTests,'test_threads_'+name,boundary_case(test_worklane.WorklaneTests,name,[
        (w.Runner,'review_threads',native_review_threads)]))
for name in ('test_defaults_are_read_only_and_config_is_per_checkout',
             'test_invalid_config_has_path_and_never_silently_uses_defaults'):
    setattr(NativeBoundaryTests,'test_config_'+name,boundary_case(test_config.ConfigTests,name,[
        (w,'load_config',native_load_config),(w,'save_config',native_save_config)]))
for name in ('test_summary_counts_only_open_blockers_and_has_no_pagination_limit',
             'test_missing_or_malformed_summary_fails_closed','test_known_blockers_do_not_make_per_slot_requests',
             'test_fresh_blocker_overrides_unblocked_snapshot_before_claim',
             'test_dependency_api_failure_or_hidden_issue_cannot_claim',
             'test_interrupted_and_retry_stages_remain_untouched_while_blocked','test_blocked_review_is_not_assigned'):
    setattr(NativeBoundaryTests,'test_dependency_'+name,boundary_case(test_dependencies.DependencyTests,name,[
        (w,'Board',NativeBoard),(w,'dependency_count',native_dependency_count),
        (w.Runner,'pick',native_pick),(w.Runner,'recover',native_recover)]))
for name in ('test_existing_linked_pr_not_implemented_twice','test_closed_issues_are_never_claimed',
             'test_blocked_task_never_auto_restarts','test_explicit_retry_reviews_current_head',
             'test_claim_quota_failure_recovers_before_and_after_status_write'):
    setattr(NativeBoundaryTests,'test_assignment_'+name,boundary_case(test_worklane.WorklaneTests,name,[
        (w.Runner,'pick',native_pick),(w.Runner,'recover',native_recover)]))
setattr(NativeBoundaryTests,'test_recovery_human_override',boundary_case(test_sessions.SessionTests,
    'test_recovery_respects_human_board_override',[(w.Runner,'recover',native_recover)]))
for name in ('test_question_feedback_uses_issue_without_a_pr',
             'test_reads_all_pages_and_kinds_ignoring_bots_markers_and_approvals',
             'test_closed_changed_or_retargeted_pr_is_not_reworked'):
    setattr(NativeBoundaryTests,'test_feedback_'+name,boundary_case(test_human_feedback.HumanFeedbackTests,name,[
        (w.Runner,'human_feedback',native_feedback)]))
for name in unittest.defaultTestLoader.getTestCaseNames(test_stacking.StackTests):
    setattr(NativeBoundaryTests,'test_stack_'+name,boundary_case(test_stacking.StackTests,name,[
        (w.Runner,'pick',native_pick),(w.Runner,'recover',native_recover),
        (w.Runner,'dependencies_ready',native_dependencies_ready),(w.Runner,'validate_stack',native_validate_stack),
        (w,'validate_config',native_validate_config)]))
for name in ('test_detached_orphan_and_ignoring_descendant_die_without_touching_unrelated',
             'test_stale_pid_cannot_own_unrelated_process'):
    setattr(NativeBoundaryTests,'test_process_'+name,boundary_case(test_stop.StopTests,name,[
        (test_stop,'owned',native_owned),(test_stop,'terminate_owned',native_terminate_owned)]))
del name
