#!/usr/bin/env python3
"""Freeze the merged executable oracle and its reviewable acceptance index."""
import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ('worklane.py', 'worklane_tui.py', 'worklane_harness.py', 'worklane_process.py')


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()


def build(revision):
    files = git('ls-tree', '-r', '--name-only', revision).splitlines()
    files = [p for p in files if p.endswith('.py') or p.startswith('docs/') or p in ('README.md', 'pyproject.toml', 'uv.lock')]
    texts = {p: subprocess.check_output(['git', 'show', f'{revision}:{p}'], cwd=ROOT).decode() for p in files}
    checks, sources, rows = {}, {}, []
    for path, source in texts.items():
        tree = ast.parse(source) if path.endswith('.py') else None
        sources[path] = {'sha256': hashlib.sha256(source.encode()).hexdigest()}
        if path.startswith('test_'):
            for cls in tree.body:
                if not isinstance(cls, ast.ClassDef):
                    continue
                for test in cls.body:
                    if isinstance(test, ast.FunctionDef) and test.name.startswith('test_'):
                        name = f'{Path(path).stem}.{cls.name}.{test.name}'
                        checks[name] = {'source': f'{path}:{test.lineno}', 'end': test.end_lineno,
                                        'contract': test.name.removeprefix('test_').replace('_', ' ')}
                        stage = 'tui' if path in ('test_tui.py', 'test_tui_editing.py') or 'tui' in test.name or 'walkthrough' in test.name else 'cli' if path in ('test_add_issue.py', 'test_installation.py') or 'cli_' in test.name else 'runtime'
                        rows.append({'id': name, 'surface': checks[name]['contract'], 'stage': stage,
                                     'source': checks[name]['source'], 'checks': [name]})
        elif tree:
            symbols = []
            def visit(nodes, prefix=''):
                for node in nodes:
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                        name = prefix + node.name
                        symbols.append({'name': name, 'line': node.lineno, 'end': node.end_lineno})
                        visit(node.body, name + '.')
            visit(tree.body)
            sources[path]['symbols'] = symbols
        else:
            sources[path]['headings'] = [line for line in source.splitlines() if line.startswith('#')]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for path in PRODUCTION:
            (root / path).write_text(texts[path])
        # Import exactly the archived reference, even when the working tree has edits.
        script = "import json, worklane as w; print(json.dumps({'config': w.CONFIG_DEFAULTS, 'parser': [{'dest': a.dest, 'flags': a.option_strings, 'default': a.default, 'choices': list(a.choices) if a.choices is not None else None, 'nargs': a.nargs, 'type': a.type.__name__ if a.type else None, 'help': a.help} for a in w.parser()._actions]}))"
        surfaces = json.loads(subprocess.check_output([sys.executable, '-c', script], cwd=root, text=True))
    def suite(module):
        return ['suite:' + module]
    # Source symbols retain exact oracle spans; mapped suites include all boundary/failure assertions.
    owners = {'worklane_tui.py': ('tui', ['test_tui','test_tui_editing','test_config']),
              'worklane_harness.py': ('runtime', ['test_harness','test_sessions']),
              'worklane_process.py': ('runtime', ['test_stop','test_harness']),
              'worklane.py': ('runtime', ['test_worklane','test_sessions','test_human_feedback','test_dependencies','test_api_budget','test_stacking','test_stop','test_config','test_add_issue','test_installation'])}
    for path in PRODUCTION:
        stage, modules = owners[path]
        for symbol in sources[path]['symbols']:
            if '.' in symbol['name'] and symbol['name'].split('.')[0][0].islower():
                continue  # Nested implementation helpers belong to their outer contract.
            rows.append({'id': 'source.' + path + '.' + symbol['name'], 'surface': symbol['name'] + ': preserve full pinned source semantics',
                         'source': f"{path}:{symbol['line']}", 'stage': stage,
                         'checks': [name for module in modules for name in suite(module)]})
    for option in surfaces['parser']:
        rows.append({'id': 'parser.' + option['dest'], 'surface': option,
                     'stage': 'cli', 'source': 'worklane.py:parser',
                     'checks': suite('test_config') + suite('test_add_issue') + suite('test_worklane') + suite('test_harness') + suite('test_installation')})
    for key, default in surfaces['config'].items():
        rows.append({'id': 'config.' + key, 'surface': {'setting':key, 'default':default, 'precedence':['explicit CLI','checkout .worklane.toml','built-in default'], 'save':'atomic confirmation only'},
                     'stage':'runtime', 'source':'worklane.py:validate_config', 'checks':suite('test_config') + suite('test_stacking') + suite('test_harness')})
    tree = ast.parse(texts['worklane_tui.py'])
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and any(isinstance(n, ast.Name) and n.id == 'key' for n in ast.walk(node.test)):
            condition = ast.get_source_segment(texts['worklane_tui.py'], node.test)
            rows.append({'id':f'tui.key.{node.lineno}', 'surface':condition, 'stage':'tui', 'source':f'worklane_tui.py:{node.lineno}', 'checks':suite('test_tui') + suite('test_tui_editing') + suite('test_config')})
    for path in PRODUCTION:
        stage, modules = owners[path]
        tree = ast.parse(texts[path])
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and node.exc is not None:
                rows.append({'id':f'failure.{path}.{node.lineno}', 'surface':ast.get_source_segment(texts[path], node),
                             'source':f'{path}:{node.lineno}', 'stage':stage, 'checks':['suite:' + m for m in modules]})
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names = [n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)]
                for name in names:
                    rows.append({'id':f'constant.{path}.{name}', 'surface':ast.get_source_segment(texts[path], node),
                                 'source':f'{path}:{node.lineno}', 'stage':stage, 'checks':['suite:' + m for m in modules]})
    for path in texts:
        if not path.endswith('.py'):
            rows.append({'id':'document.' + path, 'surface':sources[path], 'source':path, 'stage':'cutover', 'checks':['suite:' + Path(p).stem for p in files if p.startswith('test_')]})
    fixtures = [f for path in sorted((ROOT / 'parity/fixtures').glob('*.json')) for f in json.loads(path.read_text())['scenarios']]
    for fixture in fixtures:
        rows.append({'id':'fixture.' + fixture['id'], 'surface': fixture['id'], 'stage':'tui' if fixture['id'].startswith('tui.') else 'runtime' if fixture['id'].startswith('runtime.') else 'cli', 'source':'parity/fixtures/runtime.json' if fixture['id'].startswith('runtime.') else 'parity/fixtures/cli.json', 'checks':['fixture:' + fixture['id']]})
    return {'schema':1, 'revision':revision, 'planning_revision':'ed9b43bdb6ce25ce9339271ffa986446105f75dd',
            'language':'Rust', 'fixture_sources':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT / 'parity/fixtures').glob('*.json'))}, 'suites':{Path(p).stem:sorted(n for n in checks if n.startswith(Path(p).stem + '.')) for p in files if p.startswith('test_')}, 'sources':sources, 'checks':checks, 'parser':surfaces['parser'], 'config':surfaces['config'], 'matrix':rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', default='origin/main')
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    revision = git('rev-parse', args.revision)
    inventory = build(revision)
    path = ROOT / 'parity/inventory.json'
    if args.write:
        path.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + '\n')
        lines = ['# Parity matrix', '', f'Reference: `{revision}`. Generated by `tools/port_inventory.py`; detailed defaults and source spans are in `inventory.json`.', '', '| ID / executable acceptance check | Contract | Owning stage |', '| --- | --- | --- |']
        for row in inventory['matrix']:
            surface = row['surface'] if isinstance(row['surface'], str) else json.dumps(row['surface'], ensure_ascii=False)
            if row['id'].startswith(('source.', 'tui.key.', 'failure.', 'constant.', 'document.')):
                continue  # Full source/key index remains machine-readable, not thousands of repeated suite names.
            lines.append('| `' + row['id'] + '` | ' + surface.replace('|', '\\|').replace('\n',' ') + ' | ' + row['stage'] + ' |')
        (ROOT / 'parity/matrix.md').write_text('\n'.join(lines) + '\n')
    else:
        if inventory != json.loads(path.read_text()):
            raise SystemExit('Reference drift: review new merged behavior, regenerate inventory and rerun evidence.')
    print(json.dumps({'revision':revision, 'files':len(inventory['sources']), 'checks':len(inventory['checks']), 'rows':len(inventory['matrix'])}))


if __name__ == '__main__':
    main()
