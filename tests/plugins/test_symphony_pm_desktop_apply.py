"""Drive a checked Robot result from the personal Desktop route through owner IPC."""

import asyncio
from pathlib import Path
from threading import Event

import pytest

from gateway.session_desktop_operator import check_payload, payload_digest
from hermes_state_runtime import RuntimeStoreError
from plugins.symphony_pm.transport import robot_apply_git
from plugins.symphony_pm.transport.types import Refused
from tests.plugins.test_symphony_pm_codex_exec import _git
from tests.plugins.test_symphony_pm_linux_operator import linux_chat, operator_roundtrip
from tests.plugins.test_symphony_pm_linux_owner_check import checker_stand


def test_every_desktop_result_input_is_in_the_signed_digest():
    payload = dict(attempt_id='attempt', job='job', capture_seq=3, check_seq=4,
                   patch_sha256='a' * 64, command_id='command')
    check_payload('apply', payload)
    assert payload_digest(payload, action='apply') != payload_digest(payload, action='archive')
    for field, value in payload.items():
        changed = value + 1 if type(value) is int else 'changed-' + value
        assert payload_digest(dict(payload, **{field: changed}), action='apply') != payload_digest(
            payload, action='apply')
    with pytest.raises(RuntimeStoreError, match='permission_denied'):
        check_payload('apply', dict(payload, capture_seq=True))


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['apply', 'archive', 'revoke'])
async def test_checked_result_is_bound_to_the_personal_window(linux_chat, monkeypatch, tmp_path, mode):
    stand = linux_chat[0]

    async def scenario(_peer, connect, signed, execute, other_window):
        repo, source = stand.repo, stand.source
        head = _git('rev-parse', 'HEAD', cwd=repo)
        worktree = _git('--no-optional-locks', 'status', '--porcelain=v1', cwd=repo)
        index = (Path(repo) / '.git/index').read_bytes()
        receipt = (await execute('recovered', await signed('recovered', 'receipt', 2)))['result']
        target = dict(attempt_id=receipt['attempt']['attempt_id'])

        async def request(action, payload, socket='recovered', generation=2, **kwargs):
            return await execute(socket, await signed(socket, action, generation, payload, **kwargs))

        response = await request('apply_info', target)
        assert 'result' in response, response
        info = response['result']
        assert info['job'] == receipt['attempt']['job'] and info['project'] == receipt['attempt']['project']
        action = 'archive' if mode == 'archive' else 'apply'
        payload = {key: value for key, value in info.items() if key != 'project'}
        payload['command_id'] = 'explicit-' + action
        lookup = dict(target, command_id=payload['command_id'])
        reference = 'refs/heads/symphony/' + info['job']
        assert (await request(action, dict(payload, patch_sha256='0' * 64)))['error']['message'] == 'OPERATOR_HOLD'
        assert (await request(action, dict(payload, check_seq=payload['check_seq'] + 1)))['error']['message'] == 'OPERATOR_HOLD'
        assert (await request(action, payload, key=other_window))['error']['message'] == 'OPERATOR_HOLD'
        assert not _git('for-each-ref', '--format=%(refname)', reference, cwd=repo)

        at_write, release = Event(), Event()
        publish = robot_apply_git.publish_prepared
        attempts = []

        def instrument(*args, readback_only=False, **kwargs):
            attempts.append(readback_only)
            if mode == 'revoke' and not readback_only:
                at_write.set()
                assert release.wait(20)
            value = publish(*args, readback_only=readback_only, **kwargs)
            if mode == 'apply' and not readback_only:
                raise Refused('ROBOT_REF_OUTCOME_UNKNOWN')
            return value

        monkeypatch.setattr(robot_apply_git, 'publish_prepared', instrument)
        if mode == 'revoke':
            task = asyncio.create_task(request(action, payload))
            try:
                assert await asyncio.to_thread(at_write.wait, 20), await task if task.done() else 'publication absent'
                await connect('apply-owner')
                recovered = await request('recover', {}, 'apply-owner', 2)
                assert recovered['result']['window_generation'] == 3
            finally:
                release.set()
                reply = await task
            assert reply['error']['message'] == 'OPERATOR_HOLD'
            assert (await request('apply_result', lookup))['error']['message'] == 'OPERATOR_HOLD'
            saved = (await request('apply_result', lookup, 'apply-owner', 3))['result']
            assert saved['state'] == 'UNKNOWN' and attempts == [False, True]
            assert not _git('for-each-ref', '--format=%(refname)', reference, cwd=repo)
        else:
            reply = await request(action, payload)
            assert 'result' in reply, reply
            assert reply['result']['state'] == ('ARCHIVED' if mode == 'archive' else 'UNKNOWN')
            saved = (await request('apply_result', lookup))['result']
            assert saved['command_id'] == payload['command_id']
            assert saved['patch_sha256'] == payload['patch_sha256']
            if mode == 'apply':
                assert saved['state'] == 'APPLIED'
                commit = _git('rev-parse', reference, cwd=repo)
                assert commit == saved['commit']
                assert _git('log', '-1', '--format=%an <%ae>', commit, cwd=repo) == 'Robot <robot@symphony.invalid>'
                assert _git('rev-parse', commit + '^', cwd=repo) == source
                assert _git('show', commit + ':task.txt', cwd=repo) == 'changed'
                assert attempts == [False, True]
            else:
                assert saved['state'] == 'ARCHIVED' and not attempts
                assert not _git('for-each-ref', '--format=%(refname)', reference, cwd=repo)
            assert (await request('apply_result', lookup, key=other_window))['error']['message'] == 'OPERATOR_HOLD'
            assert (await request(action, dict(payload, command_id='extra-command')))['error']['message'] == 'OPERATOR_HOLD'
        assert _git('rev-parse', 'HEAD', cwd=repo) == head
        assert (Path(repo) / '.git/index').read_bytes() == index
        assert _git('--no-optional-locks', 'status', '--porcelain=v1', cwd=repo) == worktree

    await operator_roundtrip(linux_chat, monkeypatch, tmp_path, revoke_before_cancel=False, desktop_result=scenario)
