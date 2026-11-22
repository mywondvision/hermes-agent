"""Run the Desktop OPERATOR wire against the real owner IPC with synthetic keys."""

import asyncio
import base64
import hashlib
import json
from collections import defaultdict
from contextlib import closing
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gateway.session_desktop_operator import Proof, payload_digest, signing_bytes
from hermes_state_runtime import list_session_admissions
from plugins.symphony_pm.contracts import Refused
from tests.plugins import test_symphony_pm_linux_typed_apply as linux_fixtures
from tests.plugins.symphony_operator_peer import OperatorPeer
from tests.plugins.test_symphony_pm_linux_owner_check import checker_stand
from tests.waiting import async_wait_until


linux_chat = linux_fixtures.linux_chat


def encoded(data):
    return base64.b64encode(data).decode('ascii')


def public(key):
    return encoded(key.public_key().public_bytes_raw())


def _run_recovery_peer(operation, data):
    # A grandchild holding one of these files does not keep a Windows pipe reader alive.
    # On timeout the child's faulthandler output remains available for diagnosis.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.run(
                [sys.executable, '-m', 'tests.plugins.symphony_operator_recovery_peer'],
                input=json.dumps(dict(operation=operation, **data)).encode('utf-8'),
                stdout=output, stderr=errors, timeout=100, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            errors.seek(0)
            raise AssertionError(f'{operation} timed out\n{errors.read().decode("utf-8", "replace")[-18000:]}') from exc
        errors.seek(0)
        assert proc.returncode == 0, errors.read().decode('utf-8', 'replace')[-12000:]
        output.seek(0)
        return json.loads(output.read())


async def run_recovery_peer(operation, **data):
    return await asyncio.to_thread(_run_recovery_peer, operation, data)


def cancel_causes(journal):
    with closing(sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True)) as db:
        return db.execute('SELECT COUNT(*) FROM serving_cancel_causes').fetchone()[0]


async def prepare_previous_reader(tmp_path, peer, ref, admission_id):
    from gateway.owner_identity import host_owner_subject
    from hermes_state import SessionDB

    # Run the predecessor's executable reader code on disposable copies. The child never opens
    # the original DB and emits only a visibility verdict, not the synthetic result or usage.
    preceding = '0849dce934f2efd44e1e173fcb3b1ce46ce4f439'
    source = tmp_path / 'reader'
    source.mkdir()
    for filename in ('session_authority.py', 'session_controls.py'):
        code = await asyncio.to_thread(subprocess.check_output, ['git', 'show', f'{preceding}:gateway/{filename}'])
        (source / filename).write_bytes(code)
    copy_home = tmp_path / 'copied-private'
    copy_home.mkdir()
    copied = copy_home / 'state.db'
    original = Path(peer.authority.db.db_path).resolve(strict=True)
    with closing(sqlite3.connect(original.as_uri() + '?mode=ro', uri=True)) as live, \
            closing(sqlite3.connect(copied)) as clone:
        live.backup(clone)
    empty_home = tmp_path / 'isolated-previous-profile'
    empty_home.mkdir()
    empty = empty_home / 'state.db'
    SessionDB(empty).close()
    return dict(**ref, reader=str(source), admission_id=admission_id, copied_private=str(copied),
                isolated_profile=str(empty), host_subject=host_owner_subject())


async def prepare_successor(peer, ref, command, receipt, reason, installation, window, *, window_generation=3,
                            expected_cancels=0, **extra):
    authority, owner = peer.authority, peer.channel.owner
    journal = owner.journal.path
    causes = cancel_causes(journal)
    previous = dict(home=ref['profile_id'], state_db=str(authority.db.db_path), namespace=owner.scope.namespace,
                    gateway_epoch=authority.epoch, gateway_instance=authority.instance_id,
                    symphony_epoch=owner.token.epoch, window_generation=window_generation, cancel_causes=causes)
    await peer.disconnect()
    assert native_cancels(peer) == expected_cancels and cancel_causes(journal) == causes
    return dict(**ref, **previous, submission_id='original', command=command, receipt=receipt, reason=reason,
                installation_seed=encoded(installation.private_bytes_raw()),
                window_seed=encoded(window.private_bytes_raw()), **extra)


def native_cancels(peer):
    return sum(receiver.cancels for receiver in peer.native.created)


def test_shared_wire_vector():
    path = Path(__file__).with_name('symphony_operator_v1_fixture.json')
    source = path.read_bytes()
    assert hashlib.sha256(source).hexdigest() == '8314e982cd0d9bb2b5e44fb0f93b95ca14c45751f8f2bd994cb160766b7dee6f'
    vector = json.loads(source)
    fields = tuple(vector['fields'])
    assert payload_digest(vector['payload']) == fields[13]
    assert encoded(signing_bytes(fields)) == vector['message']
    Proof(fields, ()).verify(vector)
    with pytest.raises(InvalidSignature):
        Proof(fields, ()).verify(dict(vector, window_signature=vector['installation_signature']))


def test_stop_followup_wire_vector():
    path = Path(__file__).with_name('symphony_operator_stop_fixture.json')
    source = path.read_bytes()
    assert hashlib.sha256(source).hexdigest() == 'b8cec2fb743ca701ff22e1e5db91394daa742c11b5a0c1b59f5fa10d977b1c4e'
    for vector in json.loads(source)['cases']:
        fields = tuple(vector['fields'])
        payload = vector['payload']
        assert payload_digest(payload, action=vector['action']) == fields[13]
        assert encoded(signing_bytes(fields)) == vector['message']
        Proof(fields, ()).verify(vector)
        for name, original in payload.items():
            changed = dict(payload, **{name: original + '-different'})
            changed_digest = payload_digest(changed, action=vector['action'])
            assert changed_digest != fields[13]
            with pytest.raises(InvalidSignature):
                Proof((*fields[:13], changed_digest, fields[14]), ()).verify(vector)


def test_explicit_stop_replacements_share_one_predecessor(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from gateway.session_contract import SessionRef
    from hermes_state import SessionDB
    from hermes_state_runtime import RuntimeStoreError
    from plugins.symphony_pm.desktop_operator_stop import StopRecords

    ref = SessionRef(str(tmp_path), 'fixture-session')
    grant = ('fixture-operator', 1)
    original = dict(command_id='old-stop', attempt_id='fixture-attempt', reason='fixture')

    def command(action, payload):
        fields = (None, None, action, None, None, None, None, None, 'fixture-submission', 'install', 'window')
        return StopRecords(ref, Proof(fields, grant), payload, lambda _conn: None)

    with closing(SessionDB(tmp_path / 'state.db')) as db:
        sealed, send = db._execute_write(command('stop_reconcile', original).accept)
        assert sealed['state'] == 'NOT_ACCEPTED' and not send
        ready = Barrier(2, timeout=10)

        def replace_once(command_id):
            followup = dict(original, command_id=command_id, previous_command_id='old-stop')
            ready.wait()
            try:
                result, should_send = db._execute_write(command('stop_replace', followup).accept)
            except RuntimeStoreError as exc:
                assert exc.reason == 'permission_denied'
                return None
            assert should_send and result['previous_command_id'] == 'old-stop'
            return command_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [value for value in pool.map(replace_once, ('one', 'two')) if value is not None]
        assert len(winners) == 1
        with db._read_ctx() as conn:
            records = command('stop_result', original)
            assert records.read(conn, 'old-stop')['replaced_by'] == winners[0]
            assert records.read(conn, 'one' if winners[0] == 'two' else 'two') is None


def test_operator_reads_the_shared_routing_index_without_its_writer_lock(tmp_path):
    from gateway.session_desktop_operator_store import routing_session_id
    from hermes_state import SessionDB

    route = 'agent:secondary:local:fixture'
    with closing(SessionDB(tmp_path / 'launch.db')) as routing, closing(SessionDB(tmp_path / 'profile.db')) as profile:
        routing.save_gateway_routing_entry(route, json.dumps(dict(session_id='correct')), scope='launch')
        profile.save_gateway_routing_entry(route, json.dumps(dict(session_id='wrong')), scope='launch')

        def lookup(conn):
            assert routing_session_id(conn, 'launch', route, separate_db=routing) == 'correct'
            assert routing_session_id(conn, 'another-scope', route, separate_db=routing) is None

        with routing._lock:
            profile._execute_write(lookup)


def test_task_peer_reports_one_profile_and_closes_database(tmp_path_factory, monkeypatch, record_property):
    root = tmp_path_factory.mktemp('stdio')
    monkeypatch.setenv('TMP', str(root))
    monkeypatch.setenv('TEMP', str(root))
    monkeypatch.setenv('TMPDIR', str(root))
    monkeypatch.setenv('UV_OFFLINE', '1')
    commands = [
        dict(id=1, op='init', public_key=public(Ed25519PrivateKey.generate()),
             operator_public_key=public(Ed25519PrivateKey.generate()), ssh_token=secrets.token_urlsafe(32),
             host_fingerprint='SHA256:' + 'A' * 43),
        dict(id=2, op='counts'), dict(id=3, op='worker', action='state'), dict(id=4, op='shutdown'),
    ]
    data = b''.join(json.dumps(command).encode('utf-8') + b'\n' for command in commands)
    with tempfile.TemporaryFile(dir=root) as output, tempfile.TemporaryFile(dir=root) as errors:
        result = subprocess.run([sys.executable, '-B', '-u', '-m', 'tests.plugins.symphony_operator_peer'],
                                input=data, stdout=output, stderr=errors, timeout=55, check=False)
        errors.seek(0)
        assert result.returncode == 0, errors.read().decode('utf-8', 'replace')[-12000:]
        output.seek(0)
        replies = [json.loads(line) for line in output]
    assert [reply['id'] for reply in replies] == [command['id'] for command in commands]
    manifest, counts, worker, shutdown = [reply['result'] for reply in replies]
    assert manifest['forward_mode'] == 'personal' and manifest['profile'] == manifest['profile_id']
    profile = Path(manifest['profile_id'])
    assert profile.is_relative_to(root) and not profile.exists()
    assert counts['admissions'] == counts['requests'] == counts['stop_receipts'] == []
    assert counts['launches'] == counts['stop_commands'] == counts['executor_cancels'] == 0
    assert worker == {'state': 'idle'} and shutdown is True
    record_property('linux_offline', dict(bootstrap='stdio', same_canonical_profile=True, forward_mode='personal',
                                        profile_removed=True, subprocess_exit=result.returncode))


def test_disconnect_while_resolving_operator_socket(tmp_path, monkeypatch):
    from tui_gateway.prompt_forward import ForwardClient, ForwardHeld

    client = ForwardClient(tmp_path)

    def close_during_resolution(sid, transport):
        client.close_operator()
        return tmp_path, 'old-owner', {'profile_id': str(tmp_path), 'session_id': sid}

    monkeypatch.setattr(client, '_target', close_during_resolution)
    monkeypatch.setattr(client, '_call', lambda *_args, **_kwargs: pytest.fail('forwarded on a closed socket'))
    with pytest.raises(ForwardHeld):
        client.operator('operator-challenge', {'session_id': 'chat'}, object())


@pytest.mark.asyncio
async def test_operator_submit_replay_and_recover(linux_chat, monkeypatch, record_property, tmp_path):
    observed = await operator_roundtrip(linux_chat, monkeypatch, tmp_path, revoke_before_cancel=False)
    record_property('linux_offline', observed)


@pytest.mark.asyncio
async def test_operator_reconciles_lost_stop_before_explicit_replacement(linux_chat, monkeypatch, record_property, tmp_path):
    observed = await operator_roundtrip(linux_chat, monkeypatch, tmp_path, revoke_before_cancel=False, followup='replace')
    record_property('linux_offline', observed)


@pytest.mark.asyncio
async def test_operator_holds_completed_attempt_while_next_one_runs(linux_chat, monkeypatch, record_property, tmp_path):
    observed = await operator_roundtrip(linux_chat, monkeypatch, tmp_path, revoke_before_cancel=False, followup='stale')
    record_property('linux_offline', observed)


@pytest.mark.asyncio
async def test_operator_reports_worker_refusal_before_cleanup(linux_chat, monkeypatch, record_property, tmp_path):
    observed = await operator_roundtrip(linux_chat, monkeypatch, tmp_path, revoke_before_cancel=False,
                                        followup='worker-refusal')
    record_property('linux_offline', observed)


@pytest.mark.asyncio
@pytest.mark.parametrize('order', ['natural', 'settlement-held', 'settled', 'grant-missing'])
async def test_operator_diagnoses_launch_after_terminal_a(linux_chat, monkeypatch, record_property, tmp_path, order):
    observed = await operator_roundtrip(linux_chat, monkeypatch, tmp_path, revoke_before_cancel=False, followup=order)
    record_property('linux_offline', observed)


@pytest.mark.asyncio
async def test_restarted_owner_reads_revoked_stop(tmp_path_factory, record_property):
    root = tmp_path_factory.mktemp('op')  # Leave space for the Git shadow's nested paths on Windows.
    first = await run_recovery_peer('first_owner', scratch=str(root))
    # subprocess.run reaped the first process. The successor opens its unchanged state DB.
    successor = await run_recovery_peer('restart', **first['successor'])
    assert successor['pid'] != first['pid']
    assert successor['repeat_dispatches'] == successor['workload_creates'] == 0
    rollback = await run_recovery_peer('previous_reader', **first['previous_reader'])
    assert rollback['copied_private'] == dict(admission_visible=True, result_visible=True,
                                              usage_visible=True, reason=None), rollback
    assert rollback['isolated_profile'] == dict(admission_visible=False, result_visible=False,
                                                usage_visible=False, reason='not_found'), rollback
    record_property('linux_offline', dict(
        old_pid=first['pid'], old_gateway_epoch=first['successor']['gateway_epoch'],
        old_symphony_epoch=first['successor']['symphony_epoch'], restart=successor, previous_reader=rollback,
    ))


@pytest.mark.asyncio
async def test_restarted_owner_preserves_stop_reconciliation(tmp_path_factory, record_property):
    root = tmp_path_factory.mktemp('op')
    first = await run_recovery_peer('first_owner', scratch=str(root), followup='restart-replace')
    successor = await run_recovery_peer('restart', **first['successor'])
    assert successor['pid'] != first['pid']
    assert successor['old_stop'] == first['old'] and successor['stop'] == first['replacement']
    assert successor['no_witness'] == 'UNKNOWN'
    assert successor['repeat_dispatches'] == successor['workload_creates'] == 0
    record_property('linux_offline', dict(restart=successor, old_pid=first['pid']))


async def lost_stop_replacement(peer, request, lose_reply, execute, early_proof, old, stop, loss):
    old_target = {key: old[key] for key in ('attempt_id', 'command_id')}
    unknown = (await request('stop_reconcile', dict(old, command_id='without-owner-witness')))['result']
    assert unknown['state'] == 'UNKNOWN'
    replacement = dict(stop, previous_command_id=old['command_id'])
    assert (await request('stop_replace', replacement))['error']['message'] == 'OPERATOR_HOLD'

    assert (await lose_reply('stop_reconcile', old))['error']['message'] == 'OPERATOR_UNKNOWN'
    sealed = (await request('stop_result', old_target))['result']
    assert sealed == dict(old_target, state='NOT_ACCEPTED', dispatched=False, local_stopped=False)
    assert loss == sealed
    before = await peer.control(dict(op='counts'))
    assert before['stop_commands'] == before['cancel_causes'] == before['executor_cancels'] == 0
    assert before['stop_reconciliations'] == [dict(old_target, state='NOT_ACCEPTED')]
    assert (await execute('recovered', early_proof))['result'] == sealed
    assert (await request('stop_reconcile', old))['result'] == sealed

    assert (await lose_reply('stop_replace', replacement))['error']['message'] == 'OPERATOR_UNKNOWN'
    new_target = {key: replacement[key] for key in ('attempt_id', 'command_id')}
    saved = (await request('stop_result', new_target))['result']
    assert saved == loss == dict(new_target, previous_command_id=old['command_id'],
                                 state='CANCEL_REQUESTED', dispatched=True, local_stopped=False)
    assert (await request('stop_replace', replacement))['result'] == saved
    assert (await request('stop_replace', dict(replacement, command_id='conflicting-replacement')))['error']['message'] == 'OPERATOR_HOLD'
    assert (await request('stop_reconcile', stop))['result'] == saved
    assert (await request('stop_result', old_target))['result'] == sealed
    after = await peer.control(dict(op='counts'))
    assert after['stop_commands'] == after['cancel_causes'] == after['executor_cancels'] == 1
    assert after['stop_reconciliations'] == before['stop_reconciliations']
    return dict(old=sealed, replacement=saved, stop_commands=after['stop_commands'], cancel_causes=after['cancel_causes'],
                executor_cancels=after['executor_cancels'], no_witness=unknown['state'], delayed_stop='NOT_ACCEPTED')


async def stale_attempt_control(peer, approve, connect, signed, execute, finished, failures, second_key):
    plan_a = await approve('operator-confirm-old')
    assert await asyncio.to_thread(finished.wait, 35) and not failures, failures
    old = (await execute('recovered', await signed('recovered', 'receipt', 2)))['result']
    old_target = dict(attempt_id=old['attempt']['attempt_id'])
    terminal = await execute('recovered', await signed('recovered', 'status', 2, old_target))
    assert terminal['result']['phase'] in ('COMPLETED', 'STOPPED'), terminal
    await async_wait_until(lambda: peer.worker_outcomes[old_target['attempt_id']]['state'] == 'finished',
                           what='fixture worker returned for terminal A')
    finished_a = (await peer.control(dict(op='counts')))['worker_diagnostics']
    assert finished_a['finished_event'] and finished_a['state'] == 'idle'
    assert finished_a['outcomes'] == [dict(**old_target, state='finished', error_type=None, error_reason=None)]
    assert finished_a['plan_answers'][0]['request_id'] == plan_a['request_id']

    await connect('next-owner')

    async def next_request(action, payload=None, generation=1):
        proof = await signed('next-owner', action, generation, payload, key=second_key, submission_id='new-work')
        return await execute('next-owner', proof)

    new = (await next_request('submit', dict(text='Inspect the next offline fixture.'), generation=0))['result']
    assert new['admission_id'] != old['admission_id']
    await async_wait_until(lambda: len(peer.requests) == 2, timeout=15, what='second registered Symphony request')
    finished.clear()
    await peer.control(dict(op='worker', action='arm'))
    old_stop = dict(old_target, command_id='operator-stale-old', reason='the completed attempt')
    delayed_old_stop = await signed('recovered', 'stop', 2, old_stop)
    plan_b = await approve('operator-confirm-new', socket='plan-next')
    assert await asyncio.to_thread(peer.worker_waiting.wait, 15), (
        await peer.control(dict(op='counts')))['worker_diagnostics']
    current = (await next_request('receipt'))['result']['attempt']
    assert current['attempt_id'] != old_target['attempt_id']
    before = await peer.control(dict(op='counts'))
    assert before['launches'] == 2 and before['stop_commands'] == 0
    running_b = before['worker_diagnostics']
    assert running_b['state'] == 'held' and not running_b['finished_event']
    assert running_b['outcomes'][-1] == dict(attempt_id=current['attempt_id'], state='running',
                                           error_type=None, error_reason=None)
    assert running_b['plan_answers'][-1] == dict(request_id=plan_b['request_id'], decision='APPROVED',
                                                launch='SUBMITTED', job=current['job'])
    launch_b = next(row for row in running_b['launches'] if row['request_id'] == plan_b['request_id'])
    assert launch_b['attempt_id'] == current['attempt_id'] and launch_b['hold_reason'] is None

    stopped = await execute('recovered', delayed_old_stop)
    assert stopped['error']['message'] == 'OPERATOR_HOLD'
    assert (await next_request('stop', old_stop))['error']['message'] == 'OPERATOR_HOLD'
    assert (await next_request('status', old_target))['error']['message'] == 'OPERATOR_HOLD'
    still_running = (await next_request('status', dict(attempt_id=current['attempt_id'])))['result']
    assert still_running['attempt_id'] == current['attempt_id'] and still_running['phase'] == 'RUNNING'
    after = await peer.control(dict(op='counts'))
    assert before['stale_stop_targets'] == 0 and after['stale_stop_targets'] == 1
    for name in ('stop_commands', 'cancel_causes', 'executor_cancels'):
        assert before[name] == after[name] == 0
    await peer.control(dict(op='worker', action='release'))
    assert await asyncio.to_thread(finished.wait, 35) and not failures, failures
    return dict(old_attempt=old['attempt'], new_attempt=current, stale_stop_targets=after['stale_stop_targets'],
                status=still_running, stop_commands=after['stop_commands'], cancel_causes=after['cancel_causes'],
                executor_cancels=after['executor_cancels'], finished_a=finished_a, running_b=running_b)


async def observe_worker_refusal(peer, approve, monkeypatch):
    def refuse(*_args):
        raise Refused('CODEX_PRECHECK_REFUSED')

    monkeypatch.setattr(peer.native, 'validate_driver_call', refuse)
    assert await peer.control(dict(op='worker', action='arm')) == {'state': 'armed'}
    plan = await approve('operator-fixture-refusal')
    await async_wait_until(lambda: any(row['state'] == 'failed' for row in tuple(peer.worker_outcomes.values())),
                           timeout=15, what='fixture worker refusal')
    counts = await peer.control(dict(op='counts'))
    evidence = counts['worker_diagnostics']
    row, = evidence['launches']
    assert evidence['finished_event'] and evidence['state'] == 'armed' and counts['launches'] == 0
    assert row == dict(request_id=plan['request_id'], attempt_id=row['attempt_id'], state='NOT_STARTED',
                       hold_reason='WORKSPACE_HELD')
    assert evidence['outcomes'] == [dict(attempt_id=row['attempt_id'], state='failed', error_type='Refused',
                                        error_reason='CLIENT_NOT_STARTED')]
    answer, = evidence['plan_answers']
    assert answer['request_id'] == plan['request_id'] and answer['launch'] == 'SUBMITTED'
    return dict(worker_diagnostics=evidence, launched=counts['launches'])


async def terminal_a(execute, signed, target):
    async with asyncio.timeout(15):
        while True:
            status = await execute('recovered', await signed('recovered', 'status', 2, target))
            assert 'result' in status, status
            if status['result']['phase'] in ('STOPPED', 'COMPLETED'):
                return status['result']['phase']


async def wait_for_joint_a_settled(peer, attempt_id, request_id):
    async with asyncio.timeout(25):
        while True:
            evidence = (await peer.control(dict(op='counts')))['worker_diagnostics']
            outcome = next((row for row in evidence['outcomes'] if row['attempt_id'] == attempt_id), None)
            launch = next((row for row in evidence['launches']
                           if row['attempt_id'] == attempt_id and row['request_id'] == request_id), None)
            if outcome and outcome['state'] == 'finished' and launch and launch['hold_reason'] is None:
                return dict(outcome=outcome, launch=launch)


async def observe_joint_b_held(peer, signed, execute, second_key, old_stop):
    assert await asyncio.to_thread(peer.worker_waiting.wait, 15)
    assert (await peer.control(dict(op='worker', action='state')))['state'] == 'held'
    before = await peer.control(dict(op='counts'))
    assert before['launches'] == before['reservations'] == 2
    assert (await execute('recovered', old_stop))['error']['message'] == 'OPERATOR_HOLD'
    target_b = {'attempt_id': before['attempts'][-1]['attempt_id']}
    status_b = await signed('next-owner', 'status', 1, target_b, key=second_key, submission_id='joint-b')
    assert (await execute('next-owner', status_b))['result']['phase'] == 'RUNNING'
    after = await peer.control(dict(op='counts'))
    assert after['stale_stop_targets'] - before['stale_stop_targets'] == 1
    deltas = {name: after[name] - before[name] for name in ('stop_commands', 'cancel_causes', 'executor_cancels')}
    assert not any(deltas.values())
    await peer.control(dict(op='worker', action='release'))
    return dict(worker='held', phase='RUNNING', launches=after['launches'], stale_stop_targets=1, deltas=deltas)


async def observe_joint_launch_order(peer, connect, signed, execute, decide, monkeypatch, second_key, order):
    owner = peer.channel.owner
    resume_a = Event()
    if order == 'settlement-held':
        finish_client = owner.finish_client_job

        def before_settlement(token, ticket):
            result = finish_client(token, ticket)
            assert resume_a.wait(35), 'test did not release workspace settlement'
            return result

        monkeypatch.setattr(owner, 'finish_client_job', before_settlement)

    try:
        await connect('a')
        submit_a = await signed('a', 'submit', 0, dict(text='Exercise original TASK A.'))
        accepted_a = (await execute('a', submit_a))['result']
        await peer.control(dict(op='worker', action='arm'))
        await async_wait_until(lambda: bool(peer.requests), timeout=15, what='original plan A')
        _, answer_a = await decide('joint-a', request_id='task-' + accepted_a['admission_id'])
        assert answer_a['launch'] == 'SUBMITTED', answer_a
        assert await asyncio.to_thread(peer.worker_waiting.wait, 15)
        await peer.control(dict(op='worker', action='release'))

        await connect('recovered')
        recovered = await execute('recovered', await signed('recovered', 'recover', 1))
        assert recovered['result']['window_generation'] == 2
        receipt_a = (await execute('recovered', await signed('recovered', 'receipt', 2)))['result']
        target = dict(attempt_id=receipt_a['attempt']['attempt_id'])
        phase = await terminal_a(execute, signed, target)
        finished_at_terminal = peer.finished.is_set()
        settled_a = None
        if order == 'settled':
            settled_a = await wait_for_joint_a_settled(peer, target['attempt_id'], 'task-' + accepted_a['admission_id'])

        await connect('next-owner')
        submit_b = await signed('next-owner', 'submit', 0, dict(text='Exercise next TASK B.'),
                                key=second_key, submission_id='joint-b')
        accepted_b = (await execute('next-owner', submit_b))['result']
        request_b = 'task-' + accepted_b['admission_id']
        await peer.control(dict(op='worker', action='arm'))
        await async_wait_until(lambda: len(peer.requests) == 2, timeout=15, what='joint-order plan B')
        old_stop = await signed('recovered', 'stop', 2, dict(target, command_id='joint-a-stop', reason='stale A'))
        if order == 'grant-missing':
            find = owner.journal.find_permit

            def find_permit(token, identity, now):
                if identity.request_id == request_b:
                    raise Refused('EXACT_PERMIT_NOT_FOUND')
                return find(token, identity, now)

            monkeypatch.setattr(owner.journal, 'find_permit', find_permit)
        binding, answer_b = await decide('joint-b', socket='plan-next', request_id=request_b)
        counts = await peer.control(dict(op='counts'))
        diagnostics = counts['worker_diagnostics']
        handoff_b = next(row for row in diagnostics['launch_handoffs'] if row['request_id'] == request_b)
        assert counts['decisions'] == counts['permits'] == 2
        assert counts['by_request'][request_b]['plan_digest'] == binding['plan_digest']
        if order == 'settlement-held':
            assert answer_b['launch'] == 'UNKNOWN' and not finished_at_terminal
            assert handoff_b == dict(request_id=request_b, grant_outcome='ISSUED', port_outcome='UNKNOWN',
                                     uncertainty=['WORKSPACE_HELD'])
            assert 'job' in answer_b and not answer_b['job']
        if order == 'grant-missing':
            assert answer_b['launch'] == 'UNKNOWN' and 'job' not in answer_b
            assert handoff_b == dict(request_id=request_b, grant_outcome='MISSING', port_outcome=None, uncertainty=[])
        if order == 'settled':
            assert answer_b['launch'] == 'SUBMITTED' and answer_b['job']
            assert handoff_b == dict(request_id=request_b, grant_outcome='ISSUED', port_outcome='SUBMITTED', uncertainty=[])
        running_b = None
        if answer_b['launch'] == 'UNKNOWN':
            assert counts['launches'] == counts['reservations'] == 1 and diagnostics['state'] == 'armed'
            assert all(row['request_id'] != request_b for row in diagnostics['launches'])
        else:
            assert answer_b['launch'] == 'SUBMITTED', answer_b
            running_b = await observe_joint_b_held(peer, signed, execute, second_key, old_stop)
        return dict(order=order, phase_a=phase, finished_a_at_terminal=finished_at_terminal,
                    handoff_b=handoff_b, launch_b=answer_b['launch'], job_field_b='job' in answer_b, worker=diagnostics,
                    settled_a_before_b=settled_a, running_b=running_b,
                    counts={key: counts[key] for key in ('permits', 'reservations', 'launches')})
    finally:
        resume_a.set()


async def operator_roundtrip(linux_chat, monkeypatch, tmp_path, *, revoke_before_cancel, followup=None,
                             desktop_result=None):
    stand, _, native, finished, failures, session, _ = linux_chat
    inbox = defaultdict(asyncio.Queue)
    peer = OperatorPeer(session, stand, native, finished, failures,
                        lambda frame: inbox[frame['socket']].put_nowait(frame['frame']),
                        result_rights=('apply', 'archive', 'apply_result') if desktop_result else ())
    installation, window, other_window = (Ed25519PrivateKey.generate() for _ in range(3))
    route_key = Ed25519PrivateKey.generate()
    token = secrets.token_urlsafe(32)
    serial = 0
    attached = {}
    disconnected = False
    at_cancel, allow_cancel = Event(), Event()
    create = native.create_suspended

    def controlled_create(*args):
        receiver = create(*args)
        original_cancel_once = receiver.cancel_once

        def cancel_once(binding, *, authorize):
            if revoke_before_cancel:
                at_cancel.set()
                assert allow_cancel.wait(20), 'fixture did not release the cancel boundary'
            return original_cancel_once(binding, authorize=authorize)

        receiver.cancel_once = cancel_once
        return receiver

    monkeypatch.setattr(native, 'create_suspended', controlled_create)

    async def rpc(socket, method, **params):
        nonlocal serial
        serial += 1
        rid = serial
        await peer.control(dict(op='frame', socket=socket, frame=dict(
            id=rid, jsonrpc='2.0', method=method, params=params)))
        while True:
            frame = await asyncio.wait_for(inbox[socket].get(), 20)
            if frame.get('id') == rid:
                return frame

    async def connect(socket):
        opened = await peer.control(dict(op='open', channel='ordinary', socket=socket, path='/api/ws',
                                         query='token=' + token, subprotocols=[]))
        assert opened['authority_bound'] is False
        reply = await rpc(socket, 'session.resume', session_id=ref['session_id'], lazy=True)
        assert 'result' in reply, (socket, reply.get('error'))
        assert reply['result']['session_key'] == ref['session_id']
        attached[socket] = reply['result']['session_id']

    async def signed(socket, action, generation, payload=None, *, key=window, submission_id='original'):
        payload = {} if payload is None else payload
        response = await rpc(socket, 'operator.challenge', session_id=attached[socket], action=action,
                             submission_id=submission_id, installation_key=public(installation),
                             window_key=public(key), window_generation=generation, payload_sha256=payload_digest(payload, action=action))
        assert 'result' in response, response
        challenge = response['result']
        assert challenge['ref'] == ref and challenge['window_generation'] == generation
        message = base64.b64decode(challenge['message'])
        return dict(challenge_id=challenge['challenge_id'], payload=payload,
                    installation_signature=encoded(installation.sign(message)), window_signature=encoded(key.sign(message)))

    async def execute(socket, proof):
        return await rpc(socket, 'operator.execute', session_id=attached[socket], **proof)

    async def decide(input_id, *, socket='plan', request_id=None):
        if socket not in peer.sockets:
            await peer.control(dict(op='open', socket=socket))
            route = (await rpc(socket, 'route.challenge', client_id=manifest['client_id'],
                               connection_id=manifest['connection_id']))['result']['fields']
            authenticated = await rpc(socket, 'route.authenticate', signature=encoded(route_key.sign(signing_bytes(route))))
            assert authenticated['result']['state'] == 'READY'

        async def plan(command):
            frame = await rpc(socket, 'command.dispatch', session_id=ref['session_id'], name='symphony',
                              interaction='displayed-plan', arg=command)
            return json.loads(frame['result']['output'])

        pending = (await plan('status'))['pending_plan']
        shown = dict(request_id=pending['request_id'], plan_digest=pending['digest'])
        assert request_id is None or shown['request_id'] == request_id
        await plan('plan-presented ' + json.dumps(shown))
        accepted_plan = dict(shown, input_id=input_id, input_kind='text', text='подтверждаю')
        answer = await plan('plan-answer ' + json.dumps(accepted_plan, ensure_ascii=False))
        assert answer['decision'] == 'APPROVED', answer
        return shown, answer

    try:
        from tui_gateway import prompt_forward

        config = dict(op='init', public_key=public(route_key), ssh_token=token, host_fingerprint='SHA256:' + 'A' * 43)
        with pytest.raises(ValueError, match='separate ROUTE and OPERATOR'):
            await peer.control(dict(config, operator_public_key=public(route_key)))
        assert peer.channel is None
        manifest = await peer.control(dict(config, operator_public_key=public(installation)))
        assert manifest['forward_mode'] == 'personal'
        ref = dict(profile_id=manifest['profile_id'], session_id=manifest['conversation_id'])
        config_path = Path(ref['profile_id']) / 'config.yaml'
        config = json.loads(config_path.read_text('utf-8'))
        settings = config['plugins']['entries']['symphony_pm']['settings']
        counts = await peer.control(dict(op='counts'))
        assert counts['admissions'] == counts['requests'] == counts['stop_receipts'] == []
        assert counts['decisions'] == counts['stop_commands'] == counts['launches'] == 0
        if followup in ('natural', 'settlement-held', 'settled', 'grant-missing'):
            return await observe_joint_launch_order(peer, connect, signed, execute, decide, monkeypatch, other_window,
                                                   followup)
        await connect('a')
        await connect('b')
        probe = (await rpc('a', 'prompt.forward_probe', session_id=attached['a']))['result']
        assert probe['mode'] == 'personal' and probe['route'] == 'forward'
        shared = await rpc('a', 'prompt.forward_submit', session_id=attached['a'], ref=ref,
                           generation=probe['generation'], submission_id='bypass', text='No signature')
        assert shared['error']['message'] == 'FORWARD_HOLD'
        assert not list_session_admissions(peer.authority.db, session_id=ref['session_id'])

        payload = dict(text='Inspect the offline fixture.')
        first = await signed('a', 'submit', 0, payload)
        assert (await execute('b', first))['error']['message'] == 'OPERATOR_HOLD'
        query = prompt_forward.private_control_query
        accepted = {}

        def lose_first_ack(home, raw, timeout):
            reply = query(home, raw, timeout)
            if json.loads(raw)['params'].get('challenge_id') == first['challenge_id']:
                accepted.update(json.loads(reply)['result'])
                raise ConnectionError('fixture lost the first admission ACK')
            return reply

        with monkeypatch.context() as lost_ack:
            lost_ack.setattr(prompt_forward, 'private_control_query', lose_first_ack)
            assert (await execute('a', first))['error']['message'] == 'OPERATOR_UNKNOWN'
        assert accepted['submission_id'] == 'original' and accepted['ref'] == ref
        assert (await execute('a', first))['error']['message'] == 'OPERATOR_HOLD'
        # Independently observed delivery makes the following an explicit server replay probe.
        duplicate = (await execute('a', await signed('a', 'submit', 0, payload)))['result']
        assert duplicate['admission_id'] == accepted['admission_id']
        changed = await execute('a', await signed('a', 'submit', 0, dict(text='Changed text.')))
        assert changed['error']['message'] == 'admission_conflict'
        wrong_window = await execute('b', await signed('b', 'receipt', 1, key=other_window))
        assert wrong_window['error']['message'] == 'OPERATOR_HOLD'

        await connect('recovered')
        await connect('contender')
        recovery = await signed('recovered', 'recover', 1)
        contender = await signed('contender', 'recover', 1)
        assert (await execute('recovered', recovery))['result']['window_generation'] == 2
        assert (await execute('contender', contender))['error']['message'] == 'OPERATOR_HOLD'
        receipt = (await execute('recovered', await signed('recovered', 'receipt', 2)))['result']
        assert receipt['admission_id'] == accepted['admission_id'] and receipt['attempt'] is None
        assert (await execute('a', await signed('a', 'submit', 0, payload)))['error']['message'] == 'OPERATOR_HOLD'
        assert (await execute('a', await signed('a', 'receipt', 1)))['error']['message'] == 'OPERATOR_HOLD'
        rows = list_session_admissions(peer.authority.db, session_id=ref['session_id'], pending_only=False)
        assert [row['admission_id'] for row in rows] == [accepted['admission_id']]

        from gateway.session_controls import AuthorityConnection

        generic = AuthorityConnection(peer.authority, object(), {'user_id': 'viewer', 'provider': 'session-token'})

        async def legacy(admission_id, expanded):
            return (await generic.dispatch(dict(id=4, method='prompt.receipt', params=dict(
                session_id=ref['session_id'], admission_id=admission_id, include_result=expanded))))['error']

        try:
            await async_wait_until(lambda: list_session_admissions(
                peer.authority.db, session_id=ref['session_id'], pending_only=False)[0]['status'] == 'terminal',
                timeout=15, what='personal admission settled')
            for expanded in (True, False):
                assert await legacy(accepted['admission_id'], expanded) == await legacy('missing-id', expanded)
        finally:
            await generic.close()

        await async_wait_until(lambda: bool(peer.requests), timeout=15, what='registered offline Symphony request')

        async def approve(input_id, *, socket='plan'):
            shown, _ = await decide(input_id, socket=socket)
            return shown

        if followup == 'stale':
            return await stale_attempt_control(peer, approve, connect, signed, execute, finished, failures, other_window)
        if followup == 'worker-refusal':
            return await observe_worker_refusal(peer, approve, monkeypatch)
        if desktop_result:
            await approve('operator-result-confirm')
            assert await asyncio.to_thread(finished.wait, 25)
            assert not failures, failures
            return await desktop_result(peer, connect, signed, execute, other_window)

        assert await peer.control(dict(op='worker', action='arm')) == {'state': 'armed'}
        shown = await approve('operator-fixture-confirm')
        assert await asyncio.to_thread(peer.worker_waiting.wait, 15)
        assert await peer.control(dict(op='worker', action='state')) == {'state': 'held'}
        during = await peer.control(dict(op='counts'))
        assert peer.worker_waiting.is_set() and not finished.is_set()
        assert during['launches'] == during['decisions'] == during['reservations'] == 1
        assert during['stop_commands'] == during['cancel_causes'] == during['executor_cancels'] == 0

        received = await execute('recovered', await signed('recovered', 'receipt', 2))
        assert received['result']['attempt'].items() >= shown.items(), received
        attempt_id = received['result']['attempt']['attempt_id']
        assert attempt_id == native.created[0].binding.ticket.attempt
        assert during['plan_inputs'] == [dict(admission_id=accepted['admission_id'], **shown)]
        assert during['attempts'] == [{key: received['result']['attempt'][key]
                                      for key in ('project', 'job', 'revision', 'attempt_id')}]
        target = dict(attempt_id=attempt_id)
        status = await execute('recovered', await signed('recovered', 'status', 2, target))
        assert status['result']['attempt_id'] == attempt_id and status['result']['evidence']

        command = dict(target, command_id='operator-stop-fixture')
        stop = dict(command, reason='offline owner test')
        refused_stop = await execute('b', await signed('b', 'stop', 2, stop, key=other_window))
        assert refused_stop['error']['message'] == 'OPERATOR_HOLD'
        before = (await execute('recovered', await signed('recovered', 'stop_result', 2, command)))['result']
        assert before['state'] == 'UNKNOWN' and native.created[0].cancels == 0

        early_command = dict(target, command_id='operator-lost-before')
        grant = settings['desktop_operator_registrations'][public(installation)]
        allowed_rights = grant['rights'][:]
        for action, fields in (('receipt', {}), ('status', target), ('stop', stop), ('stop_result', command)):
            revoked_proof = await signed('recovered', action, 2, fields)
            grant['rights'] = [right for right in allowed_rights if right != action]
            config_path.write_text(json.dumps(config), 'utf-8')
            assert (await execute('recovered', revoked_proof))['error']['message'] == 'OPERATOR_HOLD'
            grant['rights'] = allowed_rights
            config_path.write_text(json.dumps(config), 'utf-8')
        old = dict(early_command, reason='lost before delivery')
        early_proof = await signed('recovered', 'stop', 2, old)
        lost_before = {early_proof['challenge_id']}
        lost_after = set()
        loss = {}

        def interrupt(home, raw, timeout):
            params = json.loads(raw)['params']
            challenge = params.get('challenge_id')
            if challenge in lost_before:
                lost_before.remove(challenge)
                raise ConnectionError('fixture lost before IPC')
            result = query(home, raw, timeout)
            if challenge in lost_after:
                lost_after.remove(challenge)
                loss.update(json.loads(result)['result'])
                raise ConnectionError('fixture lost after committed STOP acknowledgement')
            return result

        monkeypatch.setattr(prompt_forward, 'private_control_query', interrupt)
        assert (await execute('recovered', early_proof))['error']['message'] == 'OPERATOR_UNKNOWN'
        empty = await execute('recovered', await signed('recovered', 'stop_result', 2, early_command))
        assert empty['result']['state'] == 'UNKNOWN'
        after_loss = await peer.control(dict(op='counts'))
        assert after_loss['stop_commands'] == after_loss['cancel_causes'] == after_loss['executor_cancels'] == 0
        if followup in ('replace', 'restart-replace'):
            async def request(action, payload):
                return await execute('recovered', await signed('recovered', action, 2, payload))

            async def lose_reply(action, payload):
                proof = await signed('recovered', action, 2, payload)
                lost_after.add(proof['challenge_id'])
                return await execute('recovered', proof)

            observed = await lost_stop_replacement(peer, request, lose_reply, execute, early_proof, old, stop, loss)
            if followup == 'restart-replace':
                assert await asyncio.to_thread(finished.wait, 35) and not failures, failures
                observed['successor'] = await prepare_successor(
                    peer, ref, command, observed['replacement'], stop['reason'], installation, window,
                    window_generation=2, expected_cancels=1, sealed=observed['old'], old_reason=old['reason'],
                )
                observed['pid'] = os.getpid()
                disconnected = True
            return observed

        stop_proof = await signed('recovered', 'stop', 2, stop)
        if not revoke_before_cancel:
            lost_after.add(stop_proof['challenge_id'])
        pending_stop = asyncio.create_task(execute('recovered', stop_proof))
        if revoke_before_cancel:
            assert await asyncio.to_thread(at_cancel.wait, 10)
            replacement = await execute('contender', await signed('contender', 'recover', 2))
            assert replacement['result']['window_generation'] == 3
            allow_cancel.set()
            assert (await pending_stop)['error']['message'] == 'OPERATOR_HOLD'
            stored = (await execute('contender', await signed('contender', 'stop_result', 3, command)))['result']
            assert stored == dict(command, state='REVOKED_BEFORE_CANCEL', dispatched=False, local_stopped=False)
            assert native.created[0].cancels == 0
        else:
            assert (await pending_stop)['error']['message'] == 'OPERATOR_UNKNOWN'
            assert loss == dict(command, state='CANCEL_REQUESTED', dispatched=True, local_stopped=False)
            stored = (await execute('recovered', await signed('recovered', 'stop_result', 2, command)))['result']
            repeated = (await execute('recovered', await signed('recovered', 'stop', 2, stop)))['result']
            assert stored == repeated == loss and native.created[0].cancels == 1
            conflict = await execute('recovered', await signed('recovered', 'stop', 2, dict(stop, reason='changed')))
            assert conflict['error']['message'] == 'admission_conflict'

        counted = await peer.control(dict(op='counts'))
        assert counted['stop_commands'] == counted['cancel_causes'] == 1
        assert counted['stop_receipts'] == [dict(command, state=stored['state'])]
        assert counted['executor_cancels'] == (0 if revoke_before_cancel else 1)
        observed = dict(
            admission_id=accepted['admission_id'], attempt_id=attempt_id, stop=stored,
            revoked=revoke_before_cancel, executor_cancels=counted['executor_cancels'],
            stop_commands=counted['stop_commands'], cancel_causes=counted['cancel_causes'],
        )
        if revoke_before_cancel:
            await peer.control(dict(op='worker', action='release'))
            assert await asyncio.to_thread(finished.wait, 20) and not failures, failures
            observed['previous_reader'] = await prepare_previous_reader(tmp_path, peer, ref, accepted['admission_id'])
            observed['successor'] = await prepare_successor(
                peer, ref, command, stored, stop['reason'], installation, window,
            )
            disconnected = True
            observed['pid'] = os.getpid()
        return observed
    finally:
        allow_cancel.set()
        if not disconnected:
            await peer.disconnect()
