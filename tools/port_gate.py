#!/usr/bin/env python3
"""Fail closed unless every row has candidate evidence and source audit coverage."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def contract_hash(inventory):
    return hashlib.sha256((json.dumps(inventory,indent=2,ensure_ascii=False) + '\n').encode()).hexdigest()


def assess(inventory, reports):
    checks, audits = {}, set()
    fixture_ids = {case for cases in inventory.get('fixture_cases', {}).values() for case in cases}
    for report in reports:
        if (report.get('mode') != 'differential' or report.get('reference') != inventory['revision']
                or report.get('inventory_sha256') != contract_hash(inventory) or not report.get('reference_verified') or not report.get('candidate_hashes') or not report.get('candidate')):
            continue
        fixture_report = ('fixtures_path' in report or 'fixtures_sha256' in report
                          or any('fixture:' + r['id'].removeprefix('fixture:') in fixture_ids for r in report.get('results', [])))
        members = set()
        if fixture_report:
            path = report.get('fixtures_path')
            expected_hash = inventory.get('fixture_sources', {}).get(path) if isinstance(path, str) else None
            if not expected_hash or report.get('fixtures_sha256') != expected_hash or path not in inventory.get('fixture_cases', {}):
                continue
            members = set(inventory['fixture_cases'][path])
        # Every report must identify the same release executable, even when a
        # separate fixture adapter or CLI argument array is used.
        if not report.get('release_sha256'):
            continue
        for result in report.get('results', []):
            key = result['id']
            fixture_key = 'fixture:' + key.removeprefix('fixture:')
            if fixture_report:
                if fixture_key not in members:
                    continue
                key = fixture_key
            elif key not in inventory.get('checks', {}):
                continue
            status = result['status']
            if status == 'pass' and (result.get('comparison') != 'equal' or 'observations' not in result):
                status = 'missing comparison observations'
            if key in checks and checks[key] != status:
                checks[key] = 'conflicting evidence'
            else:
                checks[key] = status
        for audit in report.get('source_audits', []):
            if audit.get('artifact_sha256') and audit.get('commands') and audit.get('unasserted_behaviors') == []:
                audits.add(audit['row'])
    releases = {r['release_sha256'] for r in reports if r.get('mode') == 'differential' and r.get('release_sha256')}
    failures = []
    for row in inventory['matrix']:
        required = []
        for check in row['checks']:
            required.extend(inventory['suites'][check.removeprefix('suite:')] if check.startswith('suite:') else [check])
        missing = [check for check in required if checks.get(check) != 'pass']
        audit_required = row['id'].startswith(('source.','constant.','failure.','document.','tui.key.'))
        if missing or audit_required and row['id'] not in audits or len(releases) != 1:
            failures.append({'row':row['id'], 'missing_checks':missing,
                             'source_audit_missing':audit_required and row['id'] not in audits,
                             'release_identity_invalid':len(releases) != 1})
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', nargs='+', type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    args = parser.parse_args()
    inventory = json.loads((ROOT / 'parity/inventory.json').read_text())
    failures = assess(inventory, [json.loads(p.read_text()) for p in args.reports])
    args.evidence.write_text(json.dumps({'reference':inventory['revision'],'failures':failures},indent=2)+'\n')
    print(json.dumps({'rows':len(inventory['matrix']), 'blocked_rows':len(failures)}))
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
