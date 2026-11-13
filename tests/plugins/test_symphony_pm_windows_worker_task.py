"""The same low-integrity worker can still do a bounded engineering task on its own.

Requires an explicitly approved native campaign; no model and no driver. The worker reads context
it may not change, works in its own copy, runs a tool as a child process, fixes what the tool
reports and reruns it. The owner side then checks the result independently of the worker's claim.
Review and repair across separate launches is not exercised: a namespace admits one launch.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import time

import pytest

from tests.plugins.symphony_native_crash_support import BoundedApi, NativeRig, base_python, save_proof
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')

BROKEN = 'def add(a, b):\n    return a - b\n'
FIXED = 'def add(a, b):\n    return a + b\n'
CHECK = ('import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n'
         'from module import add\nsys.exit(0 if add(2, 3) == 5 else 1)\n')
TASK = r'''
import hashlib, json, subprocess, sys
from pathlib import Path
source, copy, result = (Path(p) for p in sys.argv[1:4])
repo = copy/'repo'
repo.mkdir()
for name in ('module.py', 'check.py'):
    (repo/name).write_bytes((source/name).read_bytes())
def check():
    return subprocess.run([sys.executable, '-I', '-B', str(repo/'check.py')], cwd=str(repo),
                          capture_output=True, timeout=30).returncode
out = {'first_check': check()}
module = repo/'module.py'
module.write_text(module.read_text(encoding='utf-8').replace('a - b', 'a + b'), encoding='utf-8', newline='\n')
out['second_check'] = check()
try:
    (source/'module.py').write_text('tampered', encoding='utf-8')
    out['changed_source'] = True
except OSError:
    out['changed_source'] = False
out['module_sha256'] = hashlib.sha256(module.read_bytes()).hexdigest()
temporary = result.with_suffix('.tmp')
temporary.write_text(json.dumps(out), encoding='utf-8')
temporary.replace(result)
'''


def test_low_integrity_worker_fixes_a_defect_in_its_own_copy_without_touching_the_source(tmp_path):
    home, source = tmp_path/'profile', tmp_path/'source'
    home.mkdir()
    source.mkdir()
    (source/'module.py').write_text(BROKEN, encoding='utf-8', newline='\n')
    (source/'check.py').write_text(CHECK, encoding='utf-8', newline='\n')
    rig = NativeRig(home)
    rig.readable.append(source)  # context the worker may read and must not change
    rig.native.api = BoundedApi(4)  # root, console host, one tool child at a time, one spare
    result = rig.work/'result.json'
    rig.argv = [base_python(), '-I', '-B', '-c', TASK, str(source), str(rig.work), str(result)]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            rig.start(pool).result(timeout=60)
        wait_until(result.exists, timeout=5, what='worker result')
        claimed = json.loads(result.read_text(encoding='utf-8'))
        delivered = (rig.work/'repo'/'module.py').read_bytes()
        save_proof('worker-task', {'claimed': claimed, 'worker': rig.outcome,
                                   'delivered_sha256': hashlib.sha256(delivered).hexdigest()})
        assert rig.outcome == {'returncode': 0, 'timed_out': False}
        assert claimed['first_check'] == 1 and claimed['second_check'] == 0
        assert claimed['changed_source'] is False
        assert (source/'module.py').read_text(encoding='utf-8') == BROKEN
        # Independent of what the worker reported: the delivered file is the fix, byte for byte.
        assert delivered == FIXED.encode('utf-8')
        assert claimed['module_sha256'] == hashlib.sha256(delivered).hexdigest()
    finally:
        rig.close(time.monotonic()+10)
