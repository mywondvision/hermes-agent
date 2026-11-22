"""CR-V1 Windows. Synthetic input through the gateway of a disposable owner profile."""
import asyncio
from contextlib import closing
import importlib
import json
import re
import sqlite3

import pytest

from gateway.platforms.event import MessageType
from tests.plugins.symphony_typed_human_support import human_session


@pytest.mark.platforms("windows")
def test_spoken_and_unattributed_commands_cannot_decide_or_maintain(tmp_path):
    with human_session(tmp_path) as channel:
        outcome, shown = channel.request("voice-fixture")
        assert outcome == "WAITING_FOR_AUTHORITY"
        approve, = re.findall(r"/symphony approve [a-f0-9]+", shown)
        deny = approve.replace(" approve ", " deny ")
        privileged = (approve, deny, "/symphony apply fixture 1234", "/symphony release fixture reason",
                      "/symphony prune", "/symphony prune approve 1234")
        for command in privileged:
            assert channel.deliver(command, message_type=MessageType.VOICE) == "ORIGINAL_TYPED_COMMAND_REQUIRED"
            assert channel.deliver(command, with_raw=False) == "ORIGINAL_TYPED_COMMAND_REQUIRED"
        assert json.loads(channel.deliver("/symphony status", message_type=MessageType.VOICE)) == {"jobs": []}
        assert channel.deliver(deny) == "DENIED"
        assert channel.counts() == {"permits": 0, "reservations": 0, "launcher_calls": 0}
        audit = channel.owner.journal.path.with_name('typed-plan-audit.sqlite3')
        with closing(sqlite3.connect(audit.as_uri()+'?mode=ro', uri=True)) as db:
            records = db.execute('SELECT plan_digest,state FROM events ORDER BY rowid').fetchall()
        assert len(records) == 2 and records == [(records[0][0], 'WAITING'), (records[0][0], 'DENIED')]
        assert records[0][0].startswith(deny.split()[-1])


def voice_fixture(channel, monkeypatch, *, request_prefix='fixture-final-', concurrent=False):
    from gateway.session_context import clear_session_vars, set_session_vars
    from tests.plugins.test_symphony_pm_adapter import submit_payload
    from tests.plugins.test_symphony_pm_serving import _call

    async def model_turn(event, source, key, generation):
        entry = channel.runner.session_store.lookup_by_session_key(key)
        assert event.message_type is MessageType.VOICE and entry.session_id == channel.entry.session_id
        def tool_call(prefix):
            payload = submit_payload(request_id=prefix + event.message_id, project_ref='fixture',
                                     goal=event.text, raw_constraints=['Only the fixture launcher may run.'])
            tokens = set_session_vars(platform=source.platform.value, chat_id=source.chat_id,
                user_id=source.user_id, chat_type=source.chat_type, session_key=key, session_id=entry.session_id)
            try:
                return _call(payload, session_id=entry.session_id)
            finally:
                clear_session_vars(tokens)
        if concurrent:
            first, second = await asyncio.gather(asyncio.to_thread(tool_call, request_prefix),
                                                 asyncio.to_thread(tool_call, 'another-model-id-'))
            assert second['outcome'] == first['outcome'] == 'WAITING_FOR_AUTHORITY'
            return first
        return await asyncio.to_thread(tool_call, request_prefix)

    monkeypatch.setattr(channel.runner, '_handle_message_with_agent', model_turn)
    channel.runner._external_drain_active = False
    def deliver():
        return channel.deliver('Record the voice fixture task.', message_type=MessageType.VOICE,
                               message_id='2011', with_raw=False)
    return deliver


@pytest.mark.platforms("windows")
def test_synthetic_final_through_dispatch_and_same_session_status(tmp_path, monkeypatch):
    with human_session(tmp_path) as channel:
        deliver_final = voice_fixture(channel, monkeypatch)
        assert deliver_final()['outcome'] == 'WAITING_FOR_AUTHORITY'
        shown = list(channel.adapter.sent)
        assert deliver_final()['outcome'] == 'WAITING_FOR_AUTHORITY' and channel.adapter.sent == shown
        code, = re.findall(r'/symphony approve ([a-f0-9]+)', '\n'.join(shown))
        audit = channel.owner.journal.path.with_name('typed-plan-audit.sqlite3')
        with closing(sqlite3.connect(audit.as_uri()+'?mode=ro', uri=True)) as db:
            sid, request, fingerprint, state = db.execute(
                'SELECT session_id,request_id,plan_digest,state FROM events').fetchone()
            assert db.execute('SELECT COUNT(*) FROM events').fetchone() == (1,)
        assert (sid, request, fingerprint[:16], state) == (channel.entry.session_id, 'fixture-final-2011', code, 'WAITING')
        assert channel.counts() == {'permits': 0, 'reservations': 0, 'launcher_calls': 0}
        assert channel.deliver('/symphony approve '+code).startswith('APPROVED')
        result = deliver_final()
        assert result['outcome'] == 'SUBMITTED'
        assert channel.launcher.called.wait(5)
        job = result['view']['job_ref']
        status = json.loads(channel.deliver('/symphony status '+job, message_type=MessageType.VOICE))
        with closing(sqlite3.connect(channel.owner.journal.path.as_uri()+'?mode=ro', uri=True)) as db:
            ticket = json.loads(db.execute('SELECT ticket FROM serving_launches WHERE job=?', (job,)).fetchone()[0])
        assert status['view']['attempt_ref'] == ticket['attempt'] == channel.launcher.calls[0][0]
        assert channel.counts() == {'permits': 1, 'reservations': 1, 'launcher_calls': 1}


@pytest.mark.platforms("windows")
@pytest.mark.parametrize('cut', ['before_delivery', 'before_receipt', 'after_receipt', 'after_deny'])
def test_voice_claim_prevents_second_delivery_across_restart(tmp_path, monkeypatch, cut):
    from plugins.symphony_pm.transport.types import Refused
    def unavailable(*args, **kwargs):
        raise Refused('fixture interrupted')

    with human_session(tmp_path) as channel:
        deliver = voice_fixture(channel, monkeypatch, concurrent=True)
        with monkeypatch.context() as fault:
            if cut == 'before_delivery':
                module = importlib.import_module(type(channel.owner.typed_approval).__module__)
                fault.setattr(module, 'publish', unavailable)
            elif cut == 'before_receipt':
                fault.setattr(channel.owner.journal, 'record_plan_event', unavailable)
            assert deliver()['outcome'] == 'WAITING_FOR_AUTHORITY'
        prompts = re.findall(r'/symphony deny [a-f0-9]+', '\n'.join(channel.adapter.sent))
        assert len(prompts) == int(cut != 'before_delivery')
        if cut == 'after_deny':
            assert channel.deliver(prompts[0]) == 'DENIED'
    with human_session(tmp_path, restart=True) as restored:
        deliver = voice_fixture(restored, monkeypatch, request_prefix='different-after-restart-')
        assert deliver()['outcome'] == 'WAITING_FOR_AUTHORITY'
        assert not restored.adapter.sent
        audit = restored.owner.journal.path.with_name('typed-plan-audit.sqlite3')
        with closing(sqlite3.connect(audit.as_uri()+'?mode=ro', uri=True)) as db:
            rows = db.execute('SELECT message_id,session_id FROM voice_inputs').fetchall()
            assert rows == [('2011', restored.entry.session_id)]
        if prompts:
            assert restored.deliver(prompts[0].replace(' deny ', ' approve ')) == 'NO_MATCHING_TYPED_PLAN'
        assert restored.counts() == {'permits': 0, 'reservations': 0, 'launcher_calls': 0}


@pytest.mark.platforms("windows")
@pytest.mark.parametrize('committed', [False, True])
def test_deny_uncertain_commit_only_reads_back(tmp_path, monkeypatch, committed):
    from plugins.symphony_pm.transport.types import Refused
    with human_session(tmp_path) as channel:
        assert voice_fixture(channel, monkeypatch)()['outcome'] == 'WAITING_FOR_AUTHORITY'
        deny, = re.findall(r'/symphony deny [a-f0-9]+', '\n'.join(channel.adapter.sent))
        journal = channel.owner.journal
        record = journal.record_plan_event
        writes = []
        def uncertain(*args, **kwargs):
            writes.append(args[2])
            if committed:
                record(*args, **kwargs)
            raise Refused('STORE_OUTCOME_UNKNOWN')
        monkeypatch.setattr(journal, 'record_plan_event', uncertain)
        assert channel.deliver(deny) == ('DENIED' if committed else 'DENIAL_OUTCOME_UNKNOWN')
        if not committed:
            assert channel.deliver(deny.replace(' deny ', ' approve ')) == 'DENIAL_OUTCOME_UNKNOWN'
            assert channel.deliver(deny) == 'DENIAL_OUTCOME_UNKNOWN'
        assert writes == ['DENIED']
        assert channel.counts() == {'permits': 0, 'reservations': 0, 'launcher_calls': 0}
