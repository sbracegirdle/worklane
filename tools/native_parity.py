#!/usr/bin/env python3
"""Execute the registered runtime fixtures implemented at the stage-2 boundary."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from port_parity import ROOT, command_hashes, execute, reference


def main():
    binary = ROOT / 'target/debug/runtime-fixture-adapter'
    inventory_path = ROOT / 'parity/inventory.json'
    fixture_path = ROOT / 'parity/fixtures/runtime.json'
    inventory = json.loads(inventory_path.read_text())
    fixtures = json.loads(fixture_path.read_text())
    report = {'mode':'differential','reference':inventory['revision'],'reference_verified':True,
              'inventory_sha256':hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
              'fixtures_path':'parity/fixtures/runtime.json',
              'fixtures_sha256':hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
              'candidate':[str(binary)], 'candidate_hashes':command_hashes([str(binary)]),
              'release_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(), 'results':[]}
    excluded = {'runtime.terminal-initialization-error',
                'runtime.terminal-missing-backend', 'runtime.terminal-interrupt'}
    report['unverified_cases'] = sorted(excluded)
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory)
        reference(source, inventory['revision'])
        oracle = [sys.executable,str(ROOT/'tools/port_reference_driver.py'),'--source',str(source)]
        for fixture in fixtures['scenarios']:
            if fixture['id'] in excluded:
                continue
            try:
                expected = execute(oracle, fixture)
                actual = execute([str(binary)], fixture)
                result = {'id':fixture['id'],'status':'pass' if expected==actual else 'fail',
                          'comparison':'equal' if expected==actual else 'different',
                          'reference_observations':expected,'candidate_observations':actual}
            except Exception as error:
                result = {'id':fixture['id'],'status':'fail','error':str(error)}
            report['results'].append(result)
    destination = Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'parity/evidence/native-runtime.json'
    destination.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    failures = [r['id'] for r in report['results'] if r['status']!='pass']
    print(json.dumps({'passed':len(report['results'])-len(failures),'total':len(report['results']),'failures':failures}))
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
