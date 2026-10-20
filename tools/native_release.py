#!/usr/bin/env python3
"""Bind staged groups 1–14 to installed native binaries; never claim production cutover."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ObservedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observations = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.observations.append({'id': test.id(), 'status': 'pass',
                                  'observations': getattr(test, 'native_observations', {})})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--tui', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    cli, tui = args.cli.resolve(), args.tui.resolve()
    for binary in (cli, tui):
        if not binary.read_bytes().startswith(b'\x7fELF'):
            raise ValueError('Staged commands must be native ELF binaries')
    identity = json.loads(subprocess.check_output([str(cli), '--version'], text=True))
    binary_hashes = {str(p): digest(p) for p in (cli, tui)}
    from tools.native_validate import checked_inputs
    runtime_path = ROOT / 'parity/evidence/native-validation.json'
    runtime = json.loads(runtime_path.read_text())
    current_inputs = checked_inputs()
    runtime_ready = (runtime['core_runtime_ready'] and runtime['checks_current']
                     and runtime['checked_inputs'] == current_inputs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cli_report = args.output.with_name('native-cli.json')
    tui_report = args.output.with_name('native-tui.json')
    os.environ.update(WORKLANE_TEST_RUST='1', WORKLANE_TEST_CLI_BINARY=str(cli),
                      WORKLANE_TUI_BIN=str(tui), WORKLANE_NATIVE_CLI_EVIDENCE=str(cli_report))
    commands = [
        ['uv', 'run', '--no-project', 'python', '-m', 'unittest', '-q', 'test_native_cli'],
        ['uv', 'run', '--no-project', 'python', 'tools/native_tui.py', str(tui_report)],
    ]
    records = []
    for index, command in enumerate(commands):
        log = Path(f'/tmp/worklane53-release-{index}.log')
        with log.open('w') as stream:
            completed = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        records.append({'command': command, 'exit': completed.returncode, 'log': str(log),
                        'log_sha256': digest(log), 'tail': '\n'.join(log.read_text().splitlines()[-8:])})
        print(json.dumps(records[-1]), flush=True)
    from test_native_integration import NativeIntegrationTests
    from test_native_installation import NativeInstallationTests
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (NativeIntegrationTests, NativeInstallationTests))
    result = unittest.TextTestRunner(verbosity=2, resultclass=ObservedResult).run(suite)
    cli_evidence = json.loads(cli_report.read_text()) if cli_report.exists() else {}
    tui_evidence = json.loads(tui_report.read_text()) if tui_report.exists() else {}
    groups = list(runtime['core_scenarios'])
    cli_ready = (records[0]['exit'] == 0 and bool(cli_evidence.get('results'))
                 and all(row['status'] == 'pass' for row in cli_evidence['results'])
                 and cli_evidence['candidate_sha256'] == binary_hashes[str(cli)])
    groups.append({'id': 'core.10.native-cli', 'status': 'pass' if cli_ready else 'fail',
                   'report': str(cli_report), 'sha256': digest(cli_report) if cli_report.exists() else None})
    for number in (11, 12):
        groups.append({'id': f'core.{number}.native-tui',
                       'status': 'pass' if records[1]['exit'] == 0 and tui_evidence.get('core_tui_ready')
                       and tui_evidence['candidate_sha256'] == binary_hashes[str(tui)] else 'fail',
                       'report': str(tui_report), 'sha256': digest(tui_report) if tui_report.exists() else None})
    integration_ready = result.wasSuccessful() and not result.skipped and result.testsRun == 7
    for number in (13, 14):
        groups.append({'id': f'core.{number}.staged-install-lifecycle',
                       'status': 'pass' if integration_ready else 'fail',
                       'assertions': [row['id'] for row in result.observations],
                       'production_cutover': 'pending reviewed merge and operator installation'})
    unchanged = binary_hashes == {str(p): digest(p) for p in (cli, tui)}
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    ready = (runtime_ready and unchanged and identity.get('runtime') == 'native'
             and identity.get('revision') == source and identity.get('dirty') is False
             and all(row['status'] == 'pass' for row in groups))
    report = {'candidate_revision': source, 'reference': runtime['reference'],
              'validation_base': runtime['validation_base'], 'candidate_identity': identity,
              'candidate_hashes': binary_hashes, 'input_hashes': current_inputs,
              'driver': 'tools/native_release.py', 'command': ['uv', 'run', '--no-project', 'python', *sys.argv],
              'runtime_evidence_sha256': digest(runtime_path), 'commands': records,
              'core_runtime_ready': runtime_ready, 'core_candidate_ready': ready,
              'core_release_ready': False, 'exhaustive_parity_complete': False,
              'core_scenarios': groups, 'observed_tests': result.observations,
              'independent_review': 'pending serialized reviewer',
              'production_cutover': 'pending reviewed merge; neither production command path replaced',
              'limits': ['Controlled GitHub/providers; no live writes or provider turns.',
                         'Installed backlog lifecycle recovers a saved build, then runs controlled independent reviewer/finalizer turns through the native controller.',
                         'Drained dual-path installer uses disposable commands and WAL state; actual operator cutover remains required.',
                         'Optional legacy uv installation and live OpenCode probes remain skipped.',
                         'Deferred: exhaustive inventory/source/failure audits, rich colors, walkthrough, cursor editing, PR badges and fuzzy search.']}
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: report[key] for key in ('core_runtime_ready', 'core_candidate_ready', 'core_release_ready', 'exhaustive_parity_complete')}))
    return 0 if ready else 1


if __name__ == '__main__':
    raise SystemExit(main())
