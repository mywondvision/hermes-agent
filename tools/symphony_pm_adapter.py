"""Hermes PM -> Symphony ingress adapter (ADD-HERMES-PM-INGRESS-01, candidate contract).

Inert by construction. This module never calls PRIMARY, spawns a process, opens a file, reads a
home/config, touches the network, persists state, schedules, queues or retries. It turns one
explicit PM intent into at most one typed call on an injected ``SymphonyPmPort`` and verifies the
scheduler's receipt against an out-of-band ``TrustedPmContextV1`` supplied by the trusted caller,
never by tool arguments. Symphony stays the sole owner of job identity, admission, attempts,
reservations, retries and tracker writes.

Without an injected port and trusted context source every invocation is UNAVAILABLE/HOLD. There
is deliberately no module-level registration (built-in discovery only imports modules that
register at import time); ``register_pm_tool(target_registry)`` is the explicit, injected binding.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, fields, replace
from types import MappingProxyType
from typing import Any, Callable, ClassVar, Mapping, Optional, Protocol

CONTRACT_VERSION = "hermes-pm-ingress/v1"
OPERATIONS = ("submit_intent", "get_status", "request_cancel")
TOOL_NAME = "symphony_pm_ingress"
TOOLSET = "symphony_pm"

ID_MAX = 128
TEXT_MAX = 4096
CONSTRAINT_MAX = 2048
CONSTRAINTS_MAX = 32

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,%d}" % (ID_MAX - 1))  # no path/space/shell
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_COMMON = ("contract_version", "operation", "request_id", "project_ref")
_JOB = ("job_ref", "expected_job_revision")
# operation -> (required fields, optional fields); every other field is rejected.
_FIELDS = {
    "submit_intent": (frozenset(_COMMON + ("goal", "raw_constraints")), frozenset()),
    "get_status": (frozenset(_COMMON + _JOB), frozenset()),
    "request_cancel": (frozenset(_COMMON + _JOB), frozenset({"reason"})),
}
DONE_PHASES = frozenset({"DONE", "COMPLETED", "SUCCEEDED", "EXITED_ZERO", "PASSED"})
CANCEL_CLAIMS = frozenset({"worker_stopped", "descendants_stopped", "external_effects_settled",
                           "billing_settled", "reservation_released"})
_STOP_CLAIMS = ("worker_stopped", "descendants_stopped", "external_effects_settled", "billing_settled")


def canonical_bytes(value: Any) -> bytes:
    """UTF-8 JSON, sorted keys, compact separators, no NaN/Infinity. A content binding only."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


def _text(value: Any, limit: int, *, allow_empty: bool = False) -> bool:
    if not isinstance(value, str) or len(value) > limit or (not value and not allow_empty):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates have no canonical UTF-8 form
        return False
    return True


def _finite(value: Any) -> bool:
    """A finite int/float that converts to float; 10**400 and -(10**400) are refused, not raised."""
    if type(value) is int:
        try:
            float(value)
        except OverflowError:
            return False
        return True
    return type(value) is float and math.isfinite(value)


# --------------------------------------------------------------------------- typed intents

class _Intent:
    operation: ClassVar[str]

    def payload(self) -> dict:
        raise NotImplementedError

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(dict(self.payload(), request_id=self.request_id))

    @property
    def payload_digest(self) -> str:
        """Request content without the stable key: same key + different digest is CONFLICT."""
        return _sha256(self.payload())

    @property
    def request_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class SubmitIntent(_Intent):
    operation: ClassVar[str] = "submit_intent"
    request_id: str
    project_ref: str
    goal: str
    raw_constraints: tuple

    def payload(self) -> dict:
        return {"contract_version": CONTRACT_VERSION, "operation": self.operation,
                "project_ref": self.project_ref, "goal": self.goal,
                "raw_constraints": list(self.raw_constraints)}


@dataclass(frozen=True)
class JobQuery(_Intent):
    operation: ClassVar[str] = "get_status"
    request_id: str
    project_ref: str
    job_ref: str
    expected_job_revision: str

    def payload(self) -> dict:
        return {"contract_version": CONTRACT_VERSION, "operation": self.operation,
                "project_ref": self.project_ref, "job_ref": self.job_ref,
                "expected_job_revision": self.expected_job_revision}


@dataclass(frozen=True)
class CancelIntent(_Intent):
    operation: ClassVar[str] = "request_cancel"
    request_id: str
    project_ref: str
    job_ref: str
    expected_job_revision: str
    reason: str = ""

    def payload(self) -> dict:
        return {"contract_version": CONTRACT_VERSION, "operation": self.operation,
                "project_ref": self.project_ref, "job_ref": self.job_ref,
                "expected_job_revision": self.expected_job_revision, "reason": self.reason}


def _field_issues(payload: Mapping, op: str) -> list:
    required, optional = _FIELDS[op]
    keys = set(payload)
    issues = []
    if keys - required - optional:
        issues.append("UNKNOWN_FIELD")
    if required - keys:
        issues.append("MISSING_FIELD")
    if payload.get("contract_version") != CONTRACT_VERSION:
        issues.append("CONTRACT_VERSION")
    names = ("request_id", "project_ref") + (_JOB if op != "submit_intent" else ())
    issues.extend("IDENTIFIER:" + n for n in names if n in payload and not _identifier(payload[n]))
    return issues


def _text_issues(payload: Mapping, op: str) -> list:
    issues = []
    if op == "submit_intent":
        if "goal" in payload and not _text(payload["goal"], TEXT_MAX):
            issues.append("GOAL")
        constraints = payload.get("raw_constraints", [])
        if (not isinstance(constraints, (list, tuple)) or len(constraints) > CONSTRAINTS_MAX
                or not all(_text(c, CONSTRAINT_MAX) for c in constraints)):
            issues.append("RAW_CONSTRAINTS")
    if op == "request_cancel" and "reason" in payload and not _text(payload["reason"], TEXT_MAX,
                                                                    allow_empty=True):
        issues.append("REASON")
    return issues


def parse_intent(payload: Any):
    """Closed-schema parse. Returns ``(intent | None, status, issues)``; nothing is repaired."""
    if not isinstance(payload, Mapping) or not all(isinstance(k, str) for k in payload):
        return None, "REJECTED", ("INTENT_NOT_OBJECT",)
    op = payload.get("operation")
    if not isinstance(op, str) or op not in OPERATIONS:
        return None, "UNSUPPORTED", ("OPERATION_NOT_ALLOWLISTED",)
    issues = _field_issues(payload, op) + _text_issues(payload, op)
    if issues:
        return None, "REJECTED", tuple(issues)
    if op == "submit_intent":
        intent = SubmitIntent(payload["request_id"], payload["project_ref"], payload["goal"],
                              tuple(payload["raw_constraints"]))
    elif op == "get_status":
        intent = JobQuery(payload["request_id"], payload["project_ref"], payload["job_ref"],
                          payload["expected_job_revision"])
    else:
        intent = CancelIntent(payload["request_id"], payload["project_ref"], payload["job_ref"],
                              payload["expected_job_revision"], payload.get("reason", ""))
    return intent, "OK", ()


# --------------------------------------------------------------------------- trusted context

@dataclass(frozen=True)
class TrustedObservation:
    """Independent scheduler readback record supplied by the trusted caller."""
    producer: str
    evidence_ref: str
    job_ref: str
    job_revision: str
    attempt_ref: str
    scheduler_epoch: int
    phase: str
    observed_at: float


@dataclass(frozen=True)
class TrustedCompletion:
    """Independent terminal + required-check evidence; ``checks`` is ((name, status), ...)."""
    producer: str
    evidence_ref: str
    job_ref: str
    job_revision: str
    attempt_ref: str
    terminal: bool
    checks: tuple


@dataclass(frozen=True)
class TrustedClaim:
    """Independent evidence for one cancel-side claim (e.g. ``worker_stopped``)."""
    producer: str
    evidence_ref: str
    job_ref: str
    job_revision: str
    claim: str


@dataclass(frozen=True)
class TrustedPmContextV1:
    """Out-of-band, immutable caller context. A reference to policy/grant, never a permit.

    A Python object is not a cryptographic trust boundary; production authentication and
    transport are a separate gate. It cannot be supplied through tool arguments.
    """
    context_ref: str
    issuer_ref: str
    project_ref: str
    allowed_operations: tuple
    request_id: str
    policy_ref: str
    grant_ref: str
    expires_at: float
    now: float
    max_age_seconds: float
    scheduler_id: str
    scheduler_epoch: int
    contract_digest: str
    contract_version: str = CONTRACT_VERSION
    approved_scope_revision: str = ""
    approved_payload_digest: str = ""
    expected_job_ref: str = ""
    expected_job_revision: str = ""
    trusted_producers: tuple = ()
    trusted_observations: tuple = ()
    trusted_completions: tuple = ()
    trusted_claims: tuple = ()
    required_checks: tuple = ()


# --------------------------------------------------------------------------- port + receipts

@dataclass(frozen=True)
class PmSubmissionReceipt:
    contract_version: str
    contract_digest: str
    request_id: str
    payload_digest: str
    project_ref: str
    issuer_ref: str
    scheduler_id: str
    scheduler_epoch: int
    outcome: str  # SUBMITTED | DUPLICATE | CONFLICT | REJECTED | UNKNOWN
    observed_at: float
    job_ref: str = ""
    job_revision: str = ""
    evidence_refs: tuple = ()
    uncertainty: tuple = ()


@dataclass(frozen=True)
class PmStatusReceipt:
    contract_version: str
    contract_digest: str
    request_id: str
    payload_digest: str
    project_ref: str
    issuer_ref: str
    scheduler_id: str
    scheduler_epoch: int
    job_ref: str
    job_revision: str
    attempt_ref: str
    phase: str
    observed_at: float
    evidence_refs: tuple = ()
    claimed_checks: tuple = ()  # producer assertions; never used for acceptance
    retention: tuple = ()  # ((key, value), ...) preserved as received
    uncertainty: tuple = ()


@dataclass(frozen=True)
class PmCancelReceipt:
    contract_version: str
    contract_digest: str
    request_id: str
    payload_digest: str
    project_ref: str
    issuer_ref: str
    scheduler_id: str
    scheduler_epoch: int
    job_ref: str
    job_revision: str
    outcome: str  # CANCEL_REQUESTED | ALREADY_REQUESTED | REJECTED | UNKNOWN
    observed_at: float
    claims: tuple = ()
    reservation_state: str = ""
    evidence_refs: tuple = ()
    retention: tuple = ()
    uncertainty: tuple = ()


_EVIDENCE_TYPES = {"trusted_observations": TrustedObservation,
                   "trusted_completions": TrustedCompletion, "trusted_claims": TrustedClaim}
_MISSING = object()


def _record_field_ok(name: str, value: Any) -> bool:
    if name == "scheduler_epoch":
        return type(value) is int
    if name == "observed_at":
        return _finite(value)
    if name == "terminal":
        return type(value) is bool
    if name == "checks":
        return type(value) is tuple and all(
            type(c) is tuple and len(c) == 2 and all(type(x) is str for x in c) for c in value)
    return type(value) is str


def _evidence_copy(records: tuple, cls: type) -> tuple:
    """Independent copies of well-formed records; anything else is not evidence and is dropped."""
    return tuple(replace(r) for r in records if type(r) is cls and all(
        _record_field_ok(f.name, getattr(r, f.name, _MISSING)) for f in fields(cls)))


def _trusted_snapshot(ctx: TrustedPmContextV1) -> tuple:
    """H2: validate the trusted collections and return ``(independent snapshot, ())``.

    Runs before the port call. Verification reads only the snapshot, so a port that mutates the
    caller's context or its records (even via ``object.__setattr__``) cannot change the decision.
    """
    issues = []
    if type(ctx.required_checks) is not tuple or not all(
            type(c) is str and _text(c, ID_MAX) for c in ctx.required_checks):
        issues.append("AUTHORITY_REQUIRED_CHECKS")
    if type(ctx.trusted_producers) is not tuple or not all(
            type(p) is str and _identifier(p) for p in ctx.trusted_producers):
        issues.append("AUTHORITY_TRUSTED_PRODUCERS")
    issues.extend("AUTHORITY_EVIDENCE:" + name for name in _EVIDENCE_TYPES
                  if type(getattr(ctx, name)) is not tuple)
    if issues:
        return None, tuple(issues)
    return replace(ctx, **{name: _evidence_copy(getattr(ctx, name), cls)
                           for name, cls in _EVIDENCE_TYPES.items()}), ()


class SymphonyPmPort(Protocol):
    """Typed capability shape of the sole scheduler owner. No implementation ships here."""
    def submit_intent(self, request: SubmitIntent,
                      context: TrustedPmContextV1) -> PmSubmissionReceipt: ...
    def get_status(self, query: JobQuery, context: TrustedPmContextV1) -> PmStatusReceipt: ...
    def request_cancel(self, request: CancelIntent,
                       context: TrustedPmContextV1) -> PmCancelReceipt: ...


@dataclass(frozen=True)
class PmResult:
    outcome: str
    hold: bool
    issues: tuple = ()
    operation: str = ""
    request_id: str = ""
    payload_digest: str = ""
    view: Mapping = field(default_factory=lambda: MappingProxyType({}))

    def to_dict(self) -> dict:
        return {"contract_version": CONTRACT_VERSION, "outcome": self.outcome, "hold": self.hold,
                "issues": list(self.issues), "operation": self.operation,
                "request_id": self.request_id, "payload_digest": self.payload_digest,
                "view": dict(self.view)}


def _result(intent: Optional[_Intent], outcome: str, hold: bool, issues=(), view=None) -> PmResult:
    return PmResult(outcome, hold, tuple(issues),
                    intent.operation if intent else "", intent.request_id if intent else "",
                    intent.payload_digest if intent else "", MappingProxyType(dict(view or {})))


# --------------------------------------------------------------------------- verification

def _context_shape_issues(ctx: TrustedPmContextV1) -> list:
    issues = []
    if ctx.contract_version != CONTRACT_VERSION or not (
            isinstance(ctx.contract_digest, str) and _HEX64_RE.fullmatch(ctx.contract_digest)):
        issues.append("AUTHORITY_CONTRACT")
    if not all(_identifier(v) for v in (ctx.context_ref, ctx.policy_ref, ctx.grant_ref)):
        issues.append("AUTHORITY_REFERENCES")
    if (not _finite(ctx.now) or not _finite(ctx.expires_at) or ctx.now >= ctx.expires_at
            or not _finite(ctx.max_age_seconds) or ctx.max_age_seconds < 0):
        issues.append("AUTHORITY_EXPIRED")
    if type(ctx.scheduler_epoch) is not int or ctx.scheduler_epoch < 1:
        issues.append("AUTHORITY_EPOCH")
    return issues


def _binding_request_issues(intent: _Intent, ctx: TrustedPmContextV1) -> list:
    issues = []
    if ctx.project_ref != intent.project_ref:
        issues.append("AUTHORITY_SCOPE")
    if type(ctx.allowed_operations) is not tuple or intent.operation not in ctx.allowed_operations:
        issues.append("AUTHORITY_OPERATION")
    if ctx.request_id != intent.request_id:
        issues.append("BINDING_REQUEST_ID")
    if isinstance(intent, SubmitIntent):
        if not _identifier(ctx.approved_scope_revision):
            issues.append("AUTHORITY_SCOPE_REVISION")
        if ctx.approved_payload_digest != intent.payload_digest:
            issues.append("BINDING_PAYLOAD")
    elif (ctx.expected_job_ref, ctx.expected_job_revision) != (intent.job_ref,
                                                               intent.expected_job_revision):
        issues.append("BINDING_JOB_REVISION")
    return issues


_BINDING_TEXT = ("contract_version", "contract_digest", "request_id", "payload_digest",
                 "project_ref", "issuer_ref", "scheduler_id")
_RECEIPT_TEXT = {PmSubmissionReceipt: ("outcome", "job_ref", "job_revision"),
                 PmStatusReceipt: ("job_ref", "job_revision", "attempt_ref", "phase"),
                 PmCancelReceipt: ("outcome", "job_ref", "job_revision", "reservation_state")}
_RECEIPT_TUPLES = {PmSubmissionReceipt: ("uncertainty",),  # evidence_refs: RECEIPT_EVIDENCE_REFS
                   PmStatusReceipt: ("retention", "uncertainty"),
                   PmCancelReceipt: ("claims", "retention", "uncertainty")}


def _json_scalar(value: Any) -> bool:
    return value is None or type(value) in (str, bool, int) or (
        type(value) is float and math.isfinite(value))


def _receipt_shape_issues(receipt: Any, cls: type) -> list:
    """H1: exact field types before any membership test, hashing, iteration or comparison."""
    if any(getattr(receipt, f.name, _MISSING) is _MISSING for f in fields(cls)):
        return ["RECEIPT_SHAPE:MISSING_FIELD"]
    issues = ["RECEIPT_SHAPE:" + name for name in _BINDING_TEXT + _RECEIPT_TEXT[cls]
              if type(getattr(receipt, name)) is not str]
    issues += ["RECEIPT_SHAPE:" + name for name in _RECEIPT_TUPLES[cls]
               if type(getattr(receipt, name)) is not tuple]
    if issues:
        return issues
    if not all(type(item) is str and _text(item, TEXT_MAX, allow_empty=True)
               for item in receipt.uncertainty):
        issues.append("RECEIPT_SHAPE:uncertainty")
    if "retention" in _RECEIPT_TUPLES[cls] and not all(
            type(pair) is tuple and len(pair) == 2 and type(pair[0]) is str
            and _json_scalar(pair[1]) for pair in receipt.retention):
        issues.append("RECEIPT_SHAPE:retention")
    return issues


def _receipt_issues(receipt: Any, cls: type, intent: _Intent, ctx: TrustedPmContextV1) -> list:
    """Bind a receipt to the independent trusted context, not to its own fields."""
    if type(receipt) is not cls:
        return ["RECEIPT_TYPE"]
    shape = _receipt_shape_issues(receipt, cls)
    if shape:
        return shape
    pairs = (("contract_version", CONTRACT_VERSION), ("contract_digest", ctx.contract_digest),
             ("request_id", intent.request_id), ("payload_digest", intent.payload_digest),
             ("project_ref", ctx.project_ref), ("issuer_ref", ctx.issuer_ref),
             ("scheduler_id", ctx.scheduler_id))
    issues = ["RECEIPT_BINDING:" + name for name, value in pairs if getattr(receipt, name) != value]
    if type(receipt.scheduler_epoch) is not int or receipt.scheduler_epoch != ctx.scheduler_epoch:
        issues.append("RECEIPT_EPOCH")
    if not isinstance(intent, SubmitIntent) and (receipt.job_ref, receipt.job_revision) != (
            intent.job_ref, intent.expected_job_revision):
        issues.append("RECEIPT_JOB_REVISION")
    at = receipt.observed_at
    if not _finite(at) or at > ctx.now or ctx.now - at > ctx.max_age_seconds:
        issues.append("RECEIPT_STALE")
    if type(receipt.evidence_refs) is not tuple or not all(_identifier(r) for r in receipt.evidence_refs):
        issues.append("RECEIPT_EVIDENCE_REFS")
    return issues


def _trusted(record: Any, cls: type, receipt: Any, ctx: TrustedPmContextV1) -> bool:
    return (type(record) is cls and record.producer in ctx.trusted_producers
            and record.evidence_ref in receipt.evidence_refs
            and (record.job_ref, record.job_revision) == (ctx.expected_job_ref,
                                                          ctx.expected_job_revision))


def _checks_pass(checks: Any, required: set) -> bool:
    if type(checks) is not tuple or not all(
            type(c) is tuple and len(c) == 2 and all(isinstance(x, str) for x in c) for c in checks):
        return False
    names = [name for name, _ in checks]
    return (len(names) == len(set(names)) and required <= set(names)
            and all(status == "pass" for _, status in checks))


def _completion(receipt: PmStatusReceipt, ctx: TrustedPmContextV1):
    if receipt.phase not in DONE_PHASES:
        return "NOT_CLAIMED", ()
    required = set(ctx.required_checks)
    if not required:
        return "NOT_ACCEPTED", ("REQUIRED_CHECKS_UNDEFINED",)
    for record in ctx.trusted_completions:
        if (_trusted(record, TrustedCompletion, receipt, ctx)
                and record.attempt_ref == receipt.attempt_ref and record.terminal is True
                and _checks_pass(record.checks, required)):
            return "ACCEPTED", ()
    return "NOT_ACCEPTED", ("TRUSTED_COMPLETION_EVIDENCE_REQUIRED",)


def _observation(receipt: PmStatusReceipt, ctx: TrustedPmContextV1):
    key = (receipt.attempt_ref, receipt.scheduler_epoch, receipt.phase, receipt.observed_at)
    for record in ctx.trusted_observations:
        if (_trusted(record, TrustedObservation, receipt, ctx)
                and (record.attempt_ref, record.scheduler_epoch, record.phase,
                     record.observed_at) == key):
            return record
    return None


def _retention(receipt: Any) -> list:
    return [list(pair) if isinstance(pair, tuple) else pair for pair in receipt.retention]


_SUBMIT_OUTCOMES = {"SUBMITTED": ("SUBMITTED", False), "DUPLICATE": ("SUBMITTED", False),
                    "CONFLICT": ("CONFLICT", True), "REJECTED": ("REJECTED", True),
                    "UNKNOWN": ("UNKNOWN", True)}
_CANCEL_OUTCOMES = {"CANCEL_REQUESTED": "CANCEL_REQUESTED", "ALREADY_REQUESTED": "CANCEL_REQUESTED",
                    "REJECTED": "REJECTED", "UNKNOWN": "UNKNOWN"}


def _verify_submit(receipt: Any, intent: SubmitIntent, ctx: TrustedPmContextV1) -> PmResult:
    issues = _receipt_issues(receipt, PmSubmissionReceipt, intent, ctx)
    if not issues and receipt.outcome not in _SUBMIT_OUTCOMES:
        issues.append("RECEIPT_OUTCOME")
    if not issues and receipt.outcome in ("SUBMITTED", "DUPLICATE") and not (
            _identifier(receipt.job_ref) and _identifier(receipt.job_revision)):
        issues.append("RECEIPT_JOB_IDENTITY")
    if issues:
        return _result(intent, "UNVERIFIED", True, issues)
    outcome, hold = _SUBMIT_OUTCOMES[receipt.outcome]
    return _result(intent, outcome, hold, (), {
        "scheduler_outcome": receipt.outcome, "job_ref": receipt.job_ref,
        "job_revision": receipt.job_revision, "admission": "NOT_CONFIRMED",
        "observed_at": receipt.observed_at, "age_seconds": ctx.now - receipt.observed_at,
        "evidence_refs": list(receipt.evidence_refs), "uncertainty": list(receipt.uncertainty)})


def _verify_status(receipt: Any, intent: JobQuery, ctx: TrustedPmContextV1) -> PmResult:
    issues = _receipt_issues(receipt, PmStatusReceipt, intent, ctx)
    record = None if issues else _observation(receipt, ctx)
    if not issues and record is None:
        issues.append("STATUS_PROVENANCE_UNVERIFIED")
    if issues:
        return _result(intent, "UNVERIFIED", True, issues)
    acceptance, reasons = _completion(receipt, ctx)
    return _result(intent, "STATUS", acceptance == "NOT_ACCEPTED", reasons, {
        "observation": "LAST_CONFIRMED_SCHEDULER_OBSERVATION", "phase": receipt.phase,
        "job_ref": receipt.job_ref, "job_revision": receipt.job_revision,
        "attempt_ref": receipt.attempt_ref, "observed_at": receipt.observed_at,
        "age_seconds": ctx.now - receipt.observed_at, "evidence_ref": record.evidence_ref,
        "stage_acceptance": acceptance, "retention": _retention(receipt),
        "uncertainty": list(receipt.uncertainty)})


def _verify_cancel(receipt: Any, intent: CancelIntent, ctx: TrustedPmContextV1) -> PmResult:
    issues = _receipt_issues(receipt, PmCancelReceipt, intent, ctx)
    if not issues and (receipt.outcome not in _CANCEL_OUTCOMES  # str/tuple guaranteed by shape
                       or not all(type(claim) is str for claim in receipt.claims)
                       or not set(receipt.claims) <= CANCEL_CLAIMS):
        issues.append("RECEIPT_OUTCOME")
    if not issues:
        issues.extend("UNBACKED_CLAIM:" + claim for claim in receipt.claims if not any(
            _trusted(r, TrustedClaim, receipt, ctx) and r.claim == claim for r in ctx.trusted_claims))
    if issues:
        return _result(intent, "UNVERIFIED", True, issues)
    outcome = _CANCEL_OUTCOMES[receipt.outcome]
    verified = sorted(receipt.claims)
    stopped = all(claim in verified for claim in _STOP_CLAIMS)
    return _result(intent, outcome, not (outcome == "CANCEL_REQUESTED" and stopped), (), {
        "cancel_state": outcome,  # a request, never "stopped"; stop proof is verified_claims
        "scheduler_outcome": receipt.outcome, "verified_claims": verified,
        "worker_stopped": "worker_stopped" in verified,
        "reservation": ("RELEASED_PER_TRUSTED_EVIDENCE" if "reservation_released" in verified
                        else "POSSIBLY_CONSUMED_HELD"),
        "scheduler_reservation_state": receipt.reservation_state,
        "observed_at": receipt.observed_at, "retention": _retention(receipt),
        "uncertainty": list(receipt.uncertainty)})


_VERIFIERS = {"submit_intent": _verify_submit, "get_status": _verify_status,
              "request_cancel": _verify_cancel}


def _verify_guarded(receipt: Any, intent: _Intent, ctx: TrustedPmContextV1) -> PmResult:
    """H1 backstop after the single port call: never re-invokes the port, never raises."""
    try:
        result = _VERIFIERS[intent.operation](receipt, intent, ctx)
        canonical_bytes(result.to_dict())  # the tool surface must be able to serialise it
        return result
    except Exception:  # a malformed receipt that slipped past the explicit shape checks
        return _result(intent, "UNVERIFIED", True, ("RECEIPT_MALFORMED",))


class SymphonyPmAdapter:
    """One explicit call -> at most one port invocation. No ledger, retry, thread or fallback."""
    __slots__ = ("_port", "_trusted_issuers", "_scheduler_id")

    def __init__(self, port: Optional[SymphonyPmPort] = None, *, trusted_issuers: tuple = (),
                 scheduler_id: str = ""):
        self._port = port
        self._trusted_issuers = tuple(trusted_issuers)
        self._scheduler_id = scheduler_id

    def _authority_issues(self, intent: _Intent, ctx: TrustedPmContextV1) -> list:
        issues = []
        if ctx.issuer_ref not in self._trusted_issuers or ctx.scheduler_id != self._scheduler_id:
            issues.append("AUTHORITY_FOREIGN")
        return issues + _context_shape_issues(ctx) + _binding_request_issues(intent, ctx)

    def handle(self, payload: Any, *, trusted_context: Optional[TrustedPmContextV1] = None) -> PmResult:
        if self._port is None:
            return _result(None, "UNAVAILABLE", True, ("NO_BACKEND",))
        intent, status, issues = parse_intent(payload)
        if intent is None:
            return _result(None, status, True, issues)
        if type(trusted_context) is not TrustedPmContextV1:
            return _result(intent, "WAITING_FOR_AUTHORITY", True, ("TRUSTED_CONTEXT_ABSENT",))
        issues = self._authority_issues(intent, trusted_context)
        if issues:
            return _result(intent, "REJECTED", True, issues)
        snapshot, issues = _trusted_snapshot(trusted_context)  # H2: before the single port call
        if snapshot is None:
            return _result(intent, "REJECTED", True, issues)
        port_intent = replace(intent)  # the port never holds the object carrying our identity
        try:  # intent.operation is one of the three allowlisted port method names
            receipt = getattr(self._port, intent.operation)(port_intent, trusted_context)
        except Exception:  # timeout/transport/unknown: same key, no second invocation
            return _result(intent, "UNKNOWN", True, ("PORT_OUTCOME_UNKNOWN",))
        if receipt is None:
            return _result(intent, "UNKNOWN", True, ("RESPONSE_LOST",))
        return _verify_guarded(receipt, intent, snapshot)


# --------------------------------------------------------------------------- inert tool surface

def tool_schema() -> dict:
    text = {"type": "string"}
    return {
        "name": TOOL_NAME,
        "description": ("Ask the Symphony scheduler to record a PM intent, read a job's last "
                        "confirmed status, or request cancellation. It never runs, retries or "
                        "approves work; authority comes only from the trusted session."),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "required": list(_COMMON),
            "properties": {
                "contract_version": {"type": "string", "enum": [CONTRACT_VERSION]},
                "operation": {"type": "string", "enum": list(OPERATIONS)},
                "request_id": text, "project_ref": text, "goal": text,
                "raw_constraints": {"type": "array", "items": text, "maxItems": CONSTRAINTS_MAX},
                "job_ref": text, "expected_job_revision": text, "reason": text,
            },
        },
    }


def make_pm_tool_handler(adapter: Optional[SymphonyPmAdapter] = None,
                         context_source: Optional[Callable[[], Any]] = None) -> Callable:
    """Context comes only from the injected trusted source, never from ``args`` or runtime kwargs."""
    def handler(args: Any, **_runtime_kwargs: Any) -> str:
        if adapter is None or context_source is None:
            result = _result(None, "UNAVAILABLE", True, ("NO_BACKEND",))
        else:
            result = adapter.handle(args, trusted_context=context_source())
        return json.dumps(result.to_dict(), sort_keys=True, ensure_ascii=False)
    return handler


def register_pm_tool(target_registry: Any, *, adapter: Optional[SymphonyPmAdapter] = None,
                     context_source: Optional[Callable[[], Any]] = None) -> None:
    """Explicit, injected binding to a Hermes-style registry. Unwired it stays unavailable."""
    wired = adapter is not None and context_source is not None
    target_registry.register(
        name=TOOL_NAME, toolset=TOOLSET, schema=tool_schema(),
        handler=make_pm_tool_handler(adapter, context_source),
        check_fn=lambda: wired, description="Symphony PM ingress (inert unless wired)")
