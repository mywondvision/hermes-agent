"""Record one authorized invocation of the unchanged reviewed restart probe."""
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

import psutil

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PROBE = ROOT / ".artifacts/live-smoke-20261106/controller_restart_probe.py"


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


with (OUT / "execution-once.json").open("x", encoding="utf-8") as marker:
    json.dump({"authorized": "current user: prepared C1->C2, no model or Codex workers",
               "started_at": time.time(), "automatic_retries": 0}, marker)
assert hashlib.sha256(PROBE.read_bytes()).hexdigest() == "000e90fa0d198f9494f82ff9f55dd606caca4b213cfd4de35f43129e63ef3057"
(OUT / "sandbox").mkdir(exist_ok=False)
command = [sys.executable, "-B", str(PROBE), "run", "--execute",
           "--directory", str(OUT / "sandbox")]
save("invocation.json", {"argv": command, "cwd": str(ROOT), "python": sys.version,
     "executable": sys.executable, "base_executable": sys._base_executable,
     "psutil": psutil.__version__, "platform": platform.platform(),
     "probe_sha256": hashlib.sha256(PROBE.read_bytes()).hexdigest(),
     "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
     "command_environment": "existing inherited environment; values not logged",
     "boundary": "synthetic Python root + child, no model, no Codex, no production",
     "monotonic_start": time.monotonic()})
seen = {}
started = time.time()
with (OUT / "probe.stdout.txt").open("wb") as stdout, (OUT / "probe.stderr.txt").open("wb") as stderr:
    process = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr)
    root_process = psutil.Process(process.pid)
    with (OUT / "process-observations.jsonl").open("x", encoding="utf-8") as log:
        while process.poll() is None:
            try:
                descendants = root_process.children(recursive=True)
            except psutil.NoSuchProcess:
                descendants = []
            for child in descendants:
                try:
                    identity = (child.pid, child.create_time())
                    if identity in seen:
                        continue
                    row = {"observed_at": time.time(), "pid": child.pid,
                           "ppid": child.ppid(), "created_at": identity[1], "name": child.name()}
                    seen[identity] = row
                    log.write(json.dumps(row) + "\n")
                    log.flush()
                except psutil.NoSuchProcess:
                    continue
            time.sleep(0.02)
    exit_code = process.wait()
save("invocation-result.json", {"exit_code": exit_code, "probe_pid": process.pid,
     "started_at": started, "finished_at": time.time(), "observed_descendants": len(seen),
     "automatic_retries": 0})
print(json.dumps({"exit_code": exit_code, "observed_descendants": len(seen), "evidence": str(OUT)}))
sys.exit(exit_code)
