#!/usr/bin/env python3
"""Strict, offline fake gh/agent transport; fixture format is language neutral."""
import fcntl
import json
import os
from pathlib import Path
import sys

root = Path(os.environ['PARITY_ROOT'])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
# CLI protocols only read stdin when requested; do not block on a TUI's terminal.
body = sys.stdin.read() if '--input' in args or '--body-file' in args or args[-1:] == ['-'] or (name == 'opencode' and args[:1] == ['run']) else ''
with (root / 'transport.lock').open('w') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    plan = json.loads((root / 'transport.json').read_text())
    cursor_path = root / 'cursor.json'
    cursors = json.loads(cursor_path.read_text()) if cursor_path.exists() else {}
    cursor = cursors.get(name, 0)
    rules = plan.get(name, [])
    event = {'tool': name, 'argv': args, 'stdin': body, 'cwd': os.getcwd()}
    with (root / 'calls.jsonl').open('a') as log:
        log.write(json.dumps(event, ensure_ascii=False) + '\n')
    if cursor >= len(rules):
        with (root / 'transport-errors.jsonl').open('a') as errors:
            errors.write(json.dumps(event) + '\n')
        print('Unscripted transport call: ' + json.dumps(event), file=sys.stderr)
        sys.exit(97)
    rule = rules[cursor]
    if args[:len(rule.get('prefix', []))] != rule.get('prefix', []) or any(s not in body for s in rule.get('stdin_contains', [])) or ('argv' in rule and args != rule['argv']):
        with (root / 'transport-errors.jsonl').open('a') as errors:
            errors.write(json.dumps(event) + '\n')
        print('Transport mismatch: ' + json.dumps(event), file=sys.stderr)
        sys.exit(98)
    cursors[name] = cursor + 1
    cursor_path.write_text(json.dumps(cursors))
for flag, content in rule.get('write_flags', {}).items():
    Path(args[args.index(flag) + 1]).write_text(content)
sys.stdout.write(rule.get('stdout', ''))
sys.stderr.write(rule.get('stderr', ''))
sys.exit(rule.get('exit', 0))
