"""Prepared bounded campaign. Execute only after the owner's separate no-model grant."""
import json
import os
from pathlib import Path
import subprocess
import time

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
HOME = OUT/'test-home'
TEMP = OUT/'test-temp'
TEST_PYTHON = Path('D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe')
if not TEST_PYTHON.is_file():
    raise SystemExit('Existing test interpreter unavailable; installation is not authorized')
for path in [HOME,TEMP,HOME/'appdata',HOME/'local']:
    path.mkdir(parents=True,exist_ok=True)
allowed = {'SYSTEMROOT','WINDIR','COMSPEC','PATH','PATHEXT','SYSTEMDRIVE','PROGRAMFILES',
           'PROGRAMFILES(X86)','PROGRAMW6432','PROCESSOR_ARCHITECTURE','NUMBER_OF_PROCESSORS'}
env = {k:v for k,v in os.environ.items() if k.upper() in allowed}
env.update(HOME=str(HOME),USERPROFILE=str(HOME),APPDATA=str(HOME/'appdata'),
           LOCALAPPDATA=str(HOME/'local'),TEMP=str(TEMP),TMP=str(TEMP),TMPDIR=str(TEMP),
           HERMES_HOME=str(HOME),HERMES_PYTHON=str(TEST_PYTHON),
           HERMES_RUNTIME_DIR=str(ROOT/'.artifacts/s4-global-voice-20261106/prepared/runtime'),
           HERMES_DISABLE_LAZY_INSTALLS='1',UV_OFFLINE='1',PIP_CONFIG_FILE=os.devnull,
           PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1')
paths = [
    'tests/hermes_cli/test_plugin_services.py',
    'tests/hermes_cli/test_web_server_plugin_services.py',
    'tests/plugins/test_symphony_pm_serving.py',
    'tests/tui_gateway/test_plugin_service_lifecycle.py',
    'tests/tools/test_symphony_pm_adapter.py',
    'tests/tools/test_symphony_pm_adapter_cancel_s2.py',
    'tests/plugins/test_symphony_pm_plugin.py',
    'tests/plugins/test_symphony_pm_ingress_offline.py',
]
argv = ['C:/Program Files/Git/bin/bash.exe','scripts/run_tests.sh','-j','1',
        '--file-retries','0','--file-timeout','60',*paths]
started = time.monotonic()
receipt = {'argv':argv,'scope':'one targeted no-model contract campaign; no native/paid/audio/production work',
           'model':False,'microphone':False,'native_worker':False}
with (OUT/'contract-campaign.log').open('w',encoding='utf-8') as log:
    try:
        process = subprocess.run(argv,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=600)
        receipt.update(exit=process.returncode,outcome='FINISHED')
    except subprocess.TimeoutExpired:
        receipt.update(exit=None,outcome='TIMEOUT',child_quiescence='UNKNOWN; do not retry')
receipt['seconds'] = round(time.monotonic()-started,3)
(OUT/'contract-campaign.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
print(json.dumps(receipt))
raise SystemExit(0 if receipt.get('exit') == 0 else 1)
