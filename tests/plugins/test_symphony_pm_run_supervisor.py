"""The serving port grants one durable reservation; local launch outlives the submitting turn."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import importlib
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace

import pytest

from agent.secret_scope import UnscopedSecretError, get_secret, reset_secret_scope, set_secret_scope
from hermes_cli.approval_transport import RegisteredApprovalTransport
from hermes_cli.plugin_request_binding import RequestIdentity, canonical_home
from hermes_cli.plugin_services import ServiceHost
from hermes_constants import get_hermes_home, get_routing_process_hermes_home
from plugins.symphony_pm import adapter as pm
from plugins.symphony_pm.status_cli import read_launches, summary
from plugins.symphony_pm.transport.bootstrap import TransportBootstrap
from plugins.symphony_pm.transport.codex_exec_probe import _ProbeWatch
from plugins.symphony_pm.transport.types import Refused
from tests.plugins.symphony_serving_support import Guard, Native, Observer, approve
from tests.plugins.test_symphony_pm_adapter import ctx, job_payload, submit_payload
from tests.plugins.test_symphony_pm_serving import loaded as discovered_host
from tests.waiting import wait_until


def service(home, launcher, *, successor=False, host_scope=None):
    now = [time.time()]
    close = []
    scope = host_scope or SimpleNamespace(profile_home=canonical_home(home), namespace='symphony_pm:ingress',
                                          active=True, on_close=close.append)
    guard = Guard(scope, 'second' if successor else 'first', successor)
    native = Native()
    approval = RegisteredApprovalTransport('fixture', lambda *_: pytest.fail('unexpected prompt'),
                                           'test', scope.profile_home)
    bootstrap = TransportBootstrap(lambda _: guard, lambda _: Observer(now), lambda _: native, launcher,
                                   approval, 'serving-test', 'controller', ('issuer',))
    port = bootstrap(scope).port
    return SimpleNamespace(scope=scope, owner=port.owner, port=port, guard=guard, native=native,
                           now=now, close=None if host_scope else close[0])


def cancel_request(owner, original, receipt, now):
    payload = job_payload('request_cancel', request_id='cancel-1', job_ref=receipt.job_ref,
                          expected_job_revision=receipt.job_revision, reason='fixture stop')
    request, _, _ = pm.parse_intent(payload)
    identity = RequestIdentity(owner.scope.profile_home, owner.scope.namespace, original.identity.session_id,
                               request.request_id, request.payload_digest)
    permit = replace(original, identity=identity, ref='cancel-permit', operation=request.operation,
                     request_digest=request.request_digest, job_ref=receipt.job_ref, job_revision=receipt.job_revision)
    owner.record_permit(owner.token, permit, 'cancel-approval', 'c'*64, now)
    context = ctx(payload, issuer_ref=permit.issuer_ref, policy_ref=permit.policy_ref,
                  grant_ref=permit.ref, approved_scope_revision=permit.scope_revision, now=now,
                  contract_digest=permit.contract_digest, scheduler_id=owner.scheduler_id,
                  scheduler_epoch=owner.token.epoch, expires_at=permit.expires_at)
    return request, context


def row(r, request, receipt):
    return r.owner.journal.read_launch(r.owner.token, request.project_ref, receipt.job_ref, receipt.job_revision)[1]


def test_submit_returns_during_launch_and_independent_cancel_reaches_exact_receiver(tmp_path):
    entered, finish = Event(), Event()
    def launch(ticket, request, runner):
        runner(['fixture', 'exec'], cwd=ticket.plan.workspace, timeout=1)
        entered.set()
        assert finish.wait(8)
        return {'completed': True}

    r = service(tmp_path, launch)
    try:
        request, context, permit = approve(r)
        receipt = r.port.submit_intent(request, context)
        assert receipt.outcome == 'SUBMITTED' and entered.wait(4)
        assert row(r, request, receipt)['result'] is None
        duplicate = r.port.submit_intent(request, context)
        assert (duplicate.outcome, duplicate.job_ref) == ('DUPLICATE', receipt.job_ref)
        control = cancel_request(r.owner, permit, receipt, r.now[0])
        assert r.port.request_cancel(*control).outcome == 'CANCEL_REQUESTED'
        receiver, = r.native.created
        assert receiver.cancels == receiver.resumes == 1
        finish.set()
        wait_until(lambda: row(r, request, receipt)['result'] is not None, timeout=4)
    finally:
        finish.set()
        r.close(time.monotonic()+4)


@pytest.mark.parametrize('created', [False, True])
def test_close_and_recover_never_repeat_or_accept_late_result(tmp_path, created):
    entered, finish, returned = Event(), Event(), Event()
    def launch(ticket, request, runner):
        if created:
            runner(['fixture', 'exec'], cwd=ticket.plan.workspace, timeout=1)
        entered.set()
        try:
            assert finish.wait(8)
            return {'stale': True}
        finally:
            returned.set()

    r = service(tmp_path, launch)
    try:
        request, context, _ = approve(r)
        receipt = r.port.submit_intent(request, context)
        assert receipt.outcome == 'SUBMITTED' and entered.wait(4)
        r.scope.active = False
        with pytest.raises(Refused, match='OWNED_CLEANUP_PENDING'):
            r.close(time.monotonic()+0.2)
    finally:
        finish.set()
        r.owner.supervisor.cleanup.join(3)
        assert not r.guard.owned
    assert returned.wait(2)
    if created:
        receiver, = r.native.created
        assert receiver.cancels == receiver.closes == 1
    replacement = service(tmp_path, lambda *_: pytest.fail('unexpected replay'), successor=True)
    try:
        saved = row(replacement, request, receipt)
        assert saved['result'] is None and not replacement.native.created
        if not created:
            assert saved['state'] == 'NOT_STARTED'
    finally:
        replacement.close(time.monotonic()+2)


def test_scope_close_between_commit_and_handoff_recovers_reserved_ticket(tmp_path, monkeypatch):
    committing, proceed = Event(), Event()
    r = service(tmp_path, lambda *_: pytest.fail('closed reservation was executed'))
    request, context, _ = approve(r)
    original = r.owner.supervisor.offer
    captured = []
    def pause_after_commit(token, ticket, submission):
        captured.append(ticket)
        committing.set()
        assert proceed.wait(6)
        original(token, ticket, submission)

    monkeypatch.setattr(r.owner.supervisor, 'offer', pause_after_commit)
    with ThreadPoolExecutor(max_workers=1) as caller:
        try:
            submitted = caller.submit(r.port.submit_intent, request, context)
            assert committing.wait(3)
            ticket, = captured
            assert r.owner.journal.read_launch(r.owner.token, ticket.project, ticket.job, ticket.revision)[1]['state'] == 'LAUNCH_INTENT'
            r.scope.active = False
            with pytest.raises(Refused, match='OWNED_CLEANUP_PENDING'):
                r.close(time.monotonic()+0.2)
            assert not submitted.done()
        finally:
            proceed.set()
            r.owner.supervisor.cleanup.join(3)
            assert not r.guard.owned
        assert submitted.result().outcome == 'UNKNOWN'
    successor = service(tmp_path, lambda *_: pytest.fail('reservation replay'), successor=True)
    try:
        assert successor.owner.journal.read_launch(successor.owner.token, ticket.project, ticket.job, ticket.revision)[1]['state'] == 'NOT_STARTED'
        assert not r.native.created and not successor.native.created
    finally:
        successor.close(time.monotonic()+2)


def test_two_service_workers_and_probe_watch_have_only_their_profile(tmp_path, monkeypatch):
    observations = {}
    done = Event()
    def launch(ticket, request, runner):
        path = Path(get_hermes_home())
        def read():
            try:
                get_secret('SYNTHETIC_TURN_SECRET')
            except UnscopedSecretError:
                return canonical_home(get_hermes_home()), True
            return canonical_home(get_hermes_home()), False
        observations[ticket.attempt] = {'worker': read(), 'workspace': ticket.plan.workspace,
                                        'journal': str(runner.owner.journal.path)}
        def observe(watch):
            observations[ticket.attempt]['watch'] = read()
        monkeypatch.setattr(_ProbeWatch, '_run', observe)
        watcher = _ProbeWatch(path/'fixture.json', lambda: None)
        watcher.thread.start()
        watcher.thread.join(3)
        done.set()

    services = []
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'first'))
    monkeypatch.setenv('SYNTHETIC_TURN_SECRET', 'fixture-process-marker')
    assert canonical_home(get_routing_process_hermes_home()) == canonical_home(tmp_path/'first')
    token = set_secret_scope({'SYNTHETIC_TURN_SECRET': 'one-turn-only'})
    try:
        for name in ('first', 'second'):
            r = service(tmp_path/name, launch)
            services.append(r)
            request, context, _ = approve(r)
            receipt = r.port.submit_intent(request, context)
            assert receipt.outcome == 'SUBMITTED' and done.wait(4)
            done.clear()
            observed = observations[row(r, request, receipt)['attempt']]
            assert observed['worker'] == observed['watch'] == (r.scope.profile_home, True)
            assert observed['workspace'] == r.scope.profile_home
            assert Path(observed['journal']).is_relative_to(Path(r.scope.profile_home))
    finally:
        for r in services:
            r.close(time.monotonic()+3)
        reset_secret_scope(token)


def test_precheck_refusal_is_visible_and_does_not_spend_create_slot(tmp_path):
    def refuse(*_):
        raise Refused('CODEX_PRECHECK_REFUSED')
    r = service(tmp_path, refuse)
    try:
        request, context, _ = approve(r)
        receipt = r.port.submit_intent(request, context)
        assert receipt.outcome == 'SUBMITTED'
        wait_until(lambda: row(r, request, receipt)['state'] == 'NOT_STARTED', timeout=4)
        assert row(r, request, receipt)['result'] is None
        saved, = read_launches(r.owner.journal.path)
        assert summary(saved)['not_started_reason'] == 'CODEX_PRECHECK_REFUSED'
        next_request, next_context, _ = approve(r, payload=submit_payload(request_id='new-approval'))
        assert r.port.submit_intent(next_request, next_context).outcome == 'SUBMITTED'
        assert not r.native.created
    finally:
        r.close(time.monotonic()+3)


def test_close_before_result_commit_rolls_back_launch_and_observations(tmp_path, monkeypatch):
    writing, proceed = Event(), Event()
    def launch(ticket, request, runner):
        runner(['fixture', 'exec'], cwd=ticket.plan.workspace, timeout=1)
        return {'exit_code': 0}

    r = service(tmp_path, launch)
    journal = r.owner.journal
    record = journal._record_observations
    def hold_transaction(db, ticket, result):
        record(db, ticket, result)
        writing.set()
        assert proceed.wait(6)

    monkeypatch.setattr(journal, '_record_observations', hold_transaction)
    request, context, _ = approve(r)
    try:
        receipt = r.port.submit_intent(request, context)
        assert receipt.outcome == 'SUBMITTED' and writing.wait(3)
        r.scope.active = False
        with pytest.raises(Refused, match='OWNED_CLEANUP_PENDING'):
            r.close(time.monotonic()+0.2)
    finally:
        proceed.set()
        r.owner.supervisor.cleanup.join(3)
    assert not r.guard.owned
    [saved] = read_launches(journal.path)
    assert saved['result'] is None and not saved['observations']


@pytest.mark.parametrize('overrun', [False, True])
def test_real_service_host_distinguishes_normal_shutdown_from_deadline_hold(discovered_host, overrun, monkeypatch):
    from hermes_cli import plugins
    home, host, module = discovered_host
    running, release = Event(), Event()
    started = []
    def launch(ticket, request, runner):
        runner(['fixture', 'exec'], cwd=ticket.plan.workspace, timeout=1)
        running.set()
        assert release.wait(8)
        return {'late': True}

    def factory(scope):
        r = service(home, launch, host_scope=scope, successor=bool(started))
        started.append(r)
        return module.ServingBinding(r.port, lambda _: None, ('issuer',), 'serving-test')

    bindings = {('symphony_pm', 'ingress'): module.ServingBootstrap(factory)}
    host.configure(home, bindings)
    host.start_profile(home)
    [r] = started
    request, context, _ = approve(r)
    try:
        receipt = r.port.submit_intent(request, context)
        assert receipt.outcome == 'SUBMITTED' and running.wait(3)
        receiver, = r.native.created
        if not overrun:
            cancel = receiver._cancel
            def acknowledge():
                outcome = cancel()
                release.set()
                return outcome
            monkeypatch.setattr(receiver, '_cancel', acknowledge)
        expected = ('Refused',) if overrun else ()
        assert host.stop(timeout=0.2 if overrun else 3) == expected
        assert not r.scope.active
    finally:
        release.set()
        r.owner.supervisor.cleanup.join(3)
    assert receiver.cancels == receiver.closes == 1 and not r.guard.owned
    assert host.stop() == expected
    [closed] = read_launches(r.owner.journal.path)
    assert closed['result'] is None

    # A replacement starts with fresh manager state, as on service process restart.
    plugins._reset_plugin_managers_for_tests()
    plugins.discover_plugins()
    consumer = plugins.get_plugin_manager()._plugins['symphony_pm'].module
    module = importlib.import_module(f'{consumer.__name__}.service')
    successor_host = ServiceHost()
    successor_host.configure(home, {('symphony_pm', 'ingress'): module.ServingBootstrap(factory)})
    try:
        successor_host.start_profile(home)
        _, recovered = started
        assert row(recovered, request, receipt)['state'] == 'STOPPED'
        assert not recovered.native.created
    finally:
        assert successor_host.stop() == ()
