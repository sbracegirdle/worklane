#!/usr/bin/env python3
"""Find specific acceptance checks that detect deletion of each pinned raise."""
import argparse
import ast
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

if __package__:
    from .port_parity import ROOT, execute, reference
else:
    from port_parity import ROOT, execute, reference


def failure_sites(inventory, root):
    sites = {}
    for path in ('worklane.py', 'worklane_tui.py', 'worklane_harness.py', 'worklane_process.py'):
        source = (root / path).read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Raise):
                sites[f'failure.{path}.{node.lineno}'] = (path, node.lineno, node.end_lineno)
    return sites


def suppress(root, site):
    path, start, end = site
    target = root / path
    lines = target.read_text().splitlines(keepends=True)
    indent = lines[start - 1][:len(lines[start - 1]) - len(lines[start - 1].lstrip())]
    lines[start - 1:end] = [indent + 'pass  # parity mutation: missing failure\n'] + ['\n'] * (end - start)
    target.write_text(''.join(lines))
    # All copies must compile the mutation, even on same-second/same-size edits.
    shutil.rmtree(root / '__pycache__', ignore_errors=True)


def command(root, kind):
    if kind == 'runtime':
        return [sys.executable, str(ROOT / 'tools/port_reference_driver.py'), '--source', str(root)]
    return [sys.executable, str(root / 'worklane.py')]


def fixture_hits(fixture, kind, root, sites):
    locations = {f'{p}:{line}': key for key, (p,line,_) in sites.items()}
    hook = '''import json,os,sys
locations = %r
def trace(frame,event,arg):
 if event == 'call': frame.f_trace_lines=True
 if event in ('exception','line'):
  key=frame.f_code.co_filename.rsplit('/',1)[-1]+':'+str(frame.f_lineno)
  if key in locations:
   with open(os.environ['PARITY_ROOT']+'/failure-trace.jsonl','a') as output: output.write(json.dumps(locations[key])+'\\n')
 return trace if frame.f_code.co_filename.rsplit('/',1)[-1] in ('worklane.py','worklane_tui.py','worklane_harness.py','worklane_process.py') else None
sys.settrace(trace)
''' % locations
    traced = copy.deepcopy(fixture)
    traced.setdefault('files', {})['repo/sitecustomize.py'] = hook
    traced.setdefault('env', {})['PYTHONPATH'] = '{repo}'
    traced.setdefault('observe_files', []).append('failure-trace.jsonl')
    observed = execute(command(root, kind), traced)
    encoded = observed['files']['failure-trace.jsonl']
    return {json.loads(line) for line in base64.b64decode(encoded).decode().splitlines()} if encoded else set()


UNIT_TRACE = r'''
import ast,json,sys,threading,unittest
from pathlib import Path
locations={}
for path in ('worklane.py','worklane_tui.py','worklane_harness.py','worklane_process.py'):
 for node in ast.walk(ast.parse(Path(path).read_text())):
  if isinstance(node,ast.Raise): locations[(path,node.lineno)]=f'failure.{path}.{node.lineno}'
hits={}; active=None
def trace(frame,event,arg):
 if event=='call': frame.f_trace_lines=True
 if event in ('exception','line') and active:
  key=(frame.f_code.co_filename.rsplit('/',1)[-1],frame.f_lineno)
  if key in locations: hits.setdefault(locations[key],set()).add(active)
 return trace if frame.f_code.co_filename.rsplit('/',1)[-1] in ('worklane.py','worklane_tui.py','worklane_harness.py','worklane_process.py') else None
class Record(unittest.TextTestResult):
 def startTest(self,test):
  global active
  active=test.id();super().startTest(test)
 def stopTest(self,test):
  global active
  super().stopTest(test);active=None
sys.settrace(trace);threading.settrace(trace)
result=unittest.TextTestRunner(verbosity=0,resultclass=Record).run(unittest.defaultTestLoader.loadTestsFromNames(json.loads(sys.argv[1])))
sys.settrace(None);threading.settrace(None)
Path(sys.argv[2]).write_text(json.dumps({k:sorted(v) for k,v in hits.items()}))
raise SystemExit(not result.wasSuccessful())
'''


UNIT_RUNNER = r'''
import json,sys,unittest
from pathlib import Path
class Record(unittest.TextTestResult):
 def __init__(self,*args): super().__init__(*args); self.detected=[]
 def addFailure(self,test,error): super().addFailure(test,error); self.detected.append(test.id())
 def addError(self,test,error): super().addError(test,error); self.detected.append(test.id())
 def addSubTest(self,test,subtest,error):
  super().addSubTest(test,subtest,error)
  if error is not None: self.detected.append(test.id())
result=unittest.TextTestRunner(verbosity=0,resultclass=Record).run(unittest.defaultTestLoader.loadTestsFromNames(json.loads(sys.argv[1])))
Path(sys.argv[2]).write_text(json.dumps(result.detected))
'''


def mutation_checks(root, site, tests, fixtures, expected):
    detected = []
    with tempfile.TemporaryDirectory(prefix='worklane-mutant-') as directory:
        mutant = Path(directory)
        shutil.copytree(root, mutant, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
        suppress(mutant, site)
        for identifier, (kind, fixture) in fixtures.items():
            try:
                actual = execute(command(mutant,kind), fixture)
                changed = actual != expected[identifier]
            except Exception:
                changed = True
            if changed:
                return ['fixture:' + identifier]
        if tests:
            output = mutant / 'detected.json'
            process = subprocess.Popen([sys.executable,'-c',UNIT_RUNNER,json.dumps(tests),str(output)], cwd=mutant,
                                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            try:
                process.wait(timeout=30)
                if output.exists():
                    detected.extend(json.loads(output.read_text()))
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                # Do not claim a specific assertion based on a timed-out group.
    return sorted(set(detected))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', help='Git revision to audit when refreshing the reference pin')
    parser.add_argument('--unit-hits', type=Path, help='Reuse exception tracing map from pinned unittest execution')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--previous', type=Path, help='Retain verified rows; retry only gaps')
    args = parser.parse_args()
    inventory = json.loads((ROOT / 'parity/inventory.json').read_text())
    if args.revision:
        inventory['revision'] = subprocess.check_output(['git','rev-parse',args.revision],cwd=ROOT,text=True).strip()
    unit_hits = json.loads(args.unit_hits.read_text()) if args.unit_hits else None
    fixtures = {}
    for path in sorted((ROOT / 'parity/fixtures').glob('*.json')):
        data = json.loads(path.read_text())
        for fixture in data['scenarios']:
            fixtures[fixture['id']] = (data.get('reference_kind','cli'), fixture)
    prior = json.loads(args.previous.read_text()) if args.previous else {}
    if prior and prior.get('reference') != inventory['revision']:
        raise ValueError('Previous audit used another reference')
    previous = prior.get('failures', {})
    report = {'fixture_hashes':{name:hashlib.sha256(json.dumps(fixture,sort_keys=True,ensure_ascii=False).encode()).hexdigest() for name,(_,fixture) in fixtures.items()},
              'harness_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [*(ROOT/'tools').glob('*.py'), ROOT/'parity/fixture-schema.json'] if p.name in ('port_parity.py','port_reference_driver.py','port_failure_cases.py','port_security_cases.py','parity_tool.py','parity_git.py','fixture-schema.json')},
              'reference':inventory['revision'], 'mutation':'Replace the complete raise statement with pass; retain line numbers', 'failures':{}}
    with tempfile.TemporaryDirectory(prefix='worklane-failure-oracle-') as directory:
        root = Path(directory)
        reference(root, inventory['revision'])
        inventory['checks'] = {f'{path.stem}.{cls.name}.{method.name}':{}
                               for path in root.glob('test_*.py') for cls in ast.parse(path.read_text()).body
                               if isinstance(cls,ast.ClassDef) for method in cls.body
                               if isinstance(method,ast.FunctionDef) and method.name.startswith('test_')}
        sites = failure_sites(inventory, root)
        if unit_hits is None:
            hits_path = root / 'unit-hits.json'
            subprocess.run([sys.executable,'-c',UNIT_TRACE,json.dumps(sorted(inventory['checks'])),str(hits_path)],cwd=root,check=True,stdout=subprocess.DEVNULL)
            unit_hits = json.loads(hits_path.read_text())
        expected, hits = {}, {}
        for identifier, (kind, fixture) in fixtures.items():
            expected[identifier] = execute(command(root,kind), fixture)
            for row in fixture_hits(fixture,kind,root,sites):
                hits.setdefault(row, []).append(identifier)
        for identifier, site in sites.items():
            cached = previous.get(identifier, {})
            cached_checks = cached.get('checks', [])
            valid_cache = cached.get('status') == 'detected' and cached.get('source_sha256') == hashlib.sha256((root/site[0]).read_bytes()).hexdigest()
            for check in cached_checks:
                if check.startswith('fixture:'):
                    name = check.removeprefix('fixture:')
                    valid_cache = (valid_cache and prior.get('harness_hashes') == report['harness_hashes']
                                   and name in report['fixture_hashes'] and prior.get('fixture_hashes', {}).get(name) == report['fixture_hashes'][name])
                else:
                    valid_cache = valid_cache and check in inventory['checks']
            if valid_cache and cached_checks:
                report['failures'][identifier] = cached
                continue
            selected = {name:fixtures[name] for name in hits.get(identifier, [])}
            checks = mutation_checks(root,site,unit_hits.get(identifier,[]),selected,expected)
            path, start, end = site
            report['failures'][identifier] = {'source':f'{path}:{start}', 'source_sha256':hashlib.sha256((root / path).read_bytes()).hexdigest(),
                                              'checks':checks, 'status':'detected' if checks else 'gap'}
            print(identifier, len(checks), flush=True)
            args.output.write_text(json.dumps(report,indent=2)+'\n')
    gaps = [row for row,value in report['failures'].items() if not value['checks']]
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'failures':len(sites),'detected':len(sites)-len(gaps),'gaps':gaps}))
    return int(bool(gaps))


if __name__ == '__main__':
    raise SystemExit(main())
