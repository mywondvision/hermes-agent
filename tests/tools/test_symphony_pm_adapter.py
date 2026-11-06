"""Offline contract tests for ``tools/symphony_pm_adapter.py`` (ADD-HERMES-PM-INGRESS-01, cases 1-8).

Source/fake-only. The adapter talks to a synthetic Symphony scheduler port; case 4 drives that fake
scheduler into the ACTUAL frozen S2 PRIMARY source (``.artifacts/frozen-s2``), loaded under an
isolated namespace after a byte/hash check against its snapshot manifest, with ``sys.path`` and
``sys.modules`` restored. PASS proves offline source-contract conformance only: no real scheduler
ingress, native launch, registered Hermes runtime, authentication or live completion.
"""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import importlib
import importlib.util
import inspect
import json
import sys
import threading
import types
from pathlib import Path
from unittest import mock

import pytest

from tools import symphony_pm_adapter as pm

REPO = Path(__file__).resolve().parents[2]
FROZEN = REPO / ".artifacts" / "frozen-s2"
P0 = "cb4e5f37f56e9dac062506a566d7cfc0febf70d4a5556a56a76af28f01ae5ece"
CANDIDATE = "7aa4321ce3e7940b7d2191979a94be88d95f3ce79fe429631b4e76cdacdf88fa"
S2_NS = "_hermes_pm_test_frozen_s2"
_S2_OWNED = ("catalog", "orchestration", S2_NS)

SCHED = "symphony-fake"
ISSUER = "pm-intake-fake"
EPOCH = 3
NOW = 1000.0
JOB = "job-1"
REV = "a" * 64
ATTEMPT = "attempt-1"

BASE = "3" * 40
CHECKPOINT = "4" * 40
REVISION = "5" * 64


# ----------------------------------------------------------------------------- side-effect spies

_TRAPS = ("subprocess.Popen", "subprocess.run", "os.system", "os.kill", "socket.socket",
          "socket.create_connection", "shutil.rmtree", "os.remove", "os.unlink", "os.rmdir",
          "os.getenv", "os.path.expanduser", "pathlib.Path.home", "builtins.open", "io.open",
          "threading.Thread.start", "importlib.import_module")


@contextlib.contextmanager
def guarded():
    """Fail on adapter network/subprocess/native/fs/home/env/thread/late-import side effects.

    Traps record before raising because the adapter turns port exceptions into UNKNOWN."""
    hits = []

    def trap(name):
        def _raise(*_a, **_k):
            hits.append(name)
            raise AssertionError("adapter side effect forbidden: " + name)
        return _raise

    threads = threading.active_count()
    with contextlib.ExitStack() as stack:
        for target in _TRAPS:
            stack.enter_context(mock.patch(target, side_effect=trap(target)))
        yield hits
    assert hits == []
    assert threading.active_count() == threads


# ----------------------------------------------------------------------------- independent oracles

def independent_payload(p: dict) -> dict:
    d = {k: v for k, v in p.items() if k != "request_id"}
    if p["operation"] == "request_cancel":
        d.setdefault("reason", "")
    return d


def independent_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def independent_digest(p: dict) -> str:
    return hashlib.sha256(independent_bytes(independent_payload(p))).hexdigest()


def submit_payload(**over):
    p = {"contract_version": pm.CONTRACT_VERSION, "operation": "submit_intent",
         "request_id": "req-1", "project_ref": "proj-alpha",
         "goal": "Add a status page. Do NOT touch prod; never delete branches.",
         "raw_constraints": ["не трогать prod", "do not push", "NOT: skip tests"]}
    p.update(over)
    return p


def job_payload(op="get_status", **over):
    p = {"contract_version": pm.CONTRACT_VERSION, "operation": op, "request_id": "req-9",
         "project_ref": "proj-alpha", "job_ref": JOB, "expected_job_revision": REV}
    p.update(over)
    return p


def ctx(payload, **over) -> pm.TrustedPmContextV1:
    values = dict(context_ref="ctx-1", issuer_ref=ISSUER, project_ref="proj-alpha",
                  allowed_operations=pm.OPERATIONS, request_id=payload["request_id"],
                  policy_ref="policy-1", grant_ref="grant-ref-1", expires_at=NOW + 60, now=NOW,
                  max_age_seconds=30, scheduler_id=SCHED, scheduler_epoch=EPOCH, contract_digest=P0,
                  trusted_producers=("symphony-readback",), required_checks=("unit", "boundary"))
    if payload["operation"] == "submit_intent":
        values.update(approved_scope_revision="scope-rev-1",
                      approved_payload_digest=independent_digest(payload))
    else:
        values.update(expected_job_ref=payload["job_ref"],
                      expected_job_revision=payload["expected_job_revision"])
    values.update(over)
    return pm.TrustedPmContextV1(**values)


def adapter(port):
    return pm.SymphonyPmAdapter(port, trusted_issuers=(ISSUER,), scheduler_id=SCHED)


def run(port, payload, context):
    with guarded():
        return adapter(port).handle(payload, trusted_context=context)


# ----------------------------------------------------------------------------- fake sole scheduler

class FakeScheduler:
    """Synthetic sole owner: dedupe map, canonical job identity, attempts and reservations live
    here and only here, solely to exercise the boundary. Not a production scheduler."""

    def __init__(self):
        self.calls = []
        self.jobs = {}
        self.admitted = {}
        self.fail = None
        self.lost = False
        self.status_receipt = None
        self.cancel_receipt = None

    @staticmethod
    def digest(intent):
        return hashlib.sha256(independent_bytes(intent.payload())).hexdigest()

    def common(self, intent, context, **extra):
        values = dict(contract_version=pm.CONTRACT_VERSION, contract_digest=context.contract_digest,
                      request_id=intent.request_id, payload_digest=self.digest(intent),
                      project_ref=intent.project_ref, issuer_ref=context.issuer_ref,
                      scheduler_id=SCHED, scheduler_epoch=EPOCH, observed_at=NOW - 1)
        values.update(extra)
        return values

    def _enter(self, op, intent):
        self.calls.append((op, intent))
        if self.fail is not None:
            raise self.fail
        return not self.lost

    def submit_intent(self, request, context):
        if not self._enter("submit_intent", request):
            return None
        digest = self.digest(request)
        if type(request) is not pm.SubmitIntent or digest != context.approved_payload_digest:
            return pm.PmSubmissionReceipt(**self.common(request, context, outcome="REJECTED"))
        known = self.jobs.get(request.request_id)
        if known is None:
            job_ref = "job-%d" % (len(self.jobs) + 1)
            known = (digest, job_ref, hashlib.sha256((job_ref + digest).encode()).hexdigest(), request)
            self.jobs[request.request_id] = known
            outcome = "SUBMITTED"
        else:
            outcome = "DUPLICATE" if known[0] == digest else "CONFLICT"
        return pm.PmSubmissionReceipt(**self.common(
            request, context, outcome=outcome, job_ref=known[1], job_revision=known[2],
            evidence_refs=("sched-ack-" + known[1],)))

    def get_status(self, query, context):
        return self.status_receipt if self._enter("get_status", query) else None

    def request_cancel(self, request, context):
        return self.cancel_receipt if self._enter("request_cancel", request) else None

    def admit(self, job_ref, *, reserve=True):
        """Scheduler-side admission: allocate attempt + (optionally) a reserved permit."""
        attempt = job_ref + "-attempt-1"
        self.admitted[job_ref] = (attempt, ("rsv-" + job_ref) if reserve else None)
        return self.admitted[job_ref]


def status_receipt(payload, context, **over):
    values = dict(contract_version=pm.CONTRACT_VERSION, contract_digest=P0,
                  request_id=payload["request_id"], payload_digest=independent_digest(payload),
                  project_ref="proj-alpha", issuer_ref=ISSUER, scheduler_id=SCHED,
                  scheduler_epoch=EPOCH, job_ref=JOB, job_revision=REV, attempt_ref=ATTEMPT,
                  phase="RUNNING", observed_at=NOW - 5, evidence_refs=("obs-1",))
    values.update(over)
    return pm.PmStatusReceipt(**values)


def cancel_receipt(payload, **over):
    values = dict(contract_version=pm.CONTRACT_VERSION, contract_digest=P0,
                  request_id=payload["request_id"], payload_digest=independent_digest(payload),
                  project_ref="proj-alpha", issuer_ref=ISSUER, scheduler_id=SCHED,
                  scheduler_epoch=EPOCH, job_ref=JOB, job_revision=REV, outcome="CANCEL_REQUESTED",
                  observed_at=NOW - 1, reservation_state="POSSIBLY_CONSUMED",
                  evidence_refs=("cancel-ack-1",))
    values.update(over)
    return pm.PmCancelReceipt(**values)


def observation(**over):
    values = dict(producer="symphony-readback", evidence_ref="obs-1", job_ref=JOB, job_revision=REV,
                  attempt_ref=ATTEMPT, scheduler_epoch=EPOCH, phase="RUNNING", observed_at=NOW - 5)
    values.update(over)
    return pm.TrustedObservation(**values)


# ----------------------------------------------------------------------------- frozen S2 loader

class S2Hold(Exception):
    """Frozen source missing, changed or colliding: HOLD, never repaired."""


def _owned(name):
    return any(name == root or name.startswith(root + ".") for root in _S2_OWNED)


def _verify_snapshot():
    manifest_path = FROZEN / "snapshot-manifest.json"
    if not manifest_path.is_file():
        raise S2Hold("frozen S2 snapshot missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("p0"), manifest.get("candidate")) != (P0, CANDIDATE):
        raise S2Hold("snapshot manifest P0/candidate mismatch")
    for entry in manifest["files"]:
        data = (FROZEN / entry["path"]).read_bytes()
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise S2Hold("frozen file changed: " + entry["path"])
    return manifest


def _package(name, directory):
    spec = importlib.util.spec_from_file_location(
        name, directory / "__init__.py", submodule_search_locations=[str(directory)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


def load_frozen_s2():
    """Scoped loader. Frozen modules insert their own dir into a throwaway sys.path copy; the
    original list object, prior ``catalog``/``orchestration`` modules and bytecode policy are
    restored. The orchestration package lives under an isolated namespace."""
    manifest = _verify_snapshot()
    saved_path, saved_dwb = sys.path, sys.dont_write_bytecode
    saved_path_copy = list(saved_path)
    saved = {k: v for k, v in sys.modules.items() if _owned(k)}
    before = set(sys.modules)
    try:
        for name in saved:
            del sys.modules[name]
        sys.path = list(saved_path)
        sys.dont_write_bytecode = True
        _package("catalog", FROZEN / "tools" / "catalog")
        _package(S2_NS, FROZEN / "tools" / "orchestration")
        primary = importlib.import_module(S2_NS + ".primary")
        maintainer = sys.modules[S2_NS + ".maintainer"]
        stray = [k for k in set(sys.modules) - before if not _owned(k)
                 and FROZEN in Path(getattr(sys.modules[k], "__file__", None) or REPO).resolve().parents]
        if stray or any(k == "orchestration" or k.startswith("orchestration.") for k in sys.modules):
            raise S2Hold("source package collision: %s" % sorted(stray))
    finally:
        for name in [k for k in sys.modules if _owned(k)]:
            del sys.modules[name]
        sys.modules.update(saved)
        sys.path, sys.dont_write_bytecode = saved_path, saved_dwb
        for key in [k for k in sys.path_importer_cache if str(FROZEN) in str(k)]:
            del sys.path_importer_cache[key]
    assert sys.path is saved_path and sys.path == saved_path_copy
    return types.SimpleNamespace(primary=primary, maintainer=maintainer, manifest=manifest)


@pytest.fixture(scope="module")
def s2():
    try:
        return load_frozen_s2()
    except S2Hold as exc:
        pytest.fail("HOLD: " + str(exc))


# =============================================================================== case 1

def test_case1_real_deterministic_projection():
    sched = FakeScheduler()
    p1 = submit_payload()
    p2 = submit_payload(request_id="req-2", goal="Refactor the parser; do not change the public API.",
                        raw_constraints=["НЕ удалять ветки", "keep tests green", "no force push"])
    i1, status, issues = pm.parse_intent(p1)
    i2 = pm.parse_intent(p2)[0]
    assert (status, issues) == ("OK", ())
    # Raw goal and prohibitions (incl. negation and order) survive exactly; nothing sanitized.
    assert i1.goal == p1["goal"] and i1.raw_constraints == tuple(p1["raw_constraints"])
    assert i1.canonical_bytes() == independent_bytes(dict(independent_payload(p1), request_id="req-1"))
    assert i1.payload_digest == independent_digest(p1)
    assert i1.canonical_bytes() != i2.canonical_bytes() and i1.payload_digest != i2.payload_digest
    again = pm.parse_intent(copy.deepcopy(p1))[0]
    assert again == i1 and again.canonical_bytes() == i1.canonical_bytes()
    reordered = pm.parse_intent(submit_payload(raw_constraints=list(reversed(p1["raw_constraints"]))))[0]
    assert reordered.payload_digest != i1.payload_digest  # ordering is content
    # The projected request carries no adapter-created attempt/permit/reservation/model.
    names = {f.name for f in dataclasses.fields(pm.SubmitIntent)}
    assert not {n for n in names if any(w in n for w in ("attempt", "permit", "reserv", "model", "epoch"))}

    r1 = run(sched, p1, ctx(p1))
    r2 = run(sched, p2, ctx(p2))
    assert [c[1] for c in sched.calls] == [i1, i2]  # port received exactly the module projection
    assert (r1.outcome, r1.hold, r1.request_id) == ("SUBMITTED", False, "req-1")
    assert (r2.outcome, r2.request_id) == ("SUBMITTED", "req-2")
    assert r1.view["job_ref"] != r2.view["job_ref"]  # identity allocated by the scheduler
    assert r1.view["admission"] == "NOT_CONFIRMED"
    assert sched.admitted == {}  # submission alone creates no attempt/reservation
    json.loads(pm.make_pm_tool_handler()(p1))  # handler output is a JSON string


# =============================================================================== case 2

def test_case2_authority_and_allowlist():
    p = submit_payload()
    good = ctx(p)
    control = FakeScheduler()
    assert run(control, p, good).outcome == "SUBMITTED" and len(control.calls) == 1

    bad_contexts = {
        "absent": (None, "WAITING_FOR_AUTHORITY"),
        "serialized_dict": (dataclasses.asdict(good), "WAITING_FOR_AUTHORITY"),
        "expired": (dataclasses.replace(good, now=NOW + 60), "REJECTED"),
        "foreign_issuer": (dataclasses.replace(good, issuer_ref="other-issuer"), "REJECTED"),
        "foreign_scheduler": (dataclasses.replace(good, scheduler_id="other-sched"), "REJECTED"),
        "project_mismatch": (dataclasses.replace(good, project_ref="proj-beta"), "REJECTED"),
        "operation_not_granted": (dataclasses.replace(good, allowed_operations=("get_status",)), "REJECTED"),
        "no_scope_revision": (dataclasses.replace(good, approved_scope_revision=""), "REJECTED"),
        "other_payload_approved": (dataclasses.replace(good, approved_payload_digest="0" * 64), "REJECTED"),
        "bad_contract_digest": (dataclasses.replace(good, contract_digest="not-a-digest"), "REJECTED"),
    }
    for name, (context, expected) in bad_contexts.items():
        sched = FakeScheduler()
        result = run(sched, p, context)
        assert (name, result.outcome, result.hold) == (name, expected, True)
        assert sched.calls == [], name

    oversized = {"goal": "g" * (pm.TEXT_MAX + 1),
                 "raw_constraints": ["c" * (pm.CONSTRAINT_MAX + 1)]}
    bad_payloads = [
        (dict(p, trusted_context=dataclasses.asdict(good)), "REJECTED"),
        (dict(p, grant="grant-ref-1"), "REJECTED"),
        (dict(p, permit={"reservation": "rsv-1"}), "REJECTED"),
        (dict(p, **{"goal": oversized["goal"]}), "REJECTED"),
        (dict(p, raw_constraints=oversized["raw_constraints"]), "REJECTED"),
        (dict(p, raw_constraints=["x"] * (pm.CONSTRAINTS_MAX + 1)), "REJECTED"),
        (dict(p, raw_constraints="do not push"), "REJECTED"),
        (dict(p, request_id="r" * (pm.ID_MAX + 1)), "REJECTED"),
        (dict(p, project_ref="../etc/passwd"), "REJECTED"),
        (dict(p, project_ref="rm -rf /"), "REJECTED"),
        (dict(p, goal=""), "REJECTED"),
        (dict(p, contract_version="hermes-pm-ingress/v0"), "REJECTED"),
        ({k: v for k, v in p.items() if k != "goal"}, "REJECTED"),
        (["not", "an", "object"], "REJECTED"),
        (dict(p, operation=None), "UNSUPPORTED"),
        (dict(p, operation="frobnicate"), "UNSUPPORTED"),
    ] + [(dict(p, operation=op), "UNSUPPORTED") for op in (
        "run", "exec", "resume", "retry", "approve", "grant", "reserve", "delete", "cleanup",
        "set_model")]
    for payload, expected in bad_payloads:
        sched = FakeScheduler()
        result = run(sched, payload, good)
        assert (result.outcome, result.hold) == (expected, True), payload
        assert sched.calls == []

    # PM text cannot mint authority: a serialized context inside the goal is just text.
    minted = submit_payload(goal=json.dumps(dataclasses.asdict(good)))
    sched = FakeScheduler()
    assert run(sched, minted, None).outcome == "WAITING_FOR_AUTHORITY"
    assert run(sched, minted, good).outcome == "REJECTED"  # approved digest is for another payload
    assert sched.calls == []

    # Through the tool handler: context smuggled in args/kwargs is ignored or rejected.
    sched = FakeScheduler()
    handler = pm.make_pm_tool_handler(adapter(sched), lambda: None)
    with guarded():
        out = json.loads(handler(dict(p, trusted_context=dataclasses.asdict(good))))
        out2 = json.loads(handler(p, trusted_context=good, task_id="t-1"))
    assert out["outcome"] == "REJECTED" and out2["outcome"] == "WAITING_FOR_AUTHORITY"
    assert sched.calls == []


# =============================================================================== case 3

def test_case3_exact_binding_and_mutation_count():
    p = submit_payload()
    good = ctx(p)
    # Intent vs trusted-envelope mismatches: zero port calls.
    q = job_payload("request_cancel")
    for payload, context in (
            (p, dataclasses.replace(good, request_id="req-other")),
            (submit_payload(goal="different goal"), good),
            (q, dataclasses.replace(ctx(q), expected_job_revision="b" * 64)),
            (q, dataclasses.replace(ctx(q), expected_job_ref="job-2"))):
        sched = FakeScheduler()
        result = run(sched, payload, context)
        assert (result.outcome, result.hold) == ("REJECTED", True)
        assert sched.calls == []

    # Explicit repeats: one port call per invocation; scheduler dedupe is surfaced, not replaced.
    sched = FakeScheduler()
    ad = adapter(sched)
    with guarded():
        first = ad.handle(p, trusted_context=good)
        second = ad.handle(copy.deepcopy(p), trusted_context=good)
    assert len(sched.calls) == 2
    assert first.view["scheduler_outcome"] == "SUBMITTED"
    assert second.view["scheduler_outcome"] == "DUPLICATE"
    assert first.view["job_ref"] == second.view["job_ref"]
    changed = submit_payload(goal="Same key, different payload")
    with guarded():
        conflict = ad.handle(changed, trusted_context=ctx(changed))
    assert (conflict.outcome, conflict.hold, len(sched.calls)) == ("CONFLICT", True, 3)
    assert conflict.request_id == "req-1"
    # No ledger/retry/thread lives in the adapter.
    assert not hasattr(ad, "__dict__")
    with pytest.raises(AttributeError):
        ad.ledger = {}

    # Receipt binding mismatches: one call, result UNVERIFIED/HOLD.
    class Forging(FakeScheduler):
        def __init__(self, **changes):
            super().__init__()
            self.changes = changes

        def submit_intent(self, request, context):
            return dataclasses.replace(super().submit_intent(request, context), **self.changes)

    for change in ({"request_id": "req-x"}, {"payload_digest": "0" * 64},
                   {"contract_digest": "1" * 64}, {"contract_version": "other/v1"},
                   {"scheduler_epoch": EPOCH + 1}, {"issuer_ref": "other"},
                   {"scheduler_id": "other"}, {"outcome": "ADMITTED_RUNNING"},
                   {"job_ref": ""}, {"observed_at": NOW + 1}):
        sched = Forging(**change)
        result = run(sched, p, good)
        assert (result.outcome, result.hold) == ("UNVERIFIED", True), change
        assert len(sched.calls) == 1
    q = job_payload()
    sched = FakeScheduler()
    sched.status_receipt = status_receipt(q, ctx(q), job_revision="b" * 64)
    result = run(sched, q, ctx(q, trusted_observations=(observation(),)))
    assert result.outcome == "UNVERIFIED" and "RECEIPT_JOB_REVISION" in result.issues
    assert len(sched.calls) == 1


# =============================================================================== case 4

def _s2_checks(s2, stage, status="pass"):
    m = s2.maintainer
    return m.Gates(stage, tuple(m.Check(n, status, required=True) for n in ("unit", "boundary")))


def project_primary(s2, sched, request_id, *, reserved=True):
    """Fake scheduler projection of its own canonical job/attempt/permit into a PRIMARY request."""
    digest, job_ref, job_rev, intent = sched.jobs[request_id]
    attempt, reservation = sched.admitted[job_ref]
    data = {
        "schemaVersion": 1, "contractDigest": s2.primary.contract_digest(), "crRevision": REVISION,
        "requestId": intent.request_id, "payloadHash": digest, "jobId": job_ref,
        "jobRevision": job_rev, "grantRef": "fixture-grant", "grantRevision": "8" * 64,
        "policyDigest": "9" * 64, "grantExpiresAt": 180, "attemptId": attempt,
        "attemptOrdinal": 1, "controllerEpoch": 1, "baseSha": BASE, "checkpointSha": CHECKPOINT,
        "sourceDigest": "a" * 64, "workspaceRef": "fixture-workspace", "runtime": "claude",
        "requested": {"model": "fixture-model", "effort": "fixture-effort",
                      "nativePlacement": "fixture-local", "inferencePlacement": "fixture-remote"},
        "limits": {"calls": 1, "tokens": 100, "seconds": 20},
        "permissions": ["read"], "acceptanceRefs": ["unit", "boundary"]}
    if reserved and reservation:
        data["reservationRef"] = reservation
    return data


def _s2_run(s2, req, *, admission_digest=None, **admission_changes):
    P = s2.primary
    q = req.to_dict()
    binding = P.NativeBinding(
        request_digest=req.digest, attempt_id=q["attemptId"], controller_epoch=1, runtime="claude",
        harness_version="fixture-only-1", session_id="fixture-session", process_id="fixture-process",
        start_id="fixture-process-start", scope_owner="symphony", observed_model="fixture-model",
        observed_effort="fixture-effort", native_placement="fixture-local",
        inference_placement="fixture-remote", observed_at=100)
    result = P.PrimaryResult(
        request_digest=req.digest, attempt_id=q["attemptId"], controller_epoch=1,
        cr_revision=REVISION, base_sha=BASE, checkpoint_sha=CHECKPOINT, source_digest="a" * 64,
        result_digest="b" * 64, artifact_digest="c" * 64,
        checks_digest=P.checks_digest(_s2_checks(s2, "final")), producer="fixture-checker",
        evidence_ref="fixture-result-evidence", stage_evidence=True, status="completed")
    done = P.NativeObservation("COMPLETED", binding, result, native_terminal=True,
                               descendants_stopped=True, effects_accounted=True,
                               billing_settled=True, observed_at=101)
    terminal = P.TrustedTerminalEvidence("fixture-terminal-reader", "fixture-terminal-readback",
                                         P.NativeObservation("COMPLETED", binding, native_terminal=True,
                                                             descendants_stopped=True,
                                                             effects_accounted=True,
                                                             billing_settled=True, observed_at=101))
    expected = P.ExpectedBinding(P.contract_digest(), REVISION, BASE, CHECKPOINT, "fixture-workspace")
    context = P.ReceiptContext(
        expected=expected, request_digest=req.digest, native_binding=binding, started_at=100,
        start_observed_at=100, assessed_at=101,
        checkpoint_checks=_s2_checks(s2, "checkpoint").checks,
        final_checks=_s2_checks(s2, "final").checks,
        trusted_evidence=(P.TrustedResultEvidence("fixture-checker", "fixture-result-evidence",
                                                  "b" * 64, "c" * 64),),
        trusted_terminal_producers=("fixture-terminal-reader",), terminal_evidence=(terminal,))
    evidence = dict(request_digest=admission_digest or req.digest, epoch=1, owner="symphony",
                    writer_count=1, eligible=True, claim_owned=True, duplicate=False, phase="READY",
                    observed_at=99, valid_until=150, remaining_calls=1, remaining_tokens=100,
                    remaining_seconds=20, allowed_permissions=("read",),
                    workspace_ref="fixture-workspace", base_sha=BASE, checkpoint_sha=CHECKPOINT,
                    workspace_clean=True, revoked=False, fenced=False, evidence_ref="fixture-readback")
    evidence.update(admission_changes)
    owner = P.FakeAdmissionPort((P.AdmissionEvidence(**evidence),))
    port = P.FakePrimaryPort(P.NativeObservation("RUNNING", binding, observed_at=100), (done,))
    receipt = P.run_primary(req, expected, owner, port, lambda s: _s2_checks(s2, s),
                            fake_enabled=True, rollout_enabled=True, now=100, context=context)
    return receipt, port, context


def test_case4_actual_frozen_s2_source_integration(s2):
    P = s2.primary
    assert P.contract_digest() == P0  # frozen contract binding, recomputed from frozen bytes
    assert Path(P.__file__).resolve() == (FROZEN / "tools" / "orchestration" / "primary.py").resolve()
    assert not [k for k in sys.modules if k == S2_NS or k.startswith(S2_NS + ".")]

    sched = FakeScheduler()
    p = submit_payload()
    with guarded():
        pm_result = adapter(sched).handle(p, trusted_context=ctx(p))
    assert pm_result.outcome == "SUBMITTED" and len(sched.calls) == 1
    # The PM request itself is not a PRIMARY request: no privilege bypass via the consumer.
    with pytest.raises(ValueError):
        P.PrimaryRequest.from_dict(p)

    sched.admit(pm_result.view["job_ref"])
    data = project_primary(s2, sched, "req-1")
    req = P.PrimaryRequest.from_dict(data)
    assert req.to_dict()["payloadHash"] == pm_result.payload_digest == independent_digest(p)
    assert req.to_dict()["jobId"] == pm_result.view["job_ref"]
    assert req.to_dict()["jobRevision"] == pm_result.view["job_revision"]
    receipt, port, context = _s2_run(s2, req)
    assert receipt["evidenceComplete"] is True and receipt["stage"] == "FAKE_EVIDENCE_PENDING_REVIEW"
    assert P.validate_receipt(receipt, req, context=context) == []
    assert receipt["reservationState"] == "POSSIBLY_CONSUMED" and len(port.starts) == 1

    # Unreserved: scheduler holds no permit -> the actual consumer rejects the projection.
    sched.admit(pm_result.view["job_ref"], reserve=False)
    with pytest.raises(ValueError):
        P.PrimaryRequest.from_dict(project_primary(s2, sched, "req-1"))
    held, port, _ = _s2_run(s2, req, claim_owned=False)
    assert held["stage"] == "HOLD" and port.starts == []

    # Mismatched mapping: admission readback bound to the true request, projection altered.
    sched.admit(pm_result.view["job_ref"])
    wrong = project_primary(s2, sched, "req-1")
    wrong["payloadHash"] = "0" * 64
    wrong_req = P.PrimaryRequest.from_dict(wrong)
    mismatch, port, _ = _s2_run(s2, wrong_req, admission_digest=req.digest)
    assert mismatch["stage"] == "HOLD" and "AUTHORITY_BINDING" in mismatch["issues"]
    assert port.starts == [] and mismatch["evidenceComplete"] is False
    forged = copy.deepcopy(receipt)
    forged["binding"]["payloadHash"] = "0" * 64
    assert P.validate_receipt(forged, req, context=context)


# =============================================================================== case 5

def test_case5_status_provenance_and_freshness():
    q = job_payload()
    trusted = ctx(q, trusted_observations=(observation(),))
    sched = FakeScheduler()
    sched.status_receipt = status_receipt(q, trusted)
    result = run(sched, q, trusted)
    assert (result.outcome, result.hold) == ("STATUS", False)
    assert result.view["observation"] == "LAST_CONFIRMED_SCHEDULER_OBSERVATION"
    assert result.view["observed_at"] == NOW - 5 and result.view["age_seconds"] == 5
    assert result.view["stage_acceptance"] == "NOT_CLAIMED"
    assert len(sched.calls) == 1

    rogue = observation(producer="worker-self-report", evidence_ref="obs-2")
    negatives = {
        "foreign_epoch": (dict(scheduler_epoch=EPOCH + 1), trusted),
        "stale": (dict(observed_at=NOW - 31), ctx(q, trusted_observations=(observation(observed_at=NOW - 31),))),
        "future": (dict(observed_at=NOW + 1), trusted),
        "forged_ref": (dict(evidence_refs=("obs-forged",)), trusted),
        "missing_evidence": (dict(evidence_refs=()), trusted),
        "phase_disagrees": (dict(phase="ADMITTED"), trusted),
        "attempt_disagrees": (dict(attempt_ref="attempt-0"), trusted),
        "copied_producer_assertion": (dict(evidence_refs=("obs-2",)),
                                      ctx(q, trusted_observations=(rogue,))),
        "no_trusted_observation": ({}, ctx(q)),
    }
    for name, (change, context) in negatives.items():
        sched = FakeScheduler()
        sched.status_receipt = status_receipt(q, context, **change)
        result = run(sched, q, context)
        assert (name, result.outcome, result.hold) == (name, "UNVERIFIED", True)
        assert len(sched.calls) == 1


# =============================================================================== case 6

def _completion(**over):
    values = dict(producer="symphony-readback", evidence_ref="done-1", job_ref=JOB, job_revision=REV,
                  attempt_ref=ATTEMPT, terminal=True, checks=(("unit", "pass"), ("boundary", "pass")))
    values.update(over)
    return pm.TrustedCompletion(**values)


def test_case6_completion_quality():
    q = job_payload()

    def status(phase, completions, **receipt_over):
        context = ctx(q, trusted_observations=(observation(phase=phase),),
                      trusted_completions=completions)
        sched = FakeScheduler()
        sched.status_receipt = status_receipt(q, context, phase=phase,
                                              evidence_refs=("obs-1", "done-1"), **receipt_over)
        result = run(sched, q, context)
        assert len(sched.calls) == 1
        return result

    positive = status("DONE", (_completion(),))
    assert (positive.outcome, positive.hold, positive.view["stage_acceptance"]) == ("STATUS", False, "ACCEPTED")

    all_pass = (("unit", "pass"), ("boundary", "pass"))
    for name, phase, completions in (
            ("done_without_evidence", "DONE", ()),
            ("exit_zero_self_claimed_checks", "EXITED_ZERO", ()),
            ("final_check_failed", "DONE", (_completion(checks=(("unit", "pass"), ("boundary", "fail"))),)),
            ("required_check_missing", "DONE", (_completion(checks=(("unit", "pass"),)),)),
            ("check_not_run", "DONE", (_completion(checks=(("unit", "pass"), ("boundary", "not-run"))),)),
            ("duplicate_check_names", "DONE", (_completion(checks=(("unit", "pass"), ("unit", "pass"))),)),
            ("not_terminal", "DONE", (_completion(terminal=False),)),
            ("late_attempt", "DONE", (_completion(attempt_ref="attempt-0"),)),
            ("late_revision", "DONE", (_completion(job_revision="b" * 64),)),
            ("untrusted_producer", "DONE", (_completion(producer="worker-self-report"),))):
        result = status(phase, completions, claimed_checks=all_pass)
        assert (name, result.outcome, result.hold, result.view["stage_acceptance"]) == (
            name, "STATUS", True, "NOT_ACCEPTED")
    # A late revision cannot replace the current accepted view at all.
    context = ctx(q, trusted_observations=(observation(phase="DONE"),),
                  trusted_completions=(_completion(),))
    sched = FakeScheduler()
    sched.status_receipt = status_receipt(q, context, phase="DONE", job_revision="b" * 64,
                                          evidence_refs=("obs-1", "done-1"))
    assert run(sched, q, context).outcome == "UNVERIFIED"


# =============================================================================== case 7

def test_case7_cancel_semantics():
    q = job_payload("request_cancel", reason="owner changed priorities")
    base = ctx(q)
    sched = FakeScheduler()
    sched.cancel_receipt = cancel_receipt(q)
    result = run(sched, q, base)
    assert (result.outcome, result.hold) == ("CANCEL_REQUESTED", True)
    assert result.view["cancel_state"] == "CANCEL_REQUESTED" and result.view["worker_stopped"] is False
    assert result.view["reservation"] == "POSSIBLY_CONSUMED_HELD"
    assert [type(c[1]) for c in sched.calls] == [pm.CancelIntent]
    assert sched.calls[0][1].reason == "owner changed priorities"
    assert not {f.name for f in dataclasses.fields(pm.CancelIntent)} & {"pid", "process_id", "signal"}

    for claim in sorted(pm.CANCEL_CLAIMS):
        sched = FakeScheduler()
        sched.cancel_receipt = cancel_receipt(q, claims=(claim,))
        forged = run(sched, q, base)
        assert (claim, forged.outcome, forged.hold) == (claim, "UNVERIFIED", True)
    sched = FakeScheduler()
    sched.cancel_receipt = cancel_receipt(q, claims=("worker_stopped",), evidence_refs=("stop-1",))
    wrong_rev = ctx(q, trusted_claims=(pm.TrustedClaim("symphony-readback", "stop-1", JOB, "b" * 64,
                                                       "worker_stopped"),))
    assert run(sched, q, wrong_rev).outcome == "UNVERIFIED"

    claims = tuple(sorted(pm.CANCEL_CLAIMS - {"reservation_released"}))
    proofs = tuple(pm.TrustedClaim("symphony-readback", "proof-" + c, JOB, REV, c) for c in claims)
    sched = FakeScheduler()
    sched.cancel_receipt = cancel_receipt(q, claims=claims,
                                          evidence_refs=tuple("proof-" + c for c in claims))
    proven = run(sched, q, ctx(q, trusted_claims=proofs))
    assert (proven.outcome, proven.hold) == ("CANCEL_REQUESTED", False)
    assert proven.view["worker_stopped"] is True and proven.view["verified_claims"] == list(claims)
    assert proven.view["reservation"] == "POSSIBLY_CONSUMED_HELD"  # no release without its own proof


# =============================================================================== case 8

def test_case8_unknown_retention_and_unavailable():
    p = submit_payload()
    for failure in (TimeoutError("port timeout"), ConnectionResetError("lost"), RuntimeError("?")):
        sched = FakeScheduler()
        sched.fail = failure
        result = run(sched, p, ctx(p))
        assert (result.outcome, result.hold, result.request_id) == ("UNKNOWN", True, "req-1")
        assert len(sched.calls) == 1 and sched.jobs == {} and sched.admitted == {}
    sched = FakeScheduler()
    sched.lost = True
    lost = run(sched, p, ctx(p))
    assert (lost.outcome, lost.request_id, len(sched.calls)) == ("UNKNOWN", "req-1", 1)

    q = job_payload("request_cancel")
    sched = FakeScheduler()
    sched.cancel_receipt = cancel_receipt(q, outcome="UNKNOWN")
    unknown_cancel = run(sched, q, ctx(q))
    assert (unknown_cancel.outcome, unknown_cancel.hold) == ("UNKNOWN", True)
    assert unknown_cancel.view["reservation"] == "POSSIBLY_CONSUMED_HELD"
    sched = FakeScheduler()
    sched.fail = TimeoutError()
    assert run(sched, q, ctx(q)).outcome == "UNKNOWN" and len(sched.calls) == 1

    retention = (("workspace", "DIRTY"), ("branch", "UNMERGED"), ("retention", "RETENTION_HOLD"))
    s = job_payload()
    context = ctx(s, trusted_observations=(observation(),))
    sched = FakeScheduler()
    sched.status_receipt = status_receipt(s, context, retention=retention)
    kept = run(sched, s, context)
    assert kept.view["retention"] == [list(r) for r in retention]
    ad = adapter(sched)
    assert not any(hasattr(ad, m) for m in ("cleanup", "delete", "release", "refund", "retry",
                                            "resume", "transfer_ownership"))

    # No backend: adapter and default registered tool are UNAVAILABLE with zero live operations.
    with guarded():
        assert pm.SymphonyPmAdapter().handle(p, trusted_context=ctx(p)).outcome == "UNAVAILABLE"

    class FakeRegistry:
        def __init__(self):
            self.entries = []

        def register(self, **kwargs):
            self.entries.append(kwargs)

    registry = FakeRegistry()
    with guarded():
        pm.register_pm_tool(registry)
        entry = registry.entries[0]
        out = json.loads(entry["handler"](p, task_id="t-1"))
        available = entry["check_fn"]()
    assert len(registry.entries) == 1 and available is False
    assert (out["outcome"], out["hold"]) == ("UNAVAILABLE", True)
    assert entry["name"] == pm.TOOL_NAME and entry["schema"]["name"] == pm.TOOL_NAME
    assert entry["schema"]["parameters"]["additionalProperties"] is False
    assert set(entry["schema"]["parameters"]["properties"]["operation"]["enum"]) == set(pm.OPERATIONS)


def test_registry_wrapper_binds_real_signature_and_wired_dispatch():
    """Injected fake registry only; the real class is inspected, never populated or discovered."""
    from tools.registry import registry as real_registry

    captured = []

    class FakeRegistry:
        def register(self, **kwargs):
            captured.append(kwargs)

    sched = FakeScheduler()
    p = submit_payload()
    pm.register_pm_tool(FakeRegistry(), adapter=adapter(sched), context_source=lambda: ctx(p))
    inspect.signature(type(real_registry).register).bind(real_registry, **captured[0])
    with guarded():
        out = json.loads(captured[0]["handler"](p))
    assert captured[0]["check_fn"]() is True
    assert out["outcome"] == "SUBMITTED" and len(sched.calls) == 1


# =============================================================================== trusted-context hardening

EXPECTED_OUTCOME = {"submit_intent": "SUBMITTED", "get_status": "STATUS", "request_cancel": "CANCEL_REQUESTED"}


def all_operations():
    return (submit_payload(), job_payload(), job_payload("request_cancel"))


def loaded(sched, payload, receipt):
    """Give the fake the receipt its status/cancel call will return."""
    if payload["operation"] == "get_status":
        sched.status_receipt = receipt
    elif payload["operation"] == "request_cancel":
        sched.cancel_receipt = receipt
    return sched


def scheduler_for(payload, **receipt_over):
    sched = FakeScheduler()
    if payload["operation"] == "get_status":
        return loaded(sched, payload, status_receipt(payload, None, **receipt_over))
    if payload["operation"] == "request_cancel":
        return loaded(sched, payload, cancel_receipt(payload, **receipt_over))
    return sched


def trusted_for(payload, **over):
    """ctx(payload) that is complete for the operation: status needs a matching trusted observation."""
    if payload["operation"] == "get_status":
        over.setdefault("trusted_observations", (observation(),))
    return ctx(payload, **over)


def _claim(**over):
    values = dict(producer="symphony-readback", evidence_ref="proof-1", job_ref=JOB, job_revision=REV,
                  claim="worker_stopped")
    values.update(over)
    return pm.TrustedClaim(**values)


def forge(obj, **changes):
    """Tamper with a frozen record the way a hostile or buggy port could."""
    for name, value in changes.items():
        object.__setattr__(obj, name, value)


def assert_closed(result, payload, sched, *, outcome="REJECTED", issues, calls=0):
    """A refusal keeps the caller's own request_id and payload_digest, is JSON-serializable and made
    exactly ``calls`` port invocations (all of the payload's own operation)."""
    digest = independent_digest(payload)
    assert (result.outcome, result.hold, result.issues) == (outcome, True, tuple(issues))
    assert (result.operation, result.request_id, result.payload_digest) == (
        payload["operation"], payload["request_id"], digest)
    wire = json.loads(json.dumps(result.to_dict(), sort_keys=True))
    assert (wire["outcome"], wire["hold"], wire["issues"]) == (outcome, True, list(issues))
    assert (wire["request_id"], wire["payload_digest"]) == (payload["request_id"], digest)
    assert [c[0] for c in sched.calls] == [payload["operation"]] * calls


BAD_STRING_TUPLES = (
    ("string", "symphony-readback"), ("none", None), ("integer", 7), ("float", 1.5),
    ("bytes", b"symphony-readback"), ("list", ["symphony-readback"]), ("set", {"symphony-readback"}),
    ("frozenset", frozenset({"symphony-readback"})), ("dict", {"symphony-readback": 1}),
    ("element_none", ("symphony-readback", None)), ("element_number", ("symphony-readback", 7)),
    ("element_bytes", (b"symphony-readback",)), ("element_list", (["symphony-readback"],)),
    ("element_nested_tuple", (("symphony-readback",),)))


def test_trusted_producers_and_required_checks_must_be_tuples_of_strings():
    for payload in all_operations():
        op = payload["operation"]
        sched = scheduler_for(payload)
        control = run(sched, payload, trusted_for(payload))
        assert control.outcome == EXPECTED_OUTCOME[op] and len(sched.calls) == 1
        # An empty tuple is a valid (if useless) tuple: it is not refused by shape.
        sched = scheduler_for(payload)
        empty = run(sched, payload, trusted_for(payload, trusted_producers=(), required_checks=()))
        assert empty.outcome != "REJECTED" and len(sched.calls) == 1
        for field_name, issue in (("trusted_producers", "AUTHORITY_PRODUCERS"),
                                  ("required_checks", "AUTHORITY_REQUIRED_CHECKS")):
            for label, bad in BAD_STRING_TUPLES:
                sched = scheduler_for(payload)
                result = run(sched, payload, trusted_for(payload, **{field_name: bad}))
                assert sched.calls == [], (op, field_name, label)
                assert_closed(result, payload, sched, issues=(issue,), calls=0)


def test_string_producers_and_required_checks_cannot_act_as_substring_or_character_set():
    q = job_payload()
    # "readback" is a substring of "symphony-readback": with a bare string, `in` trusted this record.
    sched = scheduler_for(q)
    result = run(sched, q, trusted_for(q, trusted_producers="symphony-readback",
                                       trusted_observations=(observation(producer="readback"),)))
    assert_closed(result, q, sched, issues=("AUTHORITY_PRODUCERS",))
    # set("unit") == {"u", "n", "i", "t"}: a completion passing exactly those four "checks" met it.
    context = trusted_for(q, required_checks="unit", trusted_observations=(observation(phase="DONE"),),
                          trusted_completions=(_completion(checks=tuple((c, "pass") for c in "unit")),))
    sched = scheduler_for(q, phase="DONE", evidence_refs=("obs-1", "done-1"))
    result = run(sched, q, context)
    assert_closed(result, q, sched, issues=("AUTHORITY_REQUIRED_CHECKS",))


CONTAINER_FAULTS = (("none", None), ("string", "obs-1"), ("integer", 3), ("list", []), ("dict", {}),
                    ("set", set()))

RECORD_FAULTS = {
    "trusted_observations": (
        ("record_dict", (dataclasses.asdict(observation()),)), ("record_string", ("obs-1",)),
        ("record_none", (None,)), ("record_other_class", (_completion(),)),
        ("producer_number", (observation(producer=7),)),
        ("epoch_bool", (observation(scheduler_epoch=True),)),
        ("time_nan", (observation(observed_at=float("nan")),)),
        ("time_inf", (observation(observed_at=float("inf")),)),
        ("time_overflow_positive", (observation(observed_at=10 ** 400),)),
        ("time_overflow_negative", (observation(observed_at=-10 ** 400),)),
        ("time_string", (observation(observed_at="1"),)), ("phase_none", (observation(phase=None),)),
        ("phase_bytes", (observation(phase=b"RUNNING"),)),
        ("attempt_ref_none", (observation(attempt_ref=None),)),
        ("valid_then_malformed", (observation(), observation(job_ref=None)))),
    "trusted_completions": (
        ("record_dict", (dataclasses.asdict(_completion()),)), ("record_none", (None,)),
        ("record_other_class", (observation(),)), ("producer_none", (_completion(producer=None),)),
        ("terminal_string", (_completion(terminal="yes"),)), ("terminal_integer", (_completion(terminal=1),)),
        ("checks_list", (_completion(checks=[("unit", "pass")]),)),
        ("checks_inner_list", (_completion(checks=(["unit", "pass"],)),)),
        ("checks_short", (_completion(checks=(("unit",),)),)),
        ("checks_non_string", (_completion(checks=(("unit", 1),)),)),
        ("checks_none", (_completion(checks=None),)),
        ("valid_then_malformed", (_completion(), _completion(evidence_ref=None)))),
    "trusted_claims": (
        ("record_dict", (dataclasses.asdict(_claim()),)), ("record_none", (None,)),
        ("record_other_class", (observation(),)),
        ("claim_none", (_claim(claim=None),)), ("claim_unhashable", (_claim(claim=["worker_stopped"]),)),
        ("valid_then_malformed", (_claim(), _claim(producer=3)))),
}


def test_each_evidence_collection_is_validated_on_its_own_before_the_port_call():
    valid = {"trusted_observations": (observation(),), "trusted_completions": (_completion(),),
             "trusted_claims": (_claim(),)}
    for payload in all_operations():
        op = payload["operation"]
        sched = scheduler_for(payload)
        control = run(sched, payload, trusted_for(payload, **valid))
        assert control.outcome == EXPECTED_OUTCOME[op] and len(sched.calls) == 1
        for name in valid:
            for label, bad in CONTAINER_FAULTS + RECORD_FAULTS[name]:
                sched = scheduler_for(payload)
                result = run(sched, payload, trusted_for(payload, **dict(valid, **{name: bad})))
                assert sched.calls == [], (op, name, label)
                # Only the faulty collection is named: the others were checked separately and passed.
                assert_closed(result, payload, sched, issues=("AUTHORITY_EVIDENCE:" + name,), calls=0)


class MutatingScheduler(FakeScheduler):
    """Synthetic port that tampers with the context it was handed, then answers like the plain fake."""

    def __init__(self, tamper):
        super().__init__()
        self.tamper = tamper
        self.seen = []

    def get_status(self, query, context):
        self.seen.append(context)
        self.tamper(context)
        return super().get_status(query, context)

    def request_cancel(self, request, context):
        self.seen.append(context)
        self.tamper(context)
        return super().request_cancel(request, context)


def snapshot_scenarios():
    """(name, payload, make_context, receipt_overrides, [(pick record, forged fields)]).

    ``make_context`` builds fresh records every call: the control run and the tampered run must not
    share a mutable object. Each scenario starts from a VALID nested record and changes it after the
    snapshot, in the direction that would flip the decision if the live objects were read."""
    q, c = job_payload(), job_payload("request_cancel")
    done = dict(phase="DONE", evidence_refs=("obs-1", "done-1"))
    stop = dict(claims=("worker_stopped",), evidence_refs=("stop-1",))
    nth = lambda attr: (lambda context: getattr(context, attr)[0])
    whole = lambda context: context
    both_pass = (("unit", "pass"), ("boundary", "pass"))
    return (
        ("completion: failing check rewritten to pass", q,
         lambda: trusted_for(q, trusted_observations=(observation(phase="DONE"),), trusted_completions=(
             _completion(checks=(("unit", "pass"), ("boundary", "fail"))),)),
         done, [(nth("trusted_completions"), {"checks": both_pass})]),
        ("completion: accepted record made non-terminal", q,
         lambda: trusted_for(q, trusted_observations=(observation(phase="DONE"),),
                             trusted_completions=(_completion(),)),
         done, [(nth("trusted_completions"), {"terminal": False})]),
        ("context: producers and required checks wiped", q,
         lambda: trusted_for(q, trusted_observations=(observation(phase="DONE"),),
                             trusted_completions=(_completion(),)),
         done, [(whole, {"required_checks": (), "trusted_producers": ()})]),
        ("observation: non-matching record rewritten to match", q,
         lambda: trusted_for(q, trusted_observations=(observation(attempt_ref="attempt-0"),)),
         {}, [(nth("trusted_observations"), {"attempt_ref": ATTEMPT})]),
        ("observation: matching record rewritten to differ", q,
         lambda: trusted_for(q, trusted_observations=(observation(),)),
         {}, [(nth("trusted_observations"), {"phase": "ADMITTED"})]),
        ("claim: wrong-revision record rewritten to verify", c,
         lambda: trusted_for(c, trusted_claims=(_claim(evidence_ref="stop-1", job_revision="b" * 64),)),
         stop, [(nth("trusted_claims"), {"job_revision": REV})]),
        ("claim: verified record rewritten to another claim", c,
         lambda: trusted_for(c, trusted_claims=(_claim(evidence_ref="stop-1"),)),
         stop, [(nth("trusted_claims"), {"claim": "billing_settled"})]),
    )


def test_port_tampering_with_nested_evidence_after_snapshot_cannot_change_the_decision():
    for name, payload, make, receipt_over, changes in snapshot_scenarios():
        receipt = (status_receipt(payload, None, **receipt_over) if payload["operation"] == "get_status"
                   else cancel_receipt(payload, **receipt_over))
        control = run(loaded(FakeScheduler(), payload, receipt), payload, make())
        original = make()

        def tamper(port_context, original=original, changes=changes):
            for pick, fields in changes:
                forge(pick(port_context), **fields)  # the copy the port was handed
                forge(pick(original), **fields)      # the caller's own object

        sched = loaded(MutatingScheduler(tamper), payload, receipt)
        result = run(sched, payload, original)
        assert len(sched.calls) == 1, name
        for pick, fields in changes:  # the tampering really happened, on both objects
            assert all(getattr(pick(original), k) == v for k, v in fields.items()), name
            assert all(getattr(pick(sched.seen[0]), k) == v for k, v in fields.items()), name
            assert pick(sched.seen[0]) is not pick(original), name  # the port never held the caller's record
        # The decision rests on the snapshot: byte-for-byte the untampered control ...
        assert result.to_dict() == control.to_dict(), name
        # ... and different from what reading the tampered objects would have decided.
        live = run(loaded(FakeScheduler(), payload, receipt), payload, original)
        assert live.to_dict() != control.to_dict(), name
        assert (result.request_id, result.payload_digest) == (payload["request_id"], independent_digest(payload))
        json.dumps(result.to_dict())


def test_port_tampering_with_the_request_cannot_change_the_result_binding():
    p = submit_payload()

    class Forging(FakeScheduler):
        def submit_intent(self, request, context):
            forge(request, request_id="req-evil", goal="forged goal")
            return super().submit_intent(request, context)

    sched = Forging()
    result = run(sched, p, ctx(p))
    assert sched.calls[0][1].request_id == "req-evil"  # the port really tampered with what it was given
    assert_closed(result, p, sched, outcome="UNVERIFIED", calls=1,
                  issues=("RECEIPT_BINDING:request_id", "RECEIPT_BINDING:payload_digest"))


# =============================================================================== number range, contract domain, str subclasses

import collections.abc  # noqa: E402  (used only by the helpers below)

HUGE = (("positive", 10 ** 400), ("negative", -(10 ** 400)), ("inf", float("inf")),
        ("minus_inf", float("-inf")))


def test_finite_refuses_numbers_it_cannot_compare_instead_of_raising():
    for label, bad in HUGE + (("nan", float("nan")), ("bool", True), ("string", "1"), ("none", None)):
        assert pm._finite(bad) is False, label
    for good in (0, -1, 1000.0, 10 ** 300, -(10 ** 300), 1.5e308):
        assert pm._finite(good) is True, good


def test_context_numbers_beyond_float_range_are_a_managed_hold_before_the_port_call():
    for payload in all_operations():
        op = payload["operation"]
        for label, bad in HUGE:
            for name in ("now", "expires_at", "max_age_seconds"):
                sched = scheduler_for(payload)
                result = run(sched, payload, trusted_for(payload, **{name: bad}))
                assert sched.calls == [], (op, name, label)
                assert_closed(result, payload, sched, issues=("AUTHORITY_EXPIRED",))
            sched = scheduler_for(payload)
            result = run(sched, payload, trusted_for(
                payload, trusted_observations=(observation(observed_at=bad),)))
            assert sched.calls == [], (op, label)
            assert_closed(result, payload, sched, issues=("AUTHORITY_EVIDENCE:trusted_observations",))
        # Control: a large number that IS representable is a number like any other.
        sched = scheduler_for(payload)
        result = run(sched, payload, trusted_for(payload, max_age_seconds=10 ** 300))
        assert result.outcome == EXPECTED_OUTCOME[op] and len(sched.calls) == 1
        json.dumps(result.to_dict())


class LateSubmit(FakeScheduler):
    """Fake whose submission receipt carries a chosen ``observed_at``."""

    def __init__(self, observed_at):
        super().__init__()
        self.observed_at = observed_at

    def submit_intent(self, request, context):
        receipt = super().submit_intent(request, context)
        return dataclasses.replace(receipt, observed_at=self.observed_at)


def test_receipt_timestamp_beyond_float_range_is_unverified_not_an_exception():
    for payload in all_operations():
        op = payload["operation"]
        for label, bad in HUGE + (("large_future", 10 ** 300), ("large_past", -(10 ** 300))):
            sched = (LateSubmit(bad) if op == "submit_intent"
                     else scheduler_for(payload, observed_at=bad))
            result = run(sched, payload, trusted_for(payload))
            assert_closed(result, payload, sched, outcome="UNVERIFIED", issues=("RECEIPT_STALE",), calls=1)


COMPAT_NAMES = ("Symphony Readback/1", "ci/build #2", "unit tests", "p" * 300, "", "café", "../x",
                "UPPER", "a b")


def test_values_accepted_before_the_hardening_are_still_accepted():
    """Producers, required-check names, phases and attempt refs were never identifier-checked, so
    spaces, slashes, case, empty strings and long names keep working; only the container is narrowed
    (a real tuple of strings)."""
    q, c = job_payload(), job_payload("request_cancel")
    for name in COMPAT_NAMES:
        phase = "waiting for worker/" + name
        sched = scheduler_for(q, phase=phase, attempt_ref=name)
        result = run(sched, q, trusted_for(q, trusted_producers=(name,), trusted_observations=(
            observation(producer=name, phase=phase, attempt_ref=name),)))
        assert (result.outcome, result.hold, result.issues) == ("STATUS", False, ()), name
        assert (result.view["phase"], result.view["attempt_ref"]) == (phase, name) and len(sched.calls) == 1

        sched = scheduler_for(q, phase="DONE", evidence_refs=("obs-1", "done-1"))
        result = run(sched, q, trusted_for(
            q, trusted_producers=(name,), required_checks=(name,),
            trusted_observations=(observation(producer=name, phase="DONE"),),
            trusted_completions=(_completion(producer=name, checks=((name, "pass"),)),)))
        assert (result.outcome, result.hold, result.view["stage_acceptance"]) == ("STATUS", False, "ACCEPTED"), name

        sched = scheduler_for(c, claims=("worker_stopped",), evidence_refs=("stop-1",))
        result = run(sched, c, trusted_for(c, trusted_producers=(name,), trusted_claims=(
            _claim(producer=name, evidence_ref="stop-1"),)))
        assert (result.outcome, result.view["verified_claims"]) == ("CANCEL_REQUESTED", ["worker_stopped"]), name


def test_type_correct_but_unmatched_evidence_records_are_still_ignored():
    q, c = job_payload(), job_payload("request_cancel")
    sched = scheduler_for(q)
    result = run(sched, q, trusted_for(q, trusted_observations=(
        observation(scheduler_epoch=0, evidence_ref="", phase=""), observation())))
    assert (result.outcome, result.hold, result.issues) == ("STATUS", False, ()) and len(sched.calls) == 1
    sched = scheduler_for(c, claims=("worker_stopped",), evidence_refs=("stop-1",))
    result = run(sched, c, trusted_for(c, trusted_claims=(
        _claim(claim="future_claim", job_revision=""), _claim(evidence_ref="stop-1"))))
    assert result.view["verified_claims"] == ["worker_stopped"] and len(sched.calls) == 1


class Liar(str):
    """Claims equality with everything, inequality with nothing, and a length of one."""

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    __hash__ = str.__hash__

    def __len__(self):
        return 1


class Tagged(str):
    """Benign subclass whose instances carry mutable per-object state."""


def strings_in(value, found=None):
    found = [] if found is None else found
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, (tuple, list)):
        for item in value:
            strings_in(item, found)
    elif isinstance(value, collections.abc.Mapping):
        for key, item in value.items():
            strings_in(key, found)
            strings_in(item, found)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            strings_in(getattr(value, f.name), found)
    return found


def tagged(value):
    """The same value with every string replaced by a stateful ``Tagged`` instance."""
    if isinstance(value, str):
        item = Tagged(value)
        item.mark = "caller-state"
        return item
    if type(value) is tuple:
        return tuple(tagged(v) for v in value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return type(value)(**{f.name: tagged(getattr(value, f.name)) for f in dataclasses.fields(value)})
    return value


class Capturing(FakeScheduler):
    """Fake that keeps every context it receives and may rewrap the receipt it returns."""

    def __init__(self, wrap=None):
        super().__init__()
        self.contexts = []
        self.wrap = wrap or (lambda receipt: receipt)

    def submit_intent(self, request, context):
        self.contexts.append(context)
        return self.wrap(super().submit_intent(request, context))

    def get_status(self, query, context):
        self.contexts.append(context)
        return self.wrap(super().get_status(query, context))

    def request_cancel(self, request, context):
        self.contexts.append(context)
        return self.wrap(super().request_cancel(request, context))


def capturing(payload, wrap=None):
    sched = Capturing(wrap)
    if payload["operation"] == "get_status":
        sched.status_receipt = status_receipt(payload, None, uncertainty=("maybe",),
                                              retention=(("lock", "held"),))
    elif payload["operation"] == "request_cancel":
        sched.cancel_receipt = cancel_receipt(payload, uncertainty=("maybe",), retention=(("lock", "held"),))
    return sched


def test_plain_keeps_the_text_and_drops_the_subclass():
    for text in ("", "abc", "кириллица", "x" * 5000):
        for cls in (Liar, Tagged):
            out = pm._plain(cls(text))
            assert type(out) is str and str.__eq__(out, text) is True
    marker = object()
    assert pm._plain(marker) is marker and pm._plain(7) == 7


def test_str_subclasses_are_replaced_by_exact_strings_so_no_mutable_reference_is_shared():
    valid = lambda: dict(trusted_observations=(observation(),), trusted_completions=(_completion(),),
                         trusted_claims=(_claim(),))
    for payload in all_operations():
        op = payload["operation"]
        control_sched = capturing(payload)
        control = run(control_sched, payload, trusted_for(payload, **valid()))
        assert control.outcome == EXPECTED_OUTCOME[op]

        original = tagged(trusted_for(payload, **valid()))
        assert any(type(s) is not str for s in strings_in(original))  # the input really holds subclasses
        sched = capturing(payload, wrap=tagged)  # request, context and receipt all carry subclasses
        with guarded():
            result = adapter(sched).handle(tagged_payload(payload), trusted_context=original)
        assert result.to_dict() == control.to_dict(), op
        assert len(sched.calls) == 1 and len(sched.contexts) == 1
        received = strings_in(sched.contexts[0]) + strings_in(sched.calls[0][1])
        assert received and all(type(s) is str for s in received), op
        assert all(type(s) is str for s in strings_in(result.to_dict())), op
        assert sched.contexts[0] is not original
        assert not {id(s) for s in strings_in(original)} & {id(s) for s in received}, op


def tagged_payload(payload):
    """A request mapping whose keys and values (including list items) are stateful str subclasses."""
    return {tagged(k): ([tagged(i) for i in v] if isinstance(v, list) else tagged(v))
            for k, v in payload.items()}


def test_str_subclass_cannot_fake_equality_in_the_trusted_context():
    q = job_payload()
    for name, issue in (("project_ref", "AUTHORITY_SCOPE"), ("request_id", "BINDING_REQUEST_ID"),
                        ("issuer_ref", "AUTHORITY_FOREIGN"), ("scheduler_id", "AUTHORITY_FOREIGN")):
        sched = scheduler_for(q)
        result = run(sched, q, trusted_for(q, **{name: Liar("some-other-value")}))
        assert_closed(result, q, sched, issues=(issue,))
    sched = scheduler_for(q)  # a producer that "equals" every record producer must not be trusted
    result = run(sched, q, trusted_for(q, trusted_producers=(Liar("evil-producer"),)))
    assert_closed(result, q, sched, outcome="UNVERIFIED", issues=("STATUS_PROVENANCE_UNVERIFIED",), calls=1)


def test_str_subclass_cannot_hide_its_length_or_text_from_the_intent_limits():
    for over, issue in (({"goal": Liar("g" * (pm.TEXT_MAX + 1))}, "GOAL"),
                        ({"raw_constraints": [Liar("c" * (pm.CONSTRAINT_MAX + 1))]}, "RAW_CONSTRAINTS")):
        assert pm.parse_intent(submit_payload(**over)) == (None, "REJECTED", (issue,))
    assert pm.parse_intent(job_payload("request_cancel", reason=Liar("r" * (pm.TEXT_MAX + 1)))) == (
        None, "REJECTED", ("REASON",))


def test_a_sequence_that_changes_between_reads_is_read_once():
    class Flip(list):
        """A valid sequence the first time it is iterated, an oversized one afterwards."""

        def __iter__(self):
            self.n = getattr(self, "n", 0) + 1
            return iter(["ok"] if self.n == 1 else ["x" * (pm.CONSTRAINT_MAX + 1)])

    intent, status, issues = pm.parse_intent(submit_payload(raw_constraints=Flip(["ok"])))
    assert (status, issues) == ("OK", ()) and intent.raw_constraints == ("ok",)
    assert intent.payload_digest == independent_digest(submit_payload(raw_constraints=["ok"]))


def test_str_subclass_in_a_receipt_cannot_fake_a_binding_or_a_claim():
    q, c = job_payload(), job_payload("request_cancel")
    sched = scheduler_for(q, request_id=Liar("req-evil"))
    result = run(sched, q, trusted_for(q))
    assert_closed(result, q, sched, outcome="UNVERIFIED", issues=("RECEIPT_BINDING:request_id",), calls=1)
    sched = scheduler_for(c, claims=(Liar("billing_settled"),), evidence_refs=("stop-1",))
    result = run(sched, c, trusted_for(c, trusted_claims=(_claim(evidence_ref="stop-1"),)))
    assert_closed(result, c, sched, outcome="UNVERIFIED", issues=("UNBACKED_CLAIM:billing_settled",), calls=1)


def test_malformed_trusted_input_is_a_managed_refusal_never_an_exception():
    class Hostile:
        def __eq__(self, other):
            raise RuntimeError("comparison refused")

        __hash__ = object.__hash__

    q = job_payload()
    sched = scheduler_for(q)  # a comparison that raises inside validation
    result = run(sched, q, trusted_for(q, allowed_operations=(Hostile(), "get_status")))
    assert_closed(result, q, sched, issues=("AUTHORITY_CONTEXT_INVALID",))
    broken = trusted_for(q)  # a field that cannot be read at all
    object.__delattr__(broken, "context_ref")
    sched = scheduler_for(q)
    assert_closed(run(sched, q, broken), q, sched, issues=("AUTHORITY_CONTEXT_UNREADABLE",))
    receipt = status_receipt(q, None)  # a receipt that cannot be read after the single port call
    object.__delattr__(receipt, "observed_at")
    sched = FakeScheduler()
    sched.status_receipt = receipt
    result = run(sched, q, trusted_for(q))
    assert_closed(result, q, sched, outcome="UNVERIFIED", issues=("RECEIPT_UNVERIFIABLE",), calls=1)


# =============================================================================== unified H1 receipt boundary

class H1Scheduler(FakeScheduler):
    """One port call with a controlled malformed receipt; no live backend."""

    def __init__(self, changes):
        super().__init__()
        self.changes = changes

    def submit_intent(self, request, context):
        return dataclasses.replace(super().submit_intent(request, context), **self.changes)


def h1_setup(op, changes):
    payload = submit_payload() if op == "submit_intent" else job_payload(op)
    sched = H1Scheduler(changes)
    if op == "get_status":
        sched.status_receipt = status_receipt(payload, None, **changes)
    elif op == "request_cancel":
        sched.cancel_receipt = cancel_receipt(payload, **changes)
    return payload, trusted_for(payload), sched


H1_RECEIPT_FAULTS = (
    ("submit_intent", {"outcome": ["SUBMITTED"]}, "RECEIPT_SHAPE:outcome"),
    ("submit_intent", {"outcome": {"SUBMITTED": 1}}, "RECEIPT_SHAPE:outcome"),
    ("submit_intent", {"outcome": None}, "RECEIPT_SHAPE:outcome"),
    ("submit_intent", {"outcome": "CONFLICT", "job_ref": object()}, "RECEIPT_SHAPE:job_ref"),
    ("submit_intent", {"request_id": ["req-1"]}, "RECEIPT_SHAPE:request_id"),
    ("submit_intent", {"uncertainty": 5}, "RECEIPT_SHAPE:uncertainty"),
    ("submit_intent", {"uncertainty": ("ok", object())}, "RECEIPT_SHAPE:uncertainty"),
    ("get_status", {"phase": ["DONE"]}, "RECEIPT_SHAPE:phase"),
    ("get_status", {"attempt_ref": None}, "RECEIPT_SHAPE:attempt_ref"),
    ("get_status", {"retention": 7}, "RECEIPT_SHAPE:retention"),
    ("get_status", {"retention": [("workspace", "DIRTY")]}, "RECEIPT_SHAPE:retention"),
    ("get_status", {"retention": (["workspace", "DIRTY"],)}, "RECEIPT_SHAPE:retention"),
    ("get_status", {"retention": (("workspace", object()),)}, "RECEIPT_SHAPE:retention"),
    ("get_status", {"retention": (("workspace", float("nan")),)}, "RECEIPT_SHAPE:retention"),
    ("get_status", {"observed_at": 10 ** 400}, "RECEIPT_STALE"),
    ("submit_intent", {"observed_at": -(10 ** 400)}, "RECEIPT_STALE"),
    ("request_cancel", {"outcome": ["CANCEL_REQUESTED"]}, "RECEIPT_SHAPE:outcome"),
    ("request_cancel", {"claims": ["worker_stopped"]}, "RECEIPT_SHAPE:claims"),
    ("request_cancel", {"claims": 5}, "RECEIPT_SHAPE:claims"),
    ("request_cancel", {"claims": (["worker_stopped"],)}, "RECEIPT_OUTCOME"),
    ("request_cancel", {"claims": ({"worker_stopped"},)}, "RECEIPT_OUTCOME"),
    ("request_cancel", {"reservation_state": None}, "RECEIPT_SHAPE:reservation_state"),
    ("request_cancel", {"retention": None}, "RECEIPT_SHAPE:retention"),
)


def test_h1_receipt_matrix_is_a_json_safe_hold_with_exactly_one_call():
    for op, changes, issue in H1_RECEIPT_FAULTS:
        for through_handler in (False, True):
            payload, context, sched = h1_setup(op, changes)
            if through_handler:
                with guarded():
                    text = pm.make_pm_tool_handler(adapter(sched), lambda: context)(payload)
                wire = json.loads(text, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
                assert (wire["outcome"], wire["hold"], wire["issues"]) == ("UNVERIFIED", True, [issue])
                assert (wire["request_id"], wire["payload_digest"]) == (
                    payload["request_id"], independent_digest(payload))
                assert [c[0] for c in sched.calls] == [op]
                text.encode("utf-8")
            else:
                result = run(sched, payload, context)
                assert_closed(result, payload, sched, outcome="UNVERIFIED", issues=(issue,), calls=1)
                pm.canonical_bytes(result.to_dict())


def test_h1_uncertainty_and_nested_retention_reject_invalid_json_values():
    for op in pm.OPERATIONS:
        faults = [("uncertainty", v) for v in ("abc", ["text"], (object(),), (float("nan"),))]
        if op != "submit_intent":
            faults.extend(("retention", (("details", value),)) for value in (
                {"deep": [object()]}, {"deep": float("inf")}, {7: "numeric key"},
                {"deep": float("nan")}, {"deep": {"set"}}, {"deep": b"bytes"}))
        for field, value in faults:
            payload, context, sched = h1_setup(op, {field: value})
            result = run(sched, payload, context)
            assert_closed(result, payload, sched, outcome="UNVERIFIED",
                          issues=("RECEIPT_SHAPE:" + field,), calls=1)
            pm.canonical_bytes(result.to_dict())


def test_h1_unencodable_unicode_and_recursive_receipt_are_managed_holds():
    recursive = []
    recursive.append(recursive)
    cases = [(op, {"uncertainty": ("\ud800",)}) for op in pm.OPERATIONS]
    cases += [("get_status", {"retention": (("details", recursive),)}),
              ("request_cancel", {"retention": (("details", {"text": "\ud800"}),)})]
    for op, changes in cases:
        payload, context, sched = h1_setup(op, changes)
        result = run(sched, payload, context)
        assert_closed(result, payload, sched, outcome="UNVERIFIED",
                      issues=("RECEIPT_UNVERIFIABLE",), calls=1)
        pm.canonical_bytes(result.to_dict())


def test_h1_verifier_exceptions_and_unencodable_results_are_contained():
    def raises(*args):
        raise TypeError("synthetic verifier fault")

    def unsafe(receipt, intent, context):
        return pm._result(intent, "SUBMITTED", False, view={"bad": object()})

    for replacement in (raises, unsafe):
        payload, context, sched = h1_setup("submit_intent", {})
        with mock.patch.dict(pm._VERIFIERS, {"submit_intent": replacement}):
            result = run(sched, payload, context)
        assert_closed(result, payload, sched, outcome="UNVERIFIED",
                      issues=("RECEIPT_UNVERIFIABLE",), calls=1)
        pm.canonical_bytes(result.to_dict())


def test_h1_receipt_projection_preserves_valid_strings_and_nested_json():
    uncertainty = ("", "unit tests / café", "u" * (pm.TEXT_MAX + 1))
    retention = (("", {"unicode": "café", "values": [None, True, 12, 3.5, ""],
                       "nested": ("tuple", {"more": ["json"]})}),
                 ("long", "r" * (pm.TEXT_MAX + 1)))
    for op in pm.OPERATIONS:
        changes = {"uncertainty": uncertainty}
        if op != "submit_intent":
            changes["retention"] = retention
        payload, context, sched = h1_setup(op, changes)
        result = run(sched, payload, context)
        assert result.outcome == EXPECTED_OUTCOME[op] and result.issues == ()
        assert result.view["uncertainty"] == list(uncertainty)
        if op != "submit_intent":
            assert result.view["retention"] == [list(pair) for pair in retention]
        assert len(sched.calls) == 1
        pm.canonical_bytes(result.to_dict())


def test_h1_nested_receipt_details_are_normalized_and_not_shared_with_the_port():
    for op in ("get_status", "request_cancel"):
        key, text = Tagged("details"), Tagged("kept")
        original_details = {key: [text, {"flag": True}]}
        payload, context, sched = h1_setup(op, {"retention": (("workspace", original_details),)})
        receipt = sched.status_receipt if op == "get_status" else sched.cancel_receipt
        result = run(sched, payload, context)
        assert result.outcome == EXPECTED_OUTCOME[op] and result.issues == ()
        assert all(type(s) is str for s in strings_in(result.to_dict()))
        before = pm.canonical_bytes(result.to_dict())
        original_details[key][0] = "changed after receipt"
        original_details[key][1]["flag"] = False
        original_details["injected"] = object()
        forge(receipt, uncertainty=(object(),), retention=())
        assert pm.canonical_bytes(result.to_dict()) == before
        assert len(sched.calls) == 1


def test_h2_mixed_valid_and_malformed_evidence_rejects_before_every_operation():
    for payload in all_operations():
        sched = scheduler_for(payload)
        context = trusted_for(payload, trusted_observations=(observation(), observation(job_ref=None)))
        result = run(sched, payload, context)
        assert_closed(result, payload, sched,
                      issues=("AUTHORITY_EVIDENCE:trusted_observations",), calls=0)


def test_h1_all_receipt_binding_fields_require_strings_before_comparison():
    binding = ("contract_version", "contract_digest", "request_id", "payload_digest",
               "project_ref", "issuer_ref", "scheduler_id")
    for op in pm.OPERATIONS:
        for name in binding:
            payload, context, sched = h1_setup(op, {name: object()})
            result = run(sched, payload, context)
            assert_closed(result, payload, sched, outcome="UNVERIFIED",
                          issues=("RECEIPT_SHAPE:" + name,), calls=1)
            pm.canonical_bytes(result.to_dict())


def test_h1_uninitialised_receipt_after_request_mutation_keeps_original_identity():
    class Broken(FakeScheduler):
        def submit_intent(self, request, context):
            self._enter("submit_intent", request)
            forge(request, request_id="rewritten", goal="rewritten")
            return object.__new__(pm.PmSubmissionReceipt)

    payload = submit_payload()
    context = trusted_for(payload)
    sched = Broken()
    with guarded():
        text = pm.make_pm_tool_handler(adapter(sched), lambda: context)(payload)
    result = json.loads(text)
    assert (result["outcome"], result["hold"], result["issues"]) == (
        "UNVERIFIED", True, ["RECEIPT_UNVERIFIABLE"])
    assert (result["request_id"], result["payload_digest"]) == (
        payload["request_id"], independent_digest(payload))
    assert len(sched.calls) == 1 and sched.calls[0][1].request_id == "rewritten"
    text.encode("utf-8")
