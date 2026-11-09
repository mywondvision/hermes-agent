"""Serving owner: fencing, journal recovery and one cancel coordinator.

The host must inject a qualified OS ownership guard, query-only observer, native
receiver provider and the existing CodexDriver launcher. There is deliberately no
default backend, process probe, model choice, worker thread, queue or auto-resume.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import subprocess
import threading
import time
from typing import Protocol

from .journal import Journal, encode
from .types import (
    NativeBinding, ProcessIdentity, Refused, ServingIdentity,
    binding_from_json, finite,
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
    job_members: tuple[ProcessIdentity, ...]
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
    def create_suspended(self, ticket, argv, cwd): ...


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
    def __init__(self, scope, guard: OwnershipGuard, observer: QueryObserver,
                 native: NativeProvider, launcher, *, scheduler_id, controller, clock=time.time):
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
        self._closing, self._closed, self._observer_available = False, False, False
        self._receivers, self._sent_cancel = {}, set()
        self.journal = Journal(scope.profile_home, scope.namespace)
        previous = self.journal.previous_owner()
        if previous is not None:
            if previous['boot_id'] != identity.boot_id:
                raise Refused('HOST_BOOT_CHANGED_HOLD')
            guard.require_predecessor_dead(previous)  # lease expiry is not an implementation
        self.token = self.journal.claim(identity, controller, previous)
        self.generation = self.journal.observer_state(self.token, healthy=False)
        observer.set_loss_handler(self.observer_lost)
        self._refresh_observer()

    def _bindings(self):
        return tuple(binding_from_json(json.loads(row['binding']))
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

    def submit(self, request, context):
        with self.lock:
            self._context(context)
            self._refresh_observer()
            ticket, _, fresh = self.journal.reserve_launch(self.token, request, context, self.clock())
            token = self.token
        if fresh:
            # The injected launcher uses the existing CodexDriver/PeerPack/result
            # collector. Every native call must pass this attempt-bound runner.
            result = self.launcher(ticket, request, AttemptRunner(self, token, ticket))
            if result is not None:
                with self.lock:
                    self._check(token)
                    self.journal.record_result(token, ticket, result)
        return ticket, fresh

    def run_command(self, token, ticket, argv, cwd, timeout, stdin_text):
        with self.lock:
            self._check(token)
            self._refresh_observer()
            actual, row = self.journal.read_launch(token, ticket.project, ticket.job, ticket.revision)
            if actual != ticket or row['state'] != 'LAUNCH_INTENT' or row['binding'] is not None:
                raise Refused('LAUNCH_TICKET_SPENT')
            self.journal.verify_launch_authority(token, ticket, self.clock())
            self.native.validate_driver_call(ticket, argv, cwd, timeout, stdin_text)
            receiver = self.native.create_suspended(ticket, argv, cwd)
            self._receivers[ticket.attempt] = receiver
            try:
                binding = receiver.binding
                if type(binding) is not NativeBinding or binding.ticket != ticket:
                    raise Refused('NATIVE_TARGET_MISMATCH')
                binding.validate()
                self._refresh_observer((binding,))
                self.journal.bind_native(token, ticket, binding)
                self.journal.mark_resume_intent(token, ticket)
                self.journal.verify_launch_authority(token, ticket, self.clock())
                if not self._observer_available:
                    raise Refused('OBSERVER_LOST_RESUME_WITHHELD')
                receiver.resume(stdin_text)
            except BaseException:
                # Close only these exact owned handles; do not mint a cancel ACK
                # or release the durable launch reservation after an uncertain cut.
                receiver.close_fail_safe(time.monotonic())
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

    def _local_stop(self, binding):
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
        if envelope:
            self.journal.record_members(self.token, binding, tuple(p.identity for p in proof.processes))
        valid = (envelope and type(proof.job_active) is int and proof.job_active == 0 and proof.job_members == ()
                 and set(required).issubset({p.identity for p in proof.processes})
                 and len({p.identity for p in proof.processes}) == len(proof.processes)
                 and all(p.signaled is True for p in proof.processes))
        self.journal.save_readback(self.token, binding, self.generation, now, asdict(proof))
        return bool(valid)

    def cancel_internal(self, binding: NativeBinding, cause, *, request_id='', detail=''):
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
                return CancelOutcome('LOCAL_STOPPED_HOLD' if stopped else 'UNKNOWN', local_stopped=stopped)
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
                acknowledged = receiver.cancel_once(binding)
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

    def status(self, binding):
        with self.lock:
            self._check()
            try:
                self._refresh_observer()
                phase = 'LOCAL_STOPPED_HOLD' if self._local_stop(binding) else 'UNKNOWN'
            except Refused:
                phase = 'UNKNOWN'
            _, row = self.journal.read_launch(self.token, binding.ticket.project, binding.ticket.job, binding.ticket.revision)
            from .types import digest
            evidence = ('journal-result:'+digest(json.loads(row['result'])),) if row['result'] else ()
            return phase, evidence

    def close(self, deadline):
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
                    try:
                        self.journal.observer_state(self.token, healthy=False)
                    except Exception as exc:  # health: allow BLE001 -- failed metadata write cannot skip owned handle cleanup
                        failures.append(type(exc).__name__)
                    self.observer.close(deadline)
                finally:
                    self.guard.close(deadline)
            if failures:
                raise Refused('OWNED_CLEANUP_INCOMPLETE')
