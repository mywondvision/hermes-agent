"""Offline control and durable observation contracts through the serving adapter."""
from dataclasses import replace
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from hermes_cli.plugin_request_binding import RequestIdentity
from plugins.symphony_pm import adapter as pm
from plugins.symphony_pm.service import ServingBinding, ServingIngress
from plugins.symphony_pm.transport.owner_check_profile import CheckCommand, OwnerCheckProfile, check_plan_fingerprint
from plugins.symphony_pm.transport.port import ServingPort
from plugins.symphony_pm.transport.types import Refused
from tests.plugins.symphony_serving_support import approve, rig
from tests.plugins.test_symphony_pm_adapter import job_payload
from tests.plugins.test_symphony_pm_permit_source import _source


@pytest.fixture
def reserved(tmp_path):
    r = rig(tmp_path)
    request, context, permit = approve(r)
    ticket, _, _ = r.owner.journal.reserve_launch(r.owner.token, request, context, r.now[0])
    r.source = _source(r, lambda *_: pytest.fail('unexpected control approval'))
    r.ticket, r.permit, r.submit = ticket, permit, request
    yield r
    r.owner.close(0)


def control(r, operation='get_status', *, session='session-1', **overrides):
    payload = job_payload(operation, **dict({'job_ref': r.ticket.job, 'expected_job_revision': r.ticket.revision,
                                             'request_id': 'req-'+operation}, **overrides))
    request, _, _ = pm.parse_intent(payload)
    identity = RequestIdentity(r.scope.profile_home, r.scope.namespace, session, request.request_id, request.payload_digest)
    return payload, r.source.for_control(identity, request)


def call(r, payload, grant, port=None):
    adapter = pm.SymphonyPmAdapter(port or ServingPort(r.owner), trusted_issuers=('issuer',), scheduler_id='serving-test')
    return adapter.handle(payload, trusted_context=grant.value).to_dict()


def test_pending_after_expiry_and_cancel_before_create(reserved):
    r = reserved
    r.now[0] += 90
    payload, grant = control(r)
    status = call(r, payload, grant)
    assert status['outcome'] == 'STATUS' and status['view']['phase'] == 'PENDING'
    with sqlite3.connect(r.owner.journal.path) as db:
        saved, = db.execute("SELECT body FROM serving_observations WHERE kind='STATUS'").fetchone()
        assert json.loads(saved)['view'] == status['view']
    payload, grant = control(r, 'request_cancel', reason='stop')
    result = call(r, payload, grant)
    assert result['outcome'] == 'CANCEL_REQUESTED' and result['hold']
    payload, grant = control(r)
    assert call(r, payload, grant)['view']['phase'] == 'NOT_STARTED'
    assert not r.native.created


@pytest.mark.parametrize('field', ['profile_home', 'namespace', 'session_id', 'request_id', 'payload_digest',
                                   'job_ref', 'project_ref', 'expected_job_revision'])
def test_only_exact_origin_can_obtain_control(reserved, field, tmp_path):
    r = reserved
    for operation in ('get_status', 'request_cancel'):
        payload, _ = control(r, operation)
        request, _, _ = pm.parse_intent(payload)
        identity = replace(r.permit.identity, request_id=request.request_id, payload_digest=request.payload_digest)
        if field in ('job_ref', 'project_ref', 'expected_job_revision'):
            request = replace(request, **{field: 'other'})
            identity = replace(identity, payload_digest=request.payload_digest)
        else:
            identity = replace(identity, **{field: str(tmp_path/'foreign') if field == 'profile_home' else 'other'})
        assert r.source.for_control(identity, request) is None
    assert r.source.for_control(r.permit.identity, r.submit) is None


def test_running_provenance_and_unconfirmed_readback(reserved, monkeypatch):
    r = reserved
    r.owner.execute_reserved(r.owner.token, r.ticket, r.submit)
    payload, grant = control(r)
    assert call(r, payload, grant)['view']['phase'] == 'RUNNING'
    r.now[0] += 1
    r.observer.stopped = True
    later_payload, later_grant = control(r)
    assert call(r, later_payload, later_grant)['view']['phase'] == 'STOPPED'
    earlier = call(r, payload, grant)['view']
    assert earlier['phase'] == 'RUNNING' and earlier['observed_at'] == r.now[0]-1
    port = ServingPort(r.owner)
    genuine = port.get_status
    for change in ({'observed_at': grant.value.now-1}, {'evidence_refs': ('wrong',)}):
        monkeypatch.setattr(port, 'get_status', lambda req, ctx: replace(genuine(req, ctx), **change))
        assert call(r, payload, grant, port)['outcome'] == 'UNVERIFIED'
    r.observer.covered = False
    payload, grant = control(r)
    assert call(r, payload, grant)['view']['phase'] == 'HELD_UNKNOWN'


def test_create_already_spent_cannot_be_cancelled_as_unstarted(reserved):
    r = reserved
    r.owner.journal.claim_create(r.owner.token, r.ticket)
    payload, grant = control(r, 'request_cancel')
    result = call(r, payload, grant)
    assert result['outcome'] == 'UNKNOWN' and result['hold']
    payload, grant = control(r)
    assert call(r, payload, grant)['view']['phase'] == 'HELD_UNKNOWN'
    assert not r.native.created


@pytest.mark.parametrize('mode,accepted', [('all', True), ('deferred', True), ('missing', False),
                                            ('failed', False), ('new_capture', False), ('missing_result', None),
                                            ('different_command', False), ('different_profile', False),
                                            ('legacy_check', False), ('reordered', False), ('different_tool', False),
                                            ('different_deferred_command', False)])
def test_stopped_stage_requires_all_sandbox_checks_for_exact_capture(reserved, mode, accepted):
    r = reserved
    root = Path(r.scope.profile_home)
    profile = OwnerCheckProfile(root, root/'client', root/'python', root/'ruff', root/'git',
                                *('a'*64 for _ in range(5)))
    plan = (CheckCommand('lint', 'ruff', ()), CheckCommand('unit', 'python', ()))
    if mode in ('deferred', 'different_deferred_command'):
        plan += (CheckCommand('git-test', 'python', (), requires_git=True),)

    def launch(ticket, request, runner):
        runner(['fixture', 'exec'], cwd=ticket.plan.workspace, timeout=1)
        r.observer.stopped = True
        assert r.owner.finish_client_job(r.owner.token, ticket)[0]
        if mode == 'missing_result':
            return None
        commands = {'lint': 0, 'unit': int(mode == 'failed')}
        if mode == 'missing':
            del commands['unit']
        check = {'commands': commands, 'attempt': ticket.attempt, 'source_sha': ticket.plan.source_sha,
                 'patch_sha256': 'c'*64, 'apply': 0, 'deferred': [], 'ok': True, 'revisions': profile.revisions()}
        if mode != 'legacy_check':
            check['plan_sha256'] = check_plan_fingerprint(profile, plan)
        if mode in ('deferred', 'different_deferred_command'):
            check.update(ok=False, reason='CHECK_GIT_TESTS_DEFERRED', deferred=['git-test'])
        return {'exit_code': 0, 'stop': None, 'patch': {'sha256': 'c'*64, 'paths': ['a.py']}, 'check': check}
    launch.job_coverage_for = lambda guard: True
    launch.settings = SimpleNamespace(checks=lambda paths: plan, owner_check=profile)
    r.owner.launcher = launch
    r.owner.execute_reserved(r.owner.token, r.ticket, r.submit)
    if mode == 'different_command':
        launch.settings.checks = lambda paths: (plan[0], CheckCommand('unit', 'python', ('new-test.py',)))
    if mode == 'different_profile':
        launch.settings.owner_check = replace(profile, config_sha256='9'*64)
    if mode == 'reordered':
        launch.settings.checks = lambda paths: tuple(reversed(plan))
    if mode == 'different_tool':
        launch.settings.checks = lambda paths: (plan[0], replace(plan[1], tool='ruff'))
    if mode == 'different_deferred_command':
        launch.settings.checks = lambda paths: (*plan[:2], replace(plan[2], arguments=('changed.py',)))
    if mode == 'new_capture':
        r.owner.journal.append_observation(r.owner.token, r.ticket, {'kind': 'CAPTURE',
            'source_sha': r.ticket.plan.source_sha, 'patch': {'sha256': 'd'*64, 'paths': ['a.py']}})
    payload, grant = control(r)
    result = call(r, payload, grant)
    assert result['outcome'] == 'STATUS'
    expected = 'NOT_CLAIMED' if accepted is None else ('ACCEPTED' if accepted else 'NOT_ACCEPTED')
    assert result['view']['stage_acceptance'] == expected
    assert result['view']['phase'] == ('STOPPED' if accepted is None else 'COMPLETED')
    view = r.owner.observe_status(r.ticket.project, r.ticket.job, r.ticket.revision)
    assert view['stage_acceptance'] == expected
    payload, grant = control(r, 'request_cancel')
    cancelled = call(r, payload, grant)
    assert cancelled['hold'] and set(cancelled['view']['verified_claims']) == {'worker_stopped', 'descendants_stopped'}


def test_ingress_follows_compression_but_excludes_neighbor_and_branch(reserved, tmp_path):
    from hermes_state import SessionDB
    r = reserved
    r.now[0] = time.time()
    r.scope.on_close = lambda callback: None
    r.scope.binding = ServingBinding(ServingPort(r.owner), r.source, ('issuer',), 'serving-test')
    ingress = ServingIngress(SimpleNamespace(register_tool=lambda **kwargs: object()), r.scope)
    payload = job_payload(job_ref=r.ticket.job, expected_job_revision=r.ticket.revision)
    def read(session, turn):
        return json.loads(ingress.handle(payload, session_id=session, turn_id=turn))['outcome']

    with closing(SessionDB(tmp_path/'state.db')) as db:
        db.create_session('session-1', source='local')
        db.create_session('neighbor', source='local')
        assert read('session-1', 'a') == read('session-1', 'b') == 'STATUS'
        db.publish_compression_child(parent_session_id='session-1', child_session_id='compressed', source='local',
                                     messages=[{'role': 'user', 'content': 'fixture'}], require_compression_lease=False)
        db.create_session('branch', source='local', parent_session_id='session-1',
                          model_config={'_branched_from': 'session-1'})
        assert read('compressed', 'c') == 'STATUS'
        assert read('neighbor', 'd') == read('branch', 'e') == 'WAITING_FOR_AUTHORITY'
    assert json.loads(ingress.handle({**payload, 'session_id': 'session-1'}, session_id='neighbor'))['hold']


def test_cancel_before_preparation_prevents_worker_entry(reserved):
    r = reserved
    outcome = r.owner.stop_by_owner(r.ticket.project, r.ticket.job, r.ticket.revision, request_id='operator-1')
    assert outcome.state == 'CANCEL_REQUESTED' and outcome.hold
    assert r.owner.stop_by_owner(r.ticket.project, r.ticket.job, r.ticket.revision,
                                 request_id='operator-2').state == 'ALREADY_REQUESTED'
    r.owner.launcher = lambda *_: pytest.fail('cancelled preparation ran')
    with pytest.raises(Refused, match='LAUNCH_TICKET_SPENT'):
        r.owner.execute_reserved(r.owner.token, r.ticket, r.submit)


def test_expired_submit_allows_own_bound_cancel_once(reserved):
    r = reserved
    r.owner.execute_reserved(r.owner.token, r.ticket, r.submit)
    r.now[0] += 90
    payload, grant = control(r, 'request_cancel', reason='fixture')
    assert call(r, payload, grant)['outcome'] == 'CANCEL_REQUESTED'
    payload, grant = control(r, 'request_cancel', reason='fixture', request_id='second')
    assert call(r, payload, grant)['hold']
    assert r.native.created[0].cancels == 1


def test_cancel_while_preparing_fences_create_and_holds_workspace_until_unwind(reserved):
    r = reserved
    started, finish = Event(), Event()
    settled = []
    def launch(ticket, request, runner):
        started.set()
        assert finish.wait(5)
        runner(['fixture', 'exec'], cwd=ticket.plan.workspace, timeout=1)
    def settle(ticket):
        settled.append(ticket.attempt)
        return True
    r.owner.launcher = launch
    launch.settle_workspace = settle
    r.owner.journal.workspace_clear = lambda: True
    def current():
        return r.owner.journal.read_launch(r.owner.token, r.ticket.project, r.ticket.job, r.ticket.revision)[1]

    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(r.owner.execute_reserved, r.owner.token, r.ticket, r.submit)
        try:
            assert started.wait(5)
            payload, grant = control(r, 'request_cancel')
            assert call(r, payload, grant)['outcome'] == 'CANCEL_REQUESTED'
            assert current()['hold_reason'] == 'WORKSPACE_HELD' and not settled
        finally:
            finish.set()
        with pytest.raises(Refused, match='LAUNCH_TICKET_SPENT'):
            work.result(timeout=5)
    assert current()['hold_reason'] is None and settled == [r.ticket.attempt]
    assert not r.native.created
