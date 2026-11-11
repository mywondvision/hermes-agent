"""Prepared native crash matrix; NOT covered by the consumed four-case grant.

Current five-case selection budgets fourteen physical processes including
measured console hosts. Kernel limits are 2 per worker Job, 4 per parent Job.
The after_create case proves the kernel, not test cleanup, stops a root whose owner
died before its binding was durable; it records a known HOLD, never cancel success.
"""
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import uuid

import pytest

from plugins.symphony_pm.transport.lifecycle import AttemptRunner
from plugins.symphony_pm.transport.types import ProcessIdentity, Refused, binding_from_json
from plugins.symphony_pm.transport.windows_observer import WindowsQueryObserver
from plugins.symphony_pm.transport.windows_ownership import WindowsOwnershipGuard
from tests.plugins.symphony_native_crash_support import (
    BoundedApi, NativeRig, capture_members, close_queries, helper_command, save_proof,
)
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')


def test_predecessor_creation_mismatch_is_hold_without_effect(tmp_path):
    from dataclasses import asdict
    from types import SimpleNamespace
    from hermes_cli.plugin_request_binding import canonical_home
    scope = SimpleNamespace(profile_home=canonical_home(tmp_path), namespace='identity-test', active=False)
    guard = WindowsOwnershipGuard(scope)
    try:
        previous = asdict(guard.identity)
        previous['process']['creation'] = 'wrong-creation-identity'
        with pytest.raises(Refused, match='PREDECESSOR_IDENTITY_UNKNOWN'):
            guard.require_predecessor_dead(previous)
    finally:
        guard.close(0)


def _signal(api, handle):
    return api.fn('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)(handle, 10000) == 0


def _finish(future):
    try:
        future.result(timeout=15)
    except Refused as exc:
        assert str(exc) == 'WRITER_FENCED'


class _ContentionWitness:
    def __init__(self, lock, event):
        self.lock, self.event, self.contender = lock, event, None

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            if threading.get_ident() == self.contender:
                self.event.set()
            self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()


def test_takeover_serializes_native_entry_and_fences_late_callback(tmp_path):
    rig = NativeRig(tmp_path)
    entered, release, rotating = threading.Event(), threading.Event(), threading.Event()
    rig.owner.lock = _ContentionWitness(rig.owner.lock, rotating)
    retained, native_entries = [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        future = rig.start(pool)
        try:
            receiver = rig.ready()
            original = receiver.cancel_once

            def paused(binding, *, authorize):
                retained.append(authorize)
                entered.set()
                assert release.wait(10)
                return original(binding, authorize=authorize)

            receiver.cancel_once = paused
            terminate = rig.native.api.terminate

            def count(job):
                native_entries.append(job)
                return terminate(job)
            rig.native.api.terminate = count
            old = rig.owner.token
            cancellation = pool.submit(rig.owner.cancel_internal, receiver.binding, 'PM_CANCEL')
            assert entered.wait(10)

            def rotate():
                rig.owner.lock.contender = threading.get_ident()
                return rig.owner.rotate_controller('controller-2')
            rotation = pool.submit(rotate)
            assert rotating.wait(10)
            release.set()
            assert cancellation.result(timeout=15).dispatched
            assert rotation.result(timeout=15).epoch == old.epoch+1
            with pytest.raises(Refused, match='WRITER_FENCED'):
                retained[0](lambda: native_entries.append('STALE_EFFECT'))
            assert len(native_entries) == 1
            with rig.owner.lock:
                effect = rig.owner.journal.effect(rig.owner.token, receiver.binding)
            assert effect['dispatch_epoch'] == old.epoch
            _finish(future)
        finally:
            release.set()
            rig.close(time.monotonic()+10)


@pytest.mark.parametrize('cut', ['observer_loss', 'lost_ack'])
def test_uncertain_effect_does_not_get_new_sender_after_observer_replacement(tmp_path, cut):
    rig = NativeRig(tmp_path)
    native_entries = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = rig.start(pool)
        query = None
        try:
            receiver = rig.ready()
            query = rig.native.api.open_process(receiver.binding.root.pid)
            terminate = rig.native.api.terminate

            def lost_ack(job):
                native_entries.append(job)
                terminate(job)
                raise OSError('synthetic lost native ACK')
            rig.native.api.terminate = lost_ack
            if cut == 'observer_loss':
                original = receiver.cancel_once

                def lose(binding, *, authorize):
                    rig.observer.close(0)
                    rig.owner.observer_lost()
                    return original(binding, authorize=authorize)
                receiver.cancel_once = lose
            first = rig.owner.cancel_internal(receiver.binding, 'PM_CANCEL')
            assert first.hold and first.state == 'UNKNOWN'
            if cut == 'lost_ack':
                _finish(future)  # drain the result writer before inspecting its journal
            with rig.owner.lock:
                effect = rig.owner.journal.effect(rig.owner.token, receiver.binding)
            assert effect['state'] == 'UNKNOWN'
            generation = rig.owner.generation
            rig.owner.replace_observer(WindowsQueryObserver(rig.scope))
            assert rig.owner.generation == generation+1
            second = rig.owner.cancel_internal(receiver.binding, 'TIMEOUT')
            assert second.hold and not second.dispatched
            assert len(native_entries) == (1 if cut == 'lost_ack' else 0)
            with rig.owner.lock:
                assert rig.owner.journal.effect(rig.owner.token, receiver.binding) == effect
            # Fail-safe shutdown is distinct from an acknowledged native cancel.
            rig.close(time.monotonic()+10)
            assert _signal(rig.native.api, query)
            if cut == 'observer_loss':
                with pytest.raises(Refused, match='RESULT_CLOSED_UNKNOWN|NATIVE_HANDLE_BINDING'):
                    future.result(timeout=15)
        finally:
            rig.close(time.monotonic()+10)
            if query is not None:
                rig.native.api.close(query)


def _snapshot(path):
    with sqlite3.connect(f'file:{Path(path).as_posix()}?mode=ro', uri=True) as db:
        return (list(db.execute('SELECT * FROM serving_launches')), list(db.execute('SELECT * FROM effects')))


@pytest.mark.parametrize('cut', ['after_create', 'before_resume', 'after_effect'])
def test_serving_crash_preserves_intent_and_never_recreates_sender(tmp_path, cut):  # health: allow CC NESTING -- natively proven as one scenario; restructuring it needs a new native permit
    home = tmp_path/'profile'
    proofs = Path(os.environ.get('HERMES_E2E_ARTIFACTS', str(tmp_path)))
    proofs.mkdir(parents=True, exist_ok=True)
    receipt = proofs/('serving-'+cut+'.json')
    helper = Path(__file__).with_name('symphony_native_crash_support.py').resolve()
    api = BoundedApi(4)
    query = process = thread = None
    member_queries = {}
    parent_job = parent_job_id = reconcile = None
    environment = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR'}}
    child_home, child_temp = tmp_path/'child-home', tmp_path/'child-temp'
    for directory in (child_home, child_temp, child_home/'appdata', child_home/'local', child_home/'runtime'):
        directory.mkdir(parents=True, exist_ok=True)
    environment.update(HOME=str(child_home), USERPROFILE=str(child_home), HERMES_HOME=str(home),
        APPDATA=str(child_home/'appdata'), LOCALAPPDATA=str(child_home/'local'),
        TEMP=str(child_temp), TMP=str(child_temp), HERMES_RUNTIME_DIR=str(child_home/'runtime'),
        HERMES_DISABLE_LAZY_INSTALLS='1', UV_OFFLINE='1', PIP_CONFIG_FILE=os.devnull,
        PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', HERMES_E2E_ARTIFACTS=str(proofs))
    with (tmp_path/'serving.log').open('w+b') as log, tempfile.TemporaryFile(dir=tmp_path) as stdin:
        try:
            parent_job_id = 'HermesTestContainment-'+uuid.uuid4().hex
            parent_job = api.create_job(parent_job_id)
            process, thread, pid, _ = api.create_suspended(
                [*helper_command(helper), str(home), cut, str(receipt)],
                helper.parents[2], environment, (stdin, log, log), parent_job, None)
            api.resume(thread)
            api.close(thread)
            thread = None
            wait_until(lambda: receipt.exists() or api.wait(process), timeout=30, what='native cut barrier')
            assert receipt.exists(), (tmp_path/'serving.log').read_text(encoding='utf-8', errors='replace')
            body = json.loads(receipt.read_text(encoding='utf-8'))
            assert body['serving']['process']['pid'] == pid and body['cut'] == cut
            assert body['serving']['process']['creation'] == api.creation(process)
            binding = binding_from_json(body['binding'])
            query = api.open_process(binding.root.pid)
            assert api.creation(query) == binding.root.creation
            if cut == 'after_create':
                assert not api.wait(query)  # suspended root is alive until its owner's Job handle closes
            if cut == 'after_effect':
                assert _signal(api, query)  # effect already happened before receipt
                required_pids, expected_active = {pid}, 2  # helper + its console host
            else:
                required_pids, expected_active = {pid, binding.root.pid}, 3
            roster = capture_members(api, parent_job, active_count=expected_active,
                required_pids=required_pids, handles=member_queries, name='parent-roster-'+cut)
            assert roster['total'] == (4 if cut == 'after_effect' else 3)
            if body['worker_roster'] is not None:
                for value in body['worker_roster']['members']:
                    identity = ProcessIdentity(**value)
                    if identity not in member_queries:
                        handle = api.open_process(identity.pid)
                        try:
                            assert api.creation(handle) == identity.creation
                            member_queries[identity], handle = handle, None
                        finally:
                            if handle is not None:
                                api.close(handle)
            if cut == 'after_effect':
                assert body['worker_roster'] is not None and len(body['worker_roster']['members']) == 2
                worker_terminal = []
                for value in body['worker_roster']['members']:
                    identity = ProcessIdentity(**value)
                    worker_terminal.append({**value, 'signaled': _signal(api, member_queries[identity])})
                save_proof('worker-before-cut-'+cut,
                    {'terminal': worker_terminal, 'parent_containment_open': True, 'cancel': 'HOLD'})
                assert all(record['signaled'] for record in worker_terminal)
            receipt.with_suffix('.release').write_text('CUT', encoding='utf-8')
            assert _signal(api, process) and api.exit_code(process) == 71
            if cut == 'after_create':
                # Parent containment Job is still OPEN: only the dead owner's Job handle can explain this stop.
                wait_until(lambda: api.wait(query), timeout=10, what='kernel Job close to stop unbound root')
            else:
                assert _signal(api, query)
            namespace_dir = next((home/'plugin-data'/'symphony_pm').iterdir())
            journal = namespace_dir/'journal.sqlite3'
            before = _snapshot(journal)
            assert before[0][0][5] == ('CREATE_INTENT' if cut == 'after_create' else 'RESUME_INTENT')
            if cut == 'after_create':
                assert before[0][0][6] is None  # binding never became durable
                recovered = NativeRig(home)
                try:
                    runner = AttemptRunner(recovered.owner, recovered.owner.token, binding.ticket)
                    with pytest.raises(Refused, match='LAUNCH_TICKET_SPENT'):
                        runner(recovered.argv, cwd=binding.ticket.plan.workspace, timeout=30, stdin_text='')
                    assert recovered.creates == 0
                    record = recovered.owner.journal.reconcile_record(recovered.owner.token, binding.ticket)
                    reconcile = json.loads(record['body'])
                    assert reconcile['outcome'] == 'CREATE_UNBOUND_UNOBSERVED' and 'job_present' not in reconcile
                finally:
                    recovered.close(0)
                # The last-resort fence has nothing left to kill: no test-side orphan cleanup exists.
                wait_until(lambda: api.snapshot(parent_job)[0] == 0, timeout=10, what='empty parent containment Job')
            else:
                with pytest.raises(Refused, match='OBSERVER_COVERAGE_UNKNOWN'):
                    NativeRig(home)  # exactly one recovery attempt; never retry the claimed epoch
            assert _snapshot(journal) == before
            if cut == 'after_effect':
                assert before[1][0][2] == 'DISPATCH_INTENT'
            # Durable evidence retained by the test temp directory; no release claim.
            (proofs/('readback-'+cut+'.json')).write_text(json.dumps({'cut': cut, 'binding': body['binding'],
                'root_signaled': True, 'launches': len(before[0]), 'effects': len(before[1]),
                'orphan_cleanup': False, 'reconcile': reconcile, 'parent_roster': roster, 'cancel': 'HOLD'}),
                encoding='utf-8')
        finally:
            # The parent-owned containment Job is only a last-resort fence for the helper
            # and its descendants; the worker root is born inside its own Job.
            failures = []
            try:
                if parent_job is not None:
                    api.close_job(parent_job_id)
            except (OSError, Refused) as exc:
                failures.append(exc)
            finally:
                records, query_failures = close_queries(api, member_queries)
                failures.extend(query_failures)
                for handle in (process, query):
                    if handle is not None:
                        try:
                            if not _signal(api, handle):
                                failures.append(Refused('TEST_QUIESCENCE_UNKNOWN'))
                        except (OSError, Refused) as exc:
                            failures.append(exc)
                for handle in (thread, process, query):
                    if handle is not None:
                        try:
                            api.close(handle)
                        except OSError as exc:
                            failures.append(exc)
                try:
                    save_proof('parent-terminal-'+cut,
                        {'terminal': records, 'errors': [str(exc) for exc in failures]})
                except (OSError, Refused) as exc:
                    failures.append(exc)
                if failures:
                    raise Refused('TEST_HANDLE_CLOSE_UNKNOWN') from failures[0]
