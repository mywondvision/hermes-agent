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


# =============================================================================== H1/H2 corrective regression

def _h_claim(**over):
    values = dict(producer="symphony-readback", evidence_ref="stop-1", job_ref=JOB, job_revision=REV,
                  claim="worker_stopped")
    values.update(over)
    return pm.TrustedClaim(**values)


class _HScheduler(FakeScheduler):
    """Fake sole owner that rewrites its own receipt and may act inside the single port call."""

    def __init__(self, changes=None, during=None):
        super().__init__()
        self.changes, self.during = changes or {}, during

    def _hostile(self, receipt, request, context):
        if self.during is not None:
            self.during(request, context)
        return None if receipt is None else dataclasses.replace(receipt, **self.changes)

    def submit_intent(self, request, context):
        return self._hostile(super().submit_intent(request, context), request, context)

    def get_status(self, query, context):
        return self._hostile(super().get_status(query, context), query, context)

    def request_cancel(self, request, context):
        return self._hostile(super().request_cancel(request, context), request, context)


def _h_setup(op, changes=None, during=None, **context_over):
    """Valid baseline per operation; ``context_over`` replaces trusted-context fields verbatim."""
    sched = _HScheduler(changes, during)
    if op == "submit_intent":
        payload = submit_payload()
        return payload, ctx(payload, **context_over), sched
    payload = job_payload(op)
    if op == "get_status":
        values = dict(trusted_observations=(observation(phase="DONE"),), trusted_completions=(_completion(),))
    else:
        values = dict(trusted_claims=(_h_claim(),))
    values.update(context_over)
    context = ctx(payload, **values)
    if op == "get_status":
        sched.status_receipt = status_receipt(payload, context, phase="DONE", evidence_refs=("obs-1", "done-1"))
    else:
        sched.cancel_receipt = cancel_receipt(payload, claims=("worker_stopped",), evidence_refs=("stop-1",))
    return payload, context, sched


def _h_expect(result, sched, payload, outcome, port_calls):
    """Negative path: HOLD, original identity, JSON-safe result, exact number of port calls."""
    assert (result.outcome, result.hold) == (outcome, True), result.issues
    assert (result.operation, result.request_id) == (payload["operation"], payload["request_id"])
    assert result.payload_digest == independent_digest(payload)
    json.dumps(result.to_dict(), allow_nan=False)
    assert sched.calls == [(payload["operation"], pm.parse_intent(payload)[0])] * port_calls


@pytest.mark.parametrize("op, outcome, hold", [("submit_intent", "SUBMITTED", False),
                                               ("get_status", "STATUS", False),
                                               ("request_cancel", "CANCEL_REQUESTED", True)])
def test_h_baseline_fixture_verifies(op, outcome, hold):
    payload, context, sched = _h_setup(op)
    result = run(sched, payload, context)
    assert (result.outcome, result.hold, result.issues) == (outcome, hold, ())
    assert sched.calls == [(op, pm.parse_intent(payload)[0])]


H1_CASES = [
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
]


@pytest.mark.parametrize("op, changes, issue", H1_CASES)
def test_h1_malformed_receipt_is_unverified_hold_without_second_call(op, changes, issue):
    payload, context, sched = _h_setup(op, changes)
    result = run(sched, payload, context)
    _h_expect(result, sched, payload, "UNVERIFIED", 1)
    assert issue in result.issues, result.issues


def test_h1_uninitialised_receipt_and_rewritten_request_keep_original_identity():
    payload = submit_payload()
    seen = []

    class Hostile(FakeScheduler):
        def submit_intent(self, request, context):
            self._enter("submit_intent", request)
            object.__setattr__(request, "request_id", "req-evil")
            seen.append(request)
            return object.__new__(pm.PmSubmissionReceipt)
    sched = Hostile()
    result = run(sched, payload, ctx(payload))
    assert seen[0].request_id == "req-evil" and len(sched.calls) == 1
    assert (result.outcome, result.hold, result.issues) == (
        "UNVERIFIED", True, ("RECEIPT_SHAPE:MISSING_FIELD",))
    assert (result.request_id, result.payload_digest) == ("req-1", independent_digest(payload))
    json.dumps(result.to_dict(), allow_nan=False)


def test_h1_verifier_fault_after_port_call_is_contained(monkeypatch):
    def broken(receipt, intent, context):
        raise TypeError("unhashable type: 'list'")
    monkeypatch.setitem(pm._VERIFIERS, "submit_intent", broken)
    payload, context, sched = _h_setup("submit_intent")
    result = run(sched, payload, context)
    _h_expect(result, sched, payload, "UNVERIFIED", 1)
    assert result.issues == ("RECEIPT_MALFORMED",)


def test_h1_tool_handler_stays_json_for_unserialisable_receipt_value():
    payload, context, sched = _h_setup("submit_intent", dict(outcome="CONFLICT", job_ref=object()))
    handler = pm.make_pm_tool_handler(adapter(sched), lambda: context)
    with guarded():
        out = json.loads(handler(payload))
    assert (out["outcome"], out["hold"], out["request_id"]) == ("UNVERIFIED", True, "req-1")
    assert out["payload_digest"] == independent_digest(payload)
    assert "RECEIPT_SHAPE:job_ref" in out["issues"]
    assert sched.calls == [("submit_intent", pm.parse_intent(payload)[0])]


@pytest.mark.parametrize("field_name", ["now", "expires_at", "max_age_seconds"])
@pytest.mark.parametrize("value", [10 ** 400, -(10 ** 400)], ids=["plus_1e400", "minus_1e400"])
def test_h_out_of_range_authority_time_is_rejected_before_port_call(field_name, value):
    payload, context, sched = _h_setup("get_status", **{field_name: value})
    result = run(sched, payload, context)
    _h_expect(result, sched, payload, "REJECTED", 0)
    assert result.issues == ("AUTHORITY_EXPIRED",)


H2_BAD_COLLECTIONS = [
    *[("required_checks", v, "AUTHORITY_REQUIRED_CHECKS")
      for v in ("unit", None, 7, ["unit", "boundary"], (1,), ("",), ("unit", None))],
    *[("trusted_producers", v, "AUTHORITY_TRUSTED_PRODUCERS")
      for v in ("symphony-readback", None, 7, ["symphony-readback"], (None,), ("symphony readback",),
                ("symphony-readback", 7))],
    *[(name, v, "AUTHORITY_EVIDENCE:" + name)
      for name, record in (("trusted_observations", observation(phase="DONE")),
                           ("trusted_completions", _completion()), ("trusted_claims", _h_claim()))
      for v in ("obs-1", None, 7, [record])],
]


@pytest.mark.parametrize("name, value, issue", H2_BAD_COLLECTIONS)
def test_h2_invalid_trusted_collection_is_rejected_before_port_call(name, value, issue):
    payload, context, sched = _h_setup("get_status", **{name: value})
    result = run(sched, payload, context)
    _h_expect(result, sched, payload, "REJECTED", 0)
    assert result.issues == (issue,)


@pytest.mark.parametrize("op", ["submit_intent", "request_cancel"])
def test_h2_collection_check_precedes_every_operation(op):
    for name, value, issue in (("required_checks", "unit", "AUTHORITY_REQUIRED_CHECKS"),
                               ("trusted_producers", "symphony-readback", "AUTHORITY_TRUSTED_PRODUCERS")):
        payload, context, sched = _h_setup(op, **{name: value})
        result = run(sched, payload, context)
        _h_expect(result, sched, payload, "REJECTED", 0)
        assert result.issues == (issue,)


_H_DENIED = {"trusted_completions": ("get_status", "STATUS", "TRUSTED_COMPLETION_EVIDENCE_REQUIRED"),
             "trusted_observations": ("get_status", "UNVERIFIED", "STATUS_PROVENANCE_UNVERIFIED"),
             "trusted_claims": ("request_cancel", "UNVERIFIED", "UNBACKED_CLAIM:worker_stopped")}
H2_BAD_RECORDS = [
    *[("trusted_completions", r) for r in (
        _completion(producer=1), _completion(terminal=1),
        _completion(checks=[("unit", "pass"), ("boundary", "pass")]),
        _completion(checks=(("unit", 1), ("boundary", "pass"))), _completion(job_revision=None),
        _h_claim(evidence_ref="done-1"), object.__new__(pm.TrustedCompletion))],
    *[("trusted_observations", r) for r in (
        observation(phase="DONE", scheduler_epoch=float(EPOCH)),
        observation(phase="DONE", observed_at=float("nan")), observation(phase="DONE", producer=None),
        _completion(evidence_ref="obs-1"))],
    *[("trusted_claims", r) for r in (
        _h_claim(claim=["worker_stopped"]), _h_claim(producer=None), _h_claim(job_ref=None),
        _completion(evidence_ref="stop-1"))],
]


@pytest.mark.parametrize("name, record", H2_BAD_RECORDS)
def test_h2_malformed_trusted_record_is_never_evidence(name, record):
    op, outcome, issue = _H_DENIED[name]
    payload, context, sched = _h_setup(op, **{name: (record,)})
    result = run(sched, payload, context)
    _h_expect(result, sched, payload, outcome, 1)
    assert result.issues == (issue,)


@pytest.mark.parametrize("observations", [(), (observation(phase="DONE", observed_at=10 ** 400),),
                                          (observation(phase="DONE", observed_at=-(10 ** 400)),)])
def test_h2_without_valid_observation_status_is_unverified_with_one_call(observations):
    payload, context, sched = _h_setup("get_status", trusted_observations=observations)
    result = run(sched, payload, context)
    _h_expect(result, sched, payload, "UNVERIFIED", 1)
    assert result.issues == ("STATUS_PROVENANCE_UNVERIFIED",)


H2_NESTED = [
    ("trusted_completions", lambda: _completion(checks=(("unit", "pass"), ("boundary", "fail"))),
     "checks", (("unit", "pass"), ("boundary", "pass"))),
    ("trusted_observations", lambda: observation(phase="RUNNING"), "phase", "DONE"),
    ("trusted_claims", lambda: _h_claim(claim="descendants_stopped"), "claim", "worker_stopped"),
]


@pytest.mark.parametrize("name, make, attr, value", H2_NESTED)
def test_h2_valid_nested_record_mutated_after_snapshot_does_not_change_decision(name, make, attr, value):
    op, outcome, issue = _H_DENIED[name]
    record = make()
    rewrite = lambda request, trusted: object.__setattr__(record, attr, value)  # noqa: E731
    payload, context, sched = _h_setup(op, during=rewrite, **{name: (record,)})
    result = run(sched, payload, context)
    assert getattr(record, attr) == value  # the port really rewrote the caller's original record
    _h_expect(result, sched, payload, outcome, 1)
    assert result.issues == (issue,)
    payload, context, sched = _h_setup(op, **{name: (dataclasses.replace(make(), **{attr: value}),)})
    assert run(sched, payload, context).issues == ()  # the rewritten record alone would be accepted


H2_TOP_LEVEL = [
    ("trusted_completions", {"trusted_completions": (_completion(checks=(("unit", "pass"),)),)},
     "required_checks", ("unit",)),
    ("trusted_completions", {"trusted_completions": (_completion(producer="worker-self-report"),)},
     "trusted_producers", ("symphony-readback", "worker-self-report")),
    ("trusted_completions", {"trusted_completions": ()}, "trusted_completions", (_completion(),)),
    ("trusted_observations", {"trusted_observations": ()}, "trusted_observations",
     (observation(phase="DONE"),)),
    ("trusted_claims", {"trusted_claims": ()}, "trusted_claims", (_h_claim(),)),
]


@pytest.mark.parametrize("name, start, field_name, value", H2_TOP_LEVEL)
def test_h2_trusted_collection_replaced_during_call_does_not_change_decision(name, start, field_name, value):
    op, outcome, issue = _H_DENIED[name]
    rewrite = lambda request, trusted: object.__setattr__(trusted, field_name, value)  # noqa: E731
    payload, context, sched = _h_setup(op, during=rewrite, **start)
    result = run(sched, payload, context)
    assert getattr(context, field_name) == value  # the port really rewrote the caller's context
    _h_expect(result, sched, payload, outcome, 1)
    assert result.issues == (issue,)
    payload, context, sched = _h_setup(op, **dict(start, **{field_name: value}))
    assert run(sched, payload, context).issues == ()


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
