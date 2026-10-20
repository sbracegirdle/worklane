#!/usr/bin/env python3
"""Record native checks and fail closed on incomplete frozen runtime evidence."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'parity/evidence'


def main():
    env = {**os.environ, 'UV_CACHE_DIR':'/tmp/worklane-50-uv', 'WORKLANE_TEST_RUST':'1',
           'WORKLANE_TEST_INSTALL':'0', 'WORKLANE_TEST_OPENCODE':'0',
           'WORKLANE_NATIVE_EVIDENCE':str(EVIDENCE/'native-acceptance.json')}
    commands = [
        ['cargo','fmt','--all','--check'],
        ['cargo','clippy','--all-targets','--locked','--offline','--','-D','warnings'],
        ['cargo','test','--locked','--offline'],
        ['uv','run','--no-project','python','-m','unittest','-q'],
        ['uv','run','--no-project','python','tools/native_parity.py',str(EVIDENCE/'native-runtime.json')],
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
    passed = {row['id'] for row in acceptance if row['reference']==row['candidate']=='pass'}
    passed.update('fixture:'+row['id'] for row in runtime['results'] if row['status']=='pass')
    rows = []
    for row in manifest['matrix']:
        if row['stage'] != 'runtime':
            continue
        checks = set()
        for check in row['checks']:
            checks.update(manifest['suites'][check[6:]] if check.startswith('suite:') else [check])
        missing = sorted(checks-passed)
        source_audit = row['id'].startswith(('source.','constant.','document.'))
        rows.append({'id':row['id'],'status':'unverified' if missing or source_audit else 'complete',
                     'missing_check_count':len(missing),'source_audit_required':source_audit})
    unverified = [row['id'] for row in rows if row['status']!='complete']
    current_main=subprocess.check_output(['git','rev-parse','origin/main'],cwd=ROOT,text=True).strip()
    main_drift=subprocess.check_output(['git','diff','--name-only',manifest['revision'],current_main],cwd=ROOT,text=True).splitlines()
    report = {'reference':manifest['revision'],'current_main':current_main,'upstream_drift':main_drift,'candidate_hashes':runtime['candidate_hashes'],
              'environment':{key:env[key] for key in ('UV_CACHE_DIR','WORKLANE_TEST_RUST','WORKLANE_TEST_INSTALL','WORKLANE_TEST_OPENCODE')},
              'commands':results,'acceptance_checks':len(acceptance),'portable_cases':len(runtime['results']),
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
