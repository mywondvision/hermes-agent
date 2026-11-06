"""Offline invariants for task-local smoke cancellation; no worker/process is launched."""
import importlib.util
import json
from dataclasses import asdict
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.fixtures.symphony_pm_ingress.owner import Owner, Target
from tools import symphony_pm_adapter as pm

ART = Path(__file__).resolve().parents[2] / ".artifacts/live-smoke-20261106"


@pytest.fixture
def ctl(monkeypatch):
    monkeypatch.syspath_prepend(str(ART))
    import cancel_control
    return cancel_control


class Crash(BaseException):
    pass


class Rig:
    def __init__(self, path, ctl):
        self.path, self.ctl, self.now = path, ctl, 1000.0
        self.calls = []
        self.stop_proof = False
        self.raise_after_effect = False
        self.before_native = None
        self.hook = None
        owner = self.factory()
        self.token = owner.claim("controller-1", self.now, ttl=60)
        request = pm.SubmitIntent("submit", "synthetic", "read input", ())
        payload = dict(request.payload(), request_id=request.request_id)
        job, rev, _ = owner.submit(request, self.context(payload, self.token), self.token, self.now)
        self.target = Target("offline-cancel-test", "synthetic", job, rev, "attempt-one",
                             request.request_digest, self.token.epoch, "codex", "thread-one", "4242", "123456")
        with owner.transaction():
            owner.db.execute("UPDATE jobs SET target=? WHERE job=?", (json.dumps(asdict(self.target)), job))
        owner.close()
        self.receiver = self.new_receiver()
        self.engine = self.new_engine()

    def factory(self):
        return Owner(self.path, namespace="offline-cancel-test")

    def context(self, payload, token):
        request, status, issues = pm.parse_intent(payload)
        assert status == "OK" and not issues
        return pm.TrustedPmContextV1("test-context", "test-intake", "synthetic", pm.OPERATIONS,
                                    request.request_id, "test-policy", "test-grant", self.now + 30,
                                    self.now, 60, "test-owner", token.epoch, "a" * 64,
                                    approved_scope_revision="one-scope", approved_payload_digest=request.payload_digest,
                                    expected_job_ref=getattr(request, "job_ref", ""),
                                    expected_job_revision=getattr(request, "expected_job_revision", ""))

    def observe(self, target):
        stopped = self.stop_proof
        return {"target": asdict(target), "observed_at": self.now,
                "active": 0 if stopped else 1, "pids": [] if stopped else [4242],
                "tree_stopped": stopped, "known_processes": [
                    {"pid": 4242, "start_ticks": "123456", "identity_matches": True, "signaled": stopped}]}

    def new_receiver(self):
        rig = self

        class Receiver:
            def __init__(self):
                self.gate = rig.ctl.NativeCancelGate()

            def check_binding(self, target):
                assert target == rig.target

            def cancel_once(self, permit=None):
                def native():
                    if rig.before_native:
                        rig.before_native()
                    rig.calls.append(permit.effect_key)
                    if rig.raise_after_effect:
                        raise TimeoutError("reply lost after effect")
                return self.gate.dispatch(permit, native)
        return Receiver()

    def new_engine(self, receiver="current"):
        return self.ctl.CancellationCoordinator(self.factory, self.token,
                                                self.receiver if receiver == "current" else receiver,
                                                self.observe, self.context, clock=lambda: self.now,
                                                hook=lambda point: self.hook(point) if self.hook else None)

    def pm_cancel(self, request_id="pm-one", engine=None):
        request = pm.CancelIntent(request_id, "synthetic", self.target.job, self.target.revision, "owner request")
        context = self.context(dict(request.payload(), request_id=request.request_id), self.token)
        return (engine or self.engine).cancel(self.target, "PM_CANCEL", request=request, context=context)

    def reopen(self, *, with_receiver=True):
        # A new connection/epoch/receiver loses every in-memory send latch.
        self.now = self.token.expires_at + 1
        owner = self.factory()
        self.token = owner.claim("controller-recovered", self.now, ttl=60)
        owner.close()
        self.receiver = self.new_receiver()
        self.engine = self.new_engine(receiver="current" if with_receiver else None)

    def rows(self, table):
        owner = self.factory()
        try:
            return [dict(row) for row in owner.db.execute("SELECT * FROM " + table)]
        finally:
            owner.close()


@pytest.fixture
def rig(tmp_path, ctl):
    return Rig(tmp_path / "owner.sqlite", ctl)


def test_timeout_pm_race_one_ticket_and_all_causes_retained(rig):
    barrier = threading.Barrier(2)
    first = rig.new_engine()
    second = rig.new_engine()

    def timeout():
        barrier.wait(timeout=5)
        return first.cancel(rig.target, "TIMEOUT")

    def pm_cancel():
        barrier.wait(timeout=5)
        return rig.pm_cancel(engine=second)

    with ThreadPoolExecutor(2) as pool:
        outcomes = [future.result(timeout=10) for future in (pool.submit(timeout), pool.submit(pm_cancel))]
    assert len(rig.calls) == 1 and len(rig.rows("effects")) == 1
    assert {row["cause"] for row in rig.rows("cancel_causes")} == {"TIMEOUT", "PM_CANCEL"}
    assert sum(r.dispatched for r in outcomes) == 1
    assert all(r.hold and r.reservation == "POSSIBLY_CONSUMED_HELD" for r in outcomes)


def test_new_request_id_timeout_and_cleanup_cannot_mint_another_ticket(rig):
    assert rig.pm_cancel().dispatched
    rig.pm_cancel("pm-another-id")
    rig.engine.cancel(rig.target, "TIMEOUT")
    rig.engine.cancel(rig.target, "EMERGENCY_CLEANUP")
    assert len(rig.calls) == 1 and len(rig.rows("effects")) == 1
    assert len(rig.rows("cancel_causes")) == 4


@pytest.mark.parametrize("point,expected", [("before_intent", 1), ("after_intent", 0),
                                           ("after_effect", 1), ("after_result", 1)])
def test_crash_boundaries_recovery_never_resends_spent_ticket(rig, point, expected):
    rig.hook = lambda at: (_ for _ in ()).throw(Crash(at)) if at == point else None
    with pytest.raises(Crash):
        rig.pm_cancel()
    calls_before_reopen = len(rig.calls)
    rig.hook = None
    rig.reopen()
    result = rig.pm_cancel("new-delivery-after-recovery")
    assert len(rig.calls) == expected
    if point != "before_intent":
        assert len(rig.calls) == calls_before_reopen and not result.dispatched
    assert result.hold and result.reservation == "POSSIBLY_CONSUMED_HELD"


def test_lost_native_reply_unknown_requires_readback_without_another_send(rig):
    rig.raise_after_effect = True
    assert rig.pm_cancel().state == "UNKNOWN"
    assert rig.rows("effects")[0]["state"] == "UNKNOWN"
    rig.reopen()
    again = rig.pm_cancel("redelivery")
    assert again.state == "UNKNOWN" and len(rig.calls) == 1
    rig.stop_proof = True
    reconciled = rig.pm_cancel("readback-only")
    assert reconciled.local_stopped and reconciled.hold and len(rig.calls) == 1
    # Partial local proof is not promoted to four settled cancellation claims.
    assert rig.rows("effects")[0]["state"] == "UNKNOWN"


def test_lost_pm_response_after_result_reopens_without_receiver(rig):
    rig.pm_cancel()  # Discard receipt, as if transport lost it after durable result commit.
    rig.reopen(with_receiver=False)
    result = rig.pm_cancel("new-transport-delivery")
    assert not result.dispatched and result.state == "UNKNOWN" and len(rig.calls) == 1
    rig.stop_proof = True
    assert rig.pm_cancel("observer-recovery").local_stopped


@pytest.mark.parametrize("mutation", ["empty", "wrong_pid", "wrong_start", "wrong_attempt", "stale"])
def test_partial_or_foreign_stop_proof_cannot_release_hold(rig, mutation):
    rig.pm_cancel()
    rig.stop_proof = True
    original = rig.observe

    def broken(target):
        proof = original(target)
        if mutation == "empty":
            proof["known_processes"] = []
        elif mutation == "wrong_pid":
            proof["known_processes"][0]["pid"] = 9999
        elif mutation == "wrong_start":
            proof["known_processes"][0]["start_ticks"] = "other-start"
        elif mutation == "wrong_attempt":
            proof["target"]["attempt"] = "foreign-attempt"
        else:
            proof["observed_at"] -= 100
        return proof
    rig.engine.observer = broken
    outcome = rig.pm_cancel("untrusted-proof")
    assert outcome.state == "UNKNOWN" and not outcome.local_stopped and outcome.hold
    assert len(rig.calls) == 1


def test_native_receiver_latch_is_spent_even_when_call_throws(ctl, rig):
    gate = ctl.NativeCancelGate()
    permits = [ctl.DispatchPermit(rig.target, lambda: None) for _ in range(2)]
    called = []

    def native():
        called.append("native")
        raise OSError("uncertain result")
    with pytest.raises(OSError):
        gate.dispatch(permits[0], native)
    assert gate.dispatch(permits[1], native) is False
    assert called == ["native"] and gate.dispatch_count == 1


def test_already_terminal_attempt_needs_no_native_cancel(rig):
    rig.stop_proof = True
    outcome = rig.engine.cancel(rig.target, "EMERGENCY_CLEANUP")
    assert outcome.local_stopped and outcome.hold and not rig.calls and not rig.rows("effects")
    assert rig.rows("cancel_causes")[0]["cause"] == "EMERGENCY_CLEANUP"


@pytest.mark.platforms("windows")
def test_windows_receiver_refuses_unticketed_cancel(monkeypatch, ctl):
    spec = importlib.util.spec_from_file_location("_smoke_winjob_test", ART / "winjob.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module, "terminate_job", lambda *_: calls.append("native") or 1)
    job = object.__new__(module.WindowsJob)
    job.handle = 123
    with pytest.raises(ctl.CancelRejected, match="DURABLE_PERMIT_REQUIRED"):
        job.cancel_once()
    assert calls == []


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("pm_first", [False, True])
@pytest.mark.parametrize("cancel_error", [False, True])
def test_real_runner_timeout_routes_to_ticket_and_keeps_both_wait_limits(rig, monkeypatch, pm_first, cancel_error):
    from types import ModuleType, SimpleNamespace
    import subprocess
    import sys
    spec = importlib.util.spec_from_file_location("_smoke_timeout_test", ART / "smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    process_module = ModuleType("orchestration.process")
    process_module.CommandResult = lambda argv, rc, timed_out=False: SimpleNamespace(
        argv=argv, returncode=rc, timed_out=timed_out)
    process_module.redact = lambda text: text
    monkeypatch.setitem(sys.modules, "orchestration.process", process_module)
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_kw: pytest.fail("native process forbidden"))
    waits = []

    class FakeProcess:
        pid = 4242

        def wait(self, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                if pm_first:
                    rig.pm_cancel()
                raise subprocess.TimeoutExpired("synthetic-native", timeout)
            return 123

    class Binder:
        def bind(self, *_args):
            return rig.target

        def close(self):
            pass

    monkeypatch.setattr(module, "LiveOwner", Binder)
    control = rig.receiver
    control.process = FakeProcess()
    control.start_suspended = lambda *_args: {"pid": 4242, "start_ticks": "123456", "job_object": "probe"}
    control.resume = lambda _prompt: None
    folder = rig.path.parent / "attempt"
    folder.mkdir()
    def timeout_cancel(cause):
        if cancel_error:
            raise RuntimeError("store unavailable")
        return rig.engine.cancel(rig.target, cause)

    attempt = SimpleNamespace(started=threading.Event(), folder=folder, control=control, token=rig.token,
                              request=pm.SubmitIntent("submit", "synthetic", "read input", ()), job=rig.target.job,
                              revision=rig.target.revision, attempt=rig.target.attempt, target=None,
                              timed_out=False, returncode=None, bind_cancel_route=lambda: None,
                              cancel=timeout_cancel)
    runner = module.NativeRunner()
    runner.active = attempt
    runner.driver = SimpleNamespace(reject_bypass=lambda _argv: None)
    def execute():
        return runner(["synthetic-codex.exe", "exec", "--sandbox", "read-only"], cwd=folder,
                      timeout=180, stdin_text="read only synthetic file")
    if cancel_error:
        with pytest.raises(RuntimeError, match="store unavailable"):
            execute()
    else:
        result = execute()
        assert result.timed_out and attempt.returncode == 123
    rig.pm_cancel("after-timeout")
    assert waits == [180, 15] and attempt.timed_out
    assert len(rig.calls) == 1
    expected = {"PM_CANCEL"} if cancel_error else {"TIMEOUT", "PM_CANCEL"}
    assert {row["cause"] for row in rig.rows("cancel_causes")} == expected


@pytest.mark.platforms("windows")
def test_explicit_cleanup_uses_same_unknown_ticket_without_another_native_send(rig, monkeypatch):
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("_smoke_close_test", ART / "winjob.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rig.raise_after_effect = True
    assert rig.pm_cancel().state == "UNKNOWN"
    job = object.__new__(module.WindowsJob)
    job.handle, job.process, job.process_handles = 123, SimpleNamespace(pid=4242), {}
    job._cancel_route = lambda cause: rig.engine.cancel(rig.target, cause)
    closed = []
    monkeypatch.setattr(module, "close_handle", lambda handle: closed.append(handle) or 1)
    job.close()
    assert closed == [123] and len(rig.calls) == 1
    assert rig.rows("effects")[0]["state"] == "UNKNOWN"
    assert {row["cause"] for row in rig.rows("cancel_causes")} == {"PM_CANCEL", "EMERGENCY_CLEANUP"}
