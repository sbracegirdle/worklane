#!/usr/bin/env python3
"""Record native checks and fail closed on incomplete frozen runtime evidence."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

try:
    from .port_gate import assess
    from .native_test_bridge import acceptance_inputs
except ImportError:
    from port_gate import assess
    from native_test_bridge import acceptance_inputs

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'parity/evidence'


def acceptance_checks(report, manifest, inventory_sha, release_sha, harness_hashes):
    """Stale binaries, changed assertion drivers and reference-only runs cannot attest parity."""
    if (report.get('mode')!='differential' or report.get('candidate_runtime')!='native'
            or report.get('reference')!=manifest['revision'] or not report.get('reference_verified')
            or report.get('inventory_sha256')!=inventory_sha or report.get('release_sha256')!=release_sha
            or not report.get('candidate') or not report.get('candidate_hashes')
            or report['candidate'][0] not in report['candidate_hashes']
            or any(value!=release_sha for value in report['candidate_hashes'].values())
            or report.get('harness_hashes')!=harness_hashes):
        return set()
    return {row['id'] for row in report.get('results',[]) if row['id'] in manifest['checks']
            and row.get('status')=='pass' and row.get('comparison')=='equal' and 'observations' in row}


def runtime_failures(manifest, reports):
    runtime_ids={row['id'] for row in manifest['matrix'] if row['stage']=='runtime'}
    # Evidence is bound to the complete frozen contract, including later stages.
    return {row['row']:row for row in assess(manifest,reports) if row['row'] in runtime_ids}


def main():
    env = {**os.environ, 'UV_CACHE_DIR':'/tmp/worklane-50-uv', 'WORKLANE_TEST_RUST':'1',
           'WORKLANE_TEST_INSTALL':'0', 'WORKLANE_TEST_OPENCODE':'0',
           'WORKLANE_NATIVE_EVIDENCE':str(EVIDENCE/'native-acceptance.json'),
           'WORKLANE_NATIVE_BOUNDARY_EVIDENCE':str(EVIDENCE/'native-boundaries.json'),
           'WORKLANE_NATIVE_WORKFLOW_EVIDENCE':str(EVIDENCE/'native-workflow.json'),
           'WORKLANE_NATIVE_LIFECYCLE_EVIDENCE':str(EVIDENCE/'native-lifecycle.json')}
    commands = [
        ['cargo','fmt','--all','--check'],
        ['cargo','clippy','--all-targets','--locked','--offline','--','-D','warnings'],
        ['cargo','test','--locked','--offline'],
        ['uv','run','--no-project','python','-m','unittest','-q'],
        ['uv','run','--no-project','python','tools/native_parity.py',str(EVIDENCE/'native-runtime.json')],
        ['uv','run','--no-project','python','tools/native_parity.py','--fixtures','parity/fixtures/security.json',str(EVIDENCE/'native-security.json')],
        ['uv','run','--no-project','python','-m','unittest','-q','test_native_security'],
        ['uv','run','--no-project','python','tools/port_inventory.py','--revision',json.loads((ROOT/'parity/inventory.json').read_text())['revision']],
        ['git','diff','--check'],
        ['git','diff','--cached','--check'],
    ]
    results = []
    for index, command in enumerate(commands):
        log = Path(f'/tmp/worklane50-validation-{index}.log')
        with log.open('w') as output:
            process = subprocess.run(command,cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT)
        tail = '\n'.join(log.read_text().splitlines()[-10:])
        results.append({'command':command,'exit':process.returncode,'log':str(log),'tail':tail})
        print(json.dumps({'command':command,'exit':process.returncode}),flush=True)
    manifest = json.loads((ROOT/'parity/inventory.json').read_text())
    acceptance = json.loads((EVIDENCE/'native-acceptance.json').read_text())
    runtime = json.loads((EVIDENCE/'native-runtime.json').read_text())
    security = json.loads((EVIDENCE/'native-security.json').read_text())
    boundary = json.loads((EVIDENCE/'native-boundaries.json').read_text())
    workflow = json.loads((EVIDENCE/'native-workflow.json').read_text())
    lifecycle = json.loads((EVIDENCE/'native-lifecycle.json').read_text())
    inventory_sha=hashlib.sha256((ROOT/'parity/inventory.json').read_bytes()).hexdigest()
    release_sha=hashlib.sha256((ROOT/'target/debug/runtime-fixture-adapter').read_bytes()).hexdigest()
    passed=set()
    reports=[]
    for evidence in (acceptance,boundary):
        driver=evidence['driver']
        hashes={path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest()
                for path in acceptance_inputs(driver)}
        checks=acceptance_checks(evidence,manifest,inventory_sha,release_sha,hashes)
        passed.update(checks)
        if checks:reports.append(evidence)
    for evidence in (runtime,security):
        if (evidence.get('release_sha256')==release_sha
                and evidence.get('candidate_hashes',{}).get(str(ROOT/'target/debug/runtime-fixture-adapter'))==release_sha):
            reports.append(evidence)
    stage_manifest={**manifest,'matrix':[row for row in manifest['matrix'] if row['stage']=='runtime']}
    failures=runtime_failures(manifest,reports)
    # The shared gate validates registered fixture membership, reference/hash binding,
    # exact observations and source audits, rather than trusting a status string.
    passed.update(check for row in stage_manifest['matrix'] if row['id'] not in failures
                  for check in row['checks'] if check.startswith('fixture:'))
    rows = []
    for row in manifest['matrix']:
        if row['stage'] != 'runtime':
            continue
        missing = failures.get(row['id'],{}).get('missing_checks',[])
        source_audit = row['id'].startswith(('source.','constant.','failure.','document.'))
        rows.append({'id':row['id'],'status':'unverified' if row['id'] in failures else 'complete',
                     'missing_check_count':len(missing),'source_audit_required':source_audit})
    unverified = [row['id'] for row in rows if row['status']!='complete']
    current_main=subprocess.check_output(['git','rev-parse','origin/main'],cwd=ROOT,text=True).strip()
    main_drift=subprocess.check_output(['git','diff','--name-only',manifest['revision'],current_main],cwd=ROOT,text=True).splitlines()
    report = {'reference':manifest['revision'],'current_main':current_main,'upstream_drift':main_drift,'candidate_hashes':runtime['candidate_hashes'],
              'environment':{key:env[key] for key in ('UV_CACHE_DIR','WORKLANE_TEST_RUST','WORKLANE_TEST_INSTALL','WORKLANE_TEST_OPENCODE')},
              'commands':results,'acceptance_checks':len(acceptance['results']),
              'boundary_checks':len(boundary['results']),
              'supplemental_workflow_scenarios':len(workflow['results']),
              'supplemental_lifecycle_scenarios':len(lifecycle['results']),
              'portable_cases':len(runtime['results']),
              'security_fixtures':{'passed':sum(row['status']=='pass' for row in security['results']),
                                   'total':len(security['results']),'candidate_runtime':security['candidate_runtime']},
              'security_ownership':manifest['security_ownership'],
              'passed_checks':sorted(passed),
              'inventory_sha256':hashlib.sha256((ROOT/'parity/inventory.json').read_bytes()).hexdigest(),
              'runtime_rows':rows,'unverified_rows':unverified,
              'stage2_complete':not unverified and not main_drift and all(row['exit']==0 for row in results),
              'limits':['No installation switch or live authenticated provider turns.',
                        'Protocol/results: 4 MiB; subprocess capture: 16 MiB; logs/events use bounded pages.',
                        'Workflow, transport and failure rows without native assertions remain unverified; source audits are pending.']}
    (EVIDENCE/'native-validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'complete_rows':len(rows)-len(unverified),'runtime_rows':len(rows),'stage2_complete':report['stage2_complete']}))
    return 0 if report['stage2_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
