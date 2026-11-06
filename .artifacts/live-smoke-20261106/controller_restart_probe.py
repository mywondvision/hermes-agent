"""PREPARED ONLY: real C1 death -> fresh C2, synthetic Python worker, zero Codex sessions.

Default only writes a plan. Runtime phases require --execute. Not executed/qualified
by the cancellation correction. The parent keeps QUERY-only Job/process handles so
C2 can independently inspect the SAME stopped native binding after C1 has died.
"""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from cancel_control import CancellationCoordinator
from tests.fixtures.symphony_pm_ingress.owner import Owner, Target, Token
from tools import symphony_pm_adapter as pm

NAMESPACE = "isolated-controller-restart-probe"
PROJECT = "synthetic-controller-restart"


def save(path, value):
    with Path(path).open("w", encoding="utf-8") as out:
        json.dump(value, out, indent=2)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def plan():
    return {"status": "NOT_RUN", "kind": "real-controller-process-restart-with-synthetic-python-worker",
            "controllers": ["C1", "C2"], "native_codex_sessions": 0,
            "controller_launcher": "existing base Python + current dependency paths; retained PID verified before worker admission",
            "worker": "current Python, bounded 90-second sleep + child; no project data",
            "cut": "C1 os._exit after OS effect and before durable acknowledgement",
            "recovery": "C2 new process, new owner epoch, same SQLite, receiver=None; observer only",
            "assertions": ["C1 exact identity signaled", "C2 PID differs", "one native API entry",
                           "spent ticket retained", "same attempt/process creation identity", "zero new launches",
                           "independent root/descendant stop", "settlement remains UNKNOWN/HOLD"],
            "deadlines_seconds": {"parent_handshake": 20, "C1_exit": 30, "C2_exit": 20},
            "authorization": "prepare only; explicit owner direction required before run --execute",
            "live_codex_variant": "NOT_PREPARED_FOR_EXECUTION; new model/environment approval required"}


def factory(work):
    return Owner(work / "owner.sqlite", namespace=NAMESPACE)


def context(payload, token):
    request, status, _ = pm.parse_intent(payload)
    if status != "OK":
        raise RuntimeError("Bad synthetic request")
    now = time.time()
    return pm.TrustedPmContextV1("restart-probe", "test-intake", PROJECT, pm.OPERATIONS,
                                request.request_id, "sandbox-only", "explicit-probe-execution", now + 60,
                                now, 60, "restart-probe-owner", token.epoch, "b" * 64,
                                approved_scope_revision="restart-probe-only",
                                approved_payload_digest=request.payload_digest,
                                expected_job_ref=getattr(request, "job_ref", ""),
                                expected_job_revision=getattr(request, "expected_job_revision", ""))


def wait_for(predicate, seconds):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError("Probe deadline; UNKNOWN/HOLD")


def c1(work):
    import winjob as w
    self_handle = w.checked(w.open_process(0x100000 | 0x1000, False, os.getpid()))
    save(work / "c1-identity.json", {"pid": os.getpid(), "start_ticks": w.creation_ticks(self_handle)})
    w.close_handle(self_handle)
    # Parent proves the retained Popen handle names C1 before any worker can exist.
    wait_for(lambda: (work / "parent-accepted-c1-identity.json").exists(), 20)
    owner = factory(work)
    token = owner.claim("C1", time.time(), ttl=90)
    request = pm.SubmitIntent("restart-submit", PROJECT, "bounded synthetic wait", ())
    job, revision, _ = owner.submit(request, context(dict(request.payload(), request_id=request.request_id), token), token, time.time())
    attempt = "restart-attempt-" + uuid.uuid4().hex
    control = w.WindowsJob("Local\\HermesSmoke-" + attempt)
    code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(90)']); print('READY',flush=True); time.sleep(90)"
    with (work / "worker.out").open("wb") as output:
        identity = control.start_suspended([sys.executable, "-B", "-c", code], str(work), os.environ.copy(), output, output)
        target = Target(NAMESPACE, PROJECT, job, revision, attempt, request.request_digest, token.epoch,
                        "python", "no-model-session", str(identity["pid"]), identity["start_ticks"])
        with owner.transaction():
            owner._authorize(token, time.time())
            owner.db.execute("UPDATE jobs SET target=? WHERE job=?", (json.dumps(asdict(target)), job))
        owner.close()

        def observer(bound):
            return dict(w.observe(control.observer_target()), target=asdict(bound))

        def crash(point):
            if point == "after_effect":
                os._exit(23)

        coordinator = CancellationCoordinator(lambda: factory(work), token, control, observer, context, hook=crash)
        control.set_cancel_route(lambda cause: coordinator.cancel(target, cause))
        original = w.terminate_job

        def counted(handle, code):
            with (work / "native-calls.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps({"event": "NATIVE_ENTER", "effect_key": target.effect_key}) + "\n")
                log.flush()
                os.fsync(log.fileno())
            result = original(handle, code)
            save(work / "native-return.json", {"acknowledged": bool(result), "effect_key": target.effect_key})
            return result
        w.terminate_job = counted
        control.resume("")
        try:
            wait_for(lambda: b"READY" in (work / "worker.out").read_bytes() and control.capture()["active"] >= 2, 15)
            save(work / "target.json", asdict(target))
            save(work / "observer-target.json", control.observer_target())
            wait_for(lambda: (work / "parent-holds-query-handles.json").exists(), 20)
            coordinator.cancel(target, "PM_CANCEL")
            raise RuntimeError("Crash cut did not run")
        finally:
            control.close()


def c2(work):
    import winjob as w
    previous = read(work / "c1-identity.json")
    hp = w.checked(w.open_process(0x100000 | 0x1000, False, previous["pid"]))
    try:
        if w.creation_ticks(hp) != previous["start_ticks"] or w.wait_handle(hp, 0) != 0:
            raise RuntimeError("Previous controller is not proven stopped")
    finally:
        w.close_handle(hp)
    owner = factory(work)
    old = owner.db.execute("SELECT * FROM writers WHERE namespace=?", (NAMESPACE,)).fetchone()
    if old["owner"] != "C1":
        raise RuntimeError("Unexpected predecessor")
    if time.time() < old["expires"]:
        token = Token(NAMESPACE, old["owner"], old["epoch"], old["expires"])
        with owner.transaction():
            owner._authorize(token, time.time())
            owner.db.execute("UPDATE writers SET expires=? WHERE namespace=? AND epoch=?", (time.time(), NAMESPACE, token.epoch))
    token = owner.claim("C2", time.time(), ttl=60)
    row = owner.db.execute("SELECT target FROM jobs").fetchone()
    target = Target(**json.loads(row["target"]))
    before = owner.snapshot(target)
    owner.close()
    if before is None or before["state"] != "UNKNOWN":
        raise RuntimeError("Spent unknown ticket missing")

    def observer(bound):
        return dict(w.observe(read(work / "observer-target.json")), target=asdict(bound))

    coordinator = CancellationCoordinator(lambda: factory(work), token, None, observer, context)
    result = coordinator.cancel(target, "PM_CANCEL")
    if not result.local_stopped or not result.hold or token.epoch <= old["epoch"] or os.getpid() == previous["pid"]:
        raise RuntimeError("Recovery not independently established; HOLD")
    save(work / "c2-result.json", {"controller_pid": os.getpid(), "old_controller_pid": previous["pid"],
                                   "epoch": token.epoch, "cancel": asdict(result), "new_worker_launches": 0,
                                   "receiver": None, "target": asdict(target)})


def reap_controller(controller):
    """Retained handle identifies our controller; never target worker PID or resend its cancel."""
    try:
        if controller.poll() is None:
            controller.kill()
    finally:
        controller.wait(timeout=10)


def controller_command():
    """Reuse the installed base Python + current dependency paths, without a venv launcher shim."""
    executable = Path(sys._base_executable)
    if not executable.is_file():
        raise RuntimeError("Existing base interpreter unavailable; no environment is created")
    script = str(Path(__file__).resolve())
    bootstrap = ("import runpy,sys;sys.path[:]=" + repr(list(sys.path)) + ";sys.argv=[" + repr(script)
                 + ",*sys.argv[1:]];runpy.run_path(" + repr(script) + ",run_name='__main__')")
    return [str(executable), "-B", "-c", bootstrap]


def run(parent):
    import winjob as w
    work = parent / ("execution-" + uuid.uuid4().hex)
    work.mkdir()
    command = controller_command()
    handles, job_handle = [], None
    with (work / "controllers.out").open("wb") as log:
        controller = subprocess.Popen(command + ["c1", "--execute", "--directory", str(work)], stdout=log, stderr=log)
        try:
            wait_for(lambda: (work / "c1-identity.json").exists(), 20)
            identity = read(work / "c1-identity.json")
            if identity["pid"] != controller.pid or identity["start_ticks"] != w.creation_ticks(int(controller._handle)):
                raise RuntimeError("C1 is not the exact retained process; no worker admission")
            save(work / "parent-accepted-c1-identity.json", identity)
            wait_for(lambda: (work / "observer-target.json").exists(), 20)
            expected = read(work / "observer-target.json")
            job_handle = w.checked(w.open_job(4, False, expected["job_object"]))
            for pid in [identity["pid"], *[p["pid"] for p in expected["known_processes"]]]:
                handle = w.open_process(0x100000 | 0x1000, False, pid)
                if handle:
                    handles.append(handle)
            save(work / "parent-holds-query-handles.json", {"query_only": True, "handles": len(handles)})
            if controller.wait(timeout=30) != 23:
                raise RuntimeError("C1 did not exit at prescribed crash cut")
            subprocess.run(command + ["c2", "--execute", "--directory", str(work)], stdout=log, stderr=log, timeout=20, check=True)
            calls = (work / "native-calls.jsonl").read_text().splitlines()
            if len(calls) != 1 or not read(work / "native-return.json")["acknowledged"]:
                raise RuntimeError("Native send count/ack unknown")
            if not all(w.wait_handle(h, 0) == 0 for h in handles):
                raise RuntimeError("Retained processes not terminal")
            save(work / "result.json", {"mechanical_controller_restart": "PASS", "overall": "HOLD",
                                        "native_calls": 1, "new_worker_launches_after_restart": 0,
                                        "codex_sessions": 0, "billing_and_external_settlement": "UNKNOWN"})
        except BaseException:
            save(work / "result.json", {"mechanical_controller_restart": "UNKNOWN", "overall": "HOLD"})
            raise
        finally:
            cleanup_errors = []
            try:
                # Reaping cannot depend on successful evidence/identity file reads.
                reap_controller(controller)
            except BaseException as exc:
                cleanup_errors.append(exc)
            finally:
                # Query handle close preserves the kernel fail-safe; no second Job cancel.
                for handle in ([job_handle] if job_handle else []) + handles:
                    try:
                        w.checked(w.close_handle(handle))
                    except OSError as exc:
                        cleanup_errors.append(exc)
            if cleanup_errors:
                save(work / "result.json", {"mechanical_controller_restart": "UNKNOWN", "overall": "HOLD",
                                            "cleanup": "CONTROLLER_OR_HANDLE_RELEASE_UNKNOWN"})
                raise RuntimeError("RESTART_CLEANUP_UNKNOWN_HOLD") from cleanup_errors[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "run", "c1", "c2"), nargs="?", default="prepare")
    parser.add_argument("--directory", type=Path, default=ROOT / ".artifacts/cancel-correction-20261106/restart-sandbox")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.phase == "prepare":
        args.directory.mkdir(parents=True, exist_ok=True)
        save(args.directory / "plan.json", plan())
        print(json.dumps(plan(), indent=2))
    elif not args.execute:
        parser.error("Preparation only. Execution needs separate owner direction and --execute.")
    else:
        {"run": run, "c1": c1, "c2": c2}[args.phase](args.directory.resolve())
