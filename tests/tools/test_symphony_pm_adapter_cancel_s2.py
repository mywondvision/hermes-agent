"""Cancel-semantics contract tests of ``tools/symphony_pm_adapter.py`` against the ACTUAL frozen S2
PRIMARY source. No stop is performed or proven.

ADD-HERMES-PM-INGRESS-01 case 7 (cancel semantics) joined with case 4 (actual frozen consumer): the
synthetic Symphony scheduler turns one adapter ``request_cancel`` into exactly one ``cancel_primary``
call on the frozen consumer (``.artifacts/frozen-s2``) with S2's own fake admission/native ports, and
projects the consumer's receipt back as a ``PmCancelReceipt``. The adapter reports a stop only when
the trusted caller supplies independent claims bound to the scheduler's job identity.

Source/fake-only. PASS proves offline source-contract conformance of the cancel path: no real
scheduler ingress, transport, authentication, OS signal, native process or live completion; the fake
native port reports TERMINAL regardless of the cancel it received.
"""
from __future__ import annotations

import builtins
import contextlib
import io

import pytest

from tests.tools.test_symphony_pm_adapter import (
    BASE,
    CHECKPOINT,
    REVISION,
    FakeScheduler,
    S2Hold,
    _s2_checks,
    adapter,
    ctx,
    guarded,
    job_payload,
    load_frozen_s2,
    project_primary,
    submit_payload,
)
from tools import symphony_pm_adapter as pm


@pytest.fixture(scope="module")
def s2():
    try:
        return load_frozen_s2()
    except S2Hold as exc:
        pytest.fail("HOLD: " + str(exc))

# The frozen consumer validates every receipt against its schema files on disk: that read belongs to
# the sole owner, so the bridge restores the real ``open`` only around its own S2 call. The adapter's
# side-effect spies (``guarded``) stay armed before and after that call.
_REAL_OPEN, _REAL_IO_OPEN = builtins.open, io.open


@contextlib.contextmanager
def sole_owner_reads():
    """Plain attribute swap (``mock.patch`` would itself trip the ``importlib.import_module`` spy)."""
    trapped = builtins.open, io.open
    builtins.open, io.open = _REAL_OPEN, _REAL_IO_OPEN
    try:
        yield
    finally:
        builtins.open, io.open = trapped


CANCEL_AT = 102  # S2 logical clock, distinct from the adapter's trusted ``now``
ASSESSED_AT = 103
FENCE_EVIDENCE = "fixture-readback"
TERMINAL_PRODUCER = "fixture-terminal-reader"
TERMINAL_EVIDENCE = "fixture-terminal-readback"
READBACK_PRODUCER = "symphony-readback"
CANCEL_REASON = "owner changed priorities"

# S2 observation flag -> adapter claim. The scheduler translates; the adapter still demands proof.
FLAG_CLAIMS = (("native_terminal", "worker_stopped"), ("descendants_stopped", "descendants_stopped"),
               ("effects_accounted", "external_effects_settled"), ("billing_settled", "billing_settled"))
STOP_CLAIMS = tuple(claim for _, claim in FLAG_CLAIMS)


class S2CancelScheduler(FakeScheduler):
    """Synthetic sole owner whose cancel path is the frozen consumer. Fence readback, native binding
    and the S2 receipt context are scheduler-owned synthetic fixtures (as S2 documents them); neither
    the adapter nor the PM payload can reach them."""

    def __init__(self, s2, *, fenced=True, independent_proof=True):
        super().__init__()
        self.s2 = s2
        self.fenced = fenced
        self.independent_proof = independent_proof
        self.s2_calls = []  # (PrimaryRequest, ReceiptContext, FakePrimaryPort, receipt) per native cancel

    # ---- scheduler-owned S2 fixtures --------------------------------------------------------------
    def _primary_request(self, job_ref):
        request_id = next(rid for rid, known in self.jobs.items() if known[1] == job_ref)
        return self.s2.primary.PrimaryRequest.from_dict(project_primary(self.s2, self, request_id))

    def _binding(self, req):
        return self.s2.primary.NativeBinding(
            request_digest=req.digest, attempt_id=req.to_dict()["attemptId"], controller_epoch=1,
            runtime="claude", harness_version="fixture-only-1", session_id="fixture-session",
            process_id="fixture-process", start_id="fixture-process-start", scope_owner="symphony",
            observed_model="fixture-model", observed_effort="fixture-effort",
            native_placement="fixture-local", inference_placement="fixture-remote", observed_at=100)

    def _terminal(self, binding):
        return self.s2.primary.NativeObservation(
            "TERMINAL", binding, native_terminal=True, descendants_stopped=True,
            effects_accounted=True, billing_settled=True, observed_at=ASSESSED_AT)

    def _context(self, req, binding):
        P = self.s2.primary
        proof = (P.TrustedTerminalEvidence(TERMINAL_PRODUCER, TERMINAL_EVIDENCE, self._terminal(binding)),)
        return P.ReceiptContext(
            expected=self._expected(), request_digest=req.digest, native_binding=binding,
            started_at=100, start_observed_at=100, assessed_at=ASSESSED_AT,
            checkpoint_checks=_s2_checks(self.s2, "checkpoint").checks,
            final_checks=_s2_checks(self.s2, "final").checks,
            trusted_evidence=(P.TrustedResultEvidence("fixture-checker", "fixture-result-evidence",
                                                      "b" * 64, "c" * 64),),
            operation="cancel", cancel_requested_at=CANCEL_AT, fence_evidence_ref=FENCE_EVIDENCE,
            trusted_terminal_producers=(TERMINAL_PRODUCER,),
            terminal_evidence=proof if self.independent_proof else ())

    def _expected(self):
        P = self.s2.primary
        return P.ExpectedBinding(P.contract_digest(), REVISION, BASE, CHECKPOINT, "fixture-workspace")

    def _owner(self, req):
        P = self.s2.primary
        evidence = P.AdmissionEvidence(
            request_digest=req.digest, epoch=1, owner="symphony", writer_count=1, eligible=True,
            claim_owned=True, duplicate=False, phase="READY", observed_at=99, valid_until=150,
            remaining_calls=1, remaining_tokens=100, remaining_seconds=20, allowed_permissions=("read",),
            workspace_ref="fixture-workspace", base_sha=BASE, checkpoint_sha=CHECKPOINT,
            workspace_clean=True, revoked=self.fenced, fenced=self.fenced, evidence_ref=FENCE_EVIDENCE)
        return P.FakeAdmissionPort((evidence,))

    # ---- the port method the adapter calls ---------------------------------------------------------
    def request_cancel(self, request, context):
        self.calls.append(("request_cancel", request))
        if self.fail is not None:
            raise self.fail
        known = next((k for k in self.jobs.values() if k[1] == request.job_ref), None)
        if known is None:
            return pm.PmCancelReceipt(**self.common(request, context, outcome="REJECTED",
                                                   job_ref=request.job_ref,
                                                   job_revision=request.expected_job_revision))
        P = self.s2.primary
        with sole_owner_reads():
            # Everything S2 (request projection, contract digest, receipt validation) reads its
            # frozen schema/contract files; the adapter's spies are re-armed on exit.
            req = self._primary_request(request.job_ref)
            binding = self._binding(req)
            # Two identical readbacks: S2 reads at most twice and keeps the last one in its receipt.
            terminal = self._terminal(binding)
            port = P.FakePrimaryPort(None, (terminal, terminal))
            s2_context = self._context(req, binding)
            receipt = P.cancel_primary(req, self._expected(), self._owner(req), port, binding,
                                       fake_enabled=True, now=CANCEL_AT, context=s2_context)
        self.s2_calls.append((req, s2_context, port, receipt))
        if self.lost:
            return None
        return self._project(request, context, known, receipt, port, s2_context)

    def _project(self, request, context, known, receipt, port, s2_context):
        """Only what the scheduler itself can vouch for: claims from the trusted terminal record S2
        validated (never from the port's self-report), the request fact from its own port counter."""
        stage = receipt["stage"]
        claims, evidence_refs = (), ()
        if stage == "CANCEL_CONFIRMED_FAKE":
            outcome = "CANCEL_REQUESTED"
            trusted = s2_context.terminal_evidence[0].observation
            claims = tuple(claim for flag, claim in FLAG_CLAIMS if getattr(trusted, flag) is True)
            evidence_refs = tuple(record.evidence_ref for record in s2_context.terminal_evidence)
        elif stage == "CANCEL_UNKNOWN":  # S2 also says UNKNOWN when the native cancel itself raised
            outcome = "CANCEL_REQUESTED" if len(port.cancels) == 1 else "UNKNOWN"
        else:  # HOLD: fence/binding refused before any native cancel
            outcome = "REJECTED"
        return pm.PmCancelReceipt(**self.common(
            request, context, outcome=outcome, job_ref=known[1], job_revision=known[2], claims=claims,
            reservation_state=receipt["reservationState"], evidence_refs=evidence_refs,
            uncertainty=tuple(receipt["issues"])))


def submitted(s2, **scheduler_options):
    """Submit through the adapter, let the scheduler admit, and build the matching cancel payload."""
    sched = S2CancelScheduler(s2, **scheduler_options)
    p = submit_payload()
    with guarded():
        result = adapter(sched).handle(p, trusted_context=ctx(p))
    assert result.outcome == "SUBMITTED" and len(sched.calls) == 1
    job_ref, job_rev = result.view["job_ref"], result.view["job_revision"]
    sched.admit(job_ref)
    q = job_payload("request_cancel", request_id="req-cancel-1", job_ref=job_ref,
                    expected_job_revision=job_rev, reason=CANCEL_REASON)
    return sched, q, result


def proofs(q, *claims, revision=None):
    """The trusted caller's own claims: the scheduler's readback relays its terminal reader's
    evidence ref, so the producer is ``symphony-readback`` (trusted by ``ctx``), not the reader."""
    return tuple(pm.TrustedClaim(READBACK_PRODUCER, TERMINAL_EVIDENCE, q["job_ref"],
                                 revision or q["expected_job_revision"], claim) for claim in claims)


def cancel(sched, q, context):
    with guarded():
        return adapter(sched).handle(q, trusted_context=context)


def native_port(sched, index=0):
    return sched.s2_calls[index][2]


def test_independent_terminal_proof_is_the_only_accepted_stop(s2):
    P = s2.primary
    sched, q, submit = submitted(s2)
    result = cancel(sched, q, ctx(q, trusted_claims=proofs(q, *STOP_CLAIMS)))

    assert (result.outcome, result.hold) == ("CANCEL_REQUESTED", False)
    assert result.view["worker_stopped"] is True
    assert result.view["verified_claims"] == sorted(STOP_CLAIMS)
    assert result.view["reservation"] == "POSSIBLY_CONSUMED_HELD"  # no release without its own proof
    assert result.view["scheduler_reservation_state"] == "POSSIBLY_CONSUMED"
    assert result.view["cancel_state"] == "CANCEL_REQUESTED"  # never "stopped"; the proof is the claims

    # The actual frozen consumer confirmed the cancel and its receipt validates against S2's own context.
    [(req, s2_context, port, receipt)] = sched.s2_calls
    assert receipt["stage"] == "CANCEL_CONFIRMED_FAKE" and receipt["reservationState"] == "POSSIBLY_CONSUMED"
    assert P.validate_receipt(receipt, req, context=s2_context) == []
    # One explicit PM request -> one scheduler call -> one native cancel; no start, at most two readbacks.
    assert [c[0] for c in sched.calls] == ["submit_intent", "request_cancel"]
    assert len(port.cancels) == 1 and port.starts == [] and port.reads <= 2
    # The native cancel is bound to the job the adapter submitted, not to a PM-chosen process.
    assert port.cancels[0].request_digest == req.digest
    assert req.to_dict()["payloadHash"] == submit.payload_digest
    assert req.to_dict()["jobId"] == q["job_ref"] and req.to_dict()["jobRevision"] == q["expected_job_revision"]
    assert port.cancels[0].attempt_id == sched.admitted[q["job_ref"]][0]
    assert sched.calls[1][1].reason == CANCEL_REASON


def test_scheduler_claims_without_independent_trusted_claims_stay_unverified(s2):
    sched, q, _ = submitted(s2)
    result = cancel(sched, q, ctx(q))  # trusted caller supplied no claims
    assert (result.outcome, result.hold) == ("UNVERIFIED", True)
    assert set(result.issues) == {"UNBACKED_CLAIM:" + claim for claim in STOP_CLAIMS}
    assert sched.s2_calls[0][3]["stage"] == "CANCEL_CONFIRMED_FAKE"  # the consumer did confirm
    assert len(sched.calls) == 2 and len(native_port(sched).cancels) == 1  # and nothing was retried

    for wrong in (dict(revision="b" * 64), dict()):
        sched, q, _ = submitted(s2)
        claims = proofs(q, *STOP_CLAIMS, **wrong)
        if not wrong:  # right binding, untrusted producer
            claims = tuple(pm.TrustedClaim("self-asserted-worker", c.evidence_ref, c.job_ref,
                                           c.job_revision, c.claim) for c in claims)
        result = cancel(sched, q, ctx(q, trusted_claims=claims))
        assert (result.outcome, result.hold) == ("UNVERIFIED", True)
        assert set(result.issues) == {"UNBACKED_CLAIM:" + claim for claim in STOP_CLAIMS}
        assert len(native_port(sched).cancels) == 1


def test_partial_proof_never_reports_a_stop(s2):
    sched, q, _ = submitted(s2)
    partial = tuple(c for c in STOP_CLAIMS if c != "descendants_stopped")
    result = cancel(sched, q, ctx(q, trusted_claims=proofs(q, *partial)))
    assert (result.outcome, result.hold) == ("UNVERIFIED", True)
    assert result.issues == ("UNBACKED_CLAIM:descendants_stopped",)
    assert len(native_port(sched).cancels) == 1


def test_cancel_without_independent_terminal_proof_is_requested_not_stopped(s2):
    P = s2.primary
    sched, q, _ = submitted(s2, independent_proof=False)
    # The trusted caller even holds claims; the scheduler made none, so nothing can be verified.
    result = cancel(sched, q, ctx(q, trusted_claims=proofs(q, *STOP_CLAIMS)))

    assert (result.outcome, result.hold) == ("CANCEL_REQUESTED", True)
    assert result.view["worker_stopped"] is False and result.view["verified_claims"] == []
    assert result.view["reservation"] == "POSSIBLY_CONSUMED_HELD"
    assert "TERMINAL_PROOF_MISSING" in result.view["uncertainty"]

    [(req, s2_context, port, receipt)] = sched.s2_calls
    assert receipt["stage"] == "CANCEL_UNKNOWN"  # the fake native port reported all four flags itself
    assert receipt["observation"]["native_terminal"] is True
    assert P.validate_receipt(receipt, req, context=s2_context) == []
    assert len(port.cancels) == 1 and port.starts == [] and port.reads <= 2
    # A receipt edited to CONFIRMED with the same no-proof context is rejected by the consumer itself.
    forged = dict(receipt, stage="CANCEL_CONFIRMED_FAKE", issues=["NATIVE_USAGE_UNKNOWN_SIMULATION_ONLY"])
    assert P.validate_receipt(forged, req, context=s2_context)


def test_unfenced_cancel_is_refused_before_any_native_cancel(s2):
    sched, q, _ = submitted(s2, fenced=False)
    result = cancel(sched, q, ctx(q, trusted_claims=proofs(q, *STOP_CLAIMS)))
    assert (result.outcome, result.hold) == ("REJECTED", True)
    assert result.view["worker_stopped"] is False
    assert result.view["reservation"] == "POSSIBLY_CONSUMED_HELD"
    [(_, _, port, receipt)] = sched.s2_calls
    assert receipt["stage"] == "HOLD" and "FENCE_REQUIRED" in receipt["issues"]
    assert port.cancels == [] and port.starts == [] and port.reads == 0
    assert "FENCE_REQUIRED" in result.view["uncertainty"]


def test_lost_cancel_response_is_unknown_without_a_second_native_cancel(s2):
    sched, q, _ = submitted(s2)
    sched.lost = True
    result = cancel(sched, q, ctx(q, trusted_claims=proofs(q, *STOP_CLAIMS)))
    assert (result.outcome, result.hold, result.issues) == ("UNKNOWN", True, ("RESPONSE_LOST",))
    assert (result.request_id, result.operation) == (q["request_id"], "request_cancel")
    assert len(sched.calls) == 2 and len(sched.s2_calls) == 1  # the native cancel happened once
    assert len(native_port(sched).cancels) == 1

    # Repeating is a new explicit request to the sole owner, never an adapter retry.
    sched.lost = False
    again = cancel(sched, q, ctx(q, trusted_claims=proofs(q, *STOP_CLAIMS)))
    assert again.outcome == "CANCEL_REQUESTED" and again.hold is False
    assert len(sched.calls) == 3 and len(sched.s2_calls) == 2


def test_cancel_cannot_reach_the_consumer_without_trusted_context(s2):
    sched, q, _ = submitted(s2)
    with guarded():
        result = adapter(sched).handle(q, trusted_context=None)
    assert (result.outcome, result.hold, result.issues) == ("WAITING_FOR_AUTHORITY", True,
                                                            ("TRUSTED_CONTEXT_ABSENT",))
    smuggled = dict(q, trusted_context={"issuer_ref": "pm-intake-fake"})
    result = cancel(sched, smuggled, ctx(q))
    assert result.outcome == "REJECTED" and result.hold is True
    assert len(sched.calls) == 1 and sched.s2_calls == []
