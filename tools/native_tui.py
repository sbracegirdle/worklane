#!/usr/bin/env python3
"""Record finite installed native TUI core evidence using the PTY assertions."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import termios
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['WORKLANE_TEST_RUST'] = '1'
from test_native_tui import BINARY, PROBE, NativeTuiTests

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases=[]
    def startTest(self, test):
        super().startTest(test)
        original = test.tearDown
        def capture():
            test.native_observations = self.observe(test)
            original()
        test.tearDown = capture
    def observe(self, test):
        observations={'saved_worker_session':test.state.session(1,'worker')['session_id'],
            'expected_worker_session':test.session_id, 'saved_phase':test.state.load(1)['phase'],
            'pending_worker_input':test.inputs(), 'gh_commands':[c['args'] for c in test.calls()]}
        if test.process is not None:
            observations.update(terminal_restored=termios.tcgetattr(test.slave)==test.original_terminal,
                exit=test.process.returncode, terminal_sha256=hashlib.sha256(test.output).hexdigest(),
                observed_terminal_tokens=[token for token in ['Overview','Logs #1','Fresh output after refresh',
                    'Second paginated comment','Input queued','Configuration saved','Cancelled',
                    'security preflight refused','Controller started','Stop requested','Session returned','Retry saved',
                    'Needle A','Needle B','Needle C','VISIBLE_TYPED_TAIL']
                    if token.encode() in test.output])
        return observations
    def addSuccess(self, test):
        super().addSuccess(test)
        self.cases.append({'id':test.id(), 'status':'pass', 'scenario':11 if '.test_group11_' in test.id() else 12,
                           'observations':test.native_observations})

if __name__ == '__main__':
    if len(sys.argv) != 2 or not os.environ.get('WORKLANE_TUI_BIN'):
        raise SystemExit('Usage: WORKLANE_TUI_BIN=/installed/bin/worklane-tui uv run --no-project python tools/native_tui.py REPORT.json')
    binary_hash=digest(BINARY)
    result=unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(unittest.defaultTestLoader.loadTestsFromTestCase(NativeTuiTests))
    paths=[*ROOT.glob('runtime/**/*.rs'), ROOT/'Cargo.toml',ROOT/'Cargo.lock',ROOT/'runtime/Cargo.toml',ROOT/'test_native_tui.py',ROOT/'tools/fixtures/tui_gh.py',Path(__file__).resolve(),ROOT/'worklane.py']
    hashes={str(p.relative_to(ROOT)):digest(p) for p in sorted(paths)}
    source=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    reference=json.loads((ROOT/'parity/inventory.json').read_text())['revision']
    assert digest(BINARY)==binary_hash, 'Candidate changed during PTY checks'
    assert digest(ROOT/'target/debug/worklane-tui')==binary_hash, 'Installed debug candidate differs from current native build'
    report={'reference':reference,'runtime_parent':'5cc71a5880dc77727987e4a496c53181f5e5d8f0',
        'candidate_source_revision':source,'candidate':str(BINARY.resolve()),'candidate_sha256':binary_hash,
        'controller_probe':str(PROBE),'controller_probe_sha256':digest(PROBE),
        'driver':'test_native_tui.py','input_hashes':hashes,
        'command':['uv','run','--no-project','python','tools/native_tui.py',sys.argv[1]],
        'environment':{'WORKLANE_TUI_BIN':str(BINARY),'WORKLANE_TEST_RUST':'1'},
        'results':result.cases,'tests_run':result.testsRun,'failures':len(result.failures),
        'errors':len(result.errors),'skipped':len(result.skipped),
        'core_tui_ready':result.wasSuccessful() and not result.skipped and {r['scenario'] for r in result.cases}=={11,12},
        'core_runtime_ready':None,'core_runtime_evidence':'Parent core groups 1–9 independently reviewed at runtime_parent; current regression results recorded in PR.',
        'core_release_ready':False,'exhaustive_parity_complete':False,'independent_review':'pending serialized reviewer',
        'limits':['Disposable installation only; active installations preserved.',
                  'Controlled gh/provider commands, real PTYs and native owned controller probe; no live GitHub writes or provider turns.',
                  '#53 owns combined CLI/TUI integration, drained installed cutover and full backlog lifecycle.',
                  'Deferred richer colors, first-use walkthrough, cursor editing, PR badges and fuzzy search.']}
    Path(sys.argv[1]).write_text(json.dumps(report,indent=2)+'\n')
    raise SystemExit(0 if report['core_tui_ready'] else 1)
