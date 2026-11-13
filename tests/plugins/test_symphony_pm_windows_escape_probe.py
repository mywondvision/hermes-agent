"""Diagnostic: can a low-integrity worker put a process outside its Job through a low neighbour?

Requires an explicitly approved native campaign. Nothing here asserts that the escape is
prevented: the outcome is recorded as evidence, and an escape that worked is a finding, not a
passed defence. The neighbour is this test's own low-integrity Python process in its own
kill-on-close Job, so whatever the worker manages to create there dies with that Job.
"""
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes as W
import json
import os
import time
import uuid

import pytest

from plugins.symphony_pm.transport.types import Refused
from tests.plugins.symphony_native_crash_support import BoundedApi, NativeRig, base_python, save_proof
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')

IDLE = 'import threading; threading.Event().wait(60)'

WORKER = r'''
import ctypes as C, json, subprocess, sys, time
from ctypes import wintypes as W
from pathlib import Path
ready, target, result, done = (Path(p) for p in sys.argv[1:5])
idle = sys.argv[5]
k = C.WinDLL('kernel32', use_last_error=True)
k.OpenProcess.argtypes, k.OpenProcess.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
k.CloseHandle.argtypes = [W.HANDLE]
k.InitializeProcThreadAttributeList.argtypes = [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)]
k.InitializeProcThreadAttributeList.restype = W.BOOL
k.UpdateProcThreadAttribute.argtypes = [C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p]
k.UpdateProcThreadAttribute.restype = W.BOOL
k.DeleteProcThreadAttributeList.argtypes = [C.c_void_p]
k.CreateProcessW.argtypes = [W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD, C.c_void_p,
                             W.LPCWSTR, C.c_void_p, C.c_void_p]
k.CreateProcessW.restype = W.BOOL

class StartupInfo(C.Structure):
    _fields_ = ([('cb', W.DWORD), ('reserved', W.LPWSTR), ('desktop', W.LPWSTR), ('title', W.LPWSTR)]
                + [(name, W.DWORD) for name in ('x', 'y', 'xs', 'ys', 'xc', 'yc', 'fill', 'flags')]
                + [('show', W.WORD), ('cb2', W.WORD), ('reserved2', C.c_void_p),
                   ('stdin', W.HANDLE), ('stdout', W.HANDLE), ('stderr', W.HANDLE)])

class StartupInfoEx(C.Structure):
    _fields_ = [('info', StartupInfo), ('attributes', C.c_void_p)]

class ProcessInformation(C.Structure):
    _fields_ = [('process', W.HANDLE), ('thread', W.HANDLE), ('pid', W.DWORD), ('tid', W.DWORD)]

def child_of(parent):
    size = C.c_size_t()
    k.InitializeProcThreadAttributeList(None, 1, 0, C.byref(size))
    buffer = (C.c_size_t*((size.value+C.sizeof(C.c_size_t)-1)//C.sizeof(C.c_size_t)))()
    if not k.InitializeProcThreadAttributeList(buffer, 1, 0, C.byref(size)):
        return {'created': False, 'stage': 'attribute-list', 'winerror': C.get_last_error()}
    try:
        value = W.HANDLE(parent)
        if not k.UpdateProcThreadAttribute(buffer, 0, 0x00020000, C.byref(value), C.sizeof(value), None, None):
            return {'created': False, 'stage': 'parent-attribute', 'winerror': C.get_last_error()}
        startup, info = StartupInfoEx(), ProcessInformation()
        startup.info.cb, startup.attributes = C.sizeof(startup), C.addressof(buffer)
        command = C.create_unicode_buffer(subprocess.list2cmdline([sys.executable, '-I', '-B', '-c', idle]))
        # EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW; nothing is inherited
        if not k.CreateProcessW(None, command, None, None, False, 0x00080000 | 0x08000000, None, None,
                                C.byref(startup), C.byref(info)):
            return {'created': False, 'stage': 'create', 'winerror': C.get_last_error()}
        k.CloseHandle(info.process)
        k.CloseHandle(info.thread)
        return {'created': True, 'pid': info.pid}
    finally:
        k.DeleteProcThreadAttributeList(buffer)

out = {'opened': {}, 'errors': {}, 'child': {'created': False, 'stage': 'not-attempted'}}
ready.write_text('ready', encoding='utf-8')
end = time.monotonic() + 20
while not target.exists() and time.monotonic() < end:
    time.sleep(0.05)
try:
    neighbour = int(target.read_text(encoding='utf-8'))
    # opened only, never used: VM_OPERATION|VM_WRITE|CREATE_THREAD (code injection)
    for name, access in (('inject', 0x0008 | 0x0020 | 0x0002), ('create_process', 0x0080)):
        handle = k.OpenProcess(access, False, neighbour)
        out['opened'][name] = bool(handle)
        if not handle:
            out['errors'][name] = C.get_last_error()
            continue
        if name == 'create_process':
            out['child'] = child_of(handle)
        k.CloseHandle(handle)
except Exception as exc:
    out['exception'] = repr(exc)
temporary = result.with_suffix('.tmp')
temporary.write_text(json.dumps(out), encoding='utf-8')
temporary.replace(result)
end = time.monotonic() + 20
while not done.exists() and time.monotonic() < end:
    time.sleep(0.05)
'''


def _signaled(api, handle, timeout_ms):
    return api.fn('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)(handle, timeout_ms) == 0


def test_records_whether_a_worker_can_leave_its_job_through_a_low_integrity_neighbour(tmp_path):
    home = tmp_path/'profile'
    home.mkdir()
    rig = NativeRig(home)
    rig.native.api = BoundedApi(4)  # room for a child that lands in the worker's own Job after all
    target, result, done = rig.work/'neighbour', rig.work/'escape.json', rig.work/'done'
    rig.argv = [base_python(), '-I', '-B', '-c', WORKER, str(rig.marker), str(target), str(result), str(done), IDLE]
    api = BoundedApi(6)  # the neighbour, its console host, and room for whatever the worker creates there
    neighbour_job_id = 'HermesTestNeighbour-'+uuid.uuid4().hex
    neighbour_job = api.create_job(neighbour_job_id)
    neighbour = child = None
    record = {'escaped_worker_job': None}
    try:
        environment = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR'}}
        token = api.worker_token()
        try:
            with (tmp_path/'neighbour-in').open('w+b') as stdin, (tmp_path/'neighbour-log').open('w+b') as log:
                neighbour, thread, neighbour_pid, _ = api.create_suspended(
                    [base_python(), '-I', '-B', '-c', IDLE], tmp_path, environment, (stdin, log, log),
                    neighbour_job, token)
        finally:
            api.close(token)
        api.resume(thread)
        api.close(thread)
        record['neighbour_integrity'] = api.integrity(neighbour)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = rig.start(pool)
            wait_until(rig.marker.exists, timeout=15, what='escape probe worker start')
            receiver = rig.receiver()
            target.write_text(str(neighbour_pid), encoding='utf-8')
            wait_until(result.exists, timeout=25, what='escape probe result')
            observed = json.loads(result.read_text(encoding='utf-8'))
            record['attempt'] = observed
            if observed['child'].get('created'):
                # SYNCHRONIZE | QUERY_LIMITED_INFORMATION | TERMINATE on this test's own stray process
                child = api.checked(api.fn('OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE)(
                    0x101001, False, observed['child']['pid']))
                record['child'] = {'in_worker_job': api.in_job(child, receiver.job),
                                   'in_neighbour_job': api.in_job(child, neighbour_job),
                                   'integrity': api.integrity(child)}
                record['escaped_worker_job'] = not record['child']['in_worker_job']
            else:
                record['escaped_worker_job'] = False
            done.write_text('done', encoding='utf-8')
            future.result(timeout=45)
        record['worker'] = rig.outcome
    finally:
        failures = []

        def step(action):
            try:
                return action()
            except (OSError, Refused) as exc:
                failures.append(repr(exc))

        if child is not None:
            step(lambda: api.terminate_process(child))  # this test's own stray process, whichever Job holds it
        step(lambda: api.close_job(neighbour_job_id))  # kill-on-close stops the neighbour and its Job members
        for name, handle in (('neighbour', neighbour), ('child', child)):
            if handle is not None:
                record[name+'_stopped'] = step(lambda handle=handle: _signaled(api, handle, 10000))
                step(lambda handle=handle: api.close(handle))
        record['cleanup_failures'] = failures
        try:
            save_proof('escape-probe', record)
        finally:
            rig.close(time.monotonic()+10)
    # Only the completeness of the diagnostic and of its own cleanup is asserted.
    assert record['worker'] == {'returncode': 0, 'timed_out': False}
    assert 'exception' not in record['attempt'] and record['escaped_worker_job'] in (True, False)
    assert not failures and record['neighbour_stopped'] and record.get('child_stopped', True)
