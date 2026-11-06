"""Authorized, task-local Hermes -> one journal owner -> existing Codex exec driver.

Reuses the offline reference's SQLite transaction/dedupe code ONLY. The live port,
process binding and query-only OS observer are explicit here; no synthetic sink,
fake PRIMARY admission, queue, network server, production plugin activation or S2.
Usage: smoke.py prepare | process-check | run | inspect
Only run may launch a model, guarded by an exclusive campaign marker and two tickets.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
import uuid

HERE = Path(__file__).resolve().parent
CODE_DIR = HERE
ROOT = HERE.parents[1]
KIT = Path("D:/AI/worktrees/kit-wave1-integration-20261031")
DB = HERE / "owner.sqlite"
CLI = Path("D:/Codex/npm-global/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe")
MODEL, EFFORT = "gpt-6.1-sol", "high"
ISSUER, SCHED = "synthetic-smoke-intake", "symphony-smoke-owner"
PROJECT, NAMESPACE = "synthetic-smoke-only", "live-smoke-20261106"
MAX_NATIVE_ATTEMPTS = 2
PREVIOUS_NATIVE_ATTEMPTS = 0
REQUEST_PREFIX = "smoke-request"
sys.path.insert(0, str(ROOT))

from tools import symphony_pm_adapter as pm
from tests.fixtures.symphony_pm_ingress.owner import Owner, Target, digest, Refusal
from cancel_control import CancellationCoordinator
from winjob import WindowsJob, observe


def save(path, value):
    path = Path(path)
    with path.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def log(event, **fields):
    record = {"at": time.time(), "event": event, **fields}
    with (HERE / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    print(json.dumps(record, ensure_ascii=False), flush=True)


def baseline():
    paths = [ROOT / "tools/symphony_pm_adapter.py", ROOT / "tests/tools/test_symphony_pm_adapter.py",
             ROOT / "tests/tools/test_symphony_pm_adapter_cancel_s2.py",
             ROOT / "tests/tools/symphony_pm_adapter.py", ROOT / "tests/tools/tools.zip",
             ROOT / "tests/plugins/test_symphony_pm_plugin.py",
             ROOT / "tests/plugins/test_symphony_pm_ingress_offline.py",
             ROOT / "tests/fixtures/symphony_pm_ingress/owner.py"]
    paths += list((ROOT / "plugins/symphony_pm").glob("*.py"))
    paths += list((ROOT / "plugins/symphony_pm").glob("*.yaml"))
    manifest = load(ROOT / ".artifacts/frozen-s2/snapshot-manifest.json")
    # Snapshot hashes are also checked by canonical adapter tests.
    return {"files": {str(p): sha(p) for p in paths}, "frozen_manifest": manifest,
            "kit_head": subprocess.check_output(["git", "-C", str(KIT), "rev-parse", "HEAD"], text=True).strip(),
            "hermes_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "kit_version": (KIT / "VERSION").read_text().strip(),
            "driver_files": {str(KIT / p): sha(KIT / p) for p in (
                "runtimes/codex/orchestration/driver.py", "runtimes/codex/orchestration/strict_schema.py",
                "tools/orchestration/drivers.py", "tools/orchestration/process.py")},
            "cli": str(CLI), "cli_sha256": sha(CLI), "python": sys.version,
            "tracked_status": subprocess.check_output(["git", "status", "--porcelain", "-uno"], cwd=ROOT, text=True)}


def prepare():
    assert not (HERE / "baseline.json").exists(), "Preparation already recorded"
    save(HERE / "baseline.json", baseline())
    save(HERE / "approval.json", {"source": "current user request + explicit async reply",
         "reply": "Да, согласую два запуска", "model": MODEL, "effort": EFFORT,
         "environment": "current Windows host, existing Codex authentication",
         "max_native_attempts": 2, "automatic_retries": False, "production": False,
         "scope": "task-local in-process binding and two synthetic read-only smoke jobs"})
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"marker": {"type": "string"}, "value": {"type": "integer"},
                             "sum": {"type": "integer"}}, "required": ["marker", "value", "sum"]}
    save(HERE / "result.schema.json", schema)
    save(HERE / "defs.schema.json", {"$defs": {}})
    for number in (1, 2):
        work = HERE / f"workspace-{number}"
        work.mkdir()
        subprocess.run(["git", "init", "--quiet", str(work)], check=True)
        marker = "synthetic-" + uuid.uuid4().hex
        save(work / "input.json", {"marker": marker, "value": 17, "numbers": [2, 3, 5]})
        (work / "AGENTS.md").write_text("Synthetic read-only smoke. Read input.json only. No other projects, tools, skills, network, or delegates.\n", encoding="utf-8")
    log("PREPARED", model=MODEL, effort=EFFORT, attempts=0)


class LiveOwner(Owner):
    def __init__(self):
        super().__init__(DB, namespace=NAMESPACE)
        self.db.execute("CREATE TABLE IF NOT EXISTS launches(job TEXT PRIMARY KEY, attempt TEXT UNIQUE, state TEXT, binding TEXT, result TEXT)")

    def ticket(self, token, job):
        with self.transaction():
            self._authorize(token, time.time())
            if self.db.execute("SELECT COUNT(*) FROM launches").fetchone()[0] >= MAX_NATIVE_ATTEMPTS:
                raise Refusal("TWO_ATTEMPT_LIMIT")
            attempt = "attempt-" + uuid.uuid4().hex
            self.db.execute("INSERT INTO launches VALUES(?,?,'DISPATCH_INTENT',NULL,NULL)", (job, attempt))
        return attempt

    def bind(self, token, request, job, revision, attempt, identity):
        with self.transaction():
            self._authorize(token, time.time())
            row = self._job(PROJECT, job, revision)
            assert row["target"] is None
            target = Target(NAMESPACE, PROJECT, job, revision, attempt, request.request_digest,
                            token.epoch, "codex", attempt, str(identity["pid"]), identity["start_ticks"])
            encoded = json.dumps(dataclasses.asdict(target))
            self.db.execute("UPDATE jobs SET target=? WHERE namespace=? AND project=? AND job=?",
                            (encoded, NAMESPACE, PROJECT, job))
            self.db.execute("UPDATE launches SET state='STARTED', binding=? WHERE attempt=?",
                            (json.dumps(identity), attempt))
        return target

    def native_session(self, token, target, session):
        with self.transaction():
            self._authorize(token, time.time())
            target = dataclasses.replace(target, session=session)
            self.db.execute("UPDATE jobs SET target=? WHERE namespace=? AND project=? AND job=?",
                            (json.dumps(dataclasses.asdict(target)), NAMESPACE, PROJECT, target.job))
        return target


class NativeRunner:
    def __init__(self):
        self.active = None

    def __call__(self, argv, cwd=None, timeout=None, stdin_text=None):
        from orchestration.process import CommandResult, redact
        if argv[1] != "exec" or "--help" in argv:
            r = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout, check=False)
            return CommandResult(tuple(argv), r.returncode, redact(r.stdout.decode("utf-8", "replace")),
                                 redact(r.stderr.decode("utf-8", "replace")))
        a = self.active
        assert a is not None and not a.started.is_set()
        overrides = ["--ignore-user-config", "-c", "approval_policy=\"never\"",
                     "-c", "windows.sandbox=\"unelevated\"", "-c", "project_doc_max_bytes=0",
                     "-c", "features.multi_agent=false", "-c", "web_search=\"disabled\""]
        # 0.167.0 reserves built-in provider IDs. Do not configure model_providers.openai,
        # rename/switch providers, or claim that internal HTTP retries are disabled.
        # Campaign/launch tickets bound native attempts; a follow-up run needs owner direction.
        argv = [argv[0], "exec", *overrides, *argv[2:]]
        self.driver.reject_bypass(argv)
        save(a.folder / "command.json", {"argv": argv, "cwd": str(cwd), "stdin_sha256": hashlib.sha256(stdin_text.encode()).hexdigest(),
                                         "model": MODEL, "effort": EFFORT, "timeout_seconds": timeout,
                                         "timeout_is_spend_limit": False})
        # Existing auth remains in Codex; do not copy credentials or inherit integration secrets.
        keep = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "USERPROFILE", "LOCALAPPDATA",
                "APPDATA", "TEMP", "TMP", "CODEX_HOME", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA")
        env = {k: v for k, v in os.environ.items() if k.upper() in keep}
        with (a.folder / "stdout.jsonl").open("wb") as out, (a.folder / "stderr.txt").open("wb") as err:
            identity = a.control.start_suspended(argv, str(cwd), env, out, err)
            worker_owner = LiveOwner()
            try:
                a.target = worker_owner.bind(a.token, a.request, a.job, a.revision, a.attempt, identity)
            finally:
                worker_owner.close()
            a.bind_cancel_route()
            save(a.folder / "binding.json", {**identity, **dataclasses.asdict(a.target)})
            a.control.resume(stdin_text)
            a.started.set()
            log("NATIVE_STARTED", job=a.job, attempt=a.attempt, **identity)
            try:
                rc = a.control.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                a.timed_out = True
                try:
                    a.cancel("TIMEOUT")
                finally:
                    # Storage/observer failure must not remove the bounded drain wait.
                    rc = a.control.process.wait(timeout=15)
            a.returncode = rc
        return CommandResult(tuple(argv), rc, timed_out=a.timed_out)


class Attempt:
    def bind_cancel_route(self):
        def readback(target):
            proof = observe(self.control.observer_target())
            proof["target"] = dataclasses.asdict(target)
            save(self.folder / "cancel-readback.json", proof)
            return proof

        def checkpoint(point):
            if point == "after_intent":
                log("NATIVE_CANCEL_DISPATCH", attempt=self.target.attempt, effect_key=self.target.effect_key,
                    pid=self.target.process, start=self.target.start, session=self.target.session)

        self.canceller = CancellationCoordinator(LiveOwner, self.token, self.control, readback, context,
                                                hook=checkpoint)
        self.control.set_cancel_route(lambda cause: self.cancel(cause))

    def cancel(self, cause, *, request=None, trusted_context=None):
        result = self.canceller.cancel(self.target, cause, request=request, context=trusted_context)
        log("CANCEL_STATE", attempt=self.target.attempt, **dataclasses.asdict(result))
        return result

    def __init__(self, owner, token, request, job, revision, driver, number, *, prepared_attempt=None):
        self.token, self.request, self.job, self.revision = token, request, job, revision
        self.attempt = prepared_attempt or owner.ticket(token, job)
        self.folder = HERE / f"attempt-{number}"
        self.folder.mkdir(exist_ok=prepared_attempt is not None)
        self.control = WindowsJob("Local\\HermesSmoke-" + self.attempt)
        self.started, self.finished = threading.Event(), threading.Event()
        self.admission_verified = threading.Event()
        self.target, self.returncode, self.error, self.timed_out = None, None, None, False
        from orchestration.drivers import PeerPack
        self.pack = PeerPack(self.attempt, "CR-hermes-symphony-ingress-offline-20261106/current-user-live-delta",
                             "review-only", "codex", "codex", "0" * 40, "0" * 40,
                             str(HERE / f"workspace-{number}"), result_path=str(self.folder / "result.json"))
        prompt = "Read only input.json in the working directory using a read-only shell command. Do not read any other paths or projects. Do not use skills, MCP, network, or delegate. Return JSON with marker copied exactly from input, value copied exactly, and sum of numbers. No file edits."
        if number == 2:
            prompt += " Before returning JSON, run this bounded read-only wait as a shell command: powershell.exe -NoProfile -NonInteractive -Command \"Write-Output 'SMOKE_CANCEL_READY'; Start-Sleep -Seconds 90\". Do not skip or shorten the wait; the owner will cancel this attempt during it."
        (self.folder / "prompt.txt").write_text(prompt, encoding="utf-8")
        driver.runner.active = self

        def run():
            try:
                if not self.admission_verified.wait(15):
                    raise RuntimeError("PM receipt was not verified; native dispatch withheld")
                result = driver.launch_review(self.pack, prompt)
                save(self.folder / "driver-result.json", result)
            except BaseException:
                self.error = traceback.format_exc()
                (self.folder / "error.txt").write_text(self.error, encoding="utf-8")
            finally:
                self.finished.set()

        self.thread = threading.Thread(target=run, name=self.attempt, daemon=True)
        self.thread.start()


class LivePort:
    def __init__(self, owner, token, driver):
        self.owner, self.token, self.driver = owner, token, driver
        self.attempts = {}
        self.lose_reply = False

    @property
    def cancel_calls(self):
        return sum(a.control._cancel_gate.dispatch_count for a in self.attempts.values())

    def common(self, request, context):
        self.owner._authorize(self.token, time.time())
        assert context.scheduler_id == SCHED and context.scheduler_epoch == self.token.epoch
        return dict(contract_version=pm.CONTRACT_VERSION, contract_digest=context.contract_digest,
                    request_id=request.request_id, payload_digest=request.payload_digest,
                    project_ref=request.project_ref, issuer_ref=context.issuer_ref,
                    # A request-scoped logical tick is shared with the adapter verifier;
                    # detailed OS observations retain their independent wall timestamps.
                    scheduler_id=SCHED, scheduler_epoch=self.token.epoch, observed_at=context.now)

    def submit_intent(self, request, context):
        common = self.common(request, context)
        job, revision, outcome = self.owner.submit(request, context, self.token, time.time())
        if outcome == "SUBMITTED":
            number = 1 + len(self.attempts)
            assert number <= 2 and (number == 1 or load(HERE / "first-verdict.json")["status"] == "PASS")
            a = Attempt(self.owner, self.token, request, job, revision, self.driver, number)
            self.attempts[job] = a
        elif job not in self.attempts and getattr(self, "resume_unstarted", False):
            known = self.owner.db.execute("SELECT * FROM launches WHERE job=?", (job,)).fetchone()
            assert known is not None and known["state"] == "DISPATCH_INTENT" and known["binding"] is None
            assert not (HERE / "attempt-1/command.json").exists()
            assert not (HERE / "attempt-1/binding.json").exists()
            self.attempts[job] = Attempt(self.owner, self.token, request, job, revision, self.driver, 1,
                                         prepared_attempt=known["attempt"])
            self.resume_unstarted = False
        return pm.PmSubmissionReceipt(**common, outcome=outcome, job_ref=job, job_revision=revision)

    def request_cancel(self, request, context):
        common = self.common(request, context)
        target = self.owner.target(request, context, self.token, time.time())
        a = self.attempts[target.job]
        assert a.target == target and a.control.process.pid == int(target.process)
        result = a.cancel("PM_CANCEL", request=request, trusted_context=context)
        if self.lose_reply:
            return None
        return pm.PmCancelReceipt(**common, job_ref=target.job, job_revision=target.revision,
                                 outcome="UNKNOWN" if result.state == "UNKNOWN" else
                                         "CANCEL_REQUESTED" if result.dispatched else "ALREADY_REQUESTED",
                                 reservation_state="POSSIBLY_CONSUMED", uncertainty=("BILLING_AND_EXTERNAL_EFFECTS_UNKNOWN",))

    def get_status(self, request, context):
        common = self.common(request, context)
        target = self.owner.target(request, context, self.token, time.time())
        a = self.attempts[target.job]
        return pm.PmStatusReceipt(**common, job_ref=target.job, job_revision=target.revision,
                                 attempt_ref=target.attempt, phase="RUNNING" if not a.finished.is_set() else "TERMINAL",
                                 retention=(("reservation", "POSSIBLY_CONSUMED_HELD"),))


def context(payload, token):
    intent, status, issues = pm.parse_intent(payload)
    assert status == "OK" and not issues
    now = time.time()
    return pm.TrustedPmContextV1(
        context_ref="synthetic-caller", issuer_ref=ISSUER, project_ref=PROJECT,
        allowed_operations=pm.OPERATIONS, request_id=intent.request_id,
        policy_ref="current-user-two-attempt-approval", grant_ref="approval.json", expires_at=now + 60,
        now=now, max_age_seconds=60, scheduler_id=SCHED, scheduler_epoch=token.epoch,
        contract_digest=sha(HERE / "approval.json"), approved_scope_revision="current-user-live-delta",
        approved_payload_digest=intent.payload_digest if intent.operation == "submit_intent" else "",
        expected_job_ref=getattr(intent, "job_ref", ""), expected_job_revision=getattr(intent, "expected_job_revision", ""))


def plugin_binding(port):
    home = HERE / "live-hermes-home"
    home.mkdir(exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    (home / "config.yaml").write_text("plugins:\n  enabled: [symphony_pm]\n", encoding="utf-8")
    from hermes_cli import plugins
    from tools.registry import registry
    plugins.discover_plugins()
    loaded = plugins.get_plugin_manager()._plugins["symphony_pm"]
    assert loaded.enabled and loaded.error is None, loaded.error
    current = {}
    loaded.module.wire(port, lambda: context(current["payload"], port.token), trusted_issuers=(ISSUER,), scheduler_id=SCHED)

    def dispatch(payload):
        current["payload"] = payload
        # Real PluginManager/registry and plugin handler; no fake PluginContext.
        result = registry.get_entry(pm.TOOL_NAME).handler(payload)
        if isinstance(result, str):
            result = json.loads(result)
        log("PM_RESPONSE", payload=payload, result=result)
        return result
    return loaded.module, dispatch


def events(a):
    path = a.folder / "stdout.jsonl"
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # an actively written final line is read again by the next observation
    return result


def bind_session(owner, token, a):
    sessions = [r["thread_id"] for r in events(a) if r.get("type") == "thread.started"]
    assert len(sessions) == 1, "Native thread identity missing or multiple starts"
    a.target = owner.native_session(token, a.target, sessions[0])
    save(a.folder / "native-target.json", dataclasses.asdict(a.target))


def independent_read(a, label):
    save(a.folder / "observer-target.json", a.control.observer_target())
    command = [sys.executable, "-B", str(CODE_DIR / "winjob.py"), str(a.folder / "observer-target.json")]
    r = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=15, check=False)
    assert r.returncode == 0, r.stderr
    proof = json.loads(r.stdout)
    save(a.folder / (label + ".json"), {"command": command, "proof": proof})
    return proof


def wait_for(predicate, seconds):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def submit(number):
    return {"contract_version": pm.CONTRACT_VERSION, "operation": "submit_intent",
            "request_id": f"{REQUEST_PREFIX}-{number}", "project_ref": PROJECT,
            "goal": f"Synthetic read-only task {number}; input.json only",
            "raw_constraints": ["No production, no network tools, no edits, no delegation, no retry"]}


def campaign(continue_unstarted=False):
    assert not (HERE / "native-campaign-hold.json").exists(), "Prior native failure: HOLD, no retry"
    marker = "continue-unstarted.once" if continue_unstarted else "campaign.once"
    with (HERE / marker).open("x", encoding="utf-8") as f:
        f.write("No automatic rerun; inspect durable tickets instead.\n")
    assert load(HERE / "approval.json")["max_native_attempts"] == 2
    sys.path.insert(0, str(KIT / "tools"))
    from orchestration.drivers import load_driver
    runner = NativeRunner()
    driver = load_driver("codex", root=KIT, runner=runner, executable=str(CLI),
                         schema_path=HERE / "result.schema.json", model=MODEL, effort=EFFORT,
                         timeout=180, probe_timeout=20)
    runner.driver = driver
    probe = driver.probe()
    probe.require(*driver.required_features)
    assert probe.readiness == "ready"
    save(HERE / "preflight.json", {"version": probe.version, "features": sorted(probe.features),
                                  "readiness": probe.readiness, "model": MODEL, "effort": EFFORT})
    owner = LiveOwner()
    if continue_unstarted:
        proof = load(HERE / "pre-native-reconciliation.json")
        assert proof["native_dispatch_not_called"] is True
        row = owner.db.execute("SELECT * FROM writers WHERE namespace=?", (NAMESPACE,)).fetchone()
        from tests.fixtures.symphony_pm_ingress.owner import Token
        token = Token(NAMESPACE, row["owner"], row["epoch"], row["expires"])
        owner._authorize(token, time.time())
    else:
        token = owner.claim("single-local-controller", time.time(), ttl=900)
    port = LivePort(owner, token, driver)
    port.resume_unstarted = continue_unstarted
    module, call = plugin_binding(port)
    try:
        first = call(submit(1))
        assert first["outcome"] == "SUBMITTED", first
        a = port.attempts[first["view"]["job_ref"]]
        a.admission_verified.set()
        assert wait_for(a.finished.is_set, 190), "First attempt unfinished; HOLD"
        assert not a.error and a.returncode == 0 and not a.timed_out, a.error or a.returncode
        bind_session(owner, token, a)
        actual = driver.collect_result(a.pack)
        data = load(HERE / "workspace-1/input.json")
        expected = {"marker": data["marker"], "value": data["value"], "sum": sum(data["numbers"])}
        assert actual == expected, {"actual": actual, "expected": expected}
        assert any(r.get("type") == "turn.completed" for r in events(a))
        proof = independent_read(a, "terminal-observer")
        assert proof["tree_stopped"] and all(a.control.terminal_handles().values())
        duplicate = call(submit(1))
        assert duplicate["view"]["job_ref"] == a.job
        assert owner.db.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == PREVIOUS_NATIVE_ATTEMPTS + 1
        save(HERE / "first-verdict.json", {"status": "PASS", "job": a.job, "attempt": a.attempt,
             "target": dataclasses.asdict(a.target), "result": actual, "native_launches": 1,
             "observer_tree_stopped": True, "billing_settled": "UNKNOWN"})
        log("FIRST_VERIFIED", job=a.job, attempt=a.attempt, result=actual)

        second = call(submit(2))
        assert second["outcome"] == "SUBMITTED", second
        b = port.attempts[second["view"]["job_ref"]]
        b.admission_verified.set()
        assert wait_for(b.started.is_set, 25), b.error or "Second process not started"

        def ready():
            if b.finished.is_set():
                return False
            snap = b.control.capture()
            started_wait = any(r.get("type") == "item.started" and
                               "SMOKE_CANCEL_READY" in r.get("item", {}).get("command", "") for r in events(b))
            return started_wait and len(snap["pids"]) >= 3

        assert wait_for(ready, 100), "Bounded worker wait/descendants not confirmed; HOLD"
        bind_session(owner, token, b)
        before = independent_read(b, "running-observer")
        assert before["active"] >= 3 and not before["tree_stopped"]
        cancel = {"contract_version": pm.CONTRACT_VERSION, "operation": "request_cancel",
                  "request_id": "cancel-lost-response", "project_ref": PROJECT,
                  "job_ref": b.job, "expected_job_revision": b.revision, "reason": "Approved bounded cancel smoke"}
        port.lose_reply = True
        lost = call(cancel)
        assert lost["outcome"] == "UNKNOWN" and lost["hold"]
        assert wait_for(b.finished.is_set, 20), "Native process did not terminate; HOLD"
        assert wait_for(lambda: b.control.capture()["active"] == 0, 10)
        terminal = independent_read(b, "terminal-observer")
        handles = b.control.terminal_handles()
        assert terminal["tree_stopped"] and all(handles.values())

        # Connection + port recovery, same surviving exact-attempt controller; no fake process restart.
        owner.close()
        owner = LiveOwner()
        port.owner = owner
        port.lose_reply = False
        module.wire(port, lambda: context(cancel, token), trusted_issuers=(ISSUER,), scheduler_id=SCHED)
        # Restore regular binding callback before each next dispatch.
        module.unwire()
        module, call = plugin_binding(port)
        replay = call(cancel)
        changed = dict(cancel, request_id="cancel-redelivery-new-id")
        replay_new = call(changed)
        resubmit = call(submit(2))
        count = owner.db.execute("SELECT COUNT(*) FROM launches").fetchone()[0]
        assert count == PREVIOUS_NATIVE_ATTEMPTS + 2 and port.cancel_calls == 1 and resubmit["view"]["job_ref"] == b.job
        assert not replay["view"]["worker_stopped"] and replay["hold"]
        save(HERE / "cancel-verdict.json", {"status": "HOLD", "mechanical_checks": "PASS",
             "job": b.job, "attempt": b.attempt, "target": dataclasses.asdict(b.target),
             "lost_reply": lost, "replay": replay, "new_id_replay": replay_new,
             "native_cancel_calls": port.cancel_calls, "native_launches_total": count,
             "independent_tree_stopped": terminal["tree_stopped"], "retained_handles_signaled": handles,
             "external_effects_settled": "UNKNOWN", "billing_settled": "UNKNOWN",
            "recovery": "SQLite connection reopened with the SAME live LivePort and OS handles; no controller restart"})
        log("CANCEL_VERIFIED_HOLD", attempt=b.attempt, native_cancel_calls=port.cancel_calls,
            native_launches=count, tree_stopped=terminal["tree_stopped"])
    finally:
        module.unwire()
        for a in port.attempts.values():
            # close() routes explicit cleanup through the same spent-ticket coordinator.
            snap = a.control.capture()
            log("FINAL_JOB_READBACK", attempt=a.attempt, snapshot=snap)
            a.control.close()
        owner.close()
        driver.cleanup(None)


def process_check():
    """Real OS supervision probe; no Codex/native model attempt."""
    if (HERE / "process-check.json").exists():
        raise RuntimeError("Historical probe already recorded; no implicit repeat")
    control = WindowsJob("Local\\HermesSmoke-process-probe-" + uuid.uuid4().hex)
    with (HERE / "process-check.out").open("wb") as out:
        code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)']); print('READY',flush=True); time.sleep(90)"
        identity = control.start_suspended([sys.executable, "-B", "-c", code], str(HERE), os.environ.copy(), out, out)
        journal = HERE / "process-probe-cancel.sqlite"
        factory = lambda: Owner(journal, namespace="bounded-os-probe")
        owner = factory()
        token = owner.claim("probe-controller", time.time(), ttl=120)
        payload = submit(1)
        request, _, _ = pm.parse_intent(payload)
        trusted = context(payload, token)
        job, revision, _ = owner.submit(request, trusted, token, time.time())
        target = Target("bounded-os-probe", PROJECT, job, revision, "probe-" + uuid.uuid4().hex,
                        request.request_digest, token.epoch, "python", "no-model-session",
                        str(identity["pid"]), identity["start_ticks"])
        with owner.transaction():
            owner._authorize(token, time.time())
            owner.db.execute("UPDATE jobs SET target=? WHERE job=?",
                             (json.dumps(dataclasses.asdict(target)), job))
        owner.close()

        def probe_readback(bound):
            return dict(observe(control.observer_target()), target=dataclasses.asdict(bound))

        canceller = CancellationCoordinator(factory, token, control, probe_readback, context)
        control.set_cancel_route(lambda cause: canceller.cancel(target, cause))
        control.resume("")
        try:
            assert wait_for(lambda: control.capture()["active"] >= 2, 10)
            save(HERE / "process-probe-target.json", control.observer_target())
            canceller.cancel(target, "EMERGENCY_CLEANUP")
            assert wait_for(lambda: control.capture()["active"] == 0, 10)
            r = subprocess.run([sys.executable, "-B", str(HERE / "winjob.py"), str(HERE / "process-probe-target.json")],
                               capture_output=True, text=True, timeout=15, check=False)
            assert r.returncode == 0, r.stderr
            proof = json.loads(r.stdout)
            assert proof["tree_stopped"] and all(control.terminal_handles().values())
            save(HERE / "process-check.json", {"status": "PASS", "proof": proof, "model_attempts": 0})
            log("PROCESS_CONTROL_CHECK_PASS", native_attempts=0)
        finally:
            control.close()


def inspect():
    original = load(HERE / "baseline.json")
    current = baseline()
    save(HERE / "preservation.json", {"source_files_unchanged": original["files"] == current["files"],
         "driver_files_unchanged": original["driver_files"] == current["driver_files"],
         "hermes_head_unchanged": original["hermes_head"] == current["hermes_head"],
         "tracked_status_unchanged": original["tracked_status"] == current["tracked_status"],
         "frozen_manifest_unchanged": original["frozen_manifest"] == current["frozen_manifest"]})
    if DB.exists():
        owner = LiveOwner()
        save(HERE / "ledger-readback.json", {table: [dict(r) for r in owner.db.execute("SELECT * FROM " + table)]
             for table in ("writers", "operations", "jobs", "launches", "effects")})
        owner.close()


def binding_check():
    from tests.fixtures.symphony_pm_ingress.owner import Token
    class ProbePort:
        token = Token(NAMESPACE, "probe-only", 1, time.time() + 60)
        calls = 0

        def get_status(self, request, context):
            self.calls += 1
            raise RuntimeError("Intentional no-effect binding probe")

    port = ProbePort()
    module, call = plugin_binding(port)
    try:
        payload = {"contract_version": pm.CONTRACT_VERSION, "operation": "get_status",
                   "request_id": "binding-probe", "project_ref": PROJECT,
                   "job_ref": "probe-job", "expected_job_revision": "a" * 64}
        response = call(payload)
        assert port.calls == 1 and response["outcome"] == "UNKNOWN" and response["hold"]
        save(HERE / "binding-check.json", {"status": "PASS", "port_calls": port.calls,
                                          "native_attempts": 0, "response": response})
    finally:
        module.unwire()


def reconcile_unstarted():
    """Readback of the failed local admission; never clear or reissue its durable ticket."""
    assert (HERE / "run-failure.txt").exists()
    owner = LiveOwner()
    try:
        rows = [dict(r) for r in owner.db.execute("SELECT * FROM launches")]
        assert len(rows) == 1 and rows[0]["binding"] is None and rows[0]["state"] == "DISPATCH_INTENT"
        assert [p.name for p in (HERE / "attempt-1").iterdir()] == ["prompt.txt"]
        # NativeRunner durably writes command.json before reaching start_suspended/Popen.
        # The parent invocation has exited (exec result exit=1). No native boundary was entered.
        from winjob import open_job, close_handle, C
        handle = open_job(4, False, "Local\\HermesSmoke-" + rows[0]["attempt"])
        if handle:
            close_handle(handle)
            raise RuntimeError("Prior job still exists; HOLD")
        assert C.get_last_error() == 2
        save(HERE / "pre-native-reconciliation.json", {"native_dispatch_not_called": True,
             "native_attempts": 0, "prepared_ticket": rows[0], "native_command_file_absent": True,
             "job_object_absent_error": 2, "previous_controller": "exec invocation exited 1",
             "failure_sha256": sha(HERE / "run-failure.txt"),
             "decision": "continue same never-dispatched ticket; no retry, no ticket reset"})
    finally:
        owner.close()


def finalize_failed_native():
    """No launch/retry. Preserve one failed attempt and independently inspect exact PID."""
    from winjob import open_job, close_handle, C, open_process, creation_ticks, wait_handle
    binding = load(HERE / "attempt-1/binding.json")
    result = load(HERE / "attempt-1/driver-result.json")
    assert result["returncode"] == 1
    hp = open_process(0x100000 | 0x1000, False, binding["pid"])
    process_proof = {"pid": binding["pid"], "expected_start_ticks": binding["start_ticks"]}
    if hp:
        try:
            ticks = creation_ticks(hp)
            process_proof.update(actual_start_ticks=ticks, identity_matches=ticks == binding["start_ticks"],
                                 signaled=wait_handle(hp, 0) == 0)
        finally:
            close_handle(hp)
    else:
        err = C.get_last_error()
        process_proof.update(absent=err == 87, error=err)
    job = open_job(4, False, binding["job_object"])
    if job:
        close_handle(job)
        job_proof = {"absent": False}
    else:
        err = C.get_last_error()
        job_proof = {"absent": err == 2, "error": err}
    native_events = (HERE / "attempt-1/stdout.jsonl").read_text(encoding="utf-8")
    assert native_events == ""
    summary = {"status": "HOLD", "successful_live_launch": "FAIL_CONFIG_BEFORE_MODEL_SESSION",
               "cancel_live": "NOT_RUN_HOLD_FIRST_RESULT_MISSING", "native_attempts": 1,
               "native_cancel_calls": 0, "native_session": None, "automatic_retry": False,
               "job": binding["job"], "request": "smoke-request-1", "attempt": binding["attempt"],
               "result": result, "stderr": (HERE / "attempt-1/stderr.txt").read_text(encoding="utf-8"),
               "root_process_readback": process_proof, "named_job_readback": job_proof,
               "descendants_independent_terminal_proof": "UNKNOWN: job closed after active=0 controller readback",
               "external_effects_settled": "UNKNOWN", "billing_settled": "UNKNOWN",
               "retry_knobs_on_builtin_openai": "UNSUPPORTED_BY_INSTALLED_CLI",
               "follow_up": "Owner direction required before any new exec; no automatic continuation"}
    save(HERE / "native-campaign-hold.json", summary)
    owner = LiveOwner()
    try:
        with owner.transaction():
            assert owner.db.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 1
            assert owner.db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
            owner.db.execute("UPDATE launches SET state='FAILED_CONFIG_HOLD',result=? WHERE attempt=?",
                             (json.dumps(summary), binding["attempt"]))
    finally:
        owner.close()
    log("FAILED_ATTEMPT_RECONCILED", native_attempts=1, native_cancel_calls=0,
        model_session=None, process_proof=process_proof, job_proof=job_proof)


def config_check():
    """Bundled catalog/config probe only: no refresh, prompt, native exec, or inference."""
    options = ["-c", 'approval_policy="never"', "-c", 'windows.sandbox="unelevated"',
               "-c", "project_doc_max_bytes=0", "-c", "features.multi_agent=false",
               "-c", 'web_search="disabled"', "-c", 'model="gpt-6.1-sol"',
               "-c", 'model_reasoning_effort="high"']
    command = [str(CLI), *options, "debug", "models", "--bundled"]
    result = subprocess.run(command, cwd=HERE / "workspace-1", capture_output=True,
                            encoding="utf-8", timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    catalog = json.loads(result.stdout)
    save(HERE / "compatible-config-check.json", {"status": "PASS_CONFIG_PROBE_ONLY", "command": command,
         "returncode": result.returncode, "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
         "catalog_type": type(catalog).__name__, "model_attempts": 0,
         "live_compatibility": "NOT_RUN", "internal_transport_retries": "NOT_QUALIFIED"})
    log("COMPATIBLE_CONFIG_PROBE_ONLY", native_exec_attempts=0)


def recovery_check():
    """Reopen actual failed-attempt journal and redeliver its submit through real plugin.

    The driver capability is withheld. This checks recovery after the config failure;
    it is explicitly NOT the requested live cancel/redelivery scenario.
    """
    from tests.fixtures.symphony_pm_ingress.owner import Token
    assert load(HERE / "native-campaign-hold.json")["native_attempts"] == 1
    owner = LiveOwner()
    writer = owner.db.execute("SELECT * FROM writers WHERE namespace=?", (NAMESPACE,)).fetchone()
    token = Token(NAMESPACE, writer["owner"], writer["epoch"], writer["expires"])
    port = LivePort(owner, token, driver=None)
    before = [dict(r) for r in owner.db.execute("SELECT * FROM launches")]
    module, call = plugin_binding(port)
    try:
        response = call(submit(1))
        after = [dict(r) for r in owner.db.execute("SELECT * FROM launches")]
        assert response["outcome"] == "SUBMITTED" and response["view"]["scheduler_outcome"] == "DUPLICATE"
        assert before == after and len(after) == 1 and not port.attempts
        save(HERE / "failed-submit-recovery.json", {"status": "PASS_NO_NEW_LAUNCH_AFTER_FAILED_SUBMIT",
             "native_attempts_before": len(before), "native_attempts_after": len(after),
             "response": response, "cancel_recovery": "NOT_RUN"})
    finally:
        module.unwire()
        owner.close()


if __name__ == "__main__":
    try:
        {"prepare": prepare, "process-check": process_check, "binding-check": binding_check,
         "reconcile-unstarted": reconcile_unstarted,
         "finalize-failed-native": finalize_failed_native,
         "config-check": config_check,
         "recovery-check": recovery_check,
         "continue-unstarted": lambda: campaign(continue_unstarted=True),
         "run": campaign, "inspect": inspect}[sys.argv[1]]()
    except BaseException:
        (HERE / (sys.argv[1] + "-failure.txt")).write_text(traceback.format_exc(), encoding="utf-8")
        raise
