"""Offline qualification composition, workload environment, and journal recovery."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from plugins.symphony_pm.status_cli import read_launches
from plugins.symphony_pm.transport import linux_sandbox
from plugins.symphony_pm.transport.linux_bootstrap import linux_exec_transport
from plugins.symphony_pm.transport.linux_launcher import LinuxSettings
from plugins.symphony_pm.transport.linux_lifecycle import LinuxLifecycleOwner
from plugins.symphony_pm.transport.types import Refused
from tests.plugins.symphony_qualification import harness, qualify
from tests.plugins.test_symphony_pm_linux_lifecycle import Guard, Launcher, Native, Observer, rig, state, submit
from tests.plugins.test_symphony_pm_linux_native_contract import report
from tests.plugins.test_symphony_pm_linux_primary_unit import installation
from tests.plugins.test_symphony_pm_serving import _call, loaded
from tests.plugins.symphony_serving_support import approve, submit_payload


@pytest.fixture
def stand(installation):
    base, _ = installation
    hashes = (*base.hashes[:4], harness.CODEX_SHA, base.hashes[5], harness.POLICY_SHA)
    options = asdict(replace(base, hashes=hashes, executable="/usr/bin/codex", probe_python="/usr/bin/python3",
                             permission_profile=harness.PROFILE, environment_ref=harness.PROFILE,
                             namespace="symphony_pm:ingress-linux", command_environment=("CODEX_SANDBOX_NETWORK_DISABLED",)))
    q = harness.StandQualification(**options, fixture_sha256="e"*64)
    manifest = json.dumps({"format": "symphony-qualification-worker/1", "cases": list(harness.CASES),
                           "fixture_sha256": q.fixture_sha256, "policy_sha256": harness.POLICY_SHA,
                           "image_sha256": q.image_sha256, "codex_sha256": harness.CODEX_SHA,
                           "python_sha256": q.hashes[5], "probe_sha256": harness.PROGRAM_SHA}).encode()
    return q, harness.StandCase(manifest, hashlib.sha256(manifest).hexdigest(), "write")


@pytest.mark.parametrize("name", harness.CASES)
def test_stand_bootstrap_keeps_probe_and_uses_only_sealed_case(stand, name, tmp_path):
    q, case = stand
    settings = SimpleNamespace(exec_timeout=35, probe_timeout=5)
    launcher = harness.stand_binding(q, settings, replace(case, case=name), owner_check=None,
                                     approval_transport=None, controller="test").factory.launcher
    request = SimpleNamespace(goal="choose a different command", raw_constraints=("wait",))
    workspace = SimpleNamespace(src=tmp_path)
    call = launcher._call(None, request, workspace, {"port": 4321})
    prefix = ("/usr/bin/codex", "sandbox", "-P", harness.PROFILE, "-C", "/work", "--", "/usr/bin/python3", "-I", "-B")
    assert call.probe_argv == (*prefix, "-c", harness.PROGRAM, call.challenge, '{"port": 4321}')
    assert call.argv == (*prefix, harness.FIXTURE, name) and call.prompt == ""
    (tmp_path / ".codex").mkdir()
    with pytest.raises(Refused, match="PROJECT_CONFIG"):
        launcher._call(None, request, workspace, {})


def test_stand_cannot_enter_production_and_manifests_fail_closed(stand, tmp_path):
    q, case = stand
    settings = LinuxSettings("test", str(tmp_path), str(tmp_path), str(tmp_path), 40, 2, 1, 1, 1)
    with pytest.raises(Refused, match="QUALIFICATION"):
        linux_exec_transport(profile_home=q.profile_home, namespace=q.namespace, qualification=q, settings=settings,
                             approval_transport=None, scheduler_id=q.scheduler_id, controller="test", trusted_issuers=q.issuer_refs)
    for bad in (replace(case, manifest=case.manifest+b" "), replace(case, case="other")):
        with pytest.raises(Refused, match="MANIFEST"):
            bad.verify(q)
    for bad in (replace(q, fixture_sha256="0"*64), replace(q, image_sha256="0"*64)):
        with pytest.raises(Refused, match="MANIFEST"):
            case.verify(bad)


def test_stand_preserves_outer_environment_and_requires_exact_inner_report(stand, installation):
    q, _ = stand
    base, _ = installation
    base.validate()
    with pytest.raises(Refused, match="QUALIFICATION"):
        replace(base, command_environment=q.command_environment).validate()
    assert set(linux_sandbox.ENVIRONMENT) == {"HOME", "PATH", "TMPDIR", "LANG", "CODEX_HOME"}
    for names in ((), ("HOME",), ("CODEX_SANDBOX_NETWORK_DISABLED", "LANG")):
        with pytest.raises(Refused, match="QUALIFICATION"):
            replace(q, command_environment=names).validate()
    valid = {**report(), "environment": list(q.command_environment)}
    assert linux_sandbox.verify_report(valid, q, "challenge", "net:[12]")
    for names in ([], ["CODEX_SANDBOX_NETWORK_DISABLED", "LC_CTYPE"]):
        with pytest.raises(Refused, match="PRECHECK"):
            linux_sandbox.verify_report({**valid, "environment": names}, q, "challenge", "net:[12]")


@pytest.mark.parametrize("env", [{}, {"CODEX_SANDBOX_NETWORK_DISABLED": "0"},
                                 {"CODEX_SANDBOX_NETWORK_DISABLED": "1", "LC_CTYPE": "C"}])
def test_workload_refuses_wrong_environment_before_work(monkeypatch, env):
    monkeypatch.setattr(qualify, "os", SimpleNamespace(environ=env))
    with pytest.raises(SystemExit, match="environment mismatch"):
        qualify.main(["write"])


def test_write_emits_only_exact_marker_and_never_overwrites(monkeypatch, tmp_path):
    monkeypatch.setattr(qualify, "os", SimpleNamespace(environ={"CODEX_SANDBOX_NETWORK_DISABLED": "1"}))
    def disposable(path):
        assert path == "/work/symphony-qualification.txt"
        return tmp_path / "marker"
    monkeypatch.setattr(qualify, "Path", disposable)
    qualify.main(["write"])
    assert (tmp_path/"marker").read_bytes() == b"symphony\n"
    with pytest.raises(FileExistsError):
        qualify.main(["write"])
    with pytest.raises(SystemExit, match="unknown"):
        qualify.main(["write", "another-path"])


def test_changed_boot_with_unsettled_launch_never_reexecutes(tmp_path, record_property):
    first = rig(tmp_path)
    ticket = submit(first, "first")
    old = first.native.created[0]
    assert first.owner.cancel_internal(old.binding, "PM_CANCEL").dispatched
    def durable_effects():
        with sqlite3.connect(first.owner.journal.path) as db:
            launches = db.execute("SELECT attempt, ticket, binding FROM serving_launches ORDER BY attempt").fetchall()
            cancels = db.execute("SELECT effect_key, dispatch_epoch, cancel_at FROM effects ORDER BY effect_key").fetchall()
        return launches, cancels
    before = state(first, ticket)
    launches, cancels = durable_effects()
    assert len(launches) == len(cancels) == len(first.native.created) == old.cancels == 1
    assert launches[0][0] == ticket.attempt and cancels[0][0] == old.binding.effect_key
    guard = Guard(first.scope, "owner-after-reboot", True)
    guard.identity = replace(guard.identity, boot_id="d"*32)
    native = Native()
    second = LinuxLifecycleOwner(first.scope, guard, Observer(first.now), native, Launcher(),
                                 scheduler_id="serving-test", controller="controller", clock=lambda: first.now[0])
    current = SimpleNamespace(owner=second, scope=first.scope, now=first.now)
    row = state(current, ticket)
    assert row["binding"] == before["binding"] and row["boot_id"] == before["boot_id"] == old.binding.boot_id
    assert row["state"] == "HELD_UNKNOWN" and row["hold_reason"] == "CGROUP_STOP_UNPROVEN"
    assert row["stop_proof"] is None and not second.journal.proven_stop(row)
    request, context, _ = approve(current, payload=submit_payload(request_id="second"))
    with pytest.raises(Refused, match="^OBSERVER_COVERAGE_UNKNOWN$"):
        second.submit(request, context)
    assert durable_effects() == (launches, cancels)
    assert not native.created and len(first.native.created) == old.cancels == 1
    keys = ("attempt", "binding", "state", "boot_id", "hold_reason", "stop_proof")
    record_property("L4", {"before": {k: before[k] for k in keys}, "after": {k: row[k] for k in keys},
                           "new_boot": guard.identity.boot_id, "launches": len(launches), "native_cancels": old.cancels,
                           "submit_refusal": "OBSERVER_COVERAGE_UNKNOWN"})


def test_test_profile_loads_at_public_ingress_and_requires_permit(stand, loaded, monkeypatch, tmp_path):
    from hermes_cli.approval_transport import RegisteredApprovalTransport
    from hermes_cli.plugin_request_binding import canonical_home
    from plugins.symphony_pm.transport import linux_bootstrap
    home, host, _ = loaded
    q, case = stand
    q = replace(q, profile_home=canonical_home(home), host_id=Guard.recovery_host_id)
    settings = LinuxSettings("fixture", str(tmp_path / "repo"), str(tmp_path / "copies"),
                             str(tmp_path / "control"), 40, 2, 1, 1, 1)
    guards = []
    def local_guard(scope, _state, _installation):
        guard = Guard(scope, "stand-owner", False)
        guards.append(guard)
        return guard
    monkeypatch.setattr(linux_bootstrap, "LinuxOwnershipGuard", local_guard)
    monkeypatch.setattr(harness.StandQualification, "verify", harness.StandQualification.validate)
    channel = RegisteredApprovalTransport("fixture", lambda *_: pytest.fail("unexpected prompt"),
                                          "fixture", q.profile_home)
    binding = harness.stand_binding(q, settings, case, owner_check=None, approval_transport=channel,
                                    controller="test")
    host.configure(home, {("symphony_pm", "ingress-linux"): binding})
    host.start_profile(home)
    try:
        assert _call(submit_payload())["outcome"] == "WAITING_FOR_AUTHORITY"
        assert len(guards) == 1 and guards[0].owned
        assert not Path(settings.state).exists()
    finally:
        host.stop()
    assert not guards[0].owned


def test_populated_group_holds_capture_after_root_exit(tmp_path, record_property):
    r = rig(tmp_path)
    ticket = submit(r, "descendants")
    r.observer.modify = lambda proof: replace(
        proof, launch_closed=True, populated=1,
        processes=tuple(replace(p, signaled=True) for p in proof.processes))
    assert not r.owner.finish_client_job(r.owner.token, ticket)[0]
    assert not r.owner.journal.proven_stop(state(r, ticket))
    with pytest.raises(Refused, match="CAPTURE_REQUIRES_SETTLED_CGROUP"):
        r.owner.journal.append_observation(r.owner.token, ticket, {"kind": "CAPTURE", "patch": {}})
    with pytest.raises(Refused, match="^UNSETTLED_PRIOR_LAUNCH_HOLD$"):
        submit(r, "held")
    with sqlite3.connect(r.owner.journal.path) as db:
        assert db.execute("SELECT attempt FROM serving_launches").fetchall() == [(ticket.attempt,)]
    r.observer.stopped = True
    r.observer.modify = lambda proof: replace(proof, generation=proof.generation + 1)
    r.owner.reconcile()
    assert not r.owner.journal.proven_stop(state(r, ticket))
    r.observer.modify = lambda proof: proof
    r.owner.reconcile()
    row = state(r, ticket)
    assert row["state"] == "STOPPED" and row["stop_proof"] == "CGROUP_EMPTY"
    assert r.owner.journal.proven_stop(row)
    capture = {"kind": "CAPTURE", "source_sha": ticket.plan.source_sha, "patch": {"sha256": "e"*64}}
    r.owner.journal.append_observation(r.owner.token, ticket, capture)
    saved, = read_launches(r.owner.journal.path)
    assert saved["attempt"] == ticket.attempt and saved["observations"][-1]["body"] == capture
    next_ticket = submit(r, "after-stop")
    assert next_ticket.attempt != ticket.attempt
    record_property("L3", {"attempt": ticket.attempt, "populated_before": 1, "capture_held": True,
                           "reserve_held": True, "wrong_generation_held": True, "terminal": row["state"],
                           "proof": json.loads(row["proof_source"]), "next_attempt": next_ticket.attempt,
                           "native_criterion": "OPEN"})
