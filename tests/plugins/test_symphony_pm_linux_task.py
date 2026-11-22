"""Task admission via an ordinary dashboard socket and the existing Linux plugin."""
import asyncio
import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from plugins.symphony_pm.desktop_route import signing_bytes
from tests.plugins.symphony_task_peer import TaskPeer
from tests.plugins.test_symphony_pm_linux_owner_check import checker_stand
from tests.plugins.test_symphony_pm_linux_typed_apply import linux_chat
from tests.waiting import async_wait_until


@pytest.mark.asyncio
async def test_ordinary_admission_reaches_symphony_once(linux_chat, record_property):
    stand, _, native, finished, failures, session, _ = linux_chat
    messages = asyncio.Queue()
    peer = TaskPeer(session, stand, native, finished, failures, messages.put_nowait)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw()
    try:
        manifest = await peer.control(dict(op='init', public_key=base64.b64encode(public).decode(),
                                           host_fingerprint='SHA256:' + 'A' * 43))
        chat = manifest['conversation_id']
        await peer.control(dict(op='open', channel='ordinary', socket='chat'))

        async def rpc(method, *, socket='chat', **params):
            if method not in ('route.challenge', 'route.authenticate'):
                params.setdefault('session_id', chat)
            await peer.control(dict(op='frame', socket=socket, frame=dict(
                jsonrpc='2.0', id='test', method=method, params=params)))
            while True:
                result = await asyncio.wait_for(messages.get(), 12)
                if result['socket'] == socket and result['frame'].get('id') == 'test':
                    return result['frame']

        assert 'result' in await rpc('session.resume')
        text = 'Inspect the offline fixture.'
        ack = (await rpc('prompt.submit', text=text, submission_id='input-1'))['result']
        assert ack['submission_id'] == 'input-1' and ack['admission_id'] != 'input-1'
        assert ack['ref'] == dict(session_id=chat, profile_id=manifest['profile_id'])
        assert (await rpc('prompt.receipt', admission_id=ack['admission_id']))['result']['admission_id'] == ack['admission_id']
        assert (await rpc('prompt.receipt', submission_id='input-1'))['error']['message'] == 'permission_denied'
        for selectors in ({}, {'admission_id': ack['admission_id'], 'submission_id': 'input-1'}):
            assert (await rpc('prompt.receipt', **selectors))['error']['message'] == 'invalid_params'
        await async_wait_until(lambda: bool(peer.requests), timeout=15, what='registered Symphony tool called')
        assert peer.requests == [dict(admission_id=ack['admission_id'], request_id='task-' + ack['admission_id'],
                                      outcome='WAITING_FOR_AUTHORITY')]
        replay = (await rpc('prompt.submit', text=text, submission_id='input-1'))['result']
        assert replay['admission_id'] == ack['admission_id']
        conflict = await rpc('prompt.submit', text=text + ' Changed.', submission_id='input-1')
        assert conflict['error']['message'] == 'admission_conflict'
        counts = await peer.control(dict(op='counts'))
        assert len(counts['requests']) == len(counts['admissions']) == 1 and counts['launches'] == 0

        await peer.control(dict(op='open', socket='plan'))
        fields = (await rpc('route.challenge', socket='plan', client_id=manifest['client_id'],
                            connection_id=manifest['connection_id']))['result']['fields']
        signature = base64.b64encode(key.sign(signing_bytes(fields))).decode()
        assert (await rpc('route.authenticate', socket='plan', signature=signature))['result']['state'] == 'READY'

        async def plan(arg):
            frame = await rpc('command.dispatch', socket='plan', name='symphony', interaction='displayed-plan', arg=arg)
            return json.loads(frame['result']['output'])

        pending = (await plan('status'))['pending_plan']
        binding = dict(request_id=pending['request_id'], plan_digest=pending['digest'])
        assert counts['plan_inputs'] == [dict(admission_id=ack['admission_id'], **binding)]
        await plan('plan-presented ' + json.dumps(binding))
        answer = dict(binding, input_id='confirm', input_kind='text', text='подтверждаю')
        approved = await plan('plan-answer ' + json.dumps(answer, ensure_ascii=False))
        assert approved['decision'] == 'APPROVED'
        before = await peer.control(dict(op='counts'))
        assert before['launches'] == before['permits'] == before['reservations'] == 1
        job = before['attempts'][0]['job']
        await peer.control(dict(op='open', channel='ordinary', socket='second'))
        assert 'result' in await rpc('session.resume', socket='second')
        assert (await rpc('prompt.receipt', socket='second', submission_id='input-1'))['error']['message'] == 'permission_denied'
        results = {}
        for verb in ('status', 'stop'):
            arg = verb + ' ' + job
            dispatched = await rpc('command.dispatch', socket='second', name='symphony', arg=arg)
            assert dispatched['result']['output'] == 'TASK_OPERATOR_REQUIRED', dispatched
            direct = await rpc('slash.exec', socket='second', command='/symphony ' + arg)
            assert direct['result']['output'] == 'TASK_OPERATOR_REQUIRED', direct
            results[verb] = {'command.dispatch': dispatched['result']['output'], 'slash.exec': direct['result']['output']}
        after = await peer.control(dict(op='counts'))
        for field in ('launches', 'decisions', 'permits', 'reservations', 'attempts', 'cancel_causes', 'executor_cancels'):
            assert before[field] == after[field]
        record_property('linux_offline', dict(admission=ack, same_id_replay=replay['admission_id'],
                        command_results=results, prior_to_decision=counts, before=before, after=after))
    finally:
        await peer.disconnect()
