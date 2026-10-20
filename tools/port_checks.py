#!/usr/bin/env python3
"""Run every pinned Python acceptance check and publish individual outcomes."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from port_parity import ROOT, reference

RUNNER = r'''
import io, json, sys, unittest
class Evidence(unittest.TextTestResult):
 def __init__(self,*args): super().__init__(*args); self.rows=[]
 def addSuccess(self,test): super().addSuccess(test); self.rows.append({'id':test.id(),'status':'pass'})
 def addSkip(self,test,reason): super().addSkip(test,reason); self.rows.append({'id':test.id(),'status':'skip','reason':reason})
 def addError(self,test,error): super().addError(test,error); self.rows.append({'id':test.id(),'status':'error','detail':self._exc_info_to_string(error,test)})
 def addFailure(self,test,error): super().addFailure(test,error); self.rows.append({'id':test.id(),'status':'fail','detail':self._exc_info_to_string(error,test)})
checks=json.load(open(sys.argv[1]))['checks']
result=unittest.TextTestRunner(stream=sys.stderr,verbosity=0,resultclass=Evidence).run(unittest.defaultTestLoader.loadTestsFromNames(sorted(checks)))
json.dump({'checks':result.rows,'passed':len(result.rows)-len(result.skipped)-len(result.errors)-len(result.failures),'total':result.testsRun},open(sys.argv[2],'w'),indent=2)
raise SystemExit(not result.wasSuccessful())
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((ROOT / 'parity/inventory.json').read_text())
    with tempfile.TemporaryDirectory() as directory:
        reference(Path(directory), manifest['revision'])
        output = Path(directory) / 'checks.json'
        process = subprocess.run([sys.executable, '-c', RUNNER, str(ROOT / 'parity/inventory.json'), str(output)], cwd=directory)
        if not output.exists():
            raise SystemExit(process.returncode or 1)
        evidence = json.loads(output.read_text())
        evidence.update(reference=manifest['revision'], mode='reference-only')
        args.evidence.write_text(json.dumps(evidence,indent=2) + '\n')
        print(json.dumps({k:v for k,v in evidence.items() if k != 'checks'}))
        return process.returncode


if __name__ == '__main__':
    raise SystemExit(main())
