"""Ordinary dashboard subscribers in two real profiles share no private plan messages."""
import asyncio
import base64
from contextlib import ExitStack
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from gateway.run_runtime import initialize_gateway_runtime
from gateway.runtime_ownership import process_ownership
from gateway.session_authorities import owner_scope
from gateway.session_controls import AuthorityConnection
from tests.gateway.test_session_authorities_multiplex import _runner
from tests.gateway.test_sibling_server_request_response import _Transport
from plugins.symphony_pm.desktop_approval import notify_plan, pending_plan
from plugins.symphony_pm.desktop_route import signing_bytes
from tests.plugins.symphony_plan_peer import Peer, Socket
from tests.plugins.test_symphony_pm_linux_desktop_decision import decision, submit
from tests.plugins.test_symphony_pm_linux_owner_check import checker_stand
from tests.plugins.test_symphony_pm_linux_typed_apply import linux_chat


@pytest.mark.asyncio
async def test_dashboard_live_replay_transcript_and_receipt_across_two_profiles(linux_chat, record_property):
    stand, _, native, done, failures, session, _ = linux_chat
    peer = Peer(session, stand, native, done, failures, lambda _: None)
    peer.start('A' * 43 + '=', 'SHA256:' + 'A' * 43)
    try:
        await peer.control(dict(op='submit', action='recorded', admission='first'))
        channel = peer.channel
        shown = pending_plan(channel.owner.typed_approval, channel.entry.session_id)
        binding = dict(request_id=shown['request_id'], plan_digest=shown['digest'])
        decision(channel, 'presented', binding)
        decision(channel, 'answer', dict(binding, input_kind='text', input_id='known-answer', text='отклоняю'))
        result = await peer.control(dict(op='privacy', request_id=shown['request_id']))
        record_property('linux_offline', result)
    finally:
        await peer.disconnect()


async def privacy(peer, request_id, monkeypatch):
    import hermes_state
    from hermes_cli import web_server
    before = await asyncio.to_thread(peer.inspect)
    receipt = before['by_request'][request_id]
    binding = dict(request_id=request_id, plan_digest=receipt['plan_digest'])
    channel = peer.channel
    matrix = {role: {} for role in ('ordinary', 'neighbor', 'revoked')}
    controls = {}
    with ExitStack() as stack:
        home = Path(channel.owner.scope.profile_home)
        neighbor = home / 'profiles' / 'beta'
        neighbor.mkdir(parents=True)
        (neighbor / 'config.yaml').write_text('{}', encoding='utf-8')
        monkeypatch.setenv('HERMES_HOME', str(home))
        monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
        homes = [('default', home), ('beta', neighbor)]
        run = _runner(home, homes)
        process_ownership.reserve([h for _, h in homes])
        for _, h in homes:
            stack.callback(process_ownership.release, h)
        await initialize_gateway_runtime(run)
        run._running, run._gateway_loop = True, asyncio.get_running_loop()
        # The test drives the ASGI listener in process after completing runtime initialization.
        run.session_runtime_descriptor['state'] = 'ready'
        monkeypatch.setattr('gateway.run._gateway_runner_ref', lambda: run)
        authorities, sessions = {}, {}
        creators = []
        scoped = None
        try:
            for name, path in homes:
                authority = authorities[name] = run.session_authorities.for_home(path)
                stack.callback(authority.db.close)
                identity = dict(user_id='fixture-owner', provider='local', profile_id=authority.profile_id,
                                instance_id=authority.instance_id, native_bootstrap=True)
                creator = AuthorityConnection(authority, _Transport(), identity, operator=True)
                creators.append(creator)
                with owner_scope(authority):
                    frame = await creator.dispatch(dict(id=1, method='session.create', params=dict(
                        source='gui', request_id=peer.desktop_request_id, cwd=str(channel.launcher.settings.repository),
                        model='fixture', toolsets=[])))
                    assert 'result' in frame, frame
                    sessions[name] = frame['result']['session_id']
            authority = authorities['default']
            chat = sessions['default']
            live = authority.sessions[chat]
            with owner_scope(authority):
                assert chat == channel.source.chat_id == channel.entry.session_id
                assert live.source.user_id == channel.source.user_id and live.route == channel.entry.session_key
                monkeypatch.setattr(channel, 'runner', run)
                monkeypatch.setattr(channel, 'source', live.source)
                monkeypatch.setattr(channel, 'entry', run.session_store.get_or_create_session(live.source))
                path = home / 'config.yaml'
                stack.callback(path.write_bytes, path.read_bytes())
                config = json.loads(path.read_text('utf-8'))
                settings = config['plugins']['entries']['symphony_pm']['settings']
                assert settings['operator'] == dict(platform='local', user_id=live.source.user_id,
                                                    chat_id=chat, session_key=live.route)
                private_key = Ed25519PrivateKey.generate()
                registration = replace(peer.reg, client_id='privacy-desktop', conversation_id=chat,
                                       operator_id=live.source.user_id, public_key=base64.b64encode(
                                           private_key.public_key().public_bytes_raw()).decode())
                settings['desktop_plan_route'] = asdict(registration)
                path.write_text(json.dumps(config), encoding='utf-8')
            token = 'v' * 43
            state = web_server.app.state
            for key, value in dict(session_authority=authority, ui_surface='webapp', auth_required=False,
                                   bound_host='127.0.0.1').items():
                monkeypatch.setattr(state, key, value, raising=False)
            monkeypatch.setattr(web_server, '_SESSION_TOKEN', token)
            monkeypatch.setattr(web_server, '_DASHBOARD_EMBEDDED_CHAT_ENABLED', True)
            client = TestClient(web_server.app, base_url='http://127.0.0.1')
            stack.callback(client.close)
            frames = {'default': [], 'beta': []}
            sockets = {}
            def open_dashboard(name):
                minted = client.post('/api/webapp/socket-ticket', params=dict(socket='gateway', profile=name),
                                     headers={'X-Hermes-Session-Token': token})
                minted.raise_for_status()
                url = f"/api/ws?native_dial=browser&ticket={minted.json()['ticket']}&profile={name}"
                return stack.enter_context(client.websocket_connect(url, headers={'host': '127.0.0.1'}))

            def rpc(name, method, /, **params):
                ws = sockets[name]
                ws.send_json(dict(jsonrpc='2.0', id='ask', method=method, params=params))
                while True:
                    result = ws.receive_json()
                    frames[name].append(result)
                    if result.get('id') == 'ask':
                        return result

            async def ask(name, method, /, **params):
                return await asyncio.wait_for(asyncio.to_thread(rpc, name, method, **params), 12)

            for name, sid in sessions.items():
                sockets[name] = await asyncio.to_thread(open_dashboard, name)
                described = await ask(name, 'runtime.describe')
                assert Path(described['result']['profile_id']) == Path(authorities[name].profile_id)
                attached = await ask(name, 'session.resume', session_id=sid)
                assert 'result' in attached, attached
            delivered = asyncio.Queue()
            scoped = Socket(run, delivered.put_nowait)
            await scoped.open()
            revoked_events = []

            async def private(method, **params):
                await scoped.frame(dict(jsonrpc='2.0', id='private', method=method, params=params))
                while True:
                    frame = await asyncio.wait_for(delivered.get(), timeout=10)
                    if frame.get('id') == 'private':
                        return frame
                    revoked_events.append(frame)

            fields = (await private('route.challenge', client_id=registration.client_id,
                                    connection_id=registration.connection_id))['result']['fields']
            signature = base64.b64encode(private_key.sign(signing_bytes(fields))).decode()
            assert (await private('route.authenticate', signature=signature))['result']['state'] == 'READY'
            command = dict(name='symphony', session_id=chat, interaction='displayed-plan')
            reads = dict(receipt='plan-receipt ' + json.dumps(dict(binding, input_id=receipt['input_id'])),
                         result='plan-result ' + json.dumps(binding))
            for operation, arg in reads.items():
                frame = await private('command.dispatch', **command, arg=arg)
                recorded = json.loads(frame['result']['output'])
                assert (recorded['request_id'], recorded['session_id'], recorded['decision'],
                        recorded['plan_digest'], recorded['input_id']) == (
                            request_id, chat, receipt['decision'], binding['plan_digest'], receipt['input_id'])
                controls['authorized_' + operation] = 1
            with owner_scope(authority):
                approval = channel.owner.typed_approval
                if pending_plan(approval, channel.entry.session_id):
                    notify_plan(live, approval.slots[channel.entry.session_id])
                else:
                    assert submit(channel, 'private-view', 'initial-input') == 'WAITING_FOR_AUTHORITY'
                shown = pending_plan(channel.owner.typed_approval, channel.entry.session_id)
            positive = await asyncio.wait_for(delivered.get(), timeout=10)
            assert positive['params']['type'] == 'symphony.plan'
            controls['authorized_live'] = 1
            current = await private('command.dispatch', **command, arg='status')
            assert json.loads(current['result']['output'])['pending_plan'] == shown
            controls['authorized_status'] = 1
            settings['desktop_plan_route'] = None
            path.write_text(json.dumps(config), encoding='utf-8')
            notify_plan(live, channel.owner.typed_approval.slots[channel.entry.session_id])
            secrets = (shown['digest'], shown['request_id'], request_id, receipt['input_id'],
                       receipt['plan_digest'], 'symphony.plan', 'shown_plan')

            def disclosures(value):
                text = json.dumps(value)
                return sum(marker in text for marker in secrets)

            for name, sid in sessions.items():
                row = matrix['ordinary' if name == 'default' else 'neighbor']
                stream = authorities[name].sessions[sid].event_stream
                stream.publish(sid, dict(marker=name), event_type='fixture.control')
                replay = await ask(name, 'session.events.since', session_id=sid, last_sequence=0,
                                   replay_epoch=stream.epoch)
                assert any(x['type'] == 'fixture.control' for x in replay['result']['events'])
                row['replay'] = disclosures(replay)
                history = await ask(name, 'session.resume', session_id=sid)
                assert 'result' in history, history
                row['history'] = disclosures(history)
                assert any(x.get('params', {}).get('type') == 'fixture.control' for x in frames[name])
                controls[name] = {'live': 1, 'replay': 1}
                status = await ask(name, 'command.dispatch', session_id=sid, name='symphony', arg='status')
                if name == 'default':
                    assert 'result' in status, status
                    assert 'jobs' in json.loads(status['result']['output'])
                row['status'] = disclosures(status)
                row['receipt'] = 0
                # Repeat the exact successful frames (including target chat) on each ordinary socket.
                for arg in ('status', *reads.values()):
                    reply = await ask(name, 'command.dispatch', **command, arg=arg)
                    if name == 'default':
                        output = reply['result']['output']
                        if arg == 'status':
                            assert 'jobs' in json.loads(output)
                        else:
                            assert output == 'DESKTOP_DECISION_ROUTE'
                    else:
                        assert 'error' in reply and reply['error']['code'] != -32603, reply
                    row['status' if arg == 'status' else 'receipt'] += disclosures(reply)
                if name == 'beta':
                    foreign = await ask(name, 'session.resume', session_id=chat)
                    assert 'error' in foreign
                row['live'] = disclosures([f for f in frames[name] if 'method' in f])
            for surface, method, params in (
                ('replay', 'session.events.since', dict(session_id=chat, last_sequence=0, replay_epoch=live.event_stream.epoch)),
                ('history', 'session.resume', dict(session_id=chat)),
                ('status', 'command.dispatch', dict(command, arg='status')),
                ('receipt', 'command.dispatch', dict(command, arg=reads['receipt'])),
                ('receipt', 'command.dispatch', dict(command, arg=reads['result'])),
            ):
                reply = await private(method, **params)
                assert reply['error']['message'] == 'PLAN_ROUTE_REFUSED'
                matrix['revoked'][surface] = matrix['revoked'].get(surface, 0) + disclosures(reply)
            matrix['revoked']['live'] = disclosures(revoked_events)
            assert all(len(row) == 5 and not any(row.values()) for row in matrix.values()), matrix
            after = await asyncio.to_thread(peer.inspect)
            assert before == after
            return dict(request_id=request_id, conversation_id=chat, session_id=recorded['session_id'],
                        plan_digest=recorded['plan_digest'], input_id=recorded['input_id'],
                        matrix=matrix, controls=controls, before=before, after=after)
        finally:
            if scoped is not None:
                await scoped.close()
            for creator in creators:
                await creator.close()
