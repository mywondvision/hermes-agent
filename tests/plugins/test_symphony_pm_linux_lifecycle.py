"""The Linux lifecycle uses an actual v2 journal and injected native readbacks."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hermes_cli.plugin_request_binding import canonical_home
from plugins.symphony_pm import status_cli
from plugins.symphony_pm.transport.lifecycle import ProcessReadback
from plugins.symphony_pm.transport.linux_identity import CONTRACT, LinuxBinding, LinuxStopReadback, unit_for
from plugins.symphony_pm.transport.linux_lifecycle import LinuxLifecycleOwner
from plugins.symphony_pm.transport.types import ProcessIdentity, Refused
from tests.plugins import symphony_serving_support as f

pytest_plugins = ('tests.plugins.test_symphony_pm_serving',)


class Guard(f.Guard):
    recovery_host_id = 'b' * 64

    def __init__(self, scope, incarnation, predecessor_dead):
        super().__init__(scope, incarnation, predecessor_dead)
        self.identity = replace(self.identity, boot_id='a' * 32)

    def predecessor_proof(self, previous):
        return replace(super().predecessor_proof(previous), contract=CONTRACT)


class Observer(f.Observer):
    def __init__(self, now):
        super().__init__(now)
        self.modify = lambda proof: proof

    def read(self, binding, epoch, generation):
        return self.modify(LinuxStopReadback(binding, epoch, generation, self.now[0], self.stopped,
                           int(not self.stopped), tuple(ProcessReadback(p, self.stopped)
                                                        for p in binding.known_processes)))


class Native(f.Native):
    ownership_contract = CONTRACT

    def create_suspended(self, ticket, *args):
        receiver = super().create_suspended(ticket, *args)
        root = ProcessIdentity(200, '327')
        unit = unit_for(ticket)
        receiver.binding = LinuxBinding(ticket, Guard.recovery_host_id, 'a' * 32, unit, 'c' * 32,
                                        '/system.slice/'+unit+'/payload', 12, 34, root, (root,))
        return receiver

    def recover_receiver(self, binding):
        receiver = f.Receiver(binding.ticket)
        receiver.binding = binding
        self.created.append(receiver)
        return receiver


class Launcher:
    def __init__(self):
        self.clear = True
        self.can_settle = True

    def require_project(self, project):
        if project != 'proj-alpha':
            raise Refused('LINUX_PROJECT_BINDING')

    def admission(self):
        pass

    def job_coverage_for(self, guard):
        return True

    def workspace_clear(self):
        return self.clear

    def never_created_workspace_is_clear(self, ticket):
        return self.clear

    def settle_workspace(self, ticket):
        self.clear = self.can_settle
        return self.clear

    def __call__(self, ticket, request, runner):
        self.clear = False
        runner(['fixture', 'exec'], cwd=ticket.plan.workspace, timeout=1, stdin_text='fixture')

    def recapture(self, journal, token, ticket, row):
        observation = {'kind': 'CAPTURE', 'source_sha': ticket.plan.source_sha, 'patch': {'sha256': 'e'*64}}
        return journal.append_observation(token, ticket, observation)


def rig(path, *, predecessor=False, stopped=False, could_settle=True, observer_ok=True, launcher=None):
    now = [1000.0]
    scope = SimpleNamespace(profile_home=canonical_home(path), namespace='symphony-linux', active=True)
    guard = Guard(scope, 'owner-two' if predecessor else 'owner-one', predecessor)
    observer, native, launcher = Observer(now), Native(), launcher or Launcher()
    observer.stopped, observer.covered, launcher.can_settle = stopped, observer_ok, could_settle
    owner = LinuxLifecycleOwner(scope, guard, observer, native, launcher, scheduler_id='serving-test',
                                controller='controller', clock=lambda: now[0])
    return SimpleNamespace(scope=scope, owner=owner, guard=guard, native=native, observer=observer, now=now)


def submit(r, request_id):
    request, context, _ = f.approve(r, payload={**f.submit_payload(), 'request_id': request_id})
    return r.owner.submit(request, context)[0]


def state(r, ticket):
    return r.owner.journal.read_launch(r.owner.token, ticket.project, ticket.job, ticket.revision)[1]


def test_cgroup_terminal_releases_only_after_archive_and_binds_check_to_capture(tmp_path):
    r = rig(tmp_path, could_settle=False)
    t = submit(r, 'first')
    r.observer.stopped = True
    assert r.owner.finish_client_job(r.owner.token, t) == (True, ())
    assert state(r, t)['stop_proof'] == 'CGROUP_EMPTY'
    with pytest.raises(Refused, match='WORKSPACE_HELD'):
        submit(r, 'second')
    with pytest.raises(Refused, match='RECAPTURE_REQUIRES_PROVEN_STOP'):
        r.owner.recapture(r.owner.token, t.project, t.job, t.revision)
    r.owner.launcher.clear = True
    r.owner.launcher.can_settle = True
    patch = {'sha256': 'e'*64}
    r.owner.journal.record_result(r.owner.token, t, {'workspace': {'renamed': True},
        'patch': patch, 'check': {'attempt': t.attempt, 'source_sha': t.plan.source_sha, 'patch_sha256': patch['sha256'],
                                  'ok': False, 'reason': 'CHECK_GIT_TESTS_DEFERRED'}})
    assert r.owner.recapture(r.owner.token, t.project, t.job, t.revision) == 3
    capture, check, recapture = status_cli.read_launches(r.owner.journal.path)[0]['observations']
    assert check['body']['capture_ref'] == {'attempt': t.attempt, 'seq': capture['seq'], 'sha256': patch['sha256']}
    assert recapture['seq'] != capture['seq']
    assert submit(r, 'second').job != t.job


@pytest.mark.parametrize('change', [{'epoch': 88}, {'generation': 9}, {'observed_at': 969.9},
                                    {'source': 'systemd-stop-ack'}, {'processes': ()}])
def test_wrong_readback_withholds_cancel_dispatch_and_terminal(tmp_path, change):
    r = rig(tmp_path)
    t = submit(r, 'first')
    r.observer.modify = lambda proof: replace(proof, **change)
    receiver = r.native.created[0]
    with pytest.raises(Refused, match='CAPTURE_REQUIRES_SETTLED_CGROUP'):
        r.owner.journal.record_result(r.owner.token, t, {'patch': {'sha256': 'c' * 64}})
    with pytest.raises(Refused, match='CAPTURE_REQUIRES_SETTLED_CGROUP'):
        r.owner.journal.append_observation(r.owner.token, t, {'kind': 'CAPTURE', 'patch': {}})
    assert state(r, t)['result'] is None
    assert r.owner.cancel_internal(receiver.binding, 'PM_CANCEL').state == 'UNKNOWN'
    assert not receiver.cancels
    assert not r.owner.finish_client_job(r.owner.token, t)[0]
    assert state(r, t)['state'] == 'HELD_UNKNOWN'
    with pytest.raises(Refused, match='UNSETTLED'):
        submit(r, 'second')


def test_takeover_spent_cancel_retains_slot_until_exact_new_readback(tmp_path):
    first = rig(tmp_path)
    t = submit(first, 'one')
    original = first.native.created[0]
    assert first.owner.cancel_internal(original.binding, 'PM_CANCEL').dispatched
    second = rig(tmp_path, predecessor=True)
    recovered = second.native.created[0]
    assert recovered.binding == original.binding and recovered.resumes == 0
    assert state(second, t)['state'] == 'HELD_UNKNOWN'
    assert not second.owner.cancel_internal(recovered.binding, 'TIMEOUT').dispatched
    assert original.cancels + recovered.cancels == 1
    with pytest.raises(Refused, match='WRITER_FENCED'):
        first.owner.journal.unfinished(first.owner.token)
    with pytest.raises(Refused, match='REQUIRES_STOP_PROOF'):
        second.owner.release_by_owner(second.owner.token, t.project, t.job, t.revision, 'operator')
    second.observer.stopped = True
    assert second.owner.status(recovered.binding)[0] == 'LOCAL_STOPPED_HOLD'
    assert state(second, t)['stop_proof'] == 'CGROUP_EMPTY'
    assert submit(second, 'two').attempt != t.attempt


@pytest.mark.parametrize('spent', [False, True])
def test_unbound_cut_before_or_after_spending_create(tmp_path, spent):
    first = rig(tmp_path)
    req, ctx, _ = f.approve(first)
    t, _, _ = first.owner.journal.reserve_launch(first.owner.token, req, ctx, first.now[0])
    if spent:
        first.owner.journal.claim_create(first.owner.token, t)
    second = rig(tmp_path, predecessor=True)
    assert state(second, t)['state'] == ('HELD_UNKNOWN' if spent else 'NOT_STARTED')
    assert not second.native.created
    if spent:
        with pytest.raises(Refused, match='UNSETTLED'):
            submit(second, 'two')
    else:
        assert submit(second, 'two').attempt != t.attempt

def test_another_submit_cannot_settle_before_primary_capture_commits(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    capturing, proceed = Event(), Event()

    class CapturingLauncher(Launcher):
        def __call__(self, ticket, request, runner):
            super().__call__(ticket, request, runner)
            assert runner.owner.finish_client_job(runner.token, ticket)[0]
            capturing.set()
            if not proceed.wait(10):
                raise TimeoutError('fixture capture was not released')
            self.clear = True
            return {'workspace': {'renamed': True}, 'patch': {'sha256': 'd'*64}}

    r = rig(tmp_path, stopped=True, launcher=CapturingLauncher())
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(submit, r, 'first')
        try:
            assert capturing.wait(10)
            with pytest.raises(Refused, match='WORKSPACE_HELD'):
                submit(r, 'second')
            assert len(r.native.created) == 1
        finally:
            proceed.set()
        t = first.result(timeout=10)
    assert state(r, t)['hold_reason'] is None
    assert status_cli.read_launches(r.owner.journal.path)[0]['observations'][0]['kind'] == 'CAPTURE'

def test_linux_profile_service_starts_before_scope_activation_and_closes(loaded, monkeypatch):
    from plugins.symphony_pm.transport import linux_bootstrap
    from plugins.symphony_pm.transport.port import ServingPort
    from tests.plugins.test_symphony_pm_serving import _call
    home, host, service = loaded
    owner = []
    def transport(scope):
        assert not scope.active
        guard = Guard(scope, 'test', False)
        candidate = LinuxLifecycleOwner(scope, guard, Observer([1000.0]), Native(), Launcher(),
                                        scheduler_id='serving-test', controller='controller', clock=lambda: 1000.0)
        owner.append(candidate)
        scope.on_close(candidate.close)
        from plugins.symphony_pm.service import ServingBinding
        return ServingBinding(ServingPort(candidate), lambda identity: None, ('issuer',), 'serving-test')
    monkeypatch.setattr(linux_bootstrap, 'linux_exec_transport', lambda **options: transport)
    host.configure(home, {('symphony_pm', 'ingress-linux'): service.linux_serving_bootstrap()})
    assert _call(f.submit_payload())['outcome'] == 'UNAVAILABLE'
    host.start_profile(home)
    assert _call(f.submit_payload())['outcome'] == 'WAITING_FOR_AUTHORITY'
    assert owner[0].scope.namespace == 'symphony_pm:ingress-linux'
    host.stop()
    assert not owner[0].guard.owned
    assert _call(f.submit_payload())['outcome'] == 'UNAVAILABLE'

def test_pending_checker_holds_next_reservation_without_erasing_primary_result(tmp_path):
    class CheckingLauncher(Launcher):
        checking = False

        def require_check_slot_clear(self):
            if self.checking:
                raise Refused('CHECK_SLOT_HELD')

    launcher = CheckingLauncher()
    r = rig(tmp_path, stopped=True, launcher=launcher)
    first = submit(r, 'first')
    r.owner.finish_client_job(r.owner.token, first)
    launcher.clear, launcher.checking = True, True
    result = {'workspace': {'renamed': True}, 'patch': {'sha256': 'e'*64},
              'check': {'attempt': first.attempt, 'source_sha': first.plan.source_sha,
                        'patch_sha256': 'e'*64, 'ok': False, 'reason': 'CHECK_TIMEOUT'}}
    journal, token = r.owner.journal, r.owner.token
    journal.record_result(token, first, result)
    req, context, _ = f.approve(r, payload=f.submit_payload(request_id='next'))
    with pytest.raises(Refused, match='CHECK_SLOT_HELD'):
        journal.reserve_launch(token, req, context, r.now[0])
    primary = status_cli.read_launches(journal.path)[0]
    assert primary['result'] == result and primary['hold_reason'] is None
    assert primary['observations'][1]['body']['capture_ref']['seq'] == primary['observations'][0]['seq']
    launcher.checking = False
    next_ticket, _, fresh = journal.reserve_launch(token, req, context, r.now[0])
    assert fresh and next_ticket.attempt != first.attempt


def test_linux_status_refuses_windows_terminal_when_reopening_v2(tmp_path):
    import sqlite3
    from plugins.symphony_pm.transport.status_observation import phase
    r = rig(tmp_path, stopped=True)
    ticket = submit(r, 'one')
    assert r.owner.finish_client_job(r.owner.token, ticket)[0]
    row = state(r, ticket)
    assert phase(r.owner, row) == 'STOPPED'
    with sqlite3.connect(r.owner.journal.path) as db:
        db.execute("UPDATE serving_launches SET stop_proof='JOB_EMPTY', proof_source='foreign' WHERE attempt=?",
                   (ticket.attempt,))
    assert phase(r.owner, state(r, ticket)) == 'HELD_UNKNOWN'
    assert r.owner.journal.unfinished(r.owner.token)


@pytest.mark.parametrize('change', ['member', 'missing_readback', 'readback_body', 'epoch', 'generation',
                                    'time', 'cancel_order'])
def test_reopen_requires_same_complete_terminal_for_status_and_admission(tmp_path, change):
    import sqlite3
    from plugins.symphony_pm.transport.linux_journal import LinuxJournal
    from plugins.symphony_pm.transport.status_observation import phase
    r = rig(tmp_path)
    ticket = submit(r, 'one')
    binding = r.native.created[0].binding
    r.owner.cancel_internal(binding, 'PM_CANCEL')
    r.observer.stopped = True
    assert r.owner.finish_client_job(r.owner.token, ticket)[0]
    row = state(r, ticket)
    r.owner.launcher.clear = True
    journal = LinuxJournal(r.scope.profile_home, r.scope.namespace, ownership_contract=CONTRACT,
                           workspace_clear=lambda: True)
    journal.workspace_settled(r.owner.token, ticket)
    assert journal.proven_stop(row)
    with sqlite3.connect(journal.path) as db:
        statements = {
            'member': ("INSERT INTO serving_members VALUES(?,356,'123')", ticket.attempt),
            'missing_readback': ('DELETE FROM serving_readbacks WHERE effect_key=?', binding.effect_key),
            'readback_body': ("UPDATE serving_readbacks SET body='{}' WHERE effect_key=?", binding.effect_key),
            'epoch': ('UPDATE serving_readbacks SET writer_epoch=writer_epoch+1 WHERE effect_key=?', binding.effect_key),
            'generation': ('UPDATE serving_readbacks SET observer_generation=observer_generation+1 WHERE effect_key=?',
                           binding.effect_key),
            'time': ('UPDATE serving_readbacks SET observed_at=observed_at+31 WHERE effect_key=?', binding.effect_key),
            'cancel_order': ('UPDATE effects SET cancel_at=cancel_at+1 WHERE effect_key=?', binding.effect_key),
        }
        sql, key = statements[change]
        db.execute(sql, (key,))
    assert not journal.proven_stop(row) and not journal.containment_empty(row)
    assert phase(r.owner, row) == 'HELD_UNKNOWN' and journal.unfinished(r.owner.token)
    req, ctx, _ = f.approve(r, payload=f.submit_payload(request_id='second'))
    with pytest.raises(Refused, match='UNSETTLED_PRIOR_LAUNCH_HOLD'):
        journal.reserve_launch(r.owner.token, req, ctx, r.now[0])


def test_bootstrap_refuses_foreign_service_before_acquiring_resources(tmp_path):
    from plugins.symphony_pm.transport.linux_bootstrap import LinuxTransportBootstrap, linux_exec_transport
    from plugins.symphony_pm.transport.bootstrap import TransportBootstrap
    from plugins.symphony_pm.retention import prune
    wrong = SimpleNamespace(namespace='symphony_pm:ingress', profile_home=str(tmp_path))
    q = SimpleNamespace(profile_home=wrong.profile_home, namespace=wrong.namespace)
    bootstrap = LinuxTransportBootstrap(SimpleNamespace(qualification=q, settings=None), None, 'local')
    with pytest.raises(Refused, match='LINUX_PROFILE_BINDING'):
        bootstrap(wrong)
    with pytest.raises(Refused, match='LINUX_PROFILE_BINDING'):
        linux_exec_transport(profile_home=wrong.profile_home, namespace=wrong.namespace, qualification=q, settings=None,
                             approval_transport=None, scheduler_id='s', controller='c', trusted_issuers=())
    windows = TransportBootstrap(None, None, None, None, None, 's', 'c', ('issuer',))
    with pytest.raises(Refused, match='WINDOWS_PROFILE_BINDING'):
        windows(SimpleNamespace(namespace='symphony_pm:ingress-linux'))
    r = rig(tmp_path)
    r.owner.scope.namespace = wrong.namespace
    with pytest.raises(Refused, match='LINUX_PRUNE_UNAVAILABLE'):
        prune(r.owner, {})
