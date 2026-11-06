"""Disk-backed OFFLINE contract model for CR-hermes-symphony-ingress-offline-20261106.

SQLite models the sole scheduler owner's journal. The sink/observer are synthetic and
survive connection reopen in the harness. No network, process control, runnable queue,
credential, real native worker or power-loss durability is implemented or qualified.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass

from tools import symphony_pm_adapter as pm

CLAIMS = ("worker_stopped", "descendants_stopped", "external_effects_settled", "billing_settled")
PRODUCER = "symphony-readback"


def digest(value):
    return hashlib.sha256(pm.canonical_bytes(value)).hexdigest()


class Refusal(Exception):
    """Synthetic admission failure; never a grant to retry an external effect."""


class Crash(BaseException):
    """A logical crash boundary, not a killed OS process or a caught transport error."""


@dataclass(frozen=True)
class Token:
    namespace: str
    owner: str
    epoch: int
    expires_at: float


@dataclass(frozen=True)
class Target:
    namespace: str
    project: str
    job: str
    revision: str
    attempt: str
    request_digest: str
    controller_epoch: int
    runtime: str
    session: str
    process: str
    start: str

    @property
    def effect_key(self):
        # Changing transport request id, reason or writer epoch cannot mint another ticket.
        return digest([self.namespace, self.project, self.job, self.revision, self.attempt, "cancel"])


@dataclass(frozen=True)
class Fence:
    target: Target
    epoch: int
    evidence_ref: str
    observed_at: float


@dataclass(frozen=True)
class TerminalProof:
    target: Target
    writer_epoch: int
    producer: str
    evidence_ref: str
    observed_at: float
    native_terminal: bool = True
    descendants_stopped: bool = True
    external_effects_settled: bool = True
    billing_settled: bool = True


class Observer:
    """Independent synthetic input; no port receipt is used to manufacture a proof."""

    def __init__(self):
        self.proof = None
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.proof

    @staticmethod
    def valid(proof, target, epoch, cancel_at, now, max_age):
        return (type(proof) is TerminalProof and proof.target == target
                and proof.writer_epoch == epoch and proof.producer == "terminal-reader-fake"
                and bool(proof.evidence_ref) and math.isfinite(proof.observed_at)
                and cancel_at <= proof.observed_at <= now and now - proof.observed_at <= max_age
                and all(getattr(proof, flag) is True for flag in
                        ("native_terminal", "descendants_stopped", "external_effects_settled", "billing_settled")))

    def caller_claims(self, target, epoch, cancel_at, now, max_age=30):
        proof = self.read()
        if not self.valid(proof, target, epoch, cancel_at, now, max_age):
            return ()
        return tuple(pm.TrustedClaim(PRODUCER, proof.evidence_ref, target.job,
                                    target.revision, claim) for claim in CLAIMS)


class FencedSink:
    """Atomic synthetic receiver. Its counters outlive every reopened owner/port instance."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tokens = {}
        self._fences = {}
        self.calls = []
        self.accepted = []
        self.on_cancel = None

    def activate(self, token):
        with self._lock:
            previous = self._tokens.get(token.namespace)
            if previous is not None and (token.epoch < previous.epoch
                                         or token.epoch == previous.epoch and token != previous):
                raise Refusal("STALE_RECEIVER_EPOCH")
            self._tokens[token.namespace] = token

    def fence(self, target, token, now):
        with self._lock:
            if target.namespace != token.namespace or self._tokens.get(token.namespace) != token:
                raise Refusal("STALE_RECEIVER_EPOCH")
            self._fences[target.effect_key] = Fence(target, token.epoch, "fence-" + target.effect_key, now)

    def read_fence(self, target):
        with self._lock:
            return self._fences.get(target.effect_key)

    def cancel(self, target, token, fence, now):
        with self._lock:
            self.calls.append((target, token, fence))
            if (self._tokens.get(token.namespace) != token or now >= token.expires_at
                    or token.namespace != target.namespace or fence != self._fences.get(target.effect_key)
                    or fence.target != target or fence.epoch != token.epoch):
                raise Refusal("RECEIVER_FENCE")
            if any(t.effect_key == target.effect_key for t in self.accepted):
                raise Refusal("RECEIVER_TICKET_REPLAY")
            self.accepted.append(target)
            if self.on_cancel is not None:
                self.on_cancel(target, token, fence)


_DDL = """
CREATE TABLE IF NOT EXISTS writers(
 namespace TEXT PRIMARY KEY, owner TEXT NOT NULL, epoch INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS operations(
 namespace TEXT, project TEXT, request_id TEXT, binding TEXT NOT NULL,
 job TEXT NOT NULL, revision TEXT NOT NULL, effect_key TEXT,
 PRIMARY KEY(namespace, project, request_id));
CREATE TABLE IF NOT EXISTS jobs(
 namespace TEXT, project TEXT, job TEXT, revision TEXT NOT NULL,
 contract TEXT NOT NULL, target TEXT, PRIMARY KEY(namespace, project, job));
CREATE TABLE IF NOT EXISTS effects(
 effect_key TEXT PRIMARY KEY, target TEXT NOT NULL, state TEXT NOT NULL,
 dispatch_epoch INTEGER NOT NULL, cancel_at REAL NOT NULL, fence_ref TEXT NOT NULL,
 proof TEXT, proof_epoch INTEGER);
"""


class Owner:
    """One namespace owner; every mutating transaction checks its current lease/epoch."""

    def __init__(self, path, namespace="offline-owner"):
        self.namespace = namespace
        self.db = sqlite3.connect(path, isolation_level=None, timeout=0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(_DDL)

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def transaction(self):
        try:
            self.db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise Refusal("STORE_BUSY") from exc
        try:
            yield
            self.db.commit()
        except sqlite3.Error as exc:
            # SQLITE_BUSY at COMMIT leaves a transaction open unless explicitly rolled back.
            self.db.rollback()
            raise Refusal("STORE_OUTCOME_UNKNOWN") from exc
        except BaseException:  # rollback also covers the harness's simulated process crash
            self.db.rollback()
            raise

    def claim(self, owner, now, ttl=60):
        if not math.isfinite(now) or not math.isfinite(ttl) or ttl <= 0:
            raise Refusal("LEASE_TIME")
        with self.transaction():
            row = self.db.execute("SELECT * FROM writers WHERE namespace=?", (self.namespace,)).fetchone()
            if row is not None and now < row["expires"]:
                raise Refusal("WRITER_ACTIVE")
            epoch = 1 if row is None else row["epoch"] + 1
            self.db.execute("INSERT OR REPLACE INTO writers VALUES(?,?,?,?)",
                            (self.namespace, owner, epoch, now + ttl))
        return Token(self.namespace, owner, epoch, now + ttl)

    def _authorize(self, token, now):
        row = self.db.execute("SELECT * FROM writers WHERE namespace=?", (self.namespace,)).fetchone()
        if (row is None or token.namespace != self.namespace or row["owner"] != token.owner
                or row["epoch"] != token.epoch or row["expires"] != token.expires_at
                or not math.isfinite(now) or now >= row["expires"]):
            raise Refusal("WRITER_FENCED")

    def _operation(self, request, context):
        binding = digest([request.operation, request.payload_digest, context.approved_scope_revision,
                          context.contract_digest, context.policy_ref, context.grant_ref,
                          context.issuer_ref, context.scheduler_id])
        row = self.db.execute("SELECT * FROM operations WHERE namespace=? AND project=? AND request_id=?",
                              (self.namespace, request.project_ref, request.request_id)).fetchone()
        if row is not None and row["binding"] != binding:
            raise Refusal("CONFLICT")
        return binding, row

    def _record(self, request, binding, job, revision, effect_key=None):
        self.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?)",
                        (self.namespace, request.project_ref, request.request_id,
                         binding, job, revision, effect_key))

    def submit(self, request, context, token, now):
        with self.transaction():
            self._authorize(token, now)
            binding, known = self._operation(request, context)
            if known is not None:
                return known["job"], known["revision"], "DUPLICATE"
            job = "job-" + uuid.uuid4().hex
            revision = digest([job, binding])
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,NULL)",
                            (self.namespace, request.project_ref, job, revision, context.contract_digest))
            self._record(request, binding, job, revision)
        return job, revision, "SUBMITTED"

    def _job(self, project, job, revision):
        row = self.db.execute("SELECT * FROM jobs WHERE namespace=? AND project=? AND job=?",
                              (self.namespace, project, job)).fetchone()
        if row is None or row["revision"] != revision:
            raise Refusal("JOB_IDENTITY")
        return row

    def admit(self, token, project, job, revision, now, *, request_digest, controller_epoch=1):
        """Synthetic admission supplies binding, never the PM request or transport."""
        with self.transaction():
            self._authorize(token, now)
            row = self._job(project, job, revision)
            if row["target"] is not None:
                raise Refusal("ALREADY_ADMITTED")
            target = Target(self.namespace, project, job, revision, job + "-attempt-1", request_digest,
                            controller_epoch, "claude", "fixture-session", "fixture-process", "fixture-process-start")
            self.db.execute("UPDATE jobs SET target=? WHERE namespace=? AND project=? AND job=?",
                            (json.dumps(asdict(target)), self.namespace, project, job))
        return target

    def target(self, request, context, token, now):
        with self.transaction():
            self._authorize(token, now)
            binding, known = self._operation(request, context)  # refuse before reaching the sink
            row = self._job(request.project_ref, request.job_ref, request.expected_job_revision)
            if row["contract"] != context.contract_digest or row["target"] is None:
                raise Refusal("ADMISSION_BINDING")
            target = Target(**json.loads(row["target"]))
            if request.operation == "get_status" and known is None:
                self._record(request, binding, target.job, target.revision, target.effect_key)
            return target

    def prepare_cancel(self, request, context, token, target, fence, now):
        with self.transaction():
            self._authorize(token, now)
            binding, known = self._operation(request, context)
            job = self._job(request.project_ref, request.job_ref, request.expected_job_revision)
            if (target.namespace != self.namespace or job["target"] != json.dumps(asdict(target))
                    or job["contract"] != context.contract_digest):
                raise Refusal("TARGET_BINDING")
            effect = self.db.execute("SELECT * FROM effects WHERE effect_key=?", (target.effect_key,)).fetchone()
            if effect is not None:
                if known is None:
                    self._record(request, binding, target.job, target.revision, target.effect_key)
                return False
            if (type(fence) is not Fence or fence.target != target or fence.epoch != token.epoch
                    or not fence.evidence_ref or not math.isfinite(fence.observed_at)
                    or not 0 <= now - fence.observed_at <= context.max_age_seconds):
                raise Refusal("FENCE_REQUIRED")
            self.db.execute("INSERT INTO effects VALUES(?,?,'DISPATCH_INTENT',?,?,?,NULL,NULL)",
                            (target.effect_key, json.dumps(asdict(target)), token.epoch, now, fence.evidence_ref))
            self._record(request, binding, target.job, target.revision, target.effect_key)
        return True

    def finish_dispatch(self, token, target, now, state):
        if state not in ("CANCEL_REQUESTED", "UNKNOWN"):
            raise Refusal("STATE")
        with self.transaction():
            self._authorize(token, now)
            self.db.execute("UPDATE effects SET state=? WHERE effect_key=? AND state='DISPATCH_INTENT'",
                            (state, target.effect_key))

    def snapshot(self, target):
        # DISPATCH_INTENT is a spent ticket even before an acknowledgement exists.
        row = self.db.execute("SELECT * FROM effects WHERE effect_key=?", (target.effect_key,)).fetchone()
        if row is None:
            return None
        state = "UNKNOWN" if row["state"] == "DISPATCH_INTENT" else row["state"]
        return {**dict(row), "state": state}

    def reconcile(self, token, target, observer, now, max_age):
        proof = observer.read()
        with self.transaction():
            self._authorize(token, now)
            effect = self.snapshot(target)
            if effect is None or effect["target"] != json.dumps(asdict(target)):
                raise Refusal("TARGET_BINDING")
            if not observer.valid(proof, target, token.epoch, effect["cancel_at"], now, max_age):
                return False
            self.db.execute("UPDATE effects SET state='PROVEN', proof=?, proof_epoch=? WHERE effect_key=?",
                            (json.dumps(asdict(proof)), token.epoch, target.effect_key))
        return True


class Port:
    """In-process OFFLINE port for wire(); intentionally no endpoint, auth or implicit default."""

    def __init__(self, owner, token, sink, observer, clock):
        self.owner, self.token, self.sink, self.observer, self.clock = owner, token, sink, observer, clock
        self.calls = []
        self.crash_at = None
        self.before_effect = None
        self.lose_reply = False

    def _crash(self, point):
        if self.crash_at == point:
            raise Crash(point)

    def _common(self, request, context):
        return dict(contract_version=pm.CONTRACT_VERSION, contract_digest=context.contract_digest,
                    request_id=request.request_id, payload_digest=request.payload_digest,
                    project_ref=request.project_ref, issuer_ref=context.issuer_ref,
                    scheduler_id="symphony-fake", scheduler_epoch=self.token.epoch, observed_at=self.clock())

    def _authorize_context(self, context):
        if (context.scheduler_id != "symphony-fake" or context.scheduler_epoch != self.token.epoch
                or context.expires_at <= self.clock()):
            raise Refusal("SCHEDULER_CONTEXT")

    def submit_intent(self, request, context):
        self.calls.append(request)
        try:
            self._authorize_context(context)
            job, revision, outcome = self.owner.submit(request, context, self.token, self.clock())
        except Refusal as exc:
            outcome = {"CONFLICT": "CONFLICT", "STORE_OUTCOME_UNKNOWN": "UNKNOWN"}.get(str(exc), "REJECTED")
            return pm.PmSubmissionReceipt(**self._common(request, context), outcome=outcome, uncertainty=(str(exc),))
        return pm.PmSubmissionReceipt(**self._common(request, context), outcome=outcome,
                                      job_ref=job, job_revision=revision)

    def request_cancel(self, request, context):
        self.calls.append(request)
        try:
            self._authorize_context(context)
            target = self.owner.target(request, context, self.token, self.clock())
            fence = self.sink.read_fence(target)
            self._crash("before_intent")
            dispatch = self.owner.prepare_cancel(request, context, self.token, target, fence, self.clock())
            if dispatch:
                self._dispatch(target, fence)
        except Refusal as exc:
            return pm.PmCancelReceipt(**self._common(request, context), job_ref=request.job_ref,
                                     job_revision=request.expected_job_revision,
                                     outcome="UNKNOWN" if str(exc) == "STORE_OUTCOME_UNKNOWN" else "REJECTED",
                                     reservation_state="POSSIBLY_CONSUMED", uncertainty=(str(exc),))
        if self.lose_reply:
            return None
        return self._cancel_receipt(request, context, target)

    def _dispatch(self, target, fence):
        self._crash("after_intent")
        if self.before_effect is not None:
            self.before_effect()
        try:
            self.sink.cancel(target, self.token, fence, self.clock())
        except (Refusal, TimeoutError):
            self.owner.finish_dispatch(self.token, target, self.clock(), "UNKNOWN")
            return
        self._crash("after_effect")
        self.owner.finish_dispatch(self.token, target, self.clock(), "CANCEL_REQUESTED")
        self._crash("after_result")

    def _cancel_receipt(self, request, context, target):
        effect = self.owner.snapshot(target)
        claims, refs = (), ()
        if effect["state"] == "PROVEN" and effect["proof_epoch"] == self.token.epoch:
            proof = json.loads(effect["proof"])
            if 0 <= self.clock() - proof["observed_at"] <= context.max_age_seconds:
                claims, refs = CLAIMS, (proof["evidence_ref"],)
        outcome = "UNKNOWN" if effect["state"] == "UNKNOWN" else "CANCEL_REQUESTED"
        return pm.PmCancelReceipt(**self._common(request, context), job_ref=target.job,
                                 job_revision=target.revision, outcome=outcome, claims=claims,
                                 evidence_refs=refs, reservation_state="POSSIBLY_CONSUMED",
                                 uncertainty=() if claims else ("TERMINAL_PROOF_MISSING",))

    def get_status(self, request, context):
        self.calls.append(request)
        self._authorize_context(context)
        target = self.owner.target(request, context, self.token, self.clock())
        effect = self.owner.snapshot(target)
        phase = "ADMITTED" if effect is None else effect["state"]
        return pm.PmStatusReceipt(**self._common(request, context), job_ref=target.job,
                                 job_revision=target.revision, attempt_ref=target.attempt, phase=phase,
                                 evidence_refs=("status-" + target.effect_key,),
                                 retention=(("reservation", "POSSIBLY_CONSUMED_HELD"),))
