"""Serving owner: fencing, journal recovery and one cancel coordinator.

The host must inject a qualified OS ownership guard, query-only observer, native
receiver provider and the existing CodexDriver launcher. The serving bootstrap
installs its profile-bound worker; recovery never schedules a new attempt.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import partial
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Protocol

from plugins.symphony_pm.contracts import LaunchExpired
from .journal import Journal, encode
from .types import (
    NativeBinding, ProcessIdentity, Refused, ServingIdentity,
    finite,
)


class OwnershipGuard(Protocol):
    identity: ServingIdentity
    def assert_owned(self) -> None: ...
    def require_predecessor_dead(self, previous: dict) -> None: ...
    def close(self, deadline: float) -> None: ...


@dataclass(frozen=True)
class ObserverHandshake:
    owner: ServingIdentity
    generation: int
    observed_at: float
    covered_bindings: tuple[str, ...]


@dataclass(frozen=True)
class ObservationTarget:
    binding: NativeBinding
    known_processes: tuple[ProcessIdentity, ...]


@dataclass(frozen=True)
class ProcessReadback:
    identity: ProcessIdentity
    signaled: bool


@dataclass(frozen=True)
class StopReadback:
    binding: NativeBinding
    epoch: int
    generation: int
    observed_at: float
    job_identity_verified: bool
    job_active: int
    job_members: tuple[ProcessIdentity | int, ...]
    processes: tuple[ProcessReadback, ...]


class QueryObserver(Protocol):
    def handshake(self, owner, generation, bindings) -> ObserverHandshake: ...
    def read(self, binding, epoch, generation) -> StopReadback: ...
    def set_loss_handler(self, callback) -> None: ...
    def close(self, deadline) -> None: ...


class NativeProvider(Protocol):
    """Provider preserves CodexDriver validation and creates only exact suspended roots.

    create_suspended either returns the owned receiver/binding or rolls back its
    own pre-admission exact handles. It never resumes before the owner commits.
    wait_result returns the existing kit CommandResult; no provider probing or
    alternate spawn is permitted in this runner path.
    """
    def validate_driver_call(self, ticket, argv, cwd, timeout, stdin_text) -> None: ...
    def create_suspended(self, ticket, argv, cwd, stdin_text): ...


@dataclass(frozen=True)
class CancelOutcome:
    state: str
    dispatched: bool = False
    local_stopped: bool = False
    reservation: str = 'POSSIBLY_CONSUMED_HELD'
    hold: bool = True


class AttemptRunner:
    """Immutable attempt-bound injection into the existing CodexDriver."""
    def __init__(self, owner, token, ticket):
        self.owner, self.token, self.ticket = owner, token, ticket

    def __call__(self, argv, cwd=None, timeout=None, stdin_text=None):
        return self.owner.run_command(self.token, self.ticket, argv, cwd, timeout, stdin_text)


class LifecycleOwner:
    journal_type = Journal
    binding_type = NativeBinding

    def __init__(self, scope, guard: OwnershipGuard, observer: QueryObserver,
                 native: NativeProvider, launcher, *, scheduler_id, controller, clock=time.time,
                 migrate_v1=False, legacy_ownership=()):
        guard.assert_owned()
        identity = guard.identity
        identity.validate()
        if identity.profile_home != scope.profile_home or identity.namespace != scope.namespace:
            raise Refused('HOST_PROFILE_BINDING')
        if not scheduler_id or not callable(launcher):
            raise Refused('QUALIFIED_DRIVER_LAUNCHER_REQUIRED')
        self.scope, self.guard, self.observer, self.native = scope, guard, observer, native
        self.launcher, self.scheduler_id, self.clock = launcher, scheduler_id, clock
        self.lock = threading.RLock()
        self.supervisor = None
        self._closing, self._closed, self._observer_available = False, False, False
        self._receivers, self._sent_cancel = {}, set()
        self._preparing = {}  # attempt -> a pre-create cancellation requires workspace settlement
        ownership_contract = getattr(native, 'ownership_contract', None)
        if not self._coverage_verified():
            # The client's own Job can be perfectly empty while an unqualified MXC command runs elsewhere.
            # Its owner's later death cannot turn that observation into a predecessor stop proof either.
            ownership_contract = None
        self.journal = self.journal_type(scope.profile_home, scope.namespace, migrate_v1=migrate_v1,
                               ownership_contract=ownership_contract,
                               host_id=getattr(guard, 'recovery_host_id', None),
                               workspace_clear=getattr(launcher, 'workspace_clear', None))
        previous = self.journal.previous_owner()
        proof = None
        if previous is not None:
            if hasattr(guard, 'predecessor_proof'):
                proof = guard.predecessor_proof(previous)
            if proof is None and previous['boot_id'] == identity.boot_id:
                guard.require_predecessor_dead(previous)
        self.token = self.journal.claim(identity, controller, previous, proof=proof, legacy_ownership=legacy_ownership)
        self._settle_workspaces()
        self.generation = self.journal.observer_state(self.token, healthy=False)
        observer.set_loss_handler(self.observer_lost)
        try:
            self._refresh_observer()
        except Refused as exc:
            # A successor may lack the native handles of an unattested predecessor.
            # Keep the writer available for explicit release. Admission stays closed
            # until a later handshake actually covers every outstanding binding.
            rows = self.journal.unfinished(self.token)
            if (str(exc) != 'OBSERVER_COVERAGE_UNKNOWN' or not rows
                    or any(row['state'] != 'HELD_UNKNOWN' for row in rows)):
                raise

    def _settle_workspaces(self):
        for ticket in self.journal.unsettled_workspaces(self.token):
            self._settle_workspace(ticket)

    def _settle_workspace(self, ticket):
        settle = getattr(self.launcher, 'settle_workspace', None)
        if settle is not None and settle(ticket):
            self.journal.workspace_settled(self.token, ticket)

    def _coverage_verified(self):
        coverage = getattr(self.launcher, 'job_coverage_for', None)
        return coverage is None or coverage(self.guard)

    def check_policy(self, ticket, row, patch, check):
        from .owner_check_profile import checked_plan
        settings = self.launcher.settings
        plan, fingerprint = checked_plan(settings.owner_check, settings.checks(tuple(patch['paths'])))
        return plan, (check.get('plan_sha256') == fingerprint
                      and check.get('revisions') == settings.owner_check.revisions()
                      and 'stop_receipts' not in check)

    def robot_check_plan(self, ticket, paths):
        from .owner_check_profile import checked_plan
        settings = self.launcher.settings
        profile = settings.owner_check
        commands, fingerprint = checked_plan(profile, settings.checks(paths))
        return profile, commands, fingerprint, settings.check_timeout

    def robot_check_context(self, profile, repository, workspace):
        return profile.qualified(profile.git, outside=(repository, Path(self.launcher.settings.attempts_root),
                                 self.journal.path.parent), workspace=workspace)

    def open_robot_git(self, path):
        from .qualification import _open_denying_writers
        return _open_denying_writers(str(path))

    def _finished_client_job(self, row):
        return (callable(getattr(self.launcher, 'job_coverage_for', None)) and row['state'] == 'STOPPED'
                and row['stop_proof'] == 'JOB_EMPTY' and bool(row['proof_source']))

    def recapture(self, token, project, job, revision):
        with self.lock:
            if token is None:
                raise Refused('WRITER_FENCED')
            self._check(token)
            ticket, row = self.journal.read_launch(token, project, job, revision)
            if not self.journal.proven_stop(row) or row['hold_reason']:
                raise Refused('RECAPTURE_REQUIRES_PROVEN_STOP')
            capture = getattr(self.launcher, 'recapture', None)
            if not callable(capture):
                raise Refused('RECAPTURE_UNAVAILABLE')
            return capture(self.journal, token, ticket, row)

    def release_by_owner(self, token, project, job, revision, reason):
        with self.lock:
            self._check(token)
            ticket, _ = self.journal.read_launch(token, project, job, revision)
            self.journal.release_by_owner(token, ticket, reason)

    def _bindings(self):
        return tuple(self.journal.decode_binding(json.loads(row['binding']))
                     for row in self.journal.unfinished(self.token) if row['binding'] is not None)

    def _check(self, expected=None):
        if self._closing or not self.scope.active or (expected is not None and expected != self.token):
            raise Refused('WRITER_FENCED')
        self.guard.assert_owned()

    def _refresh_observer(self, extra=()):
        from .types import digest
        bindings = tuple(dict.fromkeys((*self._bindings(), *extra)))
        targets = tuple(ObservationTarget(b, tuple(dict.fromkeys((
            *b.known_processes, *self.journal.known_members(self.token, b))))) for b in bindings)
        try:
            proof = self.observer.handshake(self.guard.identity, self.generation, targets)
            now = self.clock()
            valid = (type(proof) is ObserverHandshake and proof.owner == self.guard.identity
                     and proof.generation == self.generation and finite(proof.observed_at)
                     and 0 <= now-proof.observed_at <= 30
                     and set(proof.covered_bindings) == {digest(asdict(target)) for target in targets})
        except Exception:  # health: allow BLE001 -- observer failures cannot open admission
            valid = False
        self._observer_available = valid
        self.journal.observer_state(self.token, healthy=valid)
        if not valid:
            raise Refused('OBSERVER_COVERAGE_UNKNOWN')

    def observer_lost(self):
        self._observer_available = False  # revoke before waiting for an in-flight native entry
        with self.lock:
            if self._closed:
                return
            self.journal.observer_state(self.token, healthy=False)

    def replace_observer(self, observer):
        with self.lock:
            self._check()
            self._observer_available = False
            self.generation = self.journal.observer_state(self.token, healthy=False, replacement=True)
            self.observer.close(time.monotonic())
            self.observer = observer
            observer.set_loss_handler(self.observer_lost)
            self._refresh_observer()

    def rotate_controller(self, controller):
        with self.lock:
            self._check()
            self.token = self.journal.rotate_controller(self.token, controller)
            self._refresh_observer()
            return self.token

    def record_permit(self, token, permit, approval_id, approval_digest, now):
        with self.lock:
            self._check(token)
            self.journal.record_permit(token, permit, approval_id, approval_digest, now)

    def find_permit(self, identity, now):
        with self.lock:
            self._check()
            return self.token, self.journal.find_permit(self.token, identity, now)

    def _context(self, context):
        self._check()
        now = self.clock()
        if (context.scheduler_id != self.scheduler_id or context.scheduler_epoch != self.token.epoch
                or not finite(context.expires_at) or now >= context.expires_at):
            raise Refused('SCHEDULER_CONTEXT')

    def _admit_submit(self, request, context):
        self._context(context)

    def submit(self, request, context, *, background=False):
        with self.lock:
            self._admit_submit(request, context)
            if background and self.supervisor is None:
                raise Refused('SUPERVISOR_UNAVAILABLE')
            self._refresh_observer()
            ticket, _, fresh = self.journal.reserve_launch(self.token, request, context, self.clock())
            token = self.token
            if fresh and background:
                try:
                    self.supervisor.offer(token, ticket, request)
                except Refused:
                    self.journal.mark_not_started(token, ticket, reason='SUPERVISOR_UNAVAILABLE',
                                                  authorize_commit=lambda: self._check(token))
                    raise
        if fresh and not background:
            self.execute_reserved(token, ticket, request)
        return ticket, fresh

    def execute_reserved(self, token, ticket, request):
        with self.lock:
            self._check(token)
            actual, row = self.journal.read_launch(token, ticket.project, ticket.job, ticket.revision)
            if actual != ticket or row['state'] != 'LAUNCH_INTENT' or ticket.attempt in self._preparing:
                raise Refused('LAUNCH_TICKET_SPENT')
            self._preparing[ticket.attempt] = False
        try:
            self.journal.verify_launch_authority(token, ticket, self.clock())
            result = self.launcher(ticket, request, AttemptRunner(self, token, ticket))
        except (Exception, SystemExit) as exc:
            with self.lock:
                self._check(token)
                reason = 'LAUNCH_PREPARATION_FAILED'
                if isinstance(exc, LaunchExpired):
                    reason = str(exc)
                if isinstance(exc, Refused) and str(exc) == 'CODEX_PRECHECK_REFUSED':
                    reason = 'CODEX_PRECHECK_REFUSED'
                self.journal.mark_not_started(token, ticket, reason=reason, authorize_commit=lambda: self._check(token))
            raise
        finally:
            with self.lock:
                if self._preparing.pop(ticket.attempt):
                    self._check(token)
                    self._settle_workspace(ticket)
        if result is not None:
            with self.lock:
                self._check(token)
                self.journal.record_result(token, ticket, result, authorize_commit=lambda: self._check(token))

    def run_command(self, token, ticket, argv, cwd, timeout, stdin_text):
        with self.lock:
            self._check(token)
            self._refresh_observer()
            actual, row = self.journal.read_launch(token, ticket.project, ticket.job, ticket.revision)
            if actual != ticket or row['state'] != 'LAUNCH_INTENT' or row['binding'] is not None:
                raise Refused('LAUNCH_TICKET_SPENT')
            self.journal.verify_launch_authority(token, ticket, self.clock())
            self.native.validate_driver_call(ticket, argv, cwd, timeout, stdin_text)
            self.journal.claim_create(token, ticket)
            receiver = self.native.create_suspended(ticket, argv, cwd, stdin_text)
            self._receivers[ticket.attempt] = receiver
            resume_entered = False
            try:
                binding = receiver.binding
                if type(binding) is not self.binding_type or binding.ticket != ticket:
                    raise Refused('NATIVE_TARGET_MISMATCH')
                binding.validate()
                self._refresh_observer((binding,))
                self.journal.bind_native(token, ticket, binding)
                self.journal.mark_resume_intent(token, ticket)
                self.journal.verify_launch_authority(token, ticket, self.clock())
                if not self._observer_available:
                    raise Refused('OBSERVER_LOST_RESUME_WITHHELD')
                generation = self.generation
                def authorize_resume(action):
                    def dispatch():
                        nonlocal resume_entered
                        resume_entered = True
                        return action()
                    with self.lock:
                        self._check(token)
                        if not self._observer_available:
                            raise Refused('OBSERVER_LOST_RESUME_WITHHELD')
                        return self.journal.dispatch_resume(token, ticket, generation, self.clock, dispatch)
                receiver.resume(stdin_text, authorize=authorize_resume)
            except BaseException as exc:
                # Close only these exact owned handles; do not mint a cancel ACK
                # or release the durable launch reservation after an uncertain cut.
                receiver.close_fail_safe(time.monotonic())
                if isinstance(exc, LaunchExpired) and not resume_entered:
                    self._record_suspended_expiry(token, ticket, receiver.binding)
                    self._receivers.pop(ticket.attempt)
                raise
        try:
            return receiver.wait_result(argv, timeout)
        except (TimeoutError, subprocess.TimeoutExpired):
            failure = None
            try:
                self.cancel_internal(binding, 'TIMEOUT')
            except Exception as exc:  # health: allow BLE001 -- cancel failure must not skip bounded drain
                failure = exc
            result = receiver.wait_result(argv, 15.0, timed_out=True)
            if failure is not None:
                raise Refused('TIMEOUT_CANCEL_UNKNOWN') from failure
            return result

    def _job_accounting(self, binding):
        required = tuple(dict.fromkeys((*binding.known_processes, *self.journal.known_members(self.token, binding))))
        proof = self.observer.read(ObservationTarget(binding, required), self.token.epoch, self.generation)
        now = self.clock()
        effect = self.journal.effect(self.token, binding)
        earliest = effect['cancel_at'] if effect else 0.0
        envelope = (type(proof) is StopReadback and proof.binding == binding
                 and proof.epoch == self.token.epoch and proof.generation == self.generation
                 and finite(proof.observed_at) and earliest <= proof.observed_at <= now
                 and now-proof.observed_at <= 30 and proof.job_identity_verified is True
                 and proof.processes and all(type(p) is ProcessReadback and p.identity.valid() for p in proof.processes))
        if not isinstance(proof, StopReadback):
            raise Refused('JOB_READBACK_UNKNOWN')
        if envelope:
            self.journal.record_members(self.token, binding, tuple(p.identity for p in proof.processes))
        valid = (envelope and type(proof.job_active) is int and proof.job_active == 0 and proof.job_members == ()
                 and set(required).issubset({p.identity for p in proof.processes})
                 and len({p.identity for p in proof.processes}) == len(proof.processes)
                 and all(p.signaled is True for p in proof.processes))
        self.journal.save_readback(self.token, binding, self.generation, now, asdict(proof))
        if valid:
            return 'STOPPED'
        if (envelope and type(proof.job_active) is int and proof.job_active > 0
                and type(proof.job_members) is tuple and len(set(proof.job_members)) == proof.job_active
                and all((type(p) is ProcessIdentity and p.valid()) or (type(p) is int and p > 0)
                        for p in proof.job_members)
                and set(required).issubset({p.identity for p in proof.processes})):
            return 'RUNNING'
        return 'HELD_UNKNOWN'

    def _local_stop(self, binding):
        return self._job_accounting(binding) == 'STOPPED'

    def _record_suspended_expiry(self, token, ticket, binding):
        self.journal.suspended_expired(token, ticket, binding)
        if ticket.attempt in self._preparing:
            self._preparing[ticket.attempt] = True

    def finish_client_job(self, token, ticket):
        """Persist the local terminal before releasing the only owned Job handle or capturing."""
        with self.lock:
            self._check(token)
            if not callable(getattr(self.launcher, 'job_coverage_for', None)):
                raise Refused('CLIENT_JOB_TRANSPORT_REQUIRED')
            actual, row = self.journal.read_launch(token, ticket.project, ticket.job, ticket.revision)
            if actual != ticket:
                raise Refused('LAUNCH_TICKET_SPENT')
            receiver = self._receivers.get(ticket.attempt)
            binding = self.journal.decode_binding(json.loads(row['binding'])) if row['binding'] else None
            if row['state'] == 'LAUNCH_INTENT':
                raise Refused('CLIENT_NOT_STARTED')
            stopped = False
            try:
                if binding and receiver and receiver.binding == binding:
                    try:
                        self._refresh_observer()
                        stopped = self._drain_client_job(binding) and self._coverage_verified()
                    except Exception:  # health: allow BLE001 -- an unreadable Job must stay held after close
                        stopped = False
                self.journal.finish_job_stop(token, ticket, self.generation, stopped=stopped)
            finally:
                if receiver is not None:
                    try:
                        receiver.close_fail_safe(time.monotonic())
                    finally:
                        self._receivers.pop(ticket.attempt, None)
            if binding is None:
                raise Refused('NATIVE_BINDING_UNKNOWN')
            return stopped, self.journal.cancel_causes(token, binding)

    def _drain_client_job(self, binding):
        if self._local_stop(binding):
            return True
        # PM, timeout and ordinary root exit with surviving children share one durable send slot.
        self.cancel_internal(binding, 'EMERGENCY_CLEANUP', detail='CLIENT_JOB_DRAIN')
        deadline = time.monotonic() + 5.0
        while True:
            if self._local_stop(binding):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))

    def cancel_internal(self, binding: NativeBinding, cause, *, request_id='', detail='', dispatch_guard=None):
        """PM, TIMEOUT and cleanup use this sole coordinator, even after grant expiry."""
        with self.lock:
            self.guard.assert_owned()
            binding.validate()
            if cause not in ('PM_CANCEL', 'TIMEOUT', 'EMERGENCY_CLEANUP'):
                raise Refused('CANCEL_CAUSE')
            actual, row = self.journal.read_launch(self.token, binding.ticket.project,
                                                  binding.ticket.job, binding.ticket.revision)
            if actual != binding.ticket or row['binding'] != encode(asdict(binding)):
                raise Refused('CANCEL_TARGET_BINDING')
            if row['state'] == 'NOT_STARTED':
                return CancelOutcome('NOT_STARTED')
            if self._finished_client_job(row):
                return CancelOutcome('LOCAL_STOPPED_HOLD', local_stopped=True)
            self.journal.note_cancel_cause(self.token, binding, cause, request_id, detail, self.clock())
            try:
                self._refresh_observer()
            except Refused:
                return CancelOutcome('UNKNOWN')
            spent = self.journal.effect(self.token, binding)
            try:
                stopped = self._local_stop(binding)
            except Exception:  # health: allow BLE001 -- query-only readback failure cannot authorize dispatch
                self._observer_available = False
                self.journal.observer_state(self.token, healthy=False)
                return CancelOutcome('UNKNOWN')
            if spent is not None or stopped:
                covered = stopped and self._coverage_verified()
                return CancelOutcome('LOCAL_STOPPED_HOLD' if covered else 'UNKNOWN', local_stopped=covered)
            receiver = self._receivers.get(binding.ticket.attempt)
            if receiver is None or receiver.binding != binding or not self._observer_available:
                return CancelOutcome('UNKNOWN')  # recovery never creates a new receiver
            receiver.check_binding(binding)
            claimed = self.journal.cancel_intent(self.token, binding, cause, request_id, detail, self.clock())
            if not claimed or binding.effect_key in self._sent_cancel:
                return CancelOutcome('UNKNOWN')
            self._sent_cancel.add(binding.effect_key)  # spent before native entry, including exceptions
            if not self._observer_available:
                self.journal.finish_cancel(self.token, binding, 'UNKNOWN')
                return CancelOutcome('UNKNOWN')
            try:
                token, generation = self.token, self.generation
                def authorize(action):
                    self.guard.assert_owned()
                    if not self._observer_available:
                        raise Refused('OBSERVER_LOST_NATIVE_WITHHELD')
                    if dispatch_guard is not None:
                        action = partial(dispatch_guard, action)
                    return self.journal.dispatch_cancel(token, binding, generation, action)
                acknowledged = receiver.cancel_once(binding, authorize=authorize)
            except Exception:  # health: allow BLE001 -- an exception cannot restore the send slot
                self.journal.finish_cancel(self.token, binding, 'UNKNOWN')
                return CancelOutcome('UNKNOWN')
            if acknowledged is not True:
                self.journal.finish_cancel(self.token, binding, 'UNKNOWN')
                return CancelOutcome('UNKNOWN')
            self.journal.finish_cancel(self.token, binding, 'CANCEL_REQUESTED')
            return CancelOutcome('CANCEL_REQUESTED', dispatched=True)

    def control_target(self, request, context):
        with self.lock:
            self._context(context)
            return self.journal.authorize_control(self.token, request, context, self.clock())

    def status_row(self, ticket, row):
        return row

    def observe_status(self, project, job, revision):
        from .status_observation import observe
        with self.lock:
            self._check()
            ticket, row = self.journal.read_launch(self.token, project, job, revision)
            return observe(self, ticket, row)['view']

    def stop_by_owner(self, project, job, revision, *, request_id, detail='', dispatch_guard=None, expected_attempt=None):
        with self.lock:
            self._check()
            ticket, row = self.journal.read_launch(self.token, project, job, revision)
            if expected_attempt is not None and ticket.attempt != expected_attempt:
                raise Refused('CANCEL_TARGET_BINDING')
            if row['binding'] is not None:
                binding = self.journal.decode_binding(json.loads(row['binding']))
                return self.cancel_internal(binding, 'PM_CANCEL', request_id=request_id, detail=detail,
                                            dispatch_guard=dispatch_guard)
            if dispatch_guard is not None:
                return dispatch_guard(partial(self._stop_before_create, ticket, row))
            return self._stop_before_create(ticket, row)

    def _stop_before_create(self, ticket, row):
        preparing = ticket.attempt in self._preparing
        if self.journal.mark_not_started(self.token, ticket, reason='PM_CANCEL', preparing=preparing):
            if preparing and self.journal.workspace_clear is not None:
                self._preparing[ticket.attempt] = True
            return CancelOutcome('CANCEL_REQUESTED')
        if row['state'] == 'NOT_STARTED' and self.journal.cancelled_before_create(self.token, ticket):
            return CancelOutcome('ALREADY_REQUESTED')
        return CancelOutcome('NOT_STARTED' if row['state'] == 'NOT_STARTED' else 'UNKNOWN')

    def status(self, binding):
        with self.lock:
            self._check()
            ticket, row = self.journal.read_launch(self.token, binding.ticket.project, binding.ticket.job, binding.ticket.revision)
            if ticket != binding.ticket or row['binding'] != encode(asdict(binding)):
                raise Refused('STATUS_TARGET_BINDING')
            if self._finished_client_job(row):
                phase = 'LOCAL_STOPPED_HOLD'
            else:
                try:
                    self._refresh_observer()
                    phase = 'LOCAL_STOPPED_HOLD' if self._local_stop(binding) and self._coverage_verified() else 'UNKNOWN'
                except (OSError, Refused):
                    phase = 'UNKNOWN'
            from .types import digest
            evidence = ('journal-result:'+digest(json.loads(row['result'])),) if row['result'] else ()
            return phase, evidence

    def close(self, deadline):
        if self.supervisor is not None:
            self.supervisor.close(deadline)
            return
        try:
            self.close_resources(deadline)
        finally:
            self.guard.close(deadline)

    def close_resources(self, deadline):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            self._closing = True
            failures = []
            try:
                for receiver in tuple(self._receivers.values()):
                    try:
                        self.cancel_internal(receiver.binding, 'EMERGENCY_CLEANUP')
                    except (Exception, SystemExit) as exc:  # health: allow BLE001 -- storage/observer failure cannot skip fail-safe close
                        failures.append(type(exc).__name__)
                    finally:
                        try:
                            receiver.close_fail_safe(deadline)
                        except (Exception, SystemExit) as exc:  # health: allow BLE001 -- continue closing other owned receivers
                            failures.append(type(exc).__name__)
            finally:
                try:
                    self.journal.observer_state(self.token, healthy=False)
                except Exception as exc:  # health: allow BLE001 -- failed metadata write cannot skip owned handle cleanup
                    failures.append(type(exc).__name__)
                self.observer.close(deadline)
            if failures:
                raise Refused('OWNED_CLEANUP_INCOMPLETE')
