"""Offline SQLite ingress reference: CR-hermes-symphony-ingress-offline-20261106.

No real stop is performed or proven. Connection reopen is not power-loss proof.
The synthetic receiver and its counters outlive owner restarts; replacing a port
must not reset the oracle that detects repeated native cancel attempts.
"""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest import mock

import pytest

from tests.fixtures.symphony_pm_ingress.owner import (
    CLAIMS,
    PRODUCER,
    Crash,
    FencedSink,
    Observer,
    Owner,
    Port,
    Refusal,
    TerminalProof,
)
from tests.plugins.test_symphony_pm_plugin import (
    _bridge_call,
    _call,
    _catalog_names,
)
from tests.tools.test_symphony_pm_adapter import (
    ISSUER,
    SCHED,
    ctx,
    job_payload,
    load_frozen_s2,
    submit_payload,
)
from tests.tools.test_symphony_pm_adapter_cancel_s2 import S2CancelScheduler
from tools import symphony_pm_adapter as pm

pytest_plugins = ("tests.plugins.test_symphony_pm_plugin",)


class Harness:
    def __init__(self, path):
        self.path = path
        self.now = 1000
        self.owner = Owner(path)
        self.token = self.owner.claim("writer-1", self.now)
        self.sink, self.observer = FencedSink(), Observer()
        self.sink.activate(self.token)
        self.port = self._port()
        self.target = None

    def _port(self):
        return Port(self.owner, self.token, self.sink, self.observer, lambda: self.now)

    def context(self, payload, **over):
        values = dict(project_ref=payload["project_ref"], now=self.now, expires_at=self.now + 30,
                      scheduler_epoch=self.token.epoch)
        if self.target is not None and payload["operation"] == "request_cancel":
            effect = self.owner.snapshot(self.target)
            if effect is not None:
                values["trusted_claims"] = self.observer.caller_claims(
                    self.target, self.token.epoch, effect["cancel_at"], self.now)
        values.update(over)
        return ctx(payload, **values)

    def request(self, payload, **context_over):
        adapter = pm.SymphonyPmAdapter(self.port, trusted_issuers=(ISSUER,), scheduler_id=SCHED)
        return adapter.handle(payload, trusted_context=self.context(payload, **context_over))

    def submit(self, *, admit=True):
        payload = submit_payload()
        result = self.request(payload)
        assert result.outcome == "SUBMITTED" and result.hold is False
        if admit:
            self.admit(result)
        return payload, result

    def admit(self, result, request_digest="b" * 64):
        self.target = self.owner.admit(
            self.token, "proj-alpha", result.view["job_ref"], result.view["job_revision"],
            self.now, request_digest=request_digest)
        self.sink.fence(self.target, self.token, self.now)

    def cancel_payload(self, **over):
        return job_payload("request_cancel", request_id="cancel-1", job_ref=self.target.job,
                           expected_job_revision=self.target.revision, **over)

    def reopen(self, *, token=None):
        self.owner.close()
        self.owner = Owner(self.path)
        if token is None:
            self.now = self.token.expires_at + 1
            token = self.owner.claim("writer-restarted", self.now)
        self.token = token
        self.sink.activate(token)
        self.port = self._port()


@pytest.fixture
def rig(tmp_path):
    harness = Harness(tmp_path / "owner.sqlite")
    yield harness
    harness.owner.close()


def typed(payload):
    intent, outcome, issues = pm.parse_intent(payload)
    assert outcome == "OK" and not issues
    return intent


def test_real_wire_dispatch_uses_the_offline_port_and_unwire_closes_it(plugin, rig):
    payload = submit_payload()
    assert _call(pm.TOOL_NAME, payload)["outcome"] == "UNAVAILABLE"
    plugin.module.wire(rig.port, lambda: rig.context(payload), trusted_issuers=(ISSUER,), scheduler_id=SCHED)
    assert _catalog_names() == [pm.TOOL_NAME]
    with mock.patch("socket.create_connection", side_effect=AssertionError("network forbidden")) as network:
        with mock.patch("subprocess.Popen", side_effect=AssertionError("native launch forbidden")) as launch:
            first = _call(pm.TOOL_NAME, payload)
            duplicate = _bridge_call(payload)
    assert first["outcome"] == duplicate["outcome"] == "SUBMITTED"
    assert duplicate["view"]["scheduler_outcome"] == "DUPLICATE"
    assert first["view"]["job_ref"] == duplicate["view"]["job_ref"]
    assert first["view"]["admission"] == "NOT_CONFIRMED"
    network.assert_not_called()
    launch.assert_not_called()
    assert len(rig.port.calls) == 2 and rig.sink.calls == []
    plugin.module.unwire()
    assert _call(pm.TOOL_NAME, payload)["outcome"] == "UNAVAILABLE"
    assert pm.TOOL_NAME not in _catalog_names() and len(rig.port.calls) == 2


def test_durable_submit_identity_survives_reopen_and_conflicts_are_closed(rig):
    payload, first = rig.submit(admit=False)
    rig.reopen()
    duplicate = rig.request(payload)
    assert duplicate.view["scheduler_outcome"] == "DUPLICATE"
    assert (duplicate.view["job_ref"], duplicate.view["job_revision"]) == (
        first.view["job_ref"], first.view["job_revision"])
    assert rig.request(dict(payload, goal="different authorized goal")).outcome == "CONFLICT"
    assert rig.request(payload, approved_scope_revision="different-scope").outcome == "CONFLICT"
    other = dict(payload, project_ref="proj-beta")
    assert rig.request(other).view["job_ref"] != first.view["job_ref"]
    other_owner = Owner(rig.path, namespace="other-scheduler")
    try:
        token = other_owner.claim("other-writer", rig.now)
        job, _, _ = other_owner.submit(typed(payload), rig.context(payload), token, rig.now)
        assert job != first.view["job_ref"]
    finally:
        other_owner.close()
    assert rig.sink.calls == []


def test_second_writer_and_old_token_cannot_mutate_after_takeover(rig):
    payload, _ = rig.submit()
    old = rig.token
    rival = Owner(rig.path)
    try:
        with pytest.raises(Refusal, match="WRITER_ACTIVE"):
            rival.claim("other-writer", rig.now)
        rig.now = old.expires_at + 1
        current = rival.claim("other-writer", rig.now)
        rig.sink.activate(current)
        with pytest.raises(Refusal, match="WRITER_FENCED"):
            rig.owner.submit(typed(payload), rig.context(payload), old, rig.now)
        with pytest.raises(Refusal, match="RECEIVER_FENCE"):
            rig.sink.cancel(rig.target, old, rig.sink.read_fence(rig.target), rig.now)
        assert rig.sink.accepted == []
    finally:
        rival.close()


@pytest.mark.parametrize("overrides", [{"owner": "foreign-writer"}, {"expires_at": 99999}])
def test_receiver_rejects_foreign_or_extended_token_with_same_epoch(rig, overrides):
    rig.submit()
    foreign = replace(rig.token, **overrides)
    with pytest.raises(Refusal, match="STALE_RECEIVER_EPOCH"):
        rig.sink.activate(foreign)
    with pytest.raises(Refusal, match="STALE_RECEIVER_EPOCH"):
        rig.sink.fence(rig.target, foreign, rig.now)
    with pytest.raises(Refusal, match="RECEIVER_FENCE"):
        rig.sink.cancel(rig.target, foreign, rig.sink.read_fence(rig.target), rig.now)
    assert rig.sink.accepted == []


@pytest.mark.parametrize("phase", ["prepare", "result"])
def test_busy_commit_rolls_back_and_connection_recovers_without_redispatch(rig, phase):
    rig.submit()
    payload = rig.cancel_payload()
    reader = sqlite3.connect(rig.path, isolation_level=None, timeout=0)

    def hold_reader():
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM jobs").fetchall()

    if phase == "prepare":
        hold_reader()
    else:
        rig.port.before_effect = hold_reader
    try:
        result = rig.request(payload)
        assert (result.outcome, result.hold) == ("UNKNOWN", True)
        assert "STORE_OUTCOME_UNKNOWN" in result.view["uncertainty"]
    finally:
        reader.rollback()
        reader.close()
    rig.port.before_effect = None
    expected_calls = 0 if phase == "prepare" else 1
    assert len(rig.sink.calls) == expected_calls
    recovered = rig.request(payload)
    assert recovered.outcome == ("CANCEL_REQUESTED" if phase == "prepare" else "UNKNOWN")
    assert len(rig.sink.calls) == 1
    # Reuse the same owner connection after the failed COMMIT, not a replacement connection.
    fresh = dict(submit_payload(), request_id="after-busy-commit")
    assert rig.request(fresh).outcome == "SUBMITTED"
    assert rig.request(payload).hold is True and len(rig.sink.calls) == 1


def test_two_connections_compete_for_one_durable_cancel_ticket(rig):
    rig.submit()
    request = typed(rig.cancel_payload())
    context = rig.context(rig.cancel_payload())
    barrier = threading.Barrier(2)
    ready = [threading.Event(), threading.Event()]

    def contender(index):
        owner = Owner(rig.path)
        try:
            port = Port(owner, rig.token, rig.sink, rig.observer, lambda: rig.now)
            ready[index].set()
            barrier.wait(timeout=5)
            return port.request_cancel(request, context)
        finally:
            owner.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(contender, 0)
        assert ready[0].wait(timeout=5)
        second = pool.submit(contender, 1)
        results = [first.result(timeout=10), second.result(timeout=10)]
    assert any(r.outcome in ("CANCEL_REQUESTED", "UNKNOWN") for r in results)
    assert len(rig.sink.calls) == len(rig.sink.accepted) == 1
    assert rig.owner.snapshot(rig.target)["state"] in ("CANCEL_REQUESTED", "UNKNOWN")


@pytest.mark.parametrize(("point", "before", "after", "outcome"), [
    ("before_intent", 0, 1, "CANCEL_REQUESTED"),
    ("after_intent", 0, 0, "UNKNOWN"),
    ("after_effect", 1, 1, "UNKNOWN"),
    ("after_result", 1, 1, "CANCEL_REQUESTED"),
])
def test_crash_boundaries_reopen_without_redispatch(rig, point, before, after, outcome):
    rig.submit()
    payload = rig.cancel_payload()
    rig.port.crash_at = point
    with pytest.raises(Crash, match=point):
        rig.request(payload)
    assert len(rig.sink.calls) == before
    rig.reopen()
    rig.sink.fence(rig.target, rig.token, rig.now)
    recovered = rig.request(payload)
    assert (recovered.outcome, recovered.hold) == (outcome, True)
    assert recovered.request_id == payload["request_id"]
    assert recovered.view["worker_stopped"] is False
    assert recovered.view["reservation"] == "POSSIBLY_CONSUMED_HELD"
    assert len(rig.sink.calls) == after
    new_key = dict(payload, request_id="cancel-new-delivery", reason="new explicit reason")
    assert rig.request(new_key).outcome == outcome
    assert len(rig.sink.calls) == after


def test_lost_reply_and_new_request_key_do_not_cancel_the_attempt_twice(plugin, rig):
    rig.submit()
    payload = rig.cancel_payload()
    rig.port.lose_reply = True
    plugin.module.wire(rig.port, lambda: rig.context(payload), trusted_issuers=(ISSUER,), scheduler_id=SCHED)
    lost = _call(pm.TOOL_NAME, payload)
    assert (lost["outcome"], lost["hold"], lost["issues"]) == ("UNKNOWN", True, ["RESPONSE_LOST"])
    assert len(rig.sink.calls) == 1
    rig.reopen()
    plugin.module.wire(rig.port, lambda: rig.context(payload), trusted_issuers=(ISSUER,), scheduler_id=SCHED)
    repeated = _bridge_call(payload)
    assert repeated["outcome"] == "CANCEL_REQUESTED" and repeated["hold"] is True
    assert repeated["view"]["worker_stopped"] is False
    newer = dict(payload, request_id="different-transport-key", reason="owner repeated intent")
    plugin.module.wire(rig.port, lambda: rig.context(newer), trusted_issuers=(ISSUER,), scheduler_id=SCHED)
    assert _call(pm.TOOL_NAME, newer)["view"]["reservation"] == "POSSIBLY_CONSUMED_HELD"
    assert len(rig.sink.calls) == len(rig.sink.accepted) == 1
    conflict = rig.request(dict(payload, reason="changed contents of original request"))
    assert (conflict.outcome, conflict.hold) == ("REJECTED", True)
    assert "CONFLICT" in conflict.view["uncertainty"] and len(rig.sink.calls) == 1


def test_absent_fence_and_wrong_authority_never_dispatch(rig):
    rig.submit()
    payload = rig.cancel_payload()
    rig.sink._fences.clear()  # withdraw the synthetic receiver's independent fence readback
    result = rig.request(payload)
    assert (result.outcome, result.hold) == ("REJECTED", True)
    assert result.view["uncertainty"] == ["FENCE_REQUIRED"]
    assert rig.owner.snapshot(rig.target) is None and rig.sink.calls == []
    before = len(rig.port.calls)
    for overrides in ({"issuer_ref": "foreign"}, {"expected_job_ref": "different-job"}):
        assert rig.request(payload, **overrides).outcome == "REJECTED"
    assert len(rig.port.calls) == before


@pytest.mark.parametrize("field", ["job", "revision", "attempt", "request_digest", "controller_epoch",
                                  "runtime", "session", "process", "start", "namespace"])
def test_exact_native_target_cannot_be_replaced_before_ticket_commit(rig, field):
    rig.submit()
    request = typed(rig.cancel_payload())
    change = 99 if field == "controller_epoch" else "wrong-identity"
    foreign = replace(rig.target, **{field: change})
    with pytest.raises(Refusal, match="TARGET_BINDING"):
        rig.owner.prepare_cancel(request, rig.context(rig.cancel_payload()), rig.token,
                                 foreign, rig.sink.read_fence(rig.target), rig.now)
    assert rig.sink.calls == [] and rig.owner.snapshot(rig.target) is None


def test_wrong_job_revision_and_smuggled_native_identity_never_cancel(rig):
    rig.submit()
    payload = rig.cancel_payload()
    for changed in (dict(payload, job_ref="unknown-job"),
                    dict(payload, expected_job_revision="a" * 64),
                    dict(payload, attempt_ref="model-chosen-process")):
        assert rig.request(changed).outcome == "REJECTED"
    assert rig.sink.calls == []
    stale_context = rig.context(payload, scheduler_epoch=rig.token.epoch + 1)
    result = rig.port.request_cancel(typed(payload), stale_context)
    assert result.outcome == "REJECTED" and result.uncertainty == ("SCHEDULER_CONTEXT",)
    assert rig.sink.calls == []


def test_receiver_rechecks_fencing_after_storage_admission(rig):
    rig.submit()
    payload = rig.cancel_payload()
    rival = Owner(rig.path)
    newer = []

    def take_over():
        rig.now = rig.token.expires_at + 1
        newer.append(rival.claim("rival", rig.now))
        rig.sink.activate(newer[0])

    rig.port.before_effect = take_over
    try:
        result = rig.request(payload)
        assert result.hold is True  # the receipt's time/epoch cannot become fresh success
        assert len(rig.sink.calls) == 1 and rig.sink.accepted == []
        assert rig.owner.snapshot(rig.target)["state"] == "UNKNOWN"
    finally:
        rival.close()
    rig.reopen(token=newer[0])
    assert rig.request(payload).outcome == "UNKNOWN"
    assert len(rig.sink.calls) == 1 and rig.sink.accepted == []


def uncertain_cancel(rig):
    rig.submit()
    payload = rig.cancel_payload()
    rig.port.crash_at = "after_effect"
    with pytest.raises(Crash):
        rig.request(payload)
    rig.reopen()
    assert rig.request(payload).outcome == "UNKNOWN"
    proof = TerminalProof(rig.target, rig.token.epoch, "terminal-reader-fake", "terminal-observer-1", rig.now)
    return payload, proof


@pytest.mark.parametrize("overrides", [
    {"native_terminal": False}, {"descendants_stopped": False}, {"external_effects_settled": False},
    {"billing_settled": False}, {"producer": "worker-self-report"}, {"evidence_ref": ""},
    {"writer_epoch": 0}, {"observed_at": 999}, {"observed_at": 2000}, {"observed_at": float("nan")},
])
def test_partial_foreign_or_stale_terminal_proof_keeps_unknown(rig, overrides):
    payload, proof = uncertain_cancel(rig)
    rig.observer.proof = replace(proof, **overrides)
    assert rig.owner.reconcile(rig.token, rig.target, rig.observer, rig.now, 30) is False
    result = rig.request(payload)
    assert (result.outcome, result.hold) == ("UNKNOWN", True)
    assert result.view["worker_stopped"] is False and len(rig.sink.calls) == 1
    assert result.view["reservation"] == "POSSIBLY_CONSUMED_HELD"


def test_wrong_attempt_proof_and_complete_independent_control(rig):
    payload, proof = uncertain_cancel(rig)
    rig.observer.proof = replace(proof, target=replace(proof.target, attempt="late-attempt"))
    assert rig.owner.reconcile(rig.token, rig.target, rig.observer, rig.now, 30) is False
    rig.observer.proof = proof
    assert rig.owner.reconcile(rig.token, rig.target, rig.observer, rig.now, 30) is True
    result = rig.request(payload)
    assert (result.outcome, result.hold) == ("CANCEL_REQUESTED", False)
    assert result.view["worker_stopped"] is True  # synthetic proof only; no real worker exists
    assert result.view["verified_claims"] == sorted(CLAIMS)
    assert result.view["reservation"] == "POSSIBLY_CONSUMED_HELD"
    assert rig.request(payload, trusted_claims=()).outcome == "UNVERIFIED"
    query = dict(payload, operation="get_status", request_id="status-1")
    phase = rig.owner.snapshot(rig.target)["state"]
    observation = pm.TrustedObservation(PRODUCER, "status-" + rig.target.effect_key, rig.target.job,
                                        rig.target.revision, rig.target.attempt, rig.token.epoch, phase, rig.now)
    status = rig.request(query, trusted_observations=(observation,))
    assert status.outcome == "STATUS" and status.view["stage_acceptance"] == "NOT_CLAIMED"
    assert status.view["retention"] == [["reservation", "POSSIBLY_CONSUMED_HELD"]]
    assert rig.request(dict(query, operation="request_cancel")).outcome == "REJECTED"
    assert len(rig.sink.calls) == 1


def test_durable_owner_cancels_actual_frozen_s2_once_across_lost_reply(rig):
    s2 = load_frozen_s2()
    scheduler = S2CancelScheduler(s2, independent_proof=False)
    payload, submitted = rig.submit(admit=False)
    intent = typed(payload)
    job, revision = submitted.view["job_ref"], submitted.view["job_revision"]
    scheduler.jobs[payload["request_id"]] = (intent.payload_digest, job, revision, intent)
    scheduler.admitted[job] = (job + "-attempt-1", "rsv-" + job)
    request = scheduler._primary_request(job)
    rig.admit(submitted, request_digest=request.digest)
    cancel = rig.cancel_payload()

    def source_consumer(target, _token, _fence):
        assert (target.request_digest, target.attempt) == (request.digest, request.to_dict()["attemptId"])
        scheduler.request_cancel(typed(cancel), rig.context(cancel))

    rig.sink.on_cancel = source_consumer
    rig.port.lose_reply = True
    assert rig.request(cancel).outcome == "UNKNOWN"
    rig.reopen()
    assert rig.request(cancel).outcome == "CANCEL_REQUESTED"
    assert rig.request(dict(cancel, request_id="new-explicit-delivery")).hold is True
    assert len(rig.sink.calls) == len(scheduler.s2_calls) == 1
    [(req, context, native, receipt)] = scheduler.s2_calls
    assert len(native.cancels) == 1 and native.starts == []
    assert receipt["stage"] == "CANCEL_UNKNOWN"
    assert s2.primary.validate_receipt(receipt, req, context=context) == []
    assert rig.request(cancel).view["worker_stopped"] is False
