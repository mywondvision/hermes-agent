"""Typed approval binds the displayed plan, the journal reserve, and the last native resume cut."""
from contextlib import closing, contextmanager
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
import asyncio
import gc
import json
from pathlib import Path
import re
import sqlite3
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.symphony_pm.contracts import LaunchExpired
from plugins.symphony_pm.transport.types import Refused, digest
from tests.plugins.symphony_serving_support import approve, rig
from tests.plugins.symphony_typed_human_support import human_session
from tests.plugins.test_symphony_pm_codex_exec import _git


def displayed(channel, action='approve'):
    status, messages = channel.request(action)
    assert status == 'WAITING_FOR_AUTHORITY'
    chunks = re.findall(r'\x60\x60\x60json\n(.*?)\n\x60\x60\x60', messages, re.DOTALL)
    body = json.loads(''.join(chunks))
    fingerprint, = re.findall(r'SHA-256 ([0-9a-f]{64})', messages)
    assert digest(body) == fingerprint
    return body, '/symphony ' + action + ' ' + fingerprint[:16]


def test_display_and_consumed_permit_match_one_ticket(tmp_path):
    with human_session(tmp_path) as channel:
        body, decision = displayed(channel)
        assert channel.request('approve') == ('WAITING_FOR_AUTHORITY', '')
        assert channel.request('deny') == ('WAITING_FOR_AUTHORITY', '')
        assert channel.counts() == {'permits': 0, 'reservations': 0, 'launcher_calls': 0}
        assert 'APPROVED' in channel.deliver(decision)
        assert 'APPROVED' not in channel.deliver(decision)
        assert channel.counts() == {'permits': 1, 'reservations': 0, 'launcher_calls': 0}
        assert channel.retry('approve') == 'SUBMITTED'
        assert channel.retry('approve') == 'SUBMITTED'  # the public adapter maps DUPLICATE to SUBMITTED
        with closing(sqlite3.connect(channel.owner.journal.path)) as db:
            raw, approval_digest, used = db.execute('SELECT body,approval_digest,used FROM serving_permits').fetchone()
            ticket = json.loads(db.execute('SELECT ticket FROM serving_launches').fetchone()[0])
        permit = json.loads(raw)
        assert permit['contract_digest'] == approval_digest == digest(body)
        assert permit['ref'] == ticket['permit_ref'] == body['permit_ref'] and used == 1
        assert ticket['plan'] == permit['plan']
        exact = json.loads(ticket['plan']['typed_details'])
        assert all(body[k] == v for k, v in exact.items())
        assert body['source_sha'] == ticket['plan']['source_sha'] and len(body['source_sha']) == 40
        assert body['identity'] == permit['identity'] and body['expires_at'] == permit['expires_at']
        assert body['raw_constraints'] and body['checks'] and body['exec_timeout'] == 30
        _, deny = displayed(channel, action='deny')
        assert channel.deliver(deny) == 'DENIED'
        assert channel.retry('approve') == 'SUBMITTED'
        assert channel.counts() == {'permits': 1, 'reservations': 1, 'launcher_calls': 1}


@pytest.mark.parametrize('after_decision', [False, True])
def test_branch_change_withholds_old_plan_at_decision_and_model_retry(tmp_path, after_decision):
    with human_session(tmp_path) as channel:
        _, decision = displayed(channel)
        if after_decision:
            assert 'APPROVED' in channel.deliver(decision)
        _git('commit', '--allow-empty', '-m', 'Next fixture revision', cwd=channel.launcher.repo)
        if not after_decision:
            assert channel.deliver(decision) == 'TYPED_PLAN_CHANGED'
        assert channel.retry('approve') == 'WAITING_FOR_AUTHORITY'
        assert channel.counts() == {'permits': int(after_decision), 'reservations': 0, 'launcher_calls': 0}


def test_legacy_writer_cannot_prompt_or_record_in_typed_profile(tmp_path):
    with human_session(tmp_path) as channel:
        _, decision = displayed(channel)
        authority = channel.owner.typed_approval
        item, = authority.slots.values()
        with pytest.raises(Refused, match='TYPED_OWNER_DECISION_REQUIRED'):
            authority.source.request_permit(item.request, item.permit)
        with pytest.raises(Refused, match='TYPED_OWNER_DECISION_REQUIRED'):
            channel.owner.record_permit(item.token, item.permit, 'legacy', digest('legacy'), channel.owner.clock())
        with pytest.raises(Refused, match='TYPED_OWNER_DECISION_REQUIRED'):
            channel.owner.journal.record_permit(item.token, item.permit, 'uncorrelated', digest('legacy'),
                                                channel.owner.clock(), writer=authority.writer)
        assert channel.counts()['permits'] == 0
        assert channel.deliver(decision.replace('approve', 'deny')) == 'DENIED'
        assert channel.request('approve') == ('WAITING_FOR_AUTHORITY', '')


def test_out_of_band_decisions_require_original_idle_dm(tmp_path, monkeypatch):
    from gateway.platforms.event import MessageEvent, MessageType
    from hermes_cli import plugins
    with human_session(tmp_path) as channel:
        _, command = displayed(channel)
        source, runner = channel.source, channel.runner
        raw = SimpleNamespace(content=command, id='witness-1', author=SimpleNamespace(id=source.user_id, bot=False),
                              channel=SimpleNamespace(id=source.chat_id), webhook_id=None, attachments=[],
                              message_snapshots=[], edited_at=None)
        event = MessageEvent(text=command, message_id=raw.id, source=source, raw_message=raw)
        for foreign in (replace(event, source=replace(source, user_id='stranger')),
                        replace(event, raw_message=None), replace(event, message_type=MessageType.VOICE),
                        replace(event, media_urls=['fixture.wav']), replace(event, edit_revision='2'),
                        replace(event, raw_message=SimpleNamespace(**dict(vars(raw), content='/alias')))):
            response = asyncio.run_coroutine_threadsafe(runner._handle_message(foreign), channel.loop).result(10)
            assert response in ('ORIGINAL_TYPED_COMMAND_REQUIRED', 'OWNER_SENDER_REQUIRED')
        assert channel.deliver(command[:-1] + ('0' if command[-1] != '0' else '1')) == 'NO_MATCHING_TYPED_PLAN'
        handler = plugins.get_plugin_command_handler('symphony')
        assert handler(command.split(' ', 1)[1], source=source, session_key=channel.entry.session_key,
                       session_id=channel.entry.session_id, platform='discord') == 'ORIGINAL_TYPED_COMMAND_REQUIRED'
        with monkeypatch.context() as m:
            m.setattr(runner.async_session_store, 'lookup_by_session_key',
                      AsyncMock(return_value=replace(channel.entry, session_id='unbound-conversation')))
            assert channel.deliver(command) == 'NO_MATCHING_TYPED_PLAN'
        with monkeypatch.context() as m:
            m.setattr(runner, '_is_session_running', lambda _: True)
            busy = AsyncMock(return_value='delivered to running turn')
            m.setattr(runner, '_hm_handle_running_session_message', busy)
            assert channel.deliver(command) == 'delivered to running turn'
            busy.assert_awaited_once()
        assert channel.counts()['permits'] == 0
        assert 'APPROVED' in channel.deliver(command)


def test_concurrent_opposite_decisions_share_one_slot(tmp_path):
    with human_session(tmp_path) as channel:
        _, command = displayed(channel)
        with ThreadPoolExecutor(2) as pool:
            replies = [pool.submit(copy_context().run, channel.deliver, choice)
                       for choice in (command, command.replace('approve', 'deny'))]
            outcomes = [r.result(20) for r in replies]
        assert outcomes.count('NO_MATCHING_TYPED_PLAN') == 1
        granted = any(s.startswith('APPROVED') for s in outcomes)
        assert channel.counts() == {'permits': int(granted), 'reservations': 0, 'launcher_calls': 0}
        assert channel.retry('approve') == ('SUBMITTED' if granted else 'WAITING_FOR_AUTHORITY')
        assert channel.counts()['launcher_calls'] == int(granted)


@pytest.mark.parametrize('failure', ['unavailable', 'truncated'])
def test_hidden_or_incomplete_plan_is_tombstoned(tmp_path, monkeypatch, failure):
    with human_session(tmp_path) as channel:
        with monkeypatch.context() as m:
            if failure == 'unavailable':
                m.setattr(channel.adapter, 'chat', 'offline')
            else:
                m.setattr(channel.adapter, 'max_message_length_for_chat', lambda _: 60)
            state, visible = channel.request('approve')
            assert state == 'WAITING_FOR_AUTHORITY' and '/symphony approve' not in visible
        assert channel.request('approve') == ('WAITING_FOR_AUTHORITY', '')
        assert channel.counts() == {'permits': 0, 'reservations': 0, 'launcher_calls': 0}


def test_expired_display_and_expired_issued_permit_cannot_launch(tmp_path, monkeypatch):
    with human_session(tmp_path) as channel:
        body, decision = displayed(channel)
        with monkeypatch.context() as m:
            m.setattr(channel.owner, 'clock', lambda: body['expires_at'])
            assert channel.deliver(decision) == 'NO_MATCHING_TYPED_PLAN'
        assert channel.retry('approve') == 'WAITING_FOR_AUTHORITY'
        body, decision = displayed(channel, action='deny')
        assert 'APPROVED' in channel.deliver(decision.replace('deny', 'approve'))
        monkeypatch.setattr(channel.owner, 'clock', lambda: body['expires_at'])
        assert channel.retry('deny') == 'WAITING_FOR_AUTHORITY'
        assert channel.counts() == {'permits': 1, 'reservations': 0, 'launcher_calls': 0}


def test_any_gateway_event_releases_expired_plan_from_old_conversation(tmp_path, monkeypatch):
    with human_session(tmp_path) as channel:
        body, command = displayed(channel)
        original = channel.entry.session_id
        retained = weakref.ref(channel.owner.typed_approval.slots[original])
        monkeypatch.setattr(channel.owner, 'clock', lambda: body['expires_at'])
        channel.entry.session_id = 'rotated-index'
        assert json.loads(channel.deliver('/symphony status')) == {'jobs': []}
        gc.collect()
        assert retained() is None
        channel.entry.session_id = original
        assert channel.deliver(command) == 'NO_MATCHING_TYPED_PLAN'
        assert channel.request('approve') == ('WAITING_FOR_AUTHORITY', '')


def test_request_budget_refuses_eviction_until_gateway_confirms_rotation(tmp_path, monkeypatch):
    from hermes_cli.lifecycle import invoke_hook
    from plugins.symphony_pm import typed_approval
    monkeypatch.setattr(typed_approval, 'MAX_REQUESTS_PER_CONVERSATION', 2)
    with human_session(tmp_path) as channel:
        _, stale = displayed(channel, 'deny')
        assert channel.deliver(stale) == 'DENIED'
        _, first = displayed(channel)
        assert 'APPROVED' in channel.deliver(first)
        assert channel.retry('approve') == 'SUBMITTED'
        assert channel.request('over-budget') == ('WAITING_FOR_AUTHORITY', '')
        assert channel.request('deny') == ('WAITING_FOR_AUTHORITY', '')
        indexed = channel.entry
        old, new = indexed.session_id, 'next-conversation'
        channel.entry = replace(indexed, session_id=new)
        assert channel.request('new') == ('WAITING_FOR_AUTHORITY', '')
        def reset(previous):
            invoke_hook('on_session_reset', platform='discord', reason='new_session',
                        old_session_id=previous, new_session_id=new, session_id=new)
        reset('foreign-conversation')
        assert channel.request('new') == ('WAITING_FOR_AUTHORITY', '')
        reset(old)
        assert channel.request('new') == ('WAITING_FOR_AUTHORITY', '')
        indexed.session_id = new
        assert channel.deliver(stale) == 'NO_MATCHING_TYPED_PLAN'
        assert '/symphony approve' in channel.request('new')[1]
        channel.entry = replace(indexed, session_id=old)
        assert channel.request('deny') == ('WAITING_FOR_AUTHORITY', '')
        assert channel.deliver(first) == 'NO_MATCHING_TYPED_PLAN'
        assert channel.retry('approve') == 'WAITING_FOR_AUTHORITY'


@pytest.mark.parametrize('cut', ['expiry', 'closing'])
def test_expiry_or_revocation_after_insert_rolls_back_permit(tmp_path, monkeypatch, cut):
    with human_session(tmp_path) as channel:
        body, command = displayed(channel)
        journal, scope = channel.owner.journal, channel.owner.scope
        clock = [channel.owner.clock()]
        monkeypatch.setattr(channel.owner, 'clock', lambda: clock[0])
        original = journal._connection
        inserted = []
        @contextmanager
        def cut_during_insert():
            with original() as db:
                def cut_before_commit(statement):
                    if statement.startswith('INSERT INTO serving_permits'):
                        inserted.append(True)
                        if cut == 'expiry':
                            clock[0] = body['expires_at']
                        else:
                            scope._close_admission()
                db.set_trace_callback(cut_before_commit)
                yield db
        monkeypatch.setattr(journal, '_connection', cut_during_insert)
        assert channel.deliver(command) == ('EXACT_PERMIT_REQUIRED' if cut == 'expiry' else 'WRITER_FENCED')
        assert inserted == [True]
        assert channel.counts() == {'permits': 0, 'reservations': 0, 'launcher_calls': 0}


def test_goal_constraints_and_runtime_session_cannot_borrow_grant(tmp_path):
    from gateway.session_context import clear_session_vars, set_session_vars
    from tests.plugins.test_symphony_pm_serving import _call
    with human_session(tmp_path) as channel:
        body, decision = displayed(channel)
        assert 'APPROVED' in channel.deliver(decision)
        payload = {k: body[k] for k in ('contract_version', 'operation', 'project_ref', 'request_id',
                                        'goal', 'raw_constraints')}
        route = body['route']
        session = body['identity']['session_id']
        tokens = set_session_vars(**route, session_id=session, chat_type='dm')
        try:
            for changed in (dict(goal='changed'), dict(raw_constraints=['changed'])):
                assert _call(dict(payload, **changed), session_id=session)['outcome'] == 'WAITING_FOR_AUTHORITY'
            assert _call(payload, session_id='different-session')['outcome'] == 'WAITING_FOR_AUTHORITY'
        finally:
            clear_session_vars(tokens)
        assert channel.counts()['reservations'] == 0
        assert channel.retry('approve') == 'SUBMITTED'


@pytest.mark.parametrize('approved', [False, True])
def test_restart_discards_pending_and_unconsumed_authority(tmp_path, approved):
    with human_session(tmp_path) as channel:
        _, stale = displayed(channel)
        if approved:
            assert 'APPROVED' in channel.deliver(stale)
    with human_session(tmp_path, restart=True) as restored:
        assert restored.deliver(stale) == 'NO_MATCHING_TYPED_PLAN'
        state, messages = restored.request('approve')
        assert state == 'WAITING_FOR_AUTHORITY'
        if approved:
            assert messages == ''  # do not reissue a previously granted request_id
        else:
            assert stale not in messages and '/symphony approve' in messages
        assert restored.deliver(stale) == 'NO_MATCHING_TYPED_PLAN'
        assert restored.counts() == {'permits': int(approved), 'reservations': 0, 'launcher_calls': 0}


@pytest.mark.platforms('windows')
def test_codex_launcher_uses_qualified_model_and_checks(tmp_path):
    from plugins.symphony_pm.transport.codex_exec_launcher import file_sha256
    from plugins.symphony_pm.transport.types import ExecutionPlan, TypedExecutionPlan
    from tests.plugins.symphony_owner_check_support import check_plan, owner_client
    from tests.plugins.test_symphony_pm_codex_exec import rig as client_rig
    r = client_rig(tmp_path)
    launcher = r.launcher
    q = launcher.qualification
    config = Path(q.codex_home)/'config.toml'
    def path_policy(paths):
        commands = check_plan(paths)
        return (replace(commands[0], name='other'),) if 'deferred.py' in paths else commands
    try:
        with owner_client(r, plan=path_policy):
            with pytest.raises(Refused, match='EXACT_EXECUTOR_SELECTION_REQUIRED'):
                launcher.typed_plan()
            config.write_text('model="base"\nprofile="pilot"\n[profiles.pilot]\nmodel="fixture-model"\n'
                              'model_reasoning_effort="high"\n', encoding='utf-8')
            with pytest.raises(Refused, match='CODEX_CONFIG_CHANGED'):
                launcher.typed_plan()
            launcher.qualification = replace(q, config_sha256=file_sha256(config))
            shown = launcher.typed_plan()
            assert (shown['configured_model'], shown['configured_effort']) == ('fixture-model', 'high')
            assert shown['executor_sha256'] == file_sha256(launcher.qualification.executable)
            assert shown['config_sha256'] == file_sha256(config)
            assert shown['executor_sha256'] != shown['config_sha256']
            fields = dict(model=shown['model'], effort=shown['effort'], environment_ref=shown['environment_ref'],
                          workspace=shown['workspace'], source_sha=r.source)
            typed = TypedExecutionPlan(**fields, typed_details=json.dumps(shown))
            ticket = SimpleNamespace(plan=typed, attempt='typed-fixture')
            settings = launcher._typed_settings(ticket)
            assert [c.name for c in settings.checks(('pkg/value.py',))] == [c['name'] for c in shown['checks']]
            with pytest.raises(Refused, match='TYPED_CHECK_PLAN_CHANGED'):
                settings.checks(('deferred.py',))
            data = tmp_path/'output'
            data.mkdir()
            workspace = SimpleNamespace(path=data, src=Path(shown['workspace']))
            argv = []
            def record(command, **kwargs):
                launcher.validate_call(ticket, command, kwargs['cwd'], kwargs['timeout'], kwargs['stdin_text'])
                argv.append(command)
                return SimpleNamespace(returncode=0, timed_out=False)
            launcher._exec(ticket, record, data, workspace, 'fixture')
            ticket.plan = ExecutionPlan(**fields)
            launcher._exec(ticket, record, data, workspace, 'fixture')
            assert argv[0][:-5] == argv[1][:-1]
            assert argv[0][-5:] == ['-c', 'model="fixture-model"', '-c', 'model_reasoning_effort="high"', '-']
            assert argv[1][-3:] == ['-c', 'default_permissions="symphony-pilot"', '-']
    finally:
        r.owner.close(0)


@pytest.mark.parametrize('cut', ['worker', 'created', 'resume'])
def test_expiry_records_not_started_without_entering_native_resume(tmp_path, monkeypatch, cut):
    r = rig(tmp_path)
    request, context, permit = approve(r)
    owner = r.owner
    try:
        ticket, _, _ = owner.journal.reserve_launch(owner.token, request, context, r.now[0])
        create = r.native.create_suspended
        def create_at_cut(*args):
            receiver = create(*args)
            if cut == 'created':
                r.now[0] = permit.expires_at
            if cut == 'resume':
                resume = receiver.resume
                def expire_then_resume(text, *, authorize):
                    r.now[0] = permit.expires_at
                    return resume(text, authorize=authorize)
                receiver.resume = expire_then_resume
            return receiver
        monkeypatch.setattr(r.native, 'create_suspended', create_at_cut)
        if cut == 'worker':
            r.now[0] = permit.expires_at
        with pytest.raises(LaunchExpired):
            owner.execute_reserved(owner.token, ticket, request)
        _, row = owner.journal.read_launch(owner.token, ticket.project, ticket.job, ticket.revision)
        assert row['state'] == 'NOT_STARTED'
        assert len(r.native.created) == int(cut != 'worker')
        assert all(not receiver.resumes and receiver.closes == 1 for receiver in r.native.created)
        assert owner.stop_by_owner(ticket.project, ticket.job, ticket.revision, request_id='stop').state == 'NOT_STARTED'
        with pytest.raises(Refused, match='LAUNCH_TICKET_SPENT'):
            owner.execute_reserved(owner.token, ticket, request)
    finally:
        owner.close(0)
