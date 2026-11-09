"""In-process test providers only. No native identity, OS lock or process proof is implemented."""
from dataclasses import asdict
from types import SimpleNamespace

from hermes_cli.plugin_request_binding import RequestIdentity, canonical_home
from plugins.symphony_pm.transport.lifecycle import ObserverHandshake, ProcessReadback, StopReadback, LifecycleOwner
from plugins.symphony_pm.transport.types import (
    ExecutionPlan, NativeBinding, Permit, ProcessIdentity, Refused, ServingIdentity, digest,
)
from tests.tools.test_symphony_pm_adapter import ctx, submit_payload
from tools import symphony_pm_adapter as pm


class Guard:
    def __init__(self, scope, incarnation='incarnation-1', predecessor_dead=False):
        self.identity = ServingIdentity(scope.profile_home, scope.namespace, 'fixture-boot',
                                        ProcessIdentity(100, 'fixture-serving-birth'), incarnation)
        self.owned, self.predecessor_dead = True, predecessor_dead

    def assert_owned(self):
        if not self.owned:
            raise Refused('GUARD_LOST')

    def require_predecessor_dead(self, _previous):
        if not self.predecessor_dead:
            raise Refused('PREDECESSOR_LIVE_OR_UNKNOWN')

    def close(self, _deadline):
        self.owned = False


class Observer:
    def __init__(self, now):
        self.now, self.stopped, self.covered = now, False, True
        self.loss = lambda: None

    def handshake(self, owner, generation, targets):
        coverage = tuple(digest(asdict(t)) for t in targets) if self.covered else ('mismatched',)
        return ObserverHandshake(owner, generation, self.now[0], coverage)

    def read(self, target, epoch, generation):
        return StopReadback(target.binding, epoch, generation, self.now[0], True,
                            0 if self.stopped else 1, () if self.stopped else (target.binding.root,),
                            tuple(ProcessReadback(p, self.stopped) for p in target.known_processes))

    def set_loss_handler(self, callback):
        self.loss = callback

    def close(self, _deadline):
        return None


class Receiver:
    def __init__(self, ticket):
        root = ProcessIdentity(200, 'fixture-worker-birth')
        self.binding = NativeBinding(ticket, 'fixture-job-'+ticket.attempt, root, (root,))
        self.resumes, self.cancels, self.closes, self.waits = 0, 0, 0, 0
        self.timeout, self.fail_cancel = False, False

    def resume(self, _stdin):
        self.resumes += 1

    def wait_result(self, _argv, _timeout, **kwargs):
        self.waits += 1
        if self.timeout and self.waits == 1:
            raise TimeoutError('fixture timeout')
        return SimpleNamespace(returncode=0, timed_out=kwargs.get('timed_out', False))

    def check_binding(self, binding):
        if binding != self.binding:
            raise Refused('RECEIVER_IDENTITY')

    def cancel_once(self, binding):
        self.check_binding(binding)
        self.cancels += 1
        if self.fail_cancel:
            raise RuntimeError('fixture lost acknowledgement')
        return True

    def close_fail_safe(self, _deadline):
        self.closes += 1


class Native:
    def __init__(self):
        self.created = []
        self.timeout = False

    def validate_driver_call(self, ticket, argv, cwd, timeout, _stdin):
        if argv != ['fixture', 'exec'] or cwd != ticket.plan.workspace or timeout != 1:
            raise Refused('DRIVER_CALL_BINDING')

    def create_suspended(self, ticket, _argv, _cwd):
        receiver = Receiver(ticket)
        receiver.timeout = self.timeout
        self.created.append(receiver)
        return receiver


def rig(tmp_path, *, incarnation='incarnation-1', predecessor_dead=False):
    now = [1000.0]
    scope = SimpleNamespace(profile_home=canonical_home(tmp_path), namespace='symphony_pm:ingress', active=True)
    guard, observer, native = Guard(scope, incarnation, predecessor_dead), Observer(now), Native()

    def launch(ticket, _request, runner):
        result = runner(['fixture', 'exec'], cwd=ticket.plan.workspace, timeout=1, stdin_text='fixture')
        return {'returncode': result.returncode, 'timed_out': result.timed_out}

    owner = LifecycleOwner(scope, guard, observer, native, launch,
                           scheduler_id='serving-test', controller='controller-1', clock=lambda: now[0])
    return SimpleNamespace(owner=owner, scope=scope, guard=guard, observer=observer, native=native, now=now)


def approve(r, *, payload=None):
    payload = payload or submit_payload()
    request, _, _ = pm.parse_intent(payload)
    identity = RequestIdentity(r.scope.profile_home, r.scope.namespace, 'session-1',
                               request.request_id, request.payload_digest)
    plan = ExecutionPlan('fixture-model', 'high', 'fixture-environment', r.scope.profile_home, 'a'*40)
    permit = Permit('permit-'+request.request_id, identity, request.project_ref, request.request_digest,
                    'b'*64, 'policy', 'scope', 'issuer', r.now[0]+60, plan)
    r.owner.record_permit(r.owner.token, permit, 'fixture-approval', 'c'*64, r.now[0])
    context = ctx(payload, issuer_ref=permit.issuer_ref, policy_ref=permit.policy_ref,
                  grant_ref=permit.ref, approved_scope_revision=permit.scope_revision,
                  contract_digest=permit.contract_digest, scheduler_id='serving-test',
                  scheduler_epoch=r.owner.token.epoch, now=r.now[0], expires_at=permit.expires_at)
    return request, context, permit
