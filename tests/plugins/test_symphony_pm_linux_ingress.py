"""A local fixture serves the installed plugin through its public tool and operator command."""
from dataclasses import replace
import json
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace

import pytest

from hermes_cli.approval_transport import RegisteredApprovalTransport
from hermes_cli.plugins_loader import _plugin_home_scope
from hermes_constants import get_hermes_home
from plugins.symphony_pm.status_cli import read_launches, summary
from plugins.symphony_pm.transport.bootstrap import serve_owner
from plugins.symphony_pm.transport.linux_lifecycle import LinuxLifecycleOwner
from tests.gateway.test_plugin_command_choice import _runner, _source, _TextAdapter
from tests.plugins.symphony_serving_support import approve, submit_payload
from tests.plugins.test_symphony_pm_adapter import job_payload
from tests.plugins.test_symphony_pm_linux_lifecycle import Guard, Native, Observer
from tests.plugins.test_symphony_pm_linux_owner_check import checker_stand
from tests.plugins.test_symphony_pm_operator_gateway import send
from tests.plugins.test_symphony_pm_serving import _call, loaded as discovered_host


@pytest.mark.asyncio
async def test_linux_public_submit_status_operator_and_exact_capture(discovered_host, checker_stand):
    home, host, module = discovered_host
    now = [time.time()]
    observer = Observer(now)
    stand = checker_stand('good', home, 'symphony_pm:ingress-linux')
    launcher = stand.launcher
    running, release = Event(), Event()

    class WaitingNative(Native):
        def create_suspended(self, ticket, *args):
            receiver = super().create_suspended(ticket, *args)
            wait = receiver.wait_result

            def wait_result(*args, **kwargs):
                assert Path(get_hermes_home()).resolve() == home.resolve()
                running.set()
                assert release.wait(10)
                observer.stopped = True
                return wait(*args, **kwargs)
            receiver.wait_result = wait_result
            return receiver
    native = WaitingNative()
    owners = []
    finished, failures = Event(), []

    class ObservedOwner(LinuxLifecycleOwner):
        def execute_reserved(self, *args):
            try:
                return super().execute_reserved(*args)
            except Exception as exc:
                failures.append(exc)
                raise
            finally:
                finished.set()

    def factory(scope):
        owner = ObservedOwner(scope, Guard(scope, 'fixture', False), observer, native, launcher,
                                   scheduler_id='serving-test', controller='fixture')
        owners.append(owner)
        scope.on_close(owner.close)
        channel = RegisteredApprovalTransport('fixture', lambda *_: pytest.fail('unexpected prompt'),
                                             'test', scope.profile_home)
        return serve_owner(owner, channel, ('issuer',))

    host.configure(home, {('symphony_pm', 'ingress-linux'): module.ServingBootstrap(factory)})
    host.start_profile(home)
    owner, = owners
    payload = submit_payload()
    approve(SimpleNamespace(owner=owner, scope=owner.scope, now=now), payload=payload)
    sender = _source()
    gateway = _runner(_TextAdapter())
    gateway.session_store.lookup_by_session_key.return_value = None
    config = dict(user_id=sender.user_id, chat_id=sender.chat_id, platform=sender.platform.value,
                  session_key=gateway._session_key_for_source(sender))
    (home/'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['symphony_pm'], 'entries': {
        'symphony_pm': {'settings': {'operator': config}}}}}), encoding='utf-8')
    with _plugin_home_scope(home):
        assert _call(payload, session_id='other')['outcome'] == 'WAITING_FOR_AUTHORITY'
        receipt = _call(payload, session_id='session-1')
        assert receipt['outcome'] == 'SUBMITTED'
        job = receipt['view']['job_ref']
        target = dict(job_ref=job, expected_job_revision=receipt['view']['job_revision'])
        status = job_payload('get_status', **target)
        try:
            assert running.wait(10)
            assert _call(status, session_id='session-1')['view']['phase'] == 'RUNNING'
            assert _call(status, session_id='other')['outcome'] == 'WAITING_FOR_AUTHORITY'
            assert await send(gateway, replace(sender, user_id='other'), 'stop '+job) == 'OWNER_SENDER_REQUIRED'
            assert not native.created[0].cancels
            assert json.loads(await send(gateway, sender, 'status '+job))['view']['phase'] == 'RUNNING'
        finally:
            release.set()
        assert finished.wait(15) and not failures, failures
        view = _call(status, session_id='session-1')['view']
        assert (view['phase'], view['stage_acceptance']) == ('COMPLETED', 'ACCEPTED')
        row, = read_launches(owner.journal.path)
        [invocation] = stand.checker_host.started
        assert row['result']['check']['runs']['value']['nonce'] == invocation.nonce
        assert row['stop_proof'] == 'CGROUP_EMPTY' and summary(row)['view'] == view
        assert json.loads(await send(gateway, sender, 'status '+job+' --saved'))['view'] == view
        cancelled = _call(job_payload('request_cancel', request_id='cancel-1', **target), session_id='session-1')
        assert cancelled['outcome'] == 'UNKNOWN' and cancelled['hold'] and not native.created[0].cancels
        assert set(cancelled['view']['verified_claims']) == {'worker_stopped', 'descendants_stopped'}
        ticket = owner.journal.read_launch(owner.token, 'proj-alpha', job, target['expected_job_revision'])[0]
        capture = next(o for o in row['observations'] if o['kind'] == 'CAPTURE')
        check = next(o for o in row['observations'] if o['kind'] == 'CHECK')['body']
        seq = owner.journal.append_observation(owner.token, ticket, capture['body'])
        owner.journal.append_observation(owner.token, ticket, {**check, 'capture_ref': {**check['capture_ref'], 'seq': seq}})
        assert _call(status, session_id='session-1')['view']['stage_acceptance'] == 'NOT_ACCEPTED'
        assert await send(gateway, sender, 'prune') == 'LINUX_PRUNE_UNAVAILABLE'
        from plugins.symphony_pm.doctor import doctor
        diagnostic = doctor(home, owner.scope.namespace, owner.journal.path, [row])
        assert 'LINUX_LIVE_ADMISSION_UNOBSERVED' in diagnostic['reasons']
        assert 'UNSETTLED_PRIOR_LAUNCH_HOLD' not in diagnostic['reasons']
    assert host.stop() == () and not owner.guard.owned
