#!/usr/bin/env python3
"""Route fixture fetch/push operations to the disposable local upstream."""
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(os.environ['PARITY_ROOT'])
args = sys.argv[1:]
if args[:1] in (['fetch'], ['push']):
    if len(args) < 2 or args[1] != 'origin':
        print('Fixture only permits network operations against origin', file=sys.stderr)
        sys.exit(96)
    with (root / 'git-calls.jsonl').open('a') as log:
        log.write(json.dumps({'argv':args,'cwd':os.getcwd()}) + '\n')
    args = ['-c', 'url.' + (root / 'upstream.git').as_uri() + '.insteadOf=https://github.com/owner/repo.git', *args]
raise SystemExit(subprocess.call([os.environ['PARITY_GIT'], *args]))
