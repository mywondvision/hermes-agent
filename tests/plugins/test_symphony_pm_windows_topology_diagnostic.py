"""Prepared one-worker topology diagnostic, not a cancel/recovery acceptance test.

Requires a new grant. Kernel active-process limit bounds this test-owned Job to
four members. No model, driver, serving helper, retry, or TerminateJobObject call.
"""
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import psutil
import pytest

from hermes_dir_acl import make_user_dir
from hermes_platform.host.facts import os_family, process_arch
from plugins.symphony_pm.transport.types import ExecutionPlan, LaunchTicket, Refused
from plugins.symphony_pm.transport.windows_api import Accounting, ExtendedLimit, WindowsApi
from plugins.symphony_pm.transport.windows_receiver import WindowsReceiver
from tests.plugins.symphony_native_crash_support import base_python, decoy_protected
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')


class _Pids(C.Structure):
    _fields_ = [('Assigned', W.DWORD), ('Count', W.DWORD), ('Values', C.c_size_t*4096)]


class _BoundedApi(WindowsApi):
    def create_job(self, job_id):
        job = super().create_job(job_id)
        try:
            limits = ExtendedLimit()
            limits.Basic.Flags = 0x2000 | 0x8  # KILL_ON_CLOSE | ACTIVE_PROCESS
            limits.Basic.ActiveLimit = 4
            self.checked(self.fn('SetInformationJobObject',
                [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL)(job, 9, C.byref(limits), C.sizeof(limits)))
            return job
        except BaseException:
            self.close_job(job_id)
            raise


def _raw(api, job):
    account, pids = Accounting(), _Pids()
    account_size, list_size = W.DWORD(), W.DWORD()
    query = api.fn('QueryInformationJobObject', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL)
    api.checked(query(job, 1, C.byref(account), C.sizeof(account), C.byref(account_size)))
    api.checked(query(job, 3, C.byref(pids), C.sizeof(pids), C.byref(list_size)))
    return {'active': account.Active, 'total': account.Total, 'terminated': account.Terminated,
            'accounting': {name: getattr(account, name) for name, _ in Accounting._fields_},
            'accounting_return_length': account_size.value, 'pid_list_return_length': list_size.value,
            'assigned': pids.Assigned, 'count': pids.Count,
            'pid_list_complete': pids.Count <= 4096 and pids.Count == pids.Assigned,
            'pids': list(pids.Values[:min(pids.Count, 4096)])}


def _image(api, handle):
    buffer, size = C.create_unicode_buffer(32768), W.DWORD(32768)
    api.checked(api.fn('QueryFullProcessImageNameW',
        [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL)(handle, 0, buffer, C.byref(size)))
    return buffer.value


def _persist(path, body):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(body, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def test_capture_exact_job_topology_without_cancel_or_crash(tmp_path):
    destination = os.environ.get('HERMES_E2E_ARTIFACTS')
    if not destination:
        raise Refused('TASK_OWNED_DURABLE_DIAGNOSTIC_PATH_REQUIRED')
    proofs = Path(destination)
    make_user_dir(proofs, mode=0o700, parents=True, exist_ok=True)
    output = proofs/'topology.json'
    if output.exists():
        raise Refused('DIAGNOSTIC_ALREADY_RECORDED_NO_RETRY')
    capture, marker = tmp_path/'capture', tmp_path/'ready'
    make_user_dir(capture, mode=0o700, parents=True, exist_ok=True)
    ticket = LaunchTicket('diagnostic', 'diagnostic', 'diagnostic', '0'*64, 'diag-'+uuid.uuid4().hex,
        '0'*64, 'no-model', ExecutionPlan('synthetic-no-model', 'none', 'local-test', str(tmp_path), '0'*40),
        'diagnostic')
    argv = [base_python(), '-I', '-B', '-c',
        'from pathlib import Path; import sys,threading; '
        'Path(sys.argv[1]).write_text("ready",encoding="utf-8"); threading.Event().wait(60)', str(marker)]
    api, receiver, handles = _BoundedApi(), None, {}
    report = {'classification': 'DIAGNOSTIC_ONLY', 'native_cancel': 'HOLD', 'kernel_job_limit': 4,
              'executable': argv[0], 'os_family': os_family(), 'process_arch': process_arch(),
              'accounting_size': C.sizeof(Accounting), 'pointer_size': C.sizeof(C.c_size_t),
              'pid_list_size': C.sizeof(_Pids),
              'pid_list_offsets': {name: getattr(_Pids, name).offset for name, _ in _Pids._fields_},
              'accounting_offsets': {name: getattr(Accounting, name).offset for name, _ in Accounting._fields_},
              'snapshots': [], 'terminal': [], 'errors': []}

    def capture_state(stage):
        snapshot = _raw(api, receiver.job)
        snapshot.update(stage=stage, members=[])
        report['snapshots'].append(snapshot)
        _persist(output, report)  # preserve unexpected raw values before any validation
        if not snapshot['pid_list_complete'] or snapshot['count'] > 4 or snapshot['assigned'] > 4:
            raise Refused('RAW_TOPOLOGY_OUTSIDE_DIAGNOSTIC_CAP')
        for pid in snapshot['pids']:
            record = {'pid': pid}
            candidate = None
            try:
                handle = handles.get(pid)
                if handle is None:
                    candidate = api.open_process(pid)
                    handle = candidate
                record['in_job'] = api.in_job(handle, receiver.job)
                if not record['in_job']:
                    record['error'] = 'QUERY_MEMBERSHIP_UNKNOWN'
                else:
                    if candidate is not None:
                        handles[pid], candidate = candidate, None
                    record.update(creation=api.creation(handle), signaled=api.wait(handle),
                                  image=_image(api, handle), ppid=psutil.Process(pid).ppid())
            except (OSError, psutil.Error, Refused) as exc:
                record['error'] = type(exc).__name__
                record['winerror'] = getattr(exc, 'winerror', None)
            finally:
                if candidate is not None:
                    api.close(candidate)  # unverified member: close only, never wait/control
            snapshot['members'].append(record)
        _persist(output, report)

    try:
        receiver = WindowsReceiver(api, ticket, argv, tmp_path, '', environment={
            k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR'}},
            protected=decoy_protected(tmp_path),
            command_result=lambda *args, **kw: SimpleNamespace(), sanitize=lambda value: value,
            private_directory=capture, data_directory=capture)
        report['binding'] = {'job': receiver.binding.job_object, 'pid': receiver.binding.root.pid,
                             'creation': receiver.binding.root.creation}
        root = receiver.binding.root
        handles[root.pid] = api.open_process(root.pid)
        if api.creation(handles[root.pid]) != root.creation:
            raise Refused('DIAGNOSTIC_ROOT_IDENTITY_UNKNOWN')
        capture_state('suspended')
        receiver.resume('', authorize=lambda action: action())
        wait_until(marker.exists, timeout=10, what='diagnostic worker ready')
        capture_state('running')
        assert report['snapshots'][-1]['active'] <= 4
    except BaseException as exc:
        report['errors'].append(type(exc).__name__+': '+str(exc))
        _persist(output, report)
        raise
    finally:
        close_failure = None
        try:
            if receiver is not None:
                receiver.close_fail_safe(0)  # owned kernel fail-safe, never cancel ACK
        except Exception as exc:  # health: allow BLE001 -- preserve close error while collecting bounded readback
            close_failure = exc
            report['errors'].append('JOB_CLOSE: '+type(exc).__name__+': '+str(exc))
        finally:
            failures = ['JOB_CLOSE_UNKNOWN'] if close_failure is not None else []
            for pid, handle in handles.items():
                try:
                    signaled = api.fn('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)(handle, 10000) == 0
                    report['terminal'].append({'pid': pid, 'creation': api.creation(handle), 'signaled': signaled})
                    if not signaled:
                        failures.append('QUIESCENCE_UNKNOWN')
                except (OSError, Refused) as exc:
                    failures.append(type(exc).__name__)
                finally:
                    try:
                        api.close(handle)
                    except OSError as exc:
                        failures.append(type(exc).__name__)
            report['errors'].extend(failures)
            _persist(output, report)
            if failures:
                raise Refused('DIAGNOSTIC_CLEANUP_UNKNOWN')
