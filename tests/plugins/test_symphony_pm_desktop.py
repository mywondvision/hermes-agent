"""Drive a Windows Desktop session through its canonical dispatcher with a fixture executor."""
import asyncio
import json
import os
import sqlite3
import subprocess
import threading
import time
from contextlib import ExitStack, closing
from hashlib import sha256

import pytest

from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner
from tests.gateway.test_sibling_server_request_response import _Transport
from tests.plugins.symphony_serving_support import Guard, Native, Observer
from tests.plugins.symphony_typed_human_support import FixtureLauncher
from tests.plugins.test_symphony_pm_adapter import submit_payload
from tests.plugins.test_symphony_pm_codex_exec import _git
from tests.plugins.test_symphony_pm_serving import _call
from tests.waiting import async_wait_until


@pytest.mark.platforms('windows')
@pytest.mark.asyncio
@pytest.mark.parametrize('voice', [False, True], ids=['text', 'synthetic-final'])
@pytest.mark.parametrize('approve', [False, True], ids=['deny', 'approve'])
async def test_desktop_plan_and_original_admission(tmp_path, monkeypatch, voice, approve):
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session_authorities import owner_scope
    from gateway.session_context import clear_session_vars, set_session_vars
    from gateway.session_controls import AuthorityConnection
    from hermes_cli import plugins
    from hermes_cli.approval_transport import RegisteredApprovalTransport
    from hermes_cli.plugin_services import ServiceHost
    from plugins.symphony_pm.contracts import ServingBootstrap
    from plugins.symphony_pm.transport.bootstrap import TransportBootstrap

    home, homes = _reserve_homes(tmp_path, monkeypatch, names=())
    repo = tmp_path / 'project'
    repo.mkdir()
    _git('init', '-q', '-b', 'fixture-base', cwd=repo)
    _git('commit', '--allow-empty', '-m', 'desktop fixture', cwd=repo)
    run = _runner(home, homes)
    launcher = FixtureLauncher(repo, tmp_path / 'attempt' / 'src')
    views = []
    request_id = 'desktop-fixture-task'
    async def model(event, source, key, generation):
        entry = run.session_store.lookup_by_session_key(key)
        payload = submit_payload(request_id=request_id, goal='Record a fixture result.',
                                 project_ref='fixture', raw_constraints=['Fixture executor only.'])
        def call():
            tokens = set_session_vars(platform=source.platform.value, chat_id=source.chat_id,
                                      user_id=source.user_id, chat_type=source.chat_type,
                                      session_key=key, session_id=entry.session_id)
            try:
                return _call(payload, session_id=entry.session_id)
            finally:
                clear_session_vars(tokens)
        result = await asyncio.to_thread(call)
        views.append(result)
        return result['outcome']

    process_ownership.reserve([home])
    with ExitStack() as stack:
        stack.callback(process_ownership.release, home)
        await initialize_gateway_runtime(run)
        authority = run.session_authority
        stack.callback(authority.db.close)
        run._running, run._gateway_loop = True, asyncio.get_running_loop()
        monkeypatch.setattr('gateway.run._gateway_runner_ref', lambda: run)
        monkeypatch.setattr(run, '_handle_message_with_agent', model)
        identity = dict(user_id='fixture-owner', provider='local', profile_id=authority.profile_id,
                        instance_id=authority.instance_id, native_bootstrap=True)
        transport = _Transport()
        owner = AuthorityConnection(authority, transport, identity, operator=True)
        async def rpc(connection, method, **params):
            with owner_scope(authority):
                frame = await connection.dispatch(dict(id=1, method=method, params=params))
            assert 'result' in frame, frame
            return frame['result']
        created = await rpc(owner, 'session.create', request_id='create-test', source='gui',
                            cwd=str(repo), model='fixture', toolsets=[])
        sid = created['session_id']
        live = authority.sessions[sid]
        settings = dict(operator=dict(platform='local', chat_id=live.source.chat_id,
                                      user_id=live.source.user_id, session_key=live.route),
                        typed_approval=dict(project_ref='fixture', repository=str(repo),
                                            branch='fixture-base', ttl_seconds=900))
        (home / 'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['symphony_pm'], 'entries': {
            'symphony_pm': {'settings': settings}}}}), encoding='utf-8')
        plugins._reset_plugin_managers_for_tests()
        stack.callback(plugins._reset_plugin_managers_for_tests)
        plugins.discover_plugins()
        host = ServiceHost()
        stack.callback(host.stop)
        def unused(*args):
            raise AssertionError('unexpected legacy approval')
        legacy = RegisteredApprovalTransport('fixture', unused, 'test', str(home))
        bindings = []
        def start(scope):
            def guard(s):
                if bindings:
                    assert not bindings[-1].port.owner.guard.owned
                    return Guard(s, incarnation=f'fixture-restarted-{len(bindings)}', predecessor_dead=True)
                return Guard(s)
            binding = TransportBootstrap(guard, lambda s: Observer([time.time()]),
                                         lambda s: Native(), launcher, legacy, 'fixture', 'controller', ('issuer',))(scope)
            bindings.append(binding)
            return binding
        host.configure(home, {('symphony_pm', 'ingress'): ServingBootstrap(start)})
        host.start_profile(home)
        def restart_service():
            nonlocal host
            host.stop()
            plugins._reset_plugin_managers_for_tests()
            plugins.discover_plugins()
            host = ServiceHost()
            stack.callback(host.stop)
            host.configure(home, {('symphony_pm', 'ingress'): ServingBootstrap(start)})
            host.start_profile(home)
        approval = bindings[0].port.owner.typed_approval
        peer = AuthorityConnection(authority, _Transport(), identity, operator=True)
        try:
            await rpc(peer, 'session.resume', session_id=sid)
            text = dict(session_id=sid, submission_id='first-input', text='Record a fixture result.')
            if voice:
                text['voice_turn'] = True
            receipt = await rpc(owner, 'prompt.submit', **text)
            same = await rpc(owner, 'prompt.receipt', session_id=sid, submission_id='first-input')
            duplicate = await rpc(owner, 'prompt.submit', **text)
            assert duplicate['admission_id'] == same['admission_id']
            assert receipt['admission_id'] == same['admission_id']
            await async_wait_until(lambda: bool(views), timeout=20, what='fixture model turn')
            assert [r['outcome'] for r in views] == ['WAITING_FOR_AUTHORITY']
            assert len(approval.slots) == 1
            status = await rpc(owner, 'command.dispatch', session_id=sid, name='symphony', arg='status')
            result = json.loads(status['output'])
            pending = result['pending_plan']
            digest = pending['digest']
            assert pending['request_id'] == 'desktop-fixture-task'
            assert pending['admission_id'] == receipt['admission_id']
            assert pending['state'] == 'WAITING_FOR_AUTHORITY' and result['jobs'] == []
            decision = dict(session_id=sid, name='symphony', arg='approve ' + digest[:16])
            for connection in (owner, peer):
                assert (await rpc(connection, 'command.dispatch', **decision))['output'] == 'ORIGINAL_TYPED_COMMAND_REQUIRED'
            if voice:
                second = await rpc(owner, 'prompt.submit', **{**text, 'submission_id': 'second-input'})
                assert second['admission_id'] != receipt['admission_id']
                await async_wait_until(lambda: len(views) == 2, timeout=20, what='distinct synthetic utterance')
            assert not launcher.calls
            journal = bindings[0].port.owner.journal
            with closing(sqlite3.connect(journal.path.with_name('typed-plan-audit.sqlite3'))) as db:
                assert db.execute('SELECT message_id,plan_digest FROM voice_inputs').fetchall() == [
                    (receipt['admission_id'], digest)]
            observation = {
                'input_id': text['submission_id'], 'session_hash': sha256(sid.encode()).hexdigest(),
                'admission_id': receipt['admission_id'], 'request_id': pending['request_id'],
                'plan_digest': digest, 'status': pending['state'], 'launches': len(launcher.calls),
                'second_admission': second['admission_id'] if voice else None,
            }
            if os.environ.get('HERMES_E2E_ARTIFACTS'):
                from tests.plugins.symphony_native_crash_support import save_proof
                save_proof('desktop-final' if voice else 'desktop-text', observation)
            offered = dict(request_id=pending['request_id'], plan_digest=digest)
            await async_wait_until(lambda: any(f.get('params', {}).get('type') == 'symphony.plan'
                                               for f in transport.frames), timeout=5, what='displayable plan')
            packet = next(f['params'] for f in transport.frames if f.get('params', {}).get('type') == 'symphony.plan')
            assert packet['session_id'] == sid and packet['payload'] == dict(offered, plan=pending['plan'])
            answer = dict(offered, input_id='decision-input', input_kind='voice' if voice else 'text',
                          text='подтверждаю' if approve else 'отклоняю')
            async def act(conn, verb, body, interactive=True):
                params = dict(session_id=sid, name='symphony', arg='plan-' + verb + ' ' + json.dumps(body))
                if interactive:
                    params['interaction'] = 'displayed-plan'
                return (await rpc(conn, 'command.dispatch', **params))['output']
            assert await act(owner, 'presented', offered, False) == 'DESKTOP_DECISION_ROUTE'
            assert await act(owner, 'answer', answer) == 'DESKTOP_PLAN_NOT_PRESENTED'
            assert json.loads(await act(owner, 'presented', offered))['state'] == 'PRESENTED'
            assert await act(peer, 'answer', answer) == 'DESKTOP_PLAN_NOT_PRESENTED'
            assert await act(owner, 'answer', dict(answer, text='не подтверждаю')) == 'EXPLICIT_PLAN_DECISION_REQUIRED'
            assert await act(owner, 'answer', dict(answer, plan_digest='f'*64)) == 'NO_MATCHING_DESKTOP_PLAN'
            assert await act(owner, 'receipt', answer) == 'DESKTOP_DECISION_UNKNOWN'
            with monkeypatch.context() as change:
                original_plan = launcher.typed_plan
                def changed():
                    if voice and approve:
                        return dict(original_plan(), config_sha256='f'*64)
                    if not voice and not approve:
                        return dict(original_plan(), config_sha256='1'*64)
                    if approve:
                        raise OSError('fixture repository unavailable')
                    raise subprocess.TimeoutExpired('git', 5)
                if not voice and not approve:
                    original_record = journal.record_desktop_decision
                    def change_during_write(*args, **kwargs):
                        change.setattr(launcher, 'typed_plan', changed)
                        return original_record(*args, **kwargs)
                    change.setattr(journal, 'record_desktop_decision', change_during_write)
                else:
                    change.setattr(launcher, 'typed_plan', changed)
                if voice == approve:
                    failed = await act(owner, 'answer', dict(answer, text='подтверждаю'))
                elif voice:
                    failed = (await rpc(owner, 'command.dispatch', session_id=sid, name='symphony', arg='status'))['output']
                else:
                    failed = await act(owner, 'presented', offered)
                assert failed == 'TYPED_PLAN_CHANGED'
            assert await act(owner, 'answer', answer) == 'NO_MATCHING_DESKTOP_PLAN'
            assert not launcher.calls
            with closing(sqlite3.connect(journal.path)) as db:
                assert db.execute('SELECT count(*) FROM serving_permits').fetchone()[0] == 0
            assert await act(owner, 'receipt', answer) == 'DESKTOP_DECISION_UNKNOWN'
            request_id = 'new-fixture-plan'
            if not voice and not approve:
                restart_service()
                assert await act(owner, 'receipt', answer) == 'DESKTOP_DECISION_UNKNOWN'
                assert await act(owner, 'answer', answer) == 'NO_MATCHING_DESKTOP_PLAN'
            admission = await rpc(owner, 'prompt.submit', **dict(text, submission_id='new-task-id'))
            await async_wait_until(lambda: len(views) == (3 if voice else 2), timeout=20, what='new request')
            new_status = await rpc(owner, 'command.dispatch', session_id=sid, name='symphony', arg='status')
            fresh = json.loads(new_status['output'])['pending_plan']
            assert fresh['admission_id'] == admission['admission_id']
            offered = dict(request_id=request_id, plan_digest=fresh['digest'])
            assert offered['plan_digest'] != digest
            answer.update(offered, input_id='new-plan-answer')
            assert await act(owner, 'answer', answer) == 'DESKTOP_PLAN_NOT_PRESENTED'
            assert json.loads(await act(owner, 'presented', offered))['state'] == 'PRESENTED'
            assert json.loads(await act(peer, 'presented', offered))['state'] == 'PRESENTED'
            other = dict(answer, input_id='other-window-input', input_kind='text' if voice else 'voice',
                         text='отклоняю' if approve else 'подтверждаю')
            entered, proceed = threading.Event(), threading.Event()
            with monkeypatch.context() as race:
                record = bindings[-1].port.owner.journal.record_desktop_decision
                def delayed_record(*args, **kwargs):
                    entered.set()
                    assert proceed.wait(10)
                    return record(*args, **kwargs)
                race.setattr(bindings[-1].port.owner.journal, 'record_desktop_decision', delayed_record)
                first = asyncio.create_task(act(owner, 'answer', answer))
                try:
                    assert await asyncio.to_thread(entered.wait, 10)
                    competing = asyncio.create_task(act(peer, 'answer', other))
                    duplicate = asyncio.create_task(act(owner, 'answer', answer))
                finally:
                    proceed.set()
                results = await asyncio.gather(first, duplicate)
                assert await competing == 'NO_MATCHING_DESKTOP_PLAN'
            recorded = json.loads(await act(owner, 'receipt', answer))
            assert recorded['shown_plan'] == fresh['plan']
            assert await act(peer, 'receipt', other) == 'DESKTOP_DECISION_UNKNOWN'
            assert json.loads(await act(peer, 'result', offered)) == recorded
            assert recorded['decision'] == ('APPROVED' if approve else 'DENIED')
            assert await act(owner, 'answer', dict(answer, text='применить')) == 'DESKTOP_INPUT_CONFLICT'
            apply = await rpc(owner, 'command.dispatch', session_id=sid, name='symphony', arg='apply sample ' + 'a'*64)
            assert apply['output'] == 'ORIGINAL_TYPED_COMMAND_REQUIRED'
            assert not _git('for-each-ref', 'refs/heads/symphony', cwd=repo)
            assert all(json.loads(row)['decision'] == recorded['decision'] for row in results)
            if approve:
                assert any(json.loads(row).get('launch') == 'SUBMITTED' for row in results), results
                await async_wait_until(lambda: bool(launcher.calls), timeout=5, what='fixture launch')
            with closing(sqlite3.connect(journal.path)) as db:
                permits = db.execute('SELECT count(*) FROM serving_permits').fetchone()[0]
                assert db.execute("SELECT count(*) FROM serving_observations WHERE kind='APPLY'").fetchone()[0] == 0
            with closing(sqlite3.connect(journal.path.with_name('typed-plan-audit.sqlite3'))) as db:
                decisions = db.execute('SELECT count(*) FROM desktop_plan_decisions').fetchone()[0]
            assert decisions == 1 and permits == len(launcher.calls) == int(approve)
            restart_service()
            assert json.loads(await act(owner, 'receipt', answer)) == recorded
            assert json.loads(await act(owner, 'answer', answer)) == recorded
            assert await act(owner, 'answer', dict(answer, input_id='another-id')) == 'NO_MATCHING_DESKTOP_PLAN'
            assert len(launcher.calls) == int(approve)
            if os.environ.get('HERMES_E2E_ARTIFACTS'):
                save_proof(f"decision-{recorded['input_kind']}-{recorded['decision']}",
                           dict(offered, decision=recorded['decision'], input_id=recorded['input_id'],
                                session_hash=observation['session_hash'], admission_id=admission['admission_id'],
                                status_before_answer=fresh['state'], permits=permits, launches=len(launcher.calls),
                                decisions=decisions, restarted=True, robot_refs=0, apply_records=0))
        finally:
            await peer.close()
            await owner.close()
