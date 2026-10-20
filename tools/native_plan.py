#!/usr/bin/env python3
"""Map unchanged runtime obligations to bounded batches without changing the contract."""
import argparse
import hashlib
import json
from pathlib import Path

try:
    from .native_validate import ROOT, admit_reports, runtime_failures
except ImportError:
    from native_validate import ROOT, admit_reports, runtime_failures


PACKAGES={
    'identity':{'order':1,'deliverable':'Reconcile binary, driver, fixture and reference identities; preserve every gate.',
                'files':['tools/native_validate.py','tools/native_plan.py','test_native_gate.py'],
                'completion':'Every report is admitted or explicitly rejected with a reason; every unresolved row is mapped.'},
    'boundaries':{'order':2,'deliverable':'Native storage, configuration, process and provider assertions.',
                  'files':['runtime/src/state.rs','runtime/src/config.rs','runtime/src/process.rs','runtime/src/harness.rs','test_native_runtime.py'],
                  'completion':'Named frozen assertions pass against both implementations; durable state and exact role/session history agree.'},
    'github':{'order':3,'deliverable':'Native transport, permission, dependency and feedback assertions.',
              'files':['runtime/src/transport.rs','runtime/src/board.rs','runtime/src/workflow.rs','test_native_boundaries.py','test_native_security.py'],
              'completion':'Named assertions pass with live command-boundary checks, including revoked permissions and visibility changes.'},
    'workflow':{'order':4,'deliverable':'Native controller, assignment, recovery and restack assertions.',
                'files':['runtime/src/controller.rs','runtime/src/workflow.rs','test_native_workflow.py','test_native_lifecycle.py','test_native_restack.py'],
                'completion':'Named assertions pass through native workflows; saved turns, exact sessions and human board edits are preserved.'},
    'audits':{'order':5,'deliverable':'Native failure fixtures and explicit source/constant audits.',
              'files':['runtime/src/bin/runtime-fixture-adapter.rs','tools/native_parity.py','tools/port_gate.py','parity/failure-checks.json'],
              'completion':'Each required guard is exercised natively; each exact pinned source span has a reviewed audit with no unasserted behaviors.'},
    'integration':{'order':6,'deliverable':'Full inventory, evidence and independent-review handoff.',
                   'files':['tools/native_validate.py','tools/port_gate.py','test_native_gate.py','parity/evidence/native-validation.json'],
                   'completion':'All original gates pass at the same pushed head; independent review remains required.'},
}


def package(identifier):
    identifier=identifier.removeprefix('fixture:')
    if identifier.startswith(('source.','constant.','failure.','fixture.runtime.guard.','runtime.guard.')):return 'audits'
    if identifier.startswith('test_port_parity.'):return 'integration'
    if identifier.startswith(('test_stacking.','test_api_budget.')):return 'workflow'
    if identifier.startswith(('test_human_feedback.','test_security.','test_dependencies.')):return 'github'
    if identifier.startswith('test_worklane.'):
        return 'github' if any(word in identifier for word in ('board','github','budget','quota','thread','rate_limit','dependency')) else 'workflow'
    if identifier.startswith(('fixture.runtime.terminal','runtime.terminal')):return 'integration'
    return 'boundaries'


def candidate_files(row):
    name=row['id']
    if 'worklane_process.py' in name:return ['runtime/src/process.rs','runtime/src/controller.rs']
    if 'worklane_harness.py' in name:return ['runtime/src/harness.rs']
    if '.State' in name:return ['runtime/src/state.rs']
    if any(value in name for value in ('.Runner','feedback','stack')):return ['runtime/src/workflow.rs','runtime/src/controller.rs']
    if any(value in name for value in ('config','CONFIG_')):return ['runtime/src/config.rs']
    if any(value in name for value in ('.Board','GitHub','raw_command','graphql','repository')):
        return ['runtime/src/board.rs','runtime/src/transport.rs']
    return PACKAGES[package(name)]['files']


def build(manifest, failures, admission):
    rows,proof_sets,checks,verified=[],{},{},[]
    review_path=ROOT/'parity/source-review.json'
    reviews={r['row']:r for r in json.loads(review_path.read_text()).get('reviews',[])} if review_path.exists() else {}
    for row in manifest['matrix']:
        if row['stage']!='runtime':continue
        failure=failures.get(row['id'])
        if failure is None:
            verified.append(row['id']);continue
        missing=sorted(set(failure['missing_checks']))
        proof_key=hashlib.sha256(json.dumps(missing).encode()).hexdigest()[:16]
        proof_sets.setdefault(proof_key,missing)
        for check in missing:
            if check in checks:continue
            owner=package(check.removeprefix('fixture:'))
            source=manifest.get('checks',{}).get(check)
            fixture=next((path for path,ids in manifest.get('fixture_cases',{}).items() if check in ids),None)
            checks[check]={'batch':owner,'source':source or fixture,
                'obligation':'Run unchanged frozen assertions against native code with bound observations.' if source else
                             'Implement the registered fixture operation natively and compare exact observations.',
                'reference_command':['uv','run','--no-project','python','-m','unittest','-q',check] if source else
                    ['uv','run','--no-project','python','tools/native_parity.py','--fixtures',fixture],
                'reference_only_is_insufficient':True}
        obligations=[]
        if missing:obligations.append('executable_assertion')
        if failure.get('source_audit_missing'):obligations.append('source_audit')
        if failure.get('release_identity_invalid'):obligations.append('release_identity')
        rows.append({'id':row['id'],'batch':package(row['id']),'source':row['source'],
            'original_stage':row['stage'],'original_checks':row['checks'],'missing_proof_set':proof_key,
            'obligations':obligations,'candidate_files_to_review':candidate_files(row),
            'implementation_status':'Review required; absent proof does not establish absent implementation.',
            'audit_status':'pending' if failure.get('source_audit_missing') else 'not required'})
        if row['id'] in reviews:
            rows[-1]['source_review']='parity/source-review.json#'+row['id']
            rows[-1]['implementation_status']=reviews[row['id']]['implementation_status']
    batches={name:{**data,'row_ids':[r['id'] for r in rows if r['batch']==name]}
             for name,data in PACKAGES.items()}
    restacks=[r['id'] for r in rows if r['id'].startswith('test_stacking.RestackTests.')]
    return {'reference':manifest['revision'],'inventory_sha256':hashlib.sha256((ROOT/'parity/inventory.json').read_bytes()).hexdigest(),
        'binary_sha256':hashlib.sha256((ROOT/'target/debug/runtime-fixture-adapter').read_bytes()).hexdigest(),
        'report_admission':admission,'verified_count':len(verified),'remaining_count':len(rows),
        'verified_rows':verified,'batches':batches,'rows':rows,'proof_sets':proof_sets,'checks':checks,
        'checkpoint_rule':'Commit/push a coherent batch; report current bound counts, commands/results and next exact rows. Full checks after integration changes.',
        'requirements_unchanged':True,'separate_workers_not_started':True,
        'possible_disjoint_assignments':[
            {'portion':'Provider exact-session assertions','files':['test_native_provider_boundaries.py'],
             'integration_order':1,'rows':[r['id'] for r in rows if r['id'].startswith(('test_harness.','test_sessions.'))],
             'boundary':'Use existing JSON operations; shared Rust/bridge edits stay with the owning worker.'},
            {'portion':'Feedback parsing assertions','files':['test_native_feedback_boundaries.py'],
             'integration_order':2,'rows':[r['id'] for r in rows if r['id'].startswith('test_human_feedback.')],
             'boundary':'Separate test file; preserve permission/provenance guards; shared adapter changes integrate serially.'}],
        'restack_batch_remaining':restacks,
        'surface_ownership':manifest['security_ownership'],
        'cross_stage_dependencies_retained':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'parity/native-plan.json')
    args=parser.parse_args()
    manifest=json.loads((ROOT/'parity/inventory.json').read_text())
    reports,admission,_=admit_reports(manifest)
    result=build(manifest,runtime_failures(manifest,reports),admission)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({key:result[key] for key in ('verified_count','remaining_count','binary_sha256')}))


if __name__=='__main__':main()
