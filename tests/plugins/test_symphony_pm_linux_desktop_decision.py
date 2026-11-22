"""Offline Linux consumer of the shared Desktop plan decision. The viewer input is synthetic."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from contextvars import copy_context
from dataclasses import replace
import json
import sqlite3
import subprocess
from threading import Barrier

import pytest

from gateway.config import Platform
from gateway.session_context import clear_session_vars, set_session_vars
from gateway.session_ingress import viewer_admission_id, viewer_turn
from hermes_cli import plugins
from plugins.symphony_pm import typed_plan
from plugins.symphony_pm.desktop_approval import pending_plan, viewer_action
from plugins.symphony_pm.status_cli import read_launches
from plugins.symphony_pm.transport.types import Refused, digest
from tests.plugins.test_symphony_pm_codex_exec import _git
from tests.plugins.test_symphony_pm_linux_owner_check import checker_stand
from tests.plugins.test_symphony_pm_linux_typed_apply import linux_chat


def submit(channel, action, admission):
    live = channel.runner.session_authority.sessions[channel.source.chat_id]
    original_turn = viewer_turn.set(live)
    original_admission = viewer_admission_id.set(admission)
    try:
        return channel._submit(action)
    finally:
        viewer_admission_id.reset(original_admission)
        viewer_turn.reset(original_turn)


def counts(channel):
    with closing(sqlite3.connect(channel.owner.journal.path.as_uri() + '?mode=ro', uri=True)) as db:
        permits, = db.execute('SELECT count(*) FROM serving_permits').fetchone()
        launches, = db.execute('SELECT count(*) FROM serving_launches').fetchone()
    return permits, launches


def decision(channel, operation, reply, *, viewer='fixture-desktop', sid=None):
    from plugins.symphony_pm.desktop_approval import decide_presented
    session_id = sid or channel.entry.session_id
    tokens = set_session_vars(platform='local', chat_id=channel.source.chat_id, user_id=channel.source.user_id,
                              chat_type='dm', session_key=channel.entry.session_key, session_id=session_id)
    try:
        return decide_presented(channel.owner.typed_approval, session_id, reply, viewer=viewer, operation=operation)
    finally:
        clear_session_vars(tokens)


def test_only_a_correlated_viewer_admission_presents_the_linux_plan(linux_chat):
    stand, profile, native, _, _, session, _ = linux_chat
    with session(desktop=True) as channel:
        approval = channel.owner.typed_approval
        sid = channel.entry.session_id
        assert channel._submit('desktop') == 'WAITING_FOR_AUTHORITY'
        assert pending_plan(approval, sid) is None
        assert submit(channel, 'desktop', 'input-1') == 'WAITING_FOR_AUTHORITY'
        shown = pending_plan(approval, sid)
        assert shown['admission_id'] == 'input-1' and shown['request_id'] == 'qualification-desktop'
        assert digest(shown['plan']) == shown['digest']
        assert shown['plan']['source_sha'] == stand.source
        assert shown['plan']['check_policy_sha256'] == profile.fingerprint
        assert shown['plan']['identity']['namespace'] == channel.owner.scope.namespace
        assert counts(channel) == (0, 0) and not native.created


@pytest.mark.parametrize(('kind', 'answer', 'expected'), [
    ('text', ' Подтверждаю ', 'APPROVED'), ('voice', 'подтверждаю', 'APPROVED'), ('voice', 'отклоняю', 'DENIED'),
])
def test_linux_consumes_exact_desktop_decision_once(linux_chat, kind, answer, expected, record_property):
    _, _, native, finished, failures, session, _ = linux_chat
    with session(desktop=True) as channel:
        assert submit(channel, 'decision', 'original-request') == 'WAITING_FOR_AUTHORITY'
        pending = pending_plan(channel.owner.typed_approval, channel.entry.session_id)
        binding = dict(request_id=pending['request_id'], plan_digest=pending['digest'])
        reply = dict(binding, input_kind=kind, input_id='reply-id', text=answer)
        with pytest.raises(Refused):
            decision(channel, 'answer', reply)
        decision(channel, 'presented', binding)
        for changes in (dict(text='не подтверждаю'), dict(text='«подтверждаю»'), dict(text=''),
                        dict(input_kind='partial'), dict(plan_digest='f'*64), dict(request_id='other')):
            with pytest.raises(Refused):
                decision(channel, 'answer', dict(reply, **changes))
        with pytest.raises(Refused):
            decision(channel, 'answer', reply, viewer='other-view')
        with pytest.raises(Refused):
            decision(channel, 'answer', reply, sid='other-conversation')
        assert counts(channel) == (0, 0) and not native.created
        accepted = decision(channel, 'answer', reply)
        assert accepted['decision'] == expected and accepted['namespace'] == channel.owner.scope.namespace
        receipt = decision(channel, 'receipt', reply)
        assert receipt['shown_plan'] == pending['plan'] and digest(receipt['shown_plan']) == pending['digest']
        assert decision(channel, 'result', binding) == receipt
        with pytest.raises(Refused, match='DESKTOP_DECISION_UNKNOWN'):
            decision(channel, 'result', dict(binding, request_id='different-request'))
        assert receipt['input_id'] == reply['input_id'] and receipt['input_kind'] == kind
        assert receipt['plan_digest'] == pending['digest'] and receipt['request_id'] == pending['request_id']
        assert decision(channel, 'answer', reply) == receipt
        expected_count = int(expected == 'APPROVED')
        if expected_count:
            assert finished.wait(30) and not failures
            row, = read_launches(channel.owner.journal.path)
            ref = 'refs/heads/symphony/' + row['job']
            args = 'apply ' + row['job'] + ' ' + row['result']['patch']['sha256']
            command = plugins.get_plugin_command_handler('symphony')
            route = dict(session_key=channel.entry.session_key, session_id=channel.entry.session_id)
            assert command(args, source=channel.source, platform='local', **route) == 'ORIGINAL_TYPED_COMMAND_REQUIRED'
            discord = replace(channel.source, platform=Platform.DISCORD)
            assert command(args, source=discord, platform='discord', **route) == 'OWNER_SENDER_REQUIRED'
            assert not _git('for-each-ref', '--format=%(refname)', ref, cwd=channel.launcher.settings.repository)
        assert counts(channel) == (expected_count, expected_count) and len(native.created) == expected_count
        with pytest.raises(Refused):
            decision(channel, 'answer', dict(reply, input_id='other-id', input_kind='text', text='подтверждаю'))
        with pytest.raises(Refused):
            decision(channel, 'answer', dict(reply, input_id='apply-voice', text='применить', input_kind='voice'))
    with session(restart=True, desktop=True) as restarted:
        assert decision(restarted, 'result', binding) == receipt
        assert decision(restarted, 'receipt', reply) == receipt
        assert decision(restarted, 'answer', reply) == receipt
        assert counts(restarted) == (expected_count, expected_count) and len(native.created) == expected_count
        assert all(obs['kind'] != 'APPLY' for row in read_launches(restarted.owner.journal.path)
                   for obs in row['observations'])
        record_property('linux_offline', dict(request_id=receipt['request_id'], input_id=receipt['input_id'],
                        session_hash=digest(receipt['session_id']), plan_digest=receipt['plan_digest'], kind=kind,
                        result=expected, permits=expected_count, reservations=expected_count, launches=len(native.created),
                        restarted_readback=True, synthetic_viewer=True))


def test_linux_rechecks_executor_before_committing_and_consumes_one_racing_answer(linux_chat, monkeypatch,
                                                                                  record_property):
    stand, _, native, finished, failures, session, _ = linux_chat
    with session(desktop=True) as channel:
        assert submit(channel, 'race', 'first-request') == 'WAITING_FOR_AUTHORITY'
        shown = pending_plan(channel.owner.typed_approval, channel.entry.session_id)
        binding = dict(request_id=shown['request_id'], plan_digest=shown['digest'])
        decision(channel, 'presented', binding)
        text = dict(binding, input_kind='text', input_id='text-answer', text='подтверждаю')
        original = stand.launcher.typed_plan
        with monkeypatch.context() as changed:
            changed.setattr(stand.launcher, 'typed_plan', lambda: dict(original(), configured_effort='changed'))
            with pytest.raises(Refused, match='TYPED_PLAN_CHANGED'):
                decision(channel, 'answer', text)
        with pytest.raises(Refused):
            decision(channel, 'answer', text)
        with pytest.raises(Refused):
            decision(channel, 'presented', binding)
        assert submit(channel, 'race', 'new-input-old-request') == 'WAITING_FOR_AUTHORITY'
        assert pending_plan(channel.owner.typed_approval, channel.entry.session_id) is None
        assert counts(channel) == (0, 0) and not native.created
        invalidated = shown['digest']
        assert submit(channel, 'revised-race', 'new-request') == 'WAITING_FOR_AUTHORITY'
        shown = pending_plan(channel.owner.typed_approval, channel.entry.session_id)
        assert shown['digest'] != invalidated
        binding = dict(request_id=shown['request_id'], plan_digest=shown['digest'])
        text = dict(binding, input_kind='text', input_id='fresh-text-answer', text='подтверждаю')
        with pytest.raises(Refused):
            decision(channel, 'answer', text)
        decision(channel, 'presented', binding)
        together = Barrier(2)

        def answer(reply):
            together.wait(timeout=10)
            try:
                return decision(channel, 'answer', reply)
            except Refused:
                return None

        voice = dict(text, input_kind='voice', input_id='final-answer', text='отклоняю')
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(copy_context().run, answer, reply) for reply in (text, voice)]
            winner, = [result for future in futures if (result := future.result(timeout=30)) is not None]
        submitted = int(winner['decision'] == 'APPROVED')
        if submitted:
            assert finished.wait(30) and not failures
        result = decision(channel, 'result', binding)
        assert (result['input_id'], result['decision']) == (winner['input_id'], winner['decision'])
        assert result['shown_plan'] == shown['plan']
        assert counts(channel) == (submitted, submitted) and len(native.created) == submitted
        record_property('linux_offline', dict(request_id=shown['request_id'], plan_digest=shown['digest'],
                        input_id=winner['input_id'], session_hash=digest(channel.entry.session_id),
                        result=winner['decision'], decisions=1, races=2, permits=submitted, launches=len(native.created),
                        invalidated_digest=invalidated, changed_plan_permits=0, reused_plan_permits=0))


def test_linux_lost_handoff_and_restart_only_read_the_committed_decision(linux_chat, monkeypatch, record_property):
    _, _, native, _, _, session, _ = linux_chat
    with session(desktop=True) as channel:
        approval = channel.owner.typed_approval
        assert submit(channel, 'lost', 'original-input') == 'WAITING_FOR_AUTHORITY'
        shown = pending_plan(approval, channel.entry.session_id)
        binding = dict(request_id=shown['request_id'], plan_digest=shown['digest'])
        reply = dict(binding, input_id='lost-answer', input_kind='text', text='подтверждаю')
        decision(channel, 'presented', binding)

        def lost(*args):
            raise OSError('simulated interruption before reservation')

        with monkeypatch.context() as cut:
            cut.setattr(approval.binding.port, 'submit_intent', lost)
            with pytest.raises(OSError, match='simulated interruption'):
                decision(channel, 'answer', reply)
        assert counts(channel) == (1, 0) and not native.created
    with session(restart=True, desktop=True) as channel:
        receipt = decision(channel, 'receipt', reply)
        assert receipt['decision'] == 'APPROVED'
        assert decision(channel, 'answer', reply) == receipt
        assert submit(channel, 'lost', 'original-input') == 'WAITING_FOR_AUTHORITY'
        assert counts(channel) == (1, 0) and not native.created
        record_property('linux_offline', dict(request_id=receipt['request_id'], input_id=receipt['input_id'],
                        session_hash=digest(receipt['session_id']), plan_digest=receipt['plan_digest'],
                        result=receipt['decision'], permits=1, reservations=0, launches=0, repeat_handoffs=0))


@pytest.mark.parametrize('stored_format', ['legacy', 'mismatched'])
def test_linux_result_reads_the_persisted_receipt_after_upgrade(linux_chat, stored_format, record_property):
    _, _, native, _, _, session, _ = linux_chat
    with session(desktop=True) as channel:
        assert submit(channel, 'upgrade', 'original') == 'WAITING_FOR_AUTHORITY'
        shown = pending_plan(channel.owner.typed_approval, channel.entry.session_id)
        binding = dict(request_id=shown['request_id'], plan_digest=shown['digest'])
        decision(channel, 'presented', binding)
        reply = dict(binding, input_id='old-input', input_kind='text', text='отклоняю')
        stored = decision(channel, 'answer', reply)
        audit_path = channel.owner.journal.path.with_name('typed-plan-audit.sqlite3')
    if stored_format == 'legacy':
        del stored['shown_plan']
    else:
        stored['shown_plan'] = dict(shown['plan'], source_sha='e' * 40)
    # Reopen a temporary journal containing the on-disk format of the earlier release.
    with closing(sqlite3.connect(audit_path)) as db, db:
        assert db.execute('UPDATE desktop_plan_decisions SET receipt=? WHERE input_id=?',
                          (json.dumps(stored), reply['input_id'])).rowcount == 1
    with session(restart=True, desktop=True) as channel:
        for operation, query in [('result', binding), ('receipt', reply)]:
            if stored_format == 'legacy':
                assert decision(channel, operation, query) == stored
            else:
                with pytest.raises(Refused, match='DESKTOP_RECEIPT_PLAN_MISMATCH'):
                    decision(channel, operation, query)
        assert counts(channel) == (0, 0) and not native.created
        record_property('linux_offline', dict(request_id=shown['request_id'], plan_digest=shown['digest'],
                        input_id=reply['input_id'], stored_format=stored_format, permits=0, launches=0))


def test_lost_presentation_and_new_session_cannot_use_a_stale_linux_plan(linux_chat, record_property):
    _, _, native, _, _, session, _ = linux_chat
    with session(desktop=True) as channel:
        approval = channel.owner.typed_approval
        assert submit(channel, 'old', 'old-prompt') == 'WAITING_FOR_AUTHORITY'
        old = pending_plan(approval, channel.entry.session_id)
        reply = dict(plan_digest=old['digest'], request_id=old['request_id'], input_kind='voice',
                     input_id='voice-at-start', text='подтверждаю')
        decision(channel, 'presented', reply)
        # No verified native command frame accompanies a plugin or model call.
        with pytest.raises(Refused, match='DESKTOP_DECISION_ROUTE'):
            viewer_action(approval, channel.source, channel.entry.session_id, 'plan-answer {}')
    with session(restart=True, desktop=True) as channel:
        assert submit(channel, 'old', 'old-prompt') == 'WAITING_FOR_AUTHORITY'
        assert pending_plan(channel.owner.typed_approval, channel.entry.session_id) is None
        with pytest.raises(Refused, match='DESKTOP_DECISION_UNKNOWN'):
            decision(channel, 'receipt', reply)
        channel.entry.session_id = 'new-owner-session'
        assert submit(channel, 'new', 'new-prompt') == 'WAITING_FOR_AUTHORITY'
        new = pending_plan(channel.owner.typed_approval, channel.entry.session_id)
        assert old['digest'] != new['digest']
        with pytest.raises(Refused):
            decision(channel, 'answer', reply)
        assert counts(channel) == (0, 0) and not native.created
        record_property('linux_offline', dict(request_id=old['request_id'], input_id=reply['input_id'],
                        plan_digest=old['digest'], new_plan_digest=new['digest'],
                        session_hash=digest(channel.entry.session_id), permits=0, launches=0, unresolved=True))


@pytest.mark.parametrize('boundary', ['read', 'presented', 'answer'])
@pytest.mark.parametrize('fault', ['filesystem', 'timeout'])
def test_unverifiable_linux_plan_consumes_the_previous_presentation(linux_chat, monkeypatch, boundary, fault,
                                                                    record_property):
    _, _, native, _, _, session, _ = linux_chat
    with session(desktop=True) as channel:
        approval, sid = channel.owner.typed_approval, channel.entry.session_id
        assert submit(channel, 'unreadable', 'initial-prompt') == 'WAITING_FOR_AUTHORITY'
        shown = pending_plan(approval, sid)
        binding = dict(request_id=shown['request_id'], plan_digest=shown['digest'])
        decision(channel, 'presented', binding)
        answer = dict(binding, input_kind='text', input_id='old-answer', text='подтверждаю')

        def unreadable(*args):
            if fault == 'filesystem':
                raise OSError('fixture unavailable path')
            raise subprocess.TimeoutExpired('fixture git read', timeout=1)

        with monkeypatch.context() as cut:
            cut.setattr(typed_plan, 'branch_commit', unreadable)
            with pytest.raises(Refused, match='TYPED_PLAN_CHANGED'):
                if boundary == 'read':
                    pending_plan(approval, sid)
                else:
                    decision(channel, boundary, binding if boundary == 'presented' else answer)
        assert pending_plan(approval, sid) is None
        with pytest.raises(Refused):
            decision(channel, 'answer', answer)
        assert submit(channel, 'unreadable', 'redelivery') == 'WAITING_FOR_AUTHORITY'
        assert pending_plan(approval, sid) is None
        assert counts(channel) == (0, 0) and not native.created
        assert submit(channel, 'replacement', 'new-prompt') == 'WAITING_FOR_AUTHORITY'
        replacement = pending_plan(approval, sid)
        assert replacement['digest'] != binding['plan_digest']
        decision(channel, 'presented', dict(request_id=replacement['request_id'], plan_digest=replacement['digest']))
        record_property('linux_offline', dict(request_id=binding['request_id'], plan_digest=binding['plan_digest'],
                        session_hash=digest(sid), input_id='old-answer', boundary=boundary, fault=fault,
                        fresh_digest=replacement['digest'], old_permits=0, old_launches=0))


@pytest.mark.parametrize('fault', ['changed', 'filesystem', 'timeout'])
def test_linux_decision_and_permit_roll_back_together_at_commit(linux_chat, monkeypatch, fault, record_property):
    _, _, native, _, _, session, _ = linux_chat
    with session(desktop=True) as channel:
        approval = channel.owner.typed_approval
        assert submit(channel, 'commit', 'original-prompt') == 'WAITING_FOR_AUTHORITY'
        shown = pending_plan(approval, channel.entry.session_id)
        binding = dict(request_id=shown['request_id'], plan_digest=shown['digest'])
        decision(channel, 'presented', binding)
        answer = dict(binding, input_kind='text', input_id='commit-answer', text='подтверждаю')
        transact = channel.owner.journal._transaction
        before_commit = []

        def changed_read(*args):
            if fault == 'changed':
                return 'd' * 40
            if fault == 'filesystem':
                raise OSError('fixture path at commit')
            raise subprocess.TimeoutExpired('fixture git at commit', timeout=1)

        @contextmanager
        def at_commit(*args, authorize_commit=None):
            with monkeypatch.context() as cut:
                with transact(*args, authorize_commit=authorize_commit) as db:
                    yield db
                    if authorize_commit is not None:
                        before_commit.append(db.execute(
                            'SELECT (SELECT count(*) FROM serving_permits), '
                            '(SELECT count(*) FROM plan_audit.desktop_plan_decisions)').fetchone()[:])
                        cut.setattr(typed_plan, 'branch_commit', changed_read)

        with monkeypatch.context() as cut:
            cut.setattr(channel.owner.journal, '_transaction', at_commit)
            with pytest.raises(Refused):
                decision(channel, 'answer', answer)
        assert before_commit == [(1, 1)]
        assert counts(channel) == (0, 0) and not native.created
        with pytest.raises(Refused, match='DESKTOP_DECISION_UNKNOWN'):
            decision(channel, 'receipt', answer)
        with pytest.raises(Refused):
            decision(channel, 'answer', answer)
        record_property('linux_offline', dict(request_id=binding['request_id'], plan_digest=binding['plan_digest'],
                        session_hash=digest(channel.entry.session_id), input_id=answer['input_id'], boundary='commit',
                        fault=fault, staged_permits=1, staged_receipts=1, durable_permits=0, durable_receipts=0,
                        launches=0))
