"""One focused campaign, requiring an exact separate owner grant before execution."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
PLAN = json.loads((OUT/'campaign-plan.json').read_text(encoding='utf-8'))
if PLAN['owner_approved'] is not True:
    raise SystemExit('No campaign grant recorded; source preparation is not execution permission')
manifest = json.loads((OUT/'syntax-and-source-manifest.json').read_text(encoding='utf-8'))
for item in manifest['files']:
    if hashlib.sha256((ROOT/item['path']).read_bytes()).hexdigest() != item['sha256']:
        raise SystemExit('Source changed since manifest; review before executing')
TEST_PYTHON = Path('D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe')
if not TEST_PYTHON.is_file():
    raise SystemExit('Existing test interpreter unavailable; no installation authorized')
with (OUT/'campaign-attempt.json').open('x', encoding='utf-8') as marker:
    json.dump({'started_utc': time.time(), 'attempt': 1, 'retry': False}, marker)
home, temp = OUT/'test-home', OUT/'test-temp'
for path in (home, temp, home/'appdata', home/'local', OUT/'test-runtime'):
    path.mkdir(parents=True, exist_ok=True)
allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'SYSTEMDRIVE', 'PROGRAMFILES',
           'PROGRAMFILES(X86)', 'PROGRAMW6432', 'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS'}
env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home/'appdata'),
           LOCALAPPDATA=str(home/'local'), TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp),
           HERMES_HOME=str(home), HERMES_PYTHON=str(TEST_PYTHON),
           HERMES_RUNTIME_DIR=str(OUT/'test-runtime'), HERMES_DISABLE_LAZY_INSTALLS='1',
           UV_OFFLINE='1', PIP_CONFIG_FILE=os.devnull, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
argv = ['C:/Program Files/Git/bin/bash.exe', 'scripts/run_tests.sh', '-j', '1',
        '--file-retries', '0', '--file-timeout', '60', *PLAN['files']]
receipt = {'argv': argv, 'attempt': 1, 'native_worker': False, 'model': False,
           'microphone': False, 'external_audio': False, 'paid_resources': False}
started = time.monotonic()
with (OUT/'campaign.log').open('x', encoding='utf-8') as log:
    try:
        completed = subprocess.run(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   timeout=PLAN['max_seconds'])
        receipt.update(exit=completed.returncode, outcome='FINISHED')
    except subprocess.TimeoutExpired:
        receipt.update(exit=None, outcome='TIMEOUT', child_quiescence='UNKNOWN; no retry')
receipt['seconds'] = round(time.monotonic()-started, 3)
(OUT/'campaign-result.json').write_text(json.dumps(receipt, indent=2)+'\n', encoding='utf-8')
print(json.dumps(receipt))
raise SystemExit(0 if receipt.get('exit') == 0 else 1)
