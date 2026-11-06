"""Run existing mocked voice contracts via the canonical runner, with no installs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

out = Path(__file__).resolve().parent
root = out.parents[1]
env = {k: v for k, v in os.environ.items() if not any(part in k.upper() for part in ("TOKEN", "SECRET", "API_KEY", "AUTH_TOKEN")) and not k.startswith(("HERMES_", "_HERMES_"))}
for key, name in (("HOME", "test-home"), ("USERPROFILE", "test-home"), ("HERMES_HOME", "test-profile"), ("HERMES_RUNTIME_DIR", "test-runtime"), ("TEMP", "test-temp"), ("TMP", "test-temp"), ("APPDATA", "test-appdata"), ("LOCALAPPDATA", "test-localappdata")):
    path = out / name
    path.mkdir(exist_ok=True)
    env[key] = str(path)
(out / "test-profile/config.yaml").write_text("security:\n  allow_lazy_installs: false\nstt:\n  enabled: false\nvoice:\n  auto_tts: false\n  submit_mode: draft\n", encoding="utf-8")
env.update(HERMES_PYTHON=sys.executable, HERMES_DISABLE_LAZY_INSTALLS="1", HERMES_TEST_WORKERS="1", HERMES_TEST_FILE_RETRIES="0", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
args = ["C:/Program Files/Git/bin/bash.exe", "scripts/run_tests.sh", "tests/hermes_cli/test_voice_wrapper.py", "tests/tools/test_voice_cli_integration.py", "tests/tools/test_voice_stop_phrase.py", "-j", "1", "--file-retries", "0"]
started = time.perf_counter()
try:
    proc = subprocess.run(args, cwd=root, env=env, capture_output=True, timeout=45)
    (out / "canonical-runner.stdout.txt").write_bytes(proc.stdout)
    (out / "canonical-runner.stderr.txt").write_bytes(proc.stderr)
    receipt = {"argv": args, "cwd": str(root), "exit_code": proc.returncode, "elapsed_seconds": round(time.perf_counter()-started, 3), "microphone_opened": False, "audio_transmitted": False, "installations_requested": False}
except subprocess.TimeoutExpired:
    receipt = {"argv": args, "outcome": "TIMEOUT", "elapsed_seconds": round(time.perf_counter()-started, 3)}
(out / "canonical-runner.json").write_text(json.dumps(receipt, indent=2)+"\n", encoding="utf-8")
print(json.dumps(receipt))
