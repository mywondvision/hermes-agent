"""Persisted identities, not OS handles or grants inferred from model arguments."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import math
from pathlib import Path
import re

from hermes_cli.plugin_request_binding import RequestIdentity, canonical_home
from tools.symphony_pm_adapter import canonical_bytes


class Refused(RuntimeError):
    """A closed admission/reconciliation boundary, never permission to retry."""


def digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def finite(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def identifier(value) -> bool:
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is not None


def sha(value, length):
    return type(value) is str and re.fullmatch(r'[0-9a-f]{'+str(length)+'}', value) is not None


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    creation: str

    def valid(self):
        return (type(self.pid) is int and self.pid > 0 and type(self.creation) is str
                and 0 < len(self.creation) <= 256)


@dataclass(frozen=True)
class ServingIdentity:
    profile_home: str
    namespace: str
    boot_id: str
    process: ProcessIdentity
    incarnation: str

    def validate(self):
        if (self.profile_home != canonical_home(self.profile_home)
                or not identifier(self.namespace) or type(self.boot_id) is not str or not self.boot_id
                or type(self.process) is not ProcessIdentity or not self.process.valid()
                or not identifier(self.incarnation)):
            raise Refused('SERVING_IDENTITY')


@dataclass(frozen=True)
class WriterToken:
    profile_home: str
    namespace: str
    incarnation: str
    controller: str
    epoch: int


@dataclass(frozen=True)
class ExecutionPlan:
    model: str
    effort: str
    environment_ref: str
    workspace: str
    source_sha: str
    driver: str = 'codex'
    attempt_budget: int = 1

    def validate(self):
        if (not all(type(v) is str and bool(v) for v in (
                self.model, self.effort, self.environment_ref, self.workspace))
                or not Path(self.workspace).is_absolute()
                or not sha(self.source_sha, 40)
                or self.driver != 'codex' or type(self.attempt_budget) is not int
                or self.attempt_budget != 1):
            raise Refused('EXACT_NATIVE_PLAN_REQUIRED')


@dataclass(frozen=True)
class Permit:
    ref: str
    identity: RequestIdentity
    project_ref: str
    request_digest: str
    contract_digest: str
    policy_ref: str
    scope_revision: str
    issuer_ref: str
    expires_at: float
    plan: ExecutionPlan
    operation: str = 'submit_intent'
    job_ref: str = ''
    job_revision: str = ''

    def validate(self, now):
        self.plan.validate()
        if (not finite(now) or not finite(self.expires_at) or now >= self.expires_at
                or not all(identifier(v) for v in (self.ref, self.project_ref, self.policy_ref,
                                                   self.scope_revision, self.issuer_ref))
                or type(self.identity) is not RequestIdentity
                or type(self.identity.session_id) is not str or not self.identity.session_id
                or not identifier(self.identity.request_id)
                or self.identity.profile_home != canonical_home(self.identity.profile_home)
                or any(not sha(v, 64) for v in (
                    self.identity.payload_digest, self.request_digest, self.contract_digest))):
            raise Refused('EXACT_PERMIT_REQUIRED')
        if self.operation not in ('submit_intent', 'get_status', 'request_cancel'):
            raise Refused('PERMIT_OPERATION')
        if self.operation != 'submit_intent' and (
                not identifier(self.job_ref) or not sha(self.job_revision, 64)):
            raise Refused('PERMIT_JOB_BINDING')

    @property
    def binding_digest(self):
        return digest(asdict(self))


@dataclass(frozen=True)
class LaunchTicket:
    namespace: str
    project: str
    job: str
    revision: str
    attempt: str
    request_digest: str
    permit_ref: str
    plan: ExecutionPlan
    session: str


def job_object_name(ticket: LaunchTicket) -> str:
    """The attempt's stable Job identifier in bindings and the journal; the kernel Job itself has no name."""
    return 'Local\\HermesSymphony-' + digest([ticket.namespace, ticket.project, ticket.job,
                                              ticket.revision, ticket.attempt, 'job'])[:32]


@dataclass(frozen=True)
class NativeBinding:
    ticket: LaunchTicket
    job_object: str
    root: ProcessIdentity
    known_processes: tuple[ProcessIdentity, ...]

    @property
    def effect_key(self):
        t = self.ticket
        return digest([t.namespace, t.project, t.job, t.revision, t.attempt, 'cancel'])

    def validate(self):
        if (type(self.job_object) is not str or not self.job_object
                or type(self.root) is not ProcessIdentity or not self.root.valid() or not self.known_processes
                or self.root not in self.known_processes
                or not all(type(p) is ProcessIdentity and p.valid() for p in self.known_processes)
                or len({p.pid for p in self.known_processes}) != len(self.known_processes)):
            raise Refused('EXACT_NATIVE_BINDING_REQUIRED')


def binding_from_json(value):
    value = dict(value)
    ticket = dict(value['ticket'])
    ticket['plan'] = ExecutionPlan(**ticket['plan'])
    value['ticket'] = LaunchTicket(**ticket)
    value['root'] = ProcessIdentity(**value['root'])
    value['known_processes'] = tuple(ProcessIdentity(**p) for p in value['known_processes'])
    result = NativeBinding(**value)
    result.validate()
    return result
