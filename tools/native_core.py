"""Finite #50 scenario selection over existing native acceptance evidence, not a new parity inventory."""
import hashlib
import json
from pathlib import Path

try:
    from .native_test_bridge import acceptance_inputs
except ImportError:
    from native_test_bridge import acceptance_inputs

# Each selector names an existing assertion/fixture. Subtests retain their actual observations.
GROUPS = {
    'core.1.storage-config': {
        'native-acceptance': [
            'test_sessions.SessionTests.test_migration_from_sqlite_v1_imports_files_and_normalizes_payloads',
            'test_sessions.SessionTests.test_json_migration_is_atomic_idempotent_and_archives_originals_in_sqlite',
            'test_sessions.SessionTests.test_input_survives_failed_delivery_and_restarts_only_in_matching_role'],
        'native-boundaries': ['test_config.ConfigTests.test_invalid_config_has_path_and_never_silently_uses_defaults'],
        'rust': ['typed_state_preserves_empty_null_integer_boolean_and_float',
                 'future_schema_refusal_does_not_modify_database','wal_backup_restores_committed_events_and_pending_inputs',
                 'uncommitted_crash_is_not_visible_and_committed_event_survives']},
    'core.2.dispatch-ownership': {
        'rust': ['bounded_workers_independent_reviewer_pause_and_drain_keep_pending_work'],
        'native-boundaries': ['test_worklane.WorklaneTests.test_existing_linked_pr_not_implemented_twice'],
        'native-storage': ['test_worklane.WorklaneTests.test_duplicate_controller_is_locked']},
    'core.3.exact-provider-sessions': {
        'native-acceptance': [
            'test_harness.HarnessTests.test_each_role_has_separate_named_session_and_persistent_wire_events',
            'test_harness.HarnessTests.test_queued_input_resumes_exact_opencode_conversation_for_every_role',
            'test_sessions.SessionTests.test_live_input_runs_followup_in_exact_session_before_returning_result',
            'test_harness.HarnessTests.test_missing_session_does_not_fall_back_to_new_conversation',
            'test_sessions.SessionTests.test_deleted_worktree_never_creates_replacement_session',
            'test_sessions.SessionTests.test_database_prevents_cross_role_session_reuse'],
        'native-runtime': ['runtime.session-codex','runtime.session-opencode']},
    'core.4.review-handoff': {
        'native-lifecycle': [
            'test_native_lifecycle.NativeLifecycleTests.test_complete_build_review_and_handoff_promotes_only_drafts',
            'test_native_lifecycle.NativeLifecycleTests.test_review_and_finalizer_failures_cannot_handoff',
            'test_native_lifecycle.NativeLifecycleTests.test_conflicts_requeue_and_respect_round_limit_and_status_override'],
        'native-boundaries': ['test_worklane.WorklaneTests.test_explicit_retry_reviews_current_head']},
    'core.5.pause-drain-stop': {
        'rust': ['bounded_workers_independent_reviewer_pause_and_drain_keep_pending_work',
                 'immediate_stop_never_waits_for_dispatch_or_downgrades','old_start_generation_cannot_clear_new_stop'],
        'native-boundaries': ['test_stop.StopTests.test_detached_orphan_and_ignoring_descendant_die_without_touching_unrelated',
                              'test_stop.StopTests.test_stale_pid_cannot_own_unrelated_process'],
        'native-acceptance': ['test_harness.HarnessTests.test_stop_request_interrupts_every_role_and_resumes_exact_session']},
    'core.6.interruption-recovery': {
        'native-workflow': ['test_native_workflow.NativeWorkflowTests.test_remote_move_crash_recovers_saved_intent_and_does_not_duplicate_claim'],
        'native-lifecycle': ['test_native_lifecycle.NativeLifecycleTests.test_saved_handoff_retries_without_duplicate_turns_or_comments',
                             'test_native_lifecycle.NativeLifecycleTests.test_quota_retry_reuses_saved_turn_and_records_each_failure_once'],
        'native-acceptance': ['test_harness.HarnessTests.test_signal_interruption_and_controller_restart_resume_exact_id',
                              'test_sessions.SessionTests.test_input_survives_failed_delivery_and_restarts_only_in_matching_role'],
        'rust': ['abrupt_process_exit_preserves_committed_session_and_discards_inflight_transaction']},
    'core.7.dependencies-stacks': {
        'native-boundaries': ['test_dependencies.DependencyTests.test_dependency_api_failure_or_hidden_issue_cannot_claim',
                              'test_stacking.StackTests.test_exact_reviewed_parent_pins_child_base_and_provenance',
                              'test_stacking.StackTests.test_all_desktop_edges_remain_hard_completion_dependencies',
                              'test_stacking.StackTests.test_completion_gate_rejects_unreviewed_merged_head'],
        'native-lifecycle': ['test_native_lifecycle.NativeLifecycleTests.test_restack_merge_forward_recovery_preserves_history_and_fresh_review',
                             'test_native_lifecycle.NativeLifecycleTests.test_restack_dirty_conflicting_or_unrelated_changes_are_preserved']},
    'core.8.privacy-authorization': {
        'native-security': ['runtime.security-safe-private','runtime.security-safe-public',
                            'runtime.security-safe-explicit-public','runtime.security-safe-project-denied',
                            'runtime.security-safe-read-only','runtime.security-safe-unknown',
                            'runtime.security-feedback-trusted-editor','runtime.security-feedback-revoked-editor',
                            'runtime.security-feedback-missing-provenance','runtime.security-deny-restack'],
        'native-workflow': ['test_native_workflow.NativeWorkflowTests.test_live_revocation_blocks_dispatch_until_author_and_editor_restored'],
        'native-acceptance': ['test_sessions.SessionTests.test_queued_followup_rechecks_security_before_resuming_agent'],
        'native-lifecycle': ['test_native_lifecycle.NativeLifecycleTests.test_saved_turn_rechecks_privacy_and_handoff_write_permissions']},
    'core.9.github-transport': {
        'native-boundaries': ['test_worklane.WorklaneTests.test_board_paginates_with_project_id_and_retains_all_issues',
                              'test_worklane.WorklaneTests.test_review_threads_include_all_pages_and_replies',
                              'test_worklane.WorklaneTests.test_incomplete_board_fails_closed',
                              'test_worklane.WorklaneTests.test_review_thread_api_errors_fail_closed',
                              'test_worklane.WorklaneTests.test_budget_shared_by_repositories_and_blocks_commands',
                              'test_worklane.WorklaneTests.test_rate_limit_backoff_survives_new_runner_and_is_bounded',
                              'test_worklane.GitHubErrorTests.test_header_reset_avoids_an_extra_probe'],
        'rust': ['quota_probe_failure_keeps_known_quota_and_shared_cooldown']},
}


def report_bound(root, evidence, reference, binary_hash, inventory_hash):
    """Reject stale/oracle-only observations, including changed drivers or fixtures."""
    candidate=evidence.get('candidate',[])
    if (evidence.get('mode')!='differential' or evidence.get('candidate_runtime')!='native'
            or evidence.get('reference')!=reference or not evidence.get('reference_verified')
            or evidence.get('inventory_sha256')!=inventory_hash
            or evidence.get('release_sha256')!=binary_hash or not candidate
            or evidence.get('candidate_hashes')!={candidate[0]:binary_hash}):
        return False
    if evidence.get('driver'):
        return evidence.get('harness_hashes')=={
            path:hashlib.sha256((root/path).read_bytes()).hexdigest()
            for path in acceptance_inputs(evidence['driver'])}
    fixture=evidence.get('fixtures_path')
    return bool(fixture and fixture in ('parity/fixtures/runtime.json','parity/fixtures/security.json')
                and evidence.get('fixtures_sha256')==hashlib.sha256((root/fixture).read_bytes()).hexdigest())


def assess_core(root, reference, binary_hash, commands, checks_current, main_drift):
    inventory_hash=hashlib.sha256((root/'parity/inventory.json').read_bytes()).hexdigest()
    reports={}
    for name in {name for group in GROUPS.values() for name in group if name!='rust'}:
        path=root/'parity/evidence'/f'{name}.json'
        evidence=json.loads(path.read_text()) if path.exists() else {}
        reports[name]=(evidence,report_bound(root,evidence,reference,binary_hash,inventory_hash))
    rust=next((row for row in commands if row['command']==['cargo','test','--locked','--offline']),{})
    groups=[]
    for group_id,requirements in GROUPS.items():
        assertions=[]
        for name,selectors in requirements.items():
            for selector in selectors:
                if name=='rust':
                    observed=selector in rust.get('passed_tests',[]) and rust.get('exit')==0
                    assertions.append({'report':'cargo test','id':selector,'status':'pass' if observed else 'missing',
                                       'log_sha256':rust.get('log_sha256'),
                                       'test_binary_hashes':rust.get('test_binary_hashes',{})})
                    continue
                evidence,bound=reports[name]
                matches=[row for row in evidence.get('results',[]) if row['id'].split(' ')[0]==selector]
                passed=bool(bound and matches and all(row.get('status')=='pass'
                    and row.get('comparison')=='equal' and 'observations' in row for row in matches))
                assertions.append({'report':f'parity/evidence/{name}.json','id':selector,
                                   'status':'pass' if passed else 'missing_or_stale_or_failed',
                                   'observation_ids':[row['id'] for row in matches],
                                   'report_sha256':hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest()})
        groups.append({'id':group_id,'status':'pass' if all(row['status']=='pass' for row in assertions) else 'fail',
                       'assertions':assertions})
    ready=bool(checks_current and commands and all(row['exit']==0 for row in commands)
               and not main_drift and all(row['status']=='pass' for row in groups))
    return {'core_runtime_ready':ready,'core_release_ready':False,'core_scenarios':groups,
            'later_stage_ownership':{'51':[10],'52':[11,12],'53':list(range(1,15))},
            'independent_review':'pending; required before Human review handoff',
            'deferred_non_core':['Exhaustive source/failure/constant audits and unselected legacy parity assertions.',
                                 'CLI/TUI presentation, installed PTY/lifecycle and cutover remain gated by #51/#52/#53.']}
