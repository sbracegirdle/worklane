#!/usr/bin/env python3
"""Execute the registered runtime fixtures implemented at the stage-2 boundary."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import argparse
from port_parity import ROOT, command_hashes, execute, reference


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', nargs='?', type=Path, default=ROOT/'parity/evidence/native-runtime.json')
    parser.add_argument('--fixtures', type=Path, default=ROOT/'parity/fixtures/runtime.json')
    args = parser.parse_args()
    binary = ROOT / 'target/debug/runtime-fixture-adapter'
    if not binary.read_bytes().startswith(b'\x7fELF'):
        raise ValueError('Native evidence requires the compiled Rust adapter, not a script.')
    inventory_path = ROOT / 'parity/inventory.json'
    fixture_path = args.fixtures.resolve()
    inventory = json.loads(inventory_path.read_text())
    fixtures = json.loads(fixture_path.read_text())
    report = {'mode':'differential','candidate_runtime':'native','reference':inventory['revision'],'reference_verified':True,
              'inventory_sha256':hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
              'fixtures_path':str(fixture_path.relative_to(ROOT)),
              'fixtures_sha256':hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
              'candidate':[str(binary)], 'candidate_hashes':command_hashes([str(binary)]),
              'release_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(), 'results':[]}
    excluded = {'runtime.terminal-initialization-error',
                'runtime.terminal-missing-backend', 'runtime.terminal-interrupt'}
    report['unverified_cases'] = sorted(excluded & {f['id'] for f in fixtures['scenarios']})
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
                result = ({'id':fixture['id'],'status':'pass','comparison':'equal','observations':expected}
                          if expected==actual else {'id':fixture['id'],'status':'fail','comparison':'different',
                          'reference_observations':expected,'candidate_observations':actual})
            except Exception as error:
                result = {'id':fixture['id'],'status':'fail','error':str(error)}
            report['results'].append(result)
    destination = args.output
    destination.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    failures = [r['id'] for r in report['results'] if r['status']!='pass']
    print(json.dumps({'passed':len(report['results'])-len(failures),'total':len(report['results']),'failures':failures}))
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
