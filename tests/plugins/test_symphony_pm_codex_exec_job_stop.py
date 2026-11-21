"""Bound attempts publish their local stop before capture, even when the root timed out."""
import json
from dataclasses import replace

import pytest

from plugins.symphony_pm.transport import codex_exec_launcher as exec_launcher
from plugins.symphony_pm.transport.lifecycle import CancelOutcome
from plugins.symphony_pm.transport.types import Refused
from tests.plugins.test_symphony_pm_codex_exec import FixtureReceiver, rig, submit

pytestmark = pytest.mark.platforms('windows')  # fixture launcher holds the interpreter with a real handle


@pytest.mark.parametrize('mode', ['exit', 'timeout', 'cancel', 'accounting-error', 'rename-error', 'capture-error'])
def test_attempt_finalization_through_submit(tmp_path, monkeypatch, mode):
    r = rig(tmp_path)
    owner, observer = r.owner, r.owner.observer
    observer.stopped = False
    events, dispatches = [], []
    wait = FixtureReceiver.wait_result
    capture = exec_launcher.capture_result

    def journal_row(ticket):
        return owner.journal.read_launch(owner.token, ticket.project, ticket.job, ticket.revision)[1]

    def cancel(receiver, binding, *, authorize):
        assert receiver.binding == binding
        def terminate():
            dispatches.append(binding)
            observer.stopped = True
            return True
        return authorize(terminate)

    def observe_exit(receiver, argv, timeout, timed_out=False):
        if mode == 'timeout' and not timed_out:
            raise TimeoutError
        if mode == 'cancel':
            receipt = owner.cancel_internal(receiver.binding, 'PM_CANCEL', request_id='fixture-cancel')
            assert isinstance(receipt, CancelOutcome) and receipt.dispatched
        result = wait(receiver, argv, timeout, timed_out)
        if mode == 'accounting-error':
            def unreadable(*args):
                raise OSError('fixture accounting unavailable')
            monkeypatch.setattr(observer, 'read', unreadable)
        elif mode != 'exit':
            observer.stopped = True
        # In the ordinary exit case a remaining member still needs a targeted terminate.
        return result

    def close(receiver, _deadline):
        row = journal_row(receiver.binding.ticket)
        events.append(('close', row['state'], row['stop_proof']))

    def checked_capture(*args, **kwargs):
        row = journal_row(r.native.created[0].binding.ticket)
        assert row['state'] == 'STOPPED' and row['hold_reason'] == 'WORKSPACE_HELD'
        events.append(('capture', row['state'], row['stop_proof']))
        if mode == 'capture-error':
            raise OSError('fixture capture denied')
        return capture(*args, **kwargs)

    monkeypatch.setattr(FixtureReceiver, 'wait_result', observe_exit)
    monkeypatch.setattr(FixtureReceiver, 'cancel_once', cancel, raising=False)
    monkeypatch.setattr(FixtureReceiver, 'close_fail_safe', close)
    monkeypatch.setattr(exec_launcher, 'capture_result', checked_capture)
    if mode == 'rename-error':
        monkeypatch.setattr(r.launcher, '_settle', lambda path, _: {'path': str(path), 'renamed': False})

    ticket, fresh = submit(r)
    row = journal_row(ticket)
    assert fresh and r.native.created[0].binding.ticket == ticket
    if mode == 'accounting-error':
        assert events == [('close', 'HELD_UNKNOWN', None)]
        assert row['state'] == 'HELD_UNKNOWN' and row['proof_source'] is None and row['hold_reason']
        assert not dispatches and 'patch' not in json.loads(row['result']) and r.launcher.settings.active.exists()
    else:
        assert events == [('close', 'STOPPED', 'JOB_EMPTY'), ('capture', 'STOPPED', 'JOB_EMPTY')]
        assert len(dispatches) == int(mode in ('exit', 'timeout', 'cancel'))
        assert row['proof_source'] and row['stop_proof'] == 'JOB_EMPTY'
        assert (row['hold_reason'] == 'WORKSPACE_HELD') == (mode == 'rename-error')
        assert r.launcher.settings.active.exists() == (mode == 'rename-error')
        result = json.loads(row['result'])
        assert result['workspace']['renamed'] == (mode != 'rename-error')
        if mode == 'capture-error':
            assert result['patch']['refused']
        else:
            assert result['patch']['paths'] == ['pkg/value.py']
        if mode == 'timeout':
            assert result['stop'] == 'TIME_LIMIT'
        if mode in ('timeout', 'cancel'):
            assert result['check'] is None and result['check_skipped'] == 'ATTEMPT_INTERRUPTED'
    if mode in ('accounting-error', 'rename-error'):
        with pytest.raises(Refused, match='HOLD|HELD|COVERAGE_UNKNOWN'):
            submit(r, request_id='second')
        assert len(r.native.created) == 1


@pytest.mark.parametrize('changed', ['missing', 'host', 'client', 'profile'])
def test_unqualified_job_never_releases_or_captures(tmp_path, monkeypatch, changed):
    from plugins.symphony_pm.transport.lifecycle import LifecycleOwner
    from tests.plugins.symphony_serving_support import Guard, Observer

    r = rig(tmp_path)
    original = r.launcher.qualification
    fields = {'host': {'host_id': 'other'}, 'client': {'executable_sha256': '0'*64},
              'profile': {'permission_profile': 'other'}}
    attestation = None if changed == 'missing' else replace(original.job_coverage, **fields[changed])
    r.launcher.qualification = replace(original, job_coverage=attestation)
    r.owner.guard.close(0)
    observer = Observer(r.now)
    observer.stopped = True
    r.owner = LifecycleOwner(r.scope, Guard(r.scope, 'next', predecessor_dead=True), observer, r.native, r.launcher,
                             scheduler_id='serving-test', controller='next', clock=lambda: r.now[0])
    wait = FixtureReceiver.wait_result
    observed = []
    def zero_while_still_bound(receiver, *args, **kwargs):
        outcome = wait(receiver, *args, **kwargs)
        phase = r.owner.status(receiver.binding)[0]
        cancel = r.owner.cancel_internal(receiver.binding, 'PM_CANCEL')
        observed.append((phase, cancel.state, cancel.local_stopped, cancel.dispatched))
        return outcome
    monkeypatch.setattr(FixtureReceiver, 'wait_result', zero_while_still_bound)
    ticket, _ = submit(r)
    assert observed == [('UNKNOWN', 'UNKNOWN', False, False)]
    _, row = r.owner.journal.read_launch(r.owner.token, ticket.project, ticket.job, ticket.revision)
    assert (row['state'], row['ownership_contract'], row['stop_proof']) == ('HELD_UNKNOWN', None, None)
    assert r.launcher.settings.active.is_dir() and 'patch' not in json.loads(row['result'])


def test_client_observer_queries_job_roster_without_opening_mxc_members(tmp_path):
    from types import SimpleNamespace
    from plugins.symphony_pm.transport.lifecycle import ObservationTarget
    from plugins.symphony_pm.transport.types import ExecutionPlan, LaunchTicket, NativeBinding, ProcessIdentity, job_object_name
    from plugins.symphony_pm.transport.windows_client_observer import WindowsClientJobObserver
    from tests.plugins.symphony_serving_support import Guard

    scope = SimpleNamespace(profile_home=str(tmp_path), namespace='fixture')
    plan = ExecutionPlan('model', 'high', 'fixture', str(tmp_path), 'a'*40)
    ticket = LaunchTicket('fixture', 'proj', 'job', 'b'*64, 'attempt-'+'1'*32, 'c'*64, 'permit', plan, 'session')
    root, known = ProcessIdentity(101, 'birth-1'), ProcessIdentity(102, 'birth-2')
    binding = NativeBinding(ticket, job_object_name(ticket), root, (root,))
    target = ObservationTarget(binding, (root, known))
    opened, closed = [], []
    signals = {root.pid: False, known.pid: False}
    roster = [root.pid, known.pid, 103]
    def open_process(pid):
        opened.append(pid)
        return pid
    observer = WindowsClientJobObserver(scope)
    observer.api = SimpleNamespace(open_job=lambda name: 1000 if name == binding.job_object else None,
        close=closed.append, open_process=open_process, creation={101: 'birth-1', 102: 'birth-2'}.__getitem__,
        wait=signals.__getitem__, in_job=lambda process, job: process == root.pid and job == 1000,
        snapshot=lambda job: (len(roster), tuple(roster)))
    observer.handshake(Guard(scope).identity, 1, (target,))
    sample = observer.read(target, 4, 1)
    assert sample.job_active == 3 and sample.job_members == tuple(roster)
    assert set(opened) == {101, 102} and sorted(closed) == sorted([*opened, 1000, 1000])
    roster.clear()
    signals[root.pid] = signals[known.pid] = True
    sample = observer.read(target, 4, 1)
    assert sample.job_active == 0 and sample.job_members == () and all(p.signaled for p in sample.processes)
