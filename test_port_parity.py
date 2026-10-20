"""Differential infrastructure must reject missing behavior and failed semantics."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.port_parity import ROOT, execute, normalize
import worklane as w
import worklane_tui as tui
from test_tui import Screen
import curses


class DifferentialTests(unittest.TestCase):
    def test_identical_controlled_transport_and_literal_unicode_input(self):
        command = [sys.executable, str(ROOT / 'worklane.py')]
        scenarios = json.loads((ROOT / 'parity/fixtures/cli.json').read_text())['scenarios']
        fixture = next(s for s in scenarios if s['id'] == 'cli.add-ready')
        self.assertEqual(execute(command, fixture), execute(command, fixture))

    def test_missing_output_and_changed_failure_exit_are_rejected(self):
        for script, step in [('pass', {'stdout_contains':['required']}), ('raise SystemExit(0)', {'exit':1})]:
            with self.subTest(script=script), self.assertRaises(AssertionError):
                execute([sys.executable,'-c',script], {'id':'negative','steps':[step]})

    def test_unconsumed_and_unexpected_transport_calls_are_rejected(self):
        with self.assertRaises(AssertionError):
            execute([sys.executable,'-c','pass'], {'id':'unused','steps':[{}], 'transport':{'gh':[{'stdout':'{}'}]}})
        script = "import subprocess; raise SystemExit(subprocess.call(['gh','bad']))"
        with self.assertRaises(AssertionError):
            execute([sys.executable,'-c',script], {'id':'unexpected','steps':[{}]})

    def test_suppressed_unscripted_transport_error_still_fails_fixture(self):
        script = "import subprocess; subprocess.run(['gh','bad'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
        with self.assertRaisesRegex(AssertionError,'transport call'):
            execute([sys.executable,'-c',script], {'id':'suppressed','steps':[{}]})

    def test_timeouts_clean_processes_and_fixture_paths_cannot_escape(self):
        import subprocess
        with self.assertRaises(subprocess.TimeoutExpired):
            execute([sys.executable,'-c','import time; time.sleep(30)'], {'id':'timeout','steps':[{'timeout':.1}]})
        with self.assertRaises(ValueError):
            execute([sys.executable,'-c','pass'], {'id':'escape','files':{'../escape':'bad'},'steps':[]})

    def test_fetch_and_push_use_only_disposable_local_upstream(self):
        script = "import subprocess; subprocess.run(['git','fetch','origin'],check=True); subprocess.run(['git','push','origin','main'],check=True)"
        observed = execute([sys.executable,'-c',script], {'id':'git','steps':[{}]})
        self.assertEqual([call['argv'][:2] for call in observed['git_calls']], [['fetch','origin'], ['push','origin']])
        self.assertIn('file://<SANDBOX>/upstream',observed['steps'][0]['stderr'])

    def test_timeout_cleans_detached_descendant_after_parent_has_exited(self):
        import os
        import signal
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'pid'
            script = f"import os,time; pid=os.fork(); os._exit(0) if pid else None; os.setsid(); open({str(marker)!r},'w').write(str(os.getpid())); time.sleep(30)"
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    execute([sys.executable,'-c',script], {'id':'orphan','steps':[{'timeout':.2}]})
                self.assertIsNone(w.process_identity(int(marker.read_text())))
            finally:
                if marker.exists() and w.process_identity(int(marker.read_text())):
                    os.kill(int(marker.read_text()),signal.SIGKILL)

    def test_only_exact_sandbox_path_is_normalized(self):
        self.assertEqual(normalize({'error':'/tmp/fixture/界 missing session 123 at 2026-10-20'}, Path('/tmp/fixture')),
                         {'error':'<SANDBOX>/界 missing session 123 at 2026-10-20'})

    def test_pty_resize_unicode_and_signal_exit_are_observed(self):
        script = "import signal,sys,time; signal.signal(signal.SIGTERM,lambda *_: sys.exit(7)); print('ready 界',flush=True); time.sleep(30)"
        observed = execute([sys.executable,'-c',script], {'id':'terminal','steps':[{'exit':7,'terminal':[{'after':'ready 界','size':[18,70],'signal':'SIGTERM'}]}]})
        self.assertIn('ready 界', observed['steps'][0]['stdout'])

    def test_tui_uppercase_stop_uses_immediate_control_and_lowercase_drains(self):
        with tempfile.TemporaryDirectory() as directory:
            state = w.State('owner/repo', directory)
            dashboard = tui.Dashboard(curses, state, 'owner/repo', .1, root=Path(directory))
            with patch.object(curses,'curs_set'), patch.object(curses,'has_colors',return_value=False), patch.object(dashboard,'control') as control:
                dashboard.run(Screen([ord('X'),ord('x'),ord('q')]))
            self.assertEqual([c.args[0] for c in control.call_args_list], ['stop-now','stop'])


class FailureContractTests(unittest.TestCase):
    def test_every_failure_has_a_specific_deletion_detecting_acceptance_check(self):
        inventory=json.loads((ROOT/'parity/inventory.json').read_text())
        proof=json.loads((ROOT/'parity/failure-checks.json').read_text())
        for row in inventory['matrix']:
            if not row['id'].startswith('failure.'):
                continue
            with self.subTest(row=row['id']):
                evidence=proof['failures'][row['id']]
                self.assertEqual(evidence['status'],'detected')
                self.assertEqual(row['checks'],evidence['checks'])
                self.assertTrue(row['checks'])
                self.assertFalse(any(check.startswith('suite:') for check in row['checks']))

    def test_terminal_initialization_fixture_rejects_suppressed_failure(self):
        from tools.port_failure_audit import suppress
        from tools.port_parity import reference
        fixture=next(f for f in json.loads((ROOT/'parity/fixtures/runtime.json').read_text())['scenarios']
                     if f['id']=='runtime.terminal-initialization-error')
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)
            reference(source,json.loads((ROOT/'parity/inventory.json').read_text())['revision'])
            command=[sys.executable,str(ROOT/'tools/port_reference_driver.py'),'--source',str(source)]
            self.assertEqual(execute(command,fixture)['steps'][0]['exit'],1)
            import ast
            failure=next(node for node in ast.walk(ast.parse((source/'worklane_tui.py').read_text()))
                         if isinstance(node,ast.Raise) and any(isinstance(part,ast.Constant) and isinstance(part.value,str)
                         and part.value.startswith('Could not initialize terminal UI:') for part in ast.walk(node)))
            suppress(source,('worklane_tui.py',failure.lineno,failure.end_lineno))
            with self.assertRaisesRegex(AssertionError,'exit 0'):
                execute(command,fixture)


class EvidenceGateTests(unittest.TestCase):
    def inventory(self):
        return {'revision':'pinned','checks':{'case':{}},'suites':{'runtime':['case']},'matrix':[{'id':'source.runtime','checks':['suite:runtime']}]}

    def report(self):
        from tools.port_gate import contract_hash
        return {'mode':'differential','reference_verified':True,'inventory_sha256':contract_hash(self.inventory()),'reference':'pinned','candidate':['/binary'],'candidate_hashes':{'/binary':'hash'},
                'release_sha256':'hash','results':[{'id':'case','status':'pass','comparison':'equal','observations':{'exact':'value'}}],
                'source_audits':[{'row':'source.runtime','artifact_sha256':'artifact','commands':[['check']], 'unasserted_behaviors':[]}]}

    def test_reference_only_skips_or_missing_source_audits_never_establish_parity(self):
        from tools.port_gate import assess
        for kind in ('reference','skip','audit','revision','release','gap','override','observations','contract'):
            with self.subTest(kind=kind):
                report = self.report()
                if kind == 'reference': report['mode'] = 'reference-only'
                if kind == 'contract': report['inventory_sha256'] = 'stale inventory'
                if kind == 'override': report['reference_verified'] = False
                if kind == 'observations': report['results'][0].pop('observations')
                if kind == 'skip': report['results'][0]['status'] = 'skip'
                if kind == 'audit': report['source_audits'] = []
                if kind == 'revision': report['reference'] = 'stale'
                if kind == 'release': report.pop('release_sha256')
                if kind == 'gap': report['source_audits'][0]['unasserted_behaviors'] = ['missing behavior']
                self.assertTrue(assess(self.inventory(), [report]))
        self.assertEqual(assess(self.inventory(), [self.report()]), [])

    def test_mixed_release_and_conflicting_check_results_block_cutover(self):
        from tools.port_gate import assess
        good, other = self.report(), self.report()
        other['release_sha256'] = 'different binary'
        self.assertTrue(assess(self.inventory(), [good,other]))
        other = self.report()
        other['results'][0]['status'] = 'fail'
        self.assertTrue(assess(self.inventory(), [good,other]))

    def test_fixture_identity_hash_and_case_membership_are_mandatory(self):
        import copy
        from tools.port_gate import assess, contract_hash
        inventory = {'revision':'pinned','checks':{},'suites':{},
                     'fixture_sources':{'cli.json':'cli-hash','runtime.json':'runtime-hash'},
                     'fixture_cases':{'cli.json':['fixture:cli.help'],'runtime.json':['fixture:runtime.config']},
                     'matrix':[{'id':'fixture.cli.help','checks':['fixture:cli.help']},
                               {'id':'fixture.runtime.config','checks':['fixture:runtime.config']}]}
        report = self.report()
        report.update(inventory_sha256=contract_hash(inventory),fixtures_path='cli.json',fixtures_sha256='cli-hash',
                      results=[{'id':'cli.help','status':'pass','comparison':'equal','observations':{'exit':0}}])
        self.assertEqual([f['row'] for f in assess(inventory,[report])], ['fixture.runtime.config'])
        for change in ('missing-path','missing-hash','missing-both','unknown-path','wrong-hash'):
            with self.subTest(change=change):
                forged = copy.deepcopy(report)
                if change == 'missing-path':
                    forged.pop('fixtures_path'); forged['fixtures_sha256'] = 'incorrect'
                if change == 'missing-hash': forged.pop('fixtures_sha256')
                if change == 'missing-both': forged.pop('fixtures_path'); forged.pop('fixtures_sha256')
                if change == 'unknown-path': forged['fixtures_path'] = 'other.json'
                if change == 'wrong-hash': forged['fixtures_sha256'] = 'incorrect'
                self.assertEqual(len(assess(inventory,[forged])),2)
        # A correctly hashed CLI report cannot attest to runtime fixture results.
        report['results'].append({'id':'runtime.config','status':'pass','comparison':'equal','observations':{'exit':0}})
        self.assertEqual([f['row'] for f in assess(inventory,[report])], ['fixture.runtime.config'])
        report['results'][1]['id'] = 'fixture:runtime.config'
        self.assertEqual([f['row'] for f in assess(inventory,[report])], ['fixture.runtime.config'])

    def test_real_cli_report_cannot_cover_cases_without_fixture_identity(self):
        import copy
        from tools.port_gate import assess
        inventory=json.loads((ROOT/'parity/inventory.json').read_text())
        report=json.loads((ROOT/'parity/evidence/cli-self-comparison.json').read_text())
        rows={row['id'] for row in inventory['matrix'] if row.get('source') == 'parity/fixtures/cli.json'}
        self.assertFalse(rows & {failure['row'] for failure in assess(inventory,[report])})
        forged=copy.deepcopy(report)
        forged.pop('fixtures_path')
        forged['fixtures_sha256']='incorrect'
        self.assertEqual(rows & {failure['row'] for failure in assess(inventory,[forged])},rows)

    def test_unknown_fixture_fields_and_bad_command_arrays_fail_closed(self):
        from tools.port_parity import command_array
        with self.assertRaises(ValueError):
            execute([sys.executable,'-c','pass'], {'id':'typo','steps':[{'exiit':1}]})
        for value in ('"binary"','[]','["relative"]','[1]'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                command_array(value)


if __name__ == '__main__':
    unittest.main()
