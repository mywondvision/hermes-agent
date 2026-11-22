"""Python side of the offline SSH-purpose session contract, with a real temporary owner journal."""
import asyncio
import base64
import hashlib
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict
import json
from pathlib import Path
from threading import Event

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes_cli import plugins
from plugins.symphony_pm.desktop_route import signing_bytes
from tests.plugins.symphony_plan_peer import Peer
from tests.plugins.test_symphony_pm_linux_typed_apply import linux_chat
from tests.plugins.test_symphony_pm_linux_owner_check import checker_stand


@asynccontextmanager
async def opened(linux_chat):
    stand, _, native, finished, failures, session, _ = linux_chat
    messages = asyncio.Queue()
    key = Ed25519PrivateKey.generate()
    peer = Peer(session, stand, native, finished, failures, messages.put_nowait)
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    manifest = peer.start(public, 'SHA256:' + 'A' * 43)
    rpc_id = 0

    async def rpc(method, params, socket='a'):
        nonlocal rpc_id
        rpc_id += 1
        await peer.control(dict(op='frame', socket=socket, frame=dict(jsonrpc='2.0', method=method,
                                                                     id=rpc_id, params=params)))
        while True:
            response = await asyncio.wait_for(messages.get(), timeout=8)
            if response['socket'] == socket and response['frame'].get('id') == rpc_id:
                return response['frame']

    async def challenge(socket='a'):
        params = {k: manifest[k] for k in ('client_id', 'connection_id')}
        return (await rpc('route.challenge', params, socket))['result']['fields']

    async def authorize(fields, socket='a', signer=key):
        return await rpc('route.authenticate', dict(signature=base64.b64encode(
            signer.sign(signing_bytes(fields))).decode()), socket)

    async def command(arg, binding=None, socket='a', **overrides):
        params = dict(session_id=manifest['conversation_id'], name='symphony', interaction='displayed-plan',
                      arg=arg if binding is None else arg + ' ' + json.dumps(binding, ensure_ascii=False))
        return await rpc('command.dispatch', dict(params, **overrides), socket)

    try:
        await peer.control(dict(op='open', socket='a'))
        yield peer, manifest, challenge, authorize, command, rpc
    finally:
        await peer.disconnect()


@pytest.mark.asyncio
async def test_registered_socket_reads_one_durable_plan_and_cannot_apply(linux_chat):
    async with opened(linux_chat) as (peer, manifest, challenge, authorize, command, rpc):
        assert 'error' in await command('status')
        fields = await challenge()
        assert fields[4:10] == [peer.reg.host_fingerprint, manifest['profile'], manifest['namespace'],
                                manifest['operator_id'], manifest['conversation_id'], manifest['session_id']]
        assert (await authorize(fields))['result']['state'] == 'READY'
        live = peer.channel.runner.session_authority.sessions[manifest['conversation_id']]
        observed = []
        live.event_stream.observers.add(observed.append)
        await peer.control(dict(op='submit', action='one', admission='one-in'))
        result = await command('status')
        pending = json.loads(result['result']['output'])['pending_plan']
        # The common stream and reconnect buffer have no decision events.
        with live.event_stream.lock:
            events = live.event_stream.since(live.event_stream.epoch, 0)['events']
        assert not observed and not events
        operator = plugins.get_plugin_command_handler('symphony')
        status = operator('status', source=live.source, platform='local',
                          session_key=live.route, session_id=manifest['session_id'])
        assert 'pending_plan' not in json.loads(status)
        binding = dict(request_id=pending['request_id'], plan_digest=pending['digest'])
        assert 'result' in await command('plan-presented', binding)
        reply = dict(binding, input_kind='text', input_id='answer', text='отклоняю')
        receipt = json.loads((await command('plan-answer', reply))['result']['output'])
        assert receipt['decision'] == 'DENIED' and receipt['shown_plan'] == pending['plan']
        for bad in ('apply', 'release', 'prune', 'status extra'):
            assert 'error' in await command(bad)
        assert 'error' in await command('status', session_id='neighbor')
        for method in ('session.events.since', 'session.resume', 'session.history', 'prompt.submit'):
            assert 'error' in await rpc(method, {})
        assert peer.inspect()['decisions'] == 1 and peer.inspect()['permits'] == 0
        assert json.loads((await command('plan-result', binding))['result']['output']) == receipt


@pytest.mark.asyncio
async def test_challenge_requires_current_socket_registration_and_boot(linux_chat, monkeypatch):
    async with opened(linux_chat) as (peer, _manifest, challenge, authorize, command, _rpc):
        fields = await challenge()
        await peer.control(dict(op='open', socket='b'))
        other = await challenge('b')
        assert fields[2] != other[2] and fields[10] != other[10]
        assert 'error' in await authorize(fields, 'b')
        assert 'error' in await authorize(other, 'b')
        assert 'error' in await authorize(fields, signer=Ed25519PrivateKey.generate())
        assert 'error' in await authorize(fields)
        fields = await challenge()
        from plugins.symphony_pm import desktop_route
        now = desktop_route.time.monotonic()
        with monkeypatch.context() as clock:
            from types import SimpleNamespace
            clock.setattr(desktop_route, 'time', SimpleNamespace(monotonic=lambda: now + 61))
            assert 'error' in await authorize(fields)
        assert 'result' in await authorize(await challenge())
        peer.configure(dict(asdict(peer.reg), boot_id='different-boot'))
        assert 'error' in await command('status')
        assert 'error' in await command('plan-result', dict(request_id='x', plan_digest='f' * 64))
        assert peer.inspect()['launches'] == 0


@pytest.mark.asyncio
async def test_revocation_before_commit_rolls_back_all_linux_effects(linux_chat, monkeypatch):
    async with opened(linux_chat) as (peer, _manifest, challenge, authorize, command, _rpc):
        assert 'result' in await authorize(await challenge())
        await peer.control(dict(op='submit', action='race', admission='race-in'))
        pending = json.loads((await command('status'))['result']['output'])['pending_plan']
        binding = dict(request_id=pending['request_id'], plan_digest=pending['digest'])
        await command('plan-presented', binding)
        journal = peer.channel.owner.journal
        transaction = journal._transaction

        @contextmanager
        def revoke(*args, authorize_commit=None):
            with transaction(*args, authorize_commit=authorize_commit) as db:
                yield db
                if authorize_commit is not None:
                    peer.configure(None)

        monkeypatch.setattr(journal, '_transaction', revoke)
        assert 'error' in await command('plan-answer', dict(binding, input_id='race', input_kind='text',
                                                              text='подтверждаю'))
        assert peer.inspect() == dict(decisions=0, permits=0, reservations=0, launches=0, by_request={})


def test_wire_v1_unicode_signature_vector():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    vector = json.loads((Path(__file__).parents[1] / 'fixtures/symphony/plan-route-v1.json').read_text('utf-8'))
    message = signing_bytes(vector['fields'])
    assert hashlib.sha256(message).hexdigest() == vector['sha256']
    Ed25519PublicKey.from_public_bytes(base64.b64decode(vector['public_key'])).verify(
        base64.b64decode(vector['signature']), message)


@pytest.mark.asyncio
@pytest.mark.parametrize('durable', [False, True], ids=['before-commit', 'after-commit'])
async def test_disconnect_during_linux_answer(linux_chat, monkeypatch, durable, record_property):
    async with opened(linux_chat) as (peer, manifest, challenge, authorize, command, _rpc):
        assert 'result' in await authorize(await challenge())
        await peer.control(dict(op='submit', action='disconnect', admission='submission'))
        pending = json.loads((await command('status'))['result']['output'])['pending_plan']
        binding = dict(request_id=pending['request_id'], plan_digest=pending['digest'])
        await command('plan-presented', binding)
        reply = dict(binding, input_id='decision', input_kind='text', text='подтверждаю')
        journal = peer.channel.owner.journal
        transaction, staged, release = journal._transaction, Event(), Event()

        @contextmanager
        def at_boundary(*args, authorize_commit=None):
            with transaction(*args, authorize_commit=authorize_commit) as db:
                yield db
                if authorize_commit is not None and not durable:
                    assert db.execute('SELECT count(*) FROM serving_permits').fetchone()[0] == 1
                    staged.set()
                    assert release.wait(10)
            if authorize_commit is not None and durable:
                staged.set()
                assert release.wait(10)

        monkeypatch.setattr(journal, '_transaction', at_boundary)
        await peer.sockets['a'].frame(dict(jsonrpc='2.0', id='lost', method='command.dispatch',
            params=dict(name='symphony', interaction='displayed-plan', session_id=manifest['conversation_id'],
                        arg='plan-answer ' + json.dumps(reply))))
        assert await asyncio.to_thread(staged.wait, 10)
        stream = peer.channel.runner.session_authority.sessions[manifest['conversation_id']].event_stream
        disconnect = asyncio.create_task(peer.control(dict(op='close', socket='a')))
        from tests.waiting import async_wait_until
        try:
            await async_wait_until(lambda: not stream.private_subscribers.get('symphony.plan'), timeout=4,
                                   what='physical disconnect delivered during transaction')
        finally:
            release.set()
            await disconnect
        await peer.control(dict(op='open', socket='b'))
        assert 'result' in await authorize(await challenge('b'), 'b')
        before = await asyncio.to_thread(peer.inspect)
        outcome = (await command('plan-result', binding, socket='b'))['result']['output']
        if durable:
            assert json.loads(outcome)['input_id'] == 'decision'
        else:
            assert outcome == 'DESKTOP_DECISION_UNKNOWN'
        assert [before[k] for k in ('decisions', 'permits', 'reservations', 'launches')] == [int(durable)] * 4
        assert before == await asyncio.to_thread(peer.inspect)
        record_property('linux_offline', dict(request_id=pending['request_id'], plan_digest=pending['digest'],
                        input_id=reply['input_id'], disconnect_after_commit=durable, **before))
