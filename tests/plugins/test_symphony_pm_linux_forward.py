"""Standalone shared-token admission through the owner's private control socket."""
import asyncio
import base64
from collections import Counter
import json
from pathlib import Path
import secrets
from contextlib import closing

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes_state_runtime import list_session_admissions
from tests.plugins.symphony_forward_peer import ForwardPeer
from tests.plugins.test_symphony_pm_linux_owner_check import checker_stand
from tests.plugins.test_symphony_pm_linux_typed_apply import linux_chat
from tests.waiting import async_wait_until


@pytest.mark.asyncio
async def test_shared_forward(linux_chat, monkeypatch, record_property):
    stand, _, native, finished, failures, session, _ = linux_chat
    inbox = asyncio.Queue()
    peer = ForwardPeer(session, stand, native, finished, failures, inbox.put_nowait)
    public = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    token = secrets.token_urlsafe(32)
    frames = Counter()
    serial = 0

    async def rpc(method, **params):
        nonlocal serial
        serial += 1
        rid = serial
        frames[method] += 1
        await peer.control(dict(op='frame', socket='chat', frame=dict(
            id=rid, jsonrpc='2.0', method=method, params=params)))
        while True:
            event = await asyncio.wait_for(inbox.get(), 20)
            if event['frame'].get('id') == rid:
                return event['frame']

    async def open_chat():
        reply = await peer.control(dict(op='open', channel='ordinary', socket='chat', path='/api/ws',
                                        query='token=' + token, subprotocols=[]))
        assert reply == dict(transport='standalone', credential='session-token', authority_bound=False)

    try:
        manifest = await peer.control(dict(op='init', public_key=base64.b64encode(public).decode(),
                                          ssh_token=token, host_fingerprint='SHA256:' + 'A' * 43))
        await open_chat()
        ref = dict(profile_id=manifest['profile_id'], session_id=manifest['conversation_id'])
        resumed = await rpc('session.resume', session_id=ref['session_id'], lazy=True)
        assert 'result' in resumed, resumed
        sid = resumed['result']['session_id']
        assert resumed['result']['session_key'] == ref['session_id']
        before_first_probe = await rpc('prompt.submit', session_id=sid, text='First ordinary message')
        assert before_first_probe['error']['message'] == 'FORWARD_HOLD'
        assert all(row['entered'] == 0 for row in (await peer.control(dict(op='counts')))['ipc'].values())
        bound = (await rpc('prompt.forward_probe', session_id=sid))['result']
        assert bound['route'] == 'forward' and bound['ref'] == ref, bound
        params = dict(session_id=sid, ref=ref, generation=bound['generation'],
                      submission_id='input-1', text='Inspect the offline fixture.')
        assert 'error' in await rpc('prompt.forward_submit', **dict(params, generation='old'))
        assert not (await peer.control(dict(op='counts')))['admissions']
        # Park the first synthetic model call. The second distinct input then
        # follows the first through the actual durable FIFO while it is busy.
        assert await peer.control(dict(op='busy', action='arm')) == dict(state='armed', admission_id=None)
        original = peer.runner._run_agent
        plain = '/example is part of this sentence'
        observed_text = []
        control_flags = []
        handle = peer.runner._handle_message
        async def observe_dispatch(event):
            if event.text == plain:
                control_flags.append((event.allow_gateway_control, event.get_command()))
            return await handle(event)
        monkeypatch.setattr(peer.runner, '_handle_message', observe_dispatch)
        async def fixture_turn(message, *args, **kwargs):
            if message == plain:
                observed_text.append(message)
            if message == params['text']:
                return await original(message, *args, **kwargs)
            return dict(final_response='Recorded', messages=[], completed=True)
        monkeypatch.setattr(peer.runner, '_run_agent', fixture_turn)
        ack = (await rpc('prompt.forward_submit', **params))['result']
        assert ack['ref'] == ref and ack['submission_id'] == 'input-1' and ack['admission_id']
        receipt_read = await rpc('prompt.receipt', session_id=sid, admission_id=ack['admission_id'])
        assert 'error' in receipt_read and 'result' not in receipt_read
        await async_wait_until(lambda: peer.busy_state == 'occupied', timeout=15, what='first synthetic turn')
        occupied = dict(state='occupied', admission_id=ack['admission_id'])
        assert await peer.control(dict(op='busy', action='state')) == occupied
        busy = (await rpc('prompt.forward_submit', **dict(params, submission_id='input-2', text=plain)))['result']
        assert busy['admission_id'] != ack['admission_id'] and busy['ref'] == ref
        duplicate = (await rpc('prompt.forward_submit', **params))['result']
        assert duplicate['admission_id'] == ack['admission_id']
        conflict = await rpc('prompt.forward_submit', **dict(params, text='A different message.'))
        assert conflict['error']['message'] == 'admission_conflict', conflict
        assert await peer.control(dict(op='busy', action='state')) == occupied
        await peer.control(dict(op='busy', action='release', admission_id=ack['admission_id']))
        await async_wait_until(lambda: bool(peer.requests), timeout=20, what='one offline Symphony request')
        await async_wait_until(lambda: bool(observed_text), timeout=20, what='literal slash text reaches model stub')
        assert observed_text == [plain] and control_flags == [(False, None)]
        counts = await peer.control(dict(op='counts'))
        assert len(counts['requests']) == len(counts['plan_inputs']) == 1
        assert len(counts['admissions']) == 2
        assert counts['launches'] == counts['permits'] == 0
        row = list_session_admissions(peer.authority.db, session_id=ref['session_id'], pending_only=False)[0]
        assert row['payload']['shared_forward_v1'] is True
        from gateway.owner_identity import host_owner_subject
        assert row['principal_id'] == host_owner_subject()
        # Two verified IPC connections compete for a fresh durable identity.
        # They bypass only the Desktop socket's own sequential request dispatch.
        from tui_gateway.prompt_forward import ForwardClient
        target = (Path(ref['profile_id']), peer.authority.instance_id, ref)
        async def concurrent_delivery():
            return await asyncio.to_thread(ForwardClient._call, target, 'forward-submit',
                                           text='Concurrent', submission_id='concurrent', generation=bound['generation'])
        pair = await asyncio.gather(concurrent_delivery(), concurrent_delivery())
        assert pair[0]['admission_id'] == pair[1]['admission_id'] and pair[0]['submission_id'] == 'concurrent'
        # Drop a synthetic input at the IPC boundary, then drop another's
        # answer after the real owner has committed. Neither may be resent.
        from tui_gateway import prompt_forward
        query = prompt_forward.private_control_query
        def interrupted(home, raw, timeout):
            body = json.loads(raw)
            if body['params'].get('submission_id') == 'lost-before':
                raise ConnectionError()
            result = query(home, raw, timeout)
            if body['params'].get('submission_id') == 'lost-after':
                assert json.loads(result)['result']['admission_id']
                raise ConnectionError()
            return result
        monkeypatch.setattr(prompt_forward, 'private_control_query', interrupted)
        for request_id in ('lost-before', 'lost-after'):
            failed = await rpc('prompt.forward_submit', **dict(params, submission_id=request_id, text='Lost input'))
            assert failed['error']['message'] == 'FORWARD_UNKNOWN'
        expected = {'input-1', 'input-2', 'concurrent', 'lost-after'}
        def recorded():
            rows = list_session_admissions(peer.authority.db, session_id=ref['session_id'], pending_only=False)
            return [r['request_id'] for r in rows]
        assert set(recorded()) == expected and len(recorded()) == len(expected)
        assert (await rpc('prompt.forward_probe', session_id='missing'))['result']['route'] == 'hold'
        assert 'error' in await rpc('prompt.forward_submit', **params)
        assert (await rpc('prompt.submit', session_id=sid, text='Held'))['error']['message'] == 'FORWARD_HOLD'
        await peer.control(dict(op='close', socket='chat'))
        await open_chat()
        assert 'error' in await rpc('prompt.forward_submit', **params)
        path = Path(ref['profile_id']) / 'config.yaml'
        config = json.loads(path.read_text('utf-8'))
        resumed = await rpc('session.resume', session_id=ref['session_id'], lazy=True)
        sid = resumed['result']['session_id']
        reopened = await rpc('prompt.submit', session_id=sid, text='New socket before classification')
        assert reopened['error']['message'] == 'FORWARD_HOLD'
        modes = config['plugins']['entries']['symphony_pm']['settings']['desktop_forward_modes']
        for mode in ('personal', None):
            if mode:
                modes[ref['session_id']] = mode
            else:
                del modes[ref['session_id']]
            path.write_text(json.dumps(config), 'utf-8')
            assert (await rpc('prompt.forward_probe', session_id=sid))['result']['route'] == 'hold'
        assert set(recorded()) == expected and len(recorded()) == len(expected)
        # Only the explicitly ordinary conversation can enter the legacy handler.
        ordinary = manifest['ordinary_session_id']
        sid = (await rpc('session.resume', session_id=ordinary, lazy=True))['result']['session_id']
        ordered = (await rpc('prompt.forward_probe', session_id=sid))['result']
        assert ordered['route'] == 'ordinary' and ordered['ref']['session_id'] == ordinary
        local_ack = await rpc('prompt.submit', session_id=sid, text='Ordinary text.')
        assert local_ack['result']['status'] == 'streaming', local_ack
        await async_wait_until(lambda: peer.ordinary_turns == 1, timeout=5, what='one ordinary turn')
        del modes[ordinary]
        path.write_text(json.dumps(config), 'utf-8')
        assert (await rpc('prompt.submit', session_id=sid, text='Revoked'))['error']['message'] == 'FORWARD_HOLD'
        assert not list_session_admissions(peer.authority.db, session_id=ordinary, pending_only=False)
        draft = (await rpc('session.create', source='gui'))['result']['session_id']
        assert (await rpc('prompt.forward_probe', session_id=draft))['result'] == {'version': 1, 'route': 'hold'}
        assert set(recorded()) == expected and len(recorded()) == len(expected)
        # A legitimate read of another on-disk profile never selects it as this
        # SSH listener's private IPC destination.
        from hermes_cli import profiles
        from hermes_state import SessionDB
        monkeypatch.setattr(profiles, '_get_default_hermes_home', lambda: Path(ref['profile_id']))
        other = profiles.get_profile_dir('neighbor')
        other.mkdir(parents=True)
        with closing(SessionDB(db_path=other / 'state.db')) as foreign:
            foreign.create_session('neighbor-history', 'gui')
            history = await rpc('session.resume', session_id='neighbor-history', profile='neighbor', lazy=True)
            assert history['result']['session_key'] == 'neighbor-history'
            destinations = []
            discover = prompt_forward.discover_owner
            def watch_discovery(home):
                destinations.append(home)
                return discover(home)
            monkeypatch.setattr(prompt_forward, 'discover_owner', watch_discovery)
            before = (await peer.control(dict(op='counts')))['ipc']
            foreign_probe = await rpc('prompt.forward_probe', session_id=history['result']['session_id'])
            assert foreign_probe['result']['route'] == 'hold' and not destinations
            assert before == (await peer.control(dict(op='counts')))['ipc']
            assert not list_session_admissions(foreign, session_id='neighbor-history', pending_only=False)
        final = await peer.control(dict(op='counts'))
        assert len(final['admissions']) == 4 and len(final['requests']) == len(final['plan_inputs']) == 1
        assert final['launches'] == final['permits'] == 0
        assert final['ordinary_turns'] == 1
        assert final['ipc']['forward-submit'] == dict(entered=7, returned=7, conflicts=1)
        record_property('linux_offline', dict(frames=dict(frames), occupied=occupied,
                                             standalone_receipt_error=receipt_read['error'],
                                             concurrent_private_submits=2, ack=ack, counts=final,
                                             shared_origin=True, personal='hold', missing_mode='hold',
                                             foreign_discoveries=len(destinations), slash_control=control_flags,
                                             concurrent_ids=[entry['admission_id'] for entry in pair],
                                             new_chat='hold', owner_ids=recorded(),
                                             ordinary_status=local_ack['result']['status']))
    finally:
        await peer.disconnect()
