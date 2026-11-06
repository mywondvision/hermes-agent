"""Explicitly authorized retry + third overall attempt for cancel. Same owner DB.

The first failed exec, spent ticket and evidence are preserved. No automatic retry.
"""
import ctypes as C
import json
from pathlib import Path
import sys
import time

import smoke as s
from winjob import open_job, close_handle, open_process, creation_ticks, wait_handle

ORIGINAL = Path(__file__).resolve().parent
RUN = ORIGINAL / "approved-retry-1"
s.HERE = RUN
s.MAX_NATIVE_ATTEMPTS = 3
s.PREVIOUS_NATIVE_ATTEMPTS = 1
s.REQUEST_PREFIX = "smoke-approved-retry-1-request"


def predecessor_stopped():
    b = s.load(ORIGINAL / "attempt-1/binding.json")
    hp = open_process(0x100000 | 0x1000, False, b["pid"])
    if hp:
        try:
            # A reused PID is a different identity and is never stopped by this task.
            stopped = creation_ticks(hp) != b["start_ticks"] or wait_handle(hp, 0) == 0
        finally:
            close_handle(hp)
    else:
        stopped = C.get_last_error() == 87
    h = open_job(4, False, b["job_object"])
    if h:
        close_handle(h)
        raise RuntimeError("Previous native Job still exists; HOLD")
    assert C.get_last_error() == 2 and stopped
    return {"exact_root_not_running": True, "previous_job_absent": True, "at": time.time()}


def prepare():
    RUN.mkdir()
    assert s.DB == ORIGINAL / "owner.sqlite", "Do not create a second owner store"
    proof = predecessor_stopped()
    owner = s.LiveOwner()
    try:
        rows = [dict(r) for r in owner.db.execute("SELECT * FROM launches")]
        assert len(rows) == 1 and rows[0]["state"] == "FAILED_CONFIG_HOLD"
        assert owner.db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
    finally:
        owner.close()
    s.prepare()
    s.save(RUN / "approval.json", {
        "source": "current user reply + explicit third-attempt approval",
        "retry_reply": "Если ты исправил, то повторяй",
        "cancel_reply": "Да, разрешаю третью для отмены",
        "model": s.MODEL, "effort": s.EFFORT,
        "environment": "same current Windows host, existing Codex authentication",
        "max_native_attempts": 2, "max_native_attempts_overall": 3,
        "previous_native_attempts": 1, "automatic_retries": False,
        "scope": "one corrected read-only retry, then one cancel attempt only after first JSON verification",
        "production": False, "owner_store": str(s.DB)})
    s.save(RUN / "predecessor-readback.json", proof)


def run():
    # Parent exec invocations are completed. Retire the task-owned previous lease only
    # after exact native terminal readback, then claim a new epoch through Owner.claim.
    proof = predecessor_stopped()
    owner = s.LiveOwner()
    try:
        row = owner.db.execute("SELECT * FROM writers WHERE namespace=?", (s.NAMESPACE,)).fetchone()
        if row is not None and time.time() < row["expires"]:
            from tests.fixtures.symphony_pm_ingress.owner import Token
            old = Token(s.NAMESPACE, row["owner"], row["epoch"], row["expires"])
            with owner.transaction():
                owner._authorize(old, time.time())
                owner.db.execute("UPDATE writers SET expires=? WHERE namespace=? AND epoch=?",
                                 (time.time(), s.NAMESPACE, old.epoch))
        s.save(RUN / "predecessor-lease-retired.json", {"proof": proof, "old_writer": dict(row)})
    finally:
        owner.close()
    s.campaign()


def inspect():
    s.inspect()
    if (RUN / "first-verdict.json").exists():
        print(json.dumps(s.load(RUN / "first-verdict.json"), indent=2))
    if (RUN / "cancel-verdict.json").exists():
        print(json.dumps(s.load(RUN / "cancel-verdict.json"), indent=2))


if __name__ == "__main__":
    try:
        {"prepare": prepare, "config-check": s.config_check, "run": run, "inspect": inspect}[sys.argv[1]]()
    except BaseException:
        import traceback
        if RUN.exists():
            (RUN / (sys.argv[1] + "-failure.txt")).write_text(traceback.format_exc(), encoding="utf-8")
        raise
