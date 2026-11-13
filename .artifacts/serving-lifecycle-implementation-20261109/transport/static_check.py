"""Stdlib-only source check. Does not import or execute project/test modules."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
paths = sorted((ROOT/'plugins/symphony_pm/transport').glob('*.py')) + [
    ROOT/'plugins/symphony_pm/service.py',
    ROOT/'tests/plugins/symphony_serving_support.py',
    *sorted((ROOT/'tests/plugins').glob('test_symphony_pm_journal.py')),
    ROOT/'tests/plugins/test_symphony_pm_lifecycle.py',
    ROOT/'tests/plugins/test_symphony_pm_permit_source.py',
    ROOT/'tests/plugins/test_symphony_pm_serving.py',
    OUT/'run-transport-campaign.py',
]
rows = []
for path in paths:
    raw = path.read_bytes()
    source = raw.decode('utf-8')
    tree = ast.parse(source, filename=str(path))
    compile(tree, str(path), 'exec')  # compilation only, no exec/import
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    imported_tests = [node.lineno for node in ast.walk(tree)
                      if isinstance(node, ast.ImportFrom) and node.module
                      and (node.module == 'tests' or node.module.startswith('tests.'))]
    if path.is_relative_to(ROOT/'plugins') and imported_tests:
        raise SystemExit(f'Fixture import in production source: {path}')
    if any(line.rstrip() != line for line in source.splitlines()):
        raise SystemExit(f'Trailing whitespace: {path}')
    units = [{'name': node.name, 'line': node.lineno, 'lines': node.end_lineno-node.lineno+1}
             for node in functions]
    if len(source.splitlines()) > 2000 or any(unit['lines'] > 300 for unit in units):
        raise SystemExit(f'Size threshold: {path}')
    rows.append({'path': path.relative_to(ROOT).as_posix(), 'sha256': hashlib.sha256(raw).hexdigest(),
                 'syntax': 'VALID', 'lines': len(source.splitlines()), 'functions': units,
                 'test_functions': sum(node.name.startswith('test_') for node in functions)})
global_diff = subprocess.run(['git', 'diff', '--check'], cwd=ROOT, capture_output=True, text=True, check=False)
(OUT/'worktree-diff-check.txt').write_text(global_diff.stdout+global_diff.stderr, encoding='utf-8')
diff = subprocess.run(['git', 'diff', '--check', '--', *[str(p.relative_to(ROOT)) for p in paths]],
                      cwd=ROOT, capture_output=True, text=True, check=False)
if diff.returncode:
    raise SystemExit(diff.stdout+diff.stderr)
old = json.loads((OUT.parent/'code/syntax-and-source-manifest.json').read_text(encoding='utf-8'))
changed_since_campaign = [item['path'] for item in old['files']
                          if hashlib.sha256((ROOT/item['path']).read_bytes()).hexdigest() != item['sha256']]
report = {'method': 'AST parse/compile, source sizes/hash scan, git diff --check; no project imports or tests',
          'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
          'project_execution': False, 'files': rows, 'scoped_diff_check': 'PASS',
          'worktree_diff_check_exit': global_diff.returncode,
          'worktree_diff_check_evidence': 'worktree-diff-check.txt',
          'changed_since_114_pass': changed_since_campaign}
(OUT/'syntax-and-source-manifest.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'files': len(rows), 'test_functions': sum(r['test_functions'] for r in rows),
                  'syntax': 'VALID', 'scoped_diff_check': 'PASS',
                  'worktree_diff_check_exit': global_diff.returncode,
                  'changed_since_114_pass': changed_since_campaign}))
