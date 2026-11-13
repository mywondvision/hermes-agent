"""Opt-in native no-model acceptance candidates. Requires a separate campaign grant.

Only test-owned synthetic Python processes and temporary profiles are used. These
cases do not qualify Codex, billing, model cancellation or external effects.
"""
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from hermes_cli.plugin_request_binding import canonical_home
from plugins.symphony_pm.transport.lifecycle import ObservationTarget
from plugins.symphony_pm.transport.types import Refused
from plugins.symphony_pm.transport.windows_api import WindowsApi
from plugins.symphony_pm.transport.windows_observer import WindowsQueryObserver
from plugins.symphony_pm.transport.windows_ownership import WindowsOwnershipGuard
from plugins.symphony_pm.transport.windows_receiver import WindowsReceiver
from tests.plugins.symphony_serving_support import approve, rig
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')


def test_exclusive_os_owner_and_live_predecessor_refusal(tmp_path):
    scope = SimpleNamespace(profile_home=canonical_home(tmp_path), namespace='native-test', active=False)
    guard = WindowsOwnershipGuard(scope)
    try:
        with pytest.raises(Refused, match='OS_OWNER_UNAVAILABLE'):
            WindowsOwnershipGuard(scope)
        from dataclasses import asdict
        with pytest.raises(Refused, match='PREDECESSOR_STILL_ALIVE'):
            guard.require_predecessor_dead(asdict(guard.identity))
    finally:
        guard.close(0)


def _receiver(tmp_path):
    r = rig(tmp_path/'journal')
    request, context, _ = approve(r)
    ticket, _, _ = r.owner.journal.reserve_launch(r.owner.token, request, context, r.now[0])
    capture = tmp_path/'capture'
    capture.mkdir()
    argv = [str(Path(sys.executable).resolve()), '-I', '-B', '-c',
            'import sys; sys.stdin.buffer.read(); sys.stdout.write("synthetic-only")']
    receiver = WindowsReceiver(WindowsApi(), ticket, argv, tmp_path, '', environment={},
        command_result=lambda argv, code, **kw: SimpleNamespace(argv=argv, returncode=code, **kw),
        sanitize=lambda text: text, private_directory=capture,
        data_directory=Path(r.owner.journal.path).parents[1])
    return r, receiver, argv


def test_suspended_root_query_coverage_then_result(tmp_path):
    r, receiver, argv = _receiver(tmp_path)
    observer = WindowsQueryObserver(r.scope)
    try:
        binding = receiver.binding
        target = ObservationTarget(binding, binding.known_processes)
        observer.handshake(r.guard.identity, 1, (target,))
        before = observer.read(target, 1, 1)
        assert before.job_active == 1 and not before.processes[0].signaled
        receiver.resume('', authorize=lambda action: action())
        result = receiver.wait_result(argv, 10)
        assert result.returncode == 0 and result.stdout == 'synthetic-only'
        # The console host leaves the Job asynchronously after the root signals.
        wait_until(lambda: observer.read(target, 1, 1).job_active == 0, timeout=10, what='empty worker Job')
        after = observer.read(target, 1, 1)
        assert after.job_active == 0 and not after.job_members and all(p.signaled for p in after.processes)
    finally:
        receiver.close_fail_safe(0)
        observer.close(0)
        r.owner.close(0)


def test_receiver_spends_slot_even_when_authorization_is_uncertain(tmp_path):
    r, receiver, _ = _receiver(tmp_path)
    calls = []
    try:
        def uncertain(action):
            calls.append('entered')
            raise Refused('AUTHORIZATION_CUT')
        with pytest.raises(Refused, match='AUTHORIZATION_CUT'):
            receiver.cancel_once(receiver.binding, authorize=uncertain)
        with pytest.raises(Refused, match='CANCEL_SLOT_SPENT'):
            receiver.cancel_once(receiver.binding, authorize=lambda action: action())
        assert calls == ['entered']
    finally:
        receiver.close_fail_safe(0)
        r.owner.close(0)


def test_query_process_handle_survives_job_fail_safe_close(tmp_path):
    r, receiver, _ = _receiver(tmp_path)
    api = receiver.api
    query = api.open_process(receiver.binding.root.pid)
    try:
        assert api.creation(query) == receiver.binding.root.creation
        receiver.close_fail_safe(0)
        wait = api.fn('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)
        assert wait(query, 10000) == 0
    finally:
        receiver.close_fail_safe(0)
        api.close(query)
        r.owner.close(0)


def test_worker_job_cannot_be_opened_by_its_identifier_and_the_owner_process_can_query_it(tmp_path):
    r, receiver, _ = _receiver(tmp_path)
    api = receiver.api
    try:
        open_by_name = api.fn('OpenJobObjectW', [W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE)
        C.set_last_error(0)
        named = open_by_name(4, False, receiver.binding.job_object)
        error = C.get_last_error()
        if named:
            api.close(named)  # a stray handle would keep the Job, and so the suspended root, alive
        assert not named and error == 2  # ERROR_FILE_NOT_FOUND: no name exists for any process to open
        job = api.open_job(receiver.binding.job_object)
        try:
            assert api.in_job(receiver.process, job)
        finally:
            api.close(job)
    finally:
        receiver.close_fail_safe(0)
        r.owner.close(0)
