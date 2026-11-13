"""A restricted low-integrity worker cannot put a process outside its Job through a neighbour.

Requires an explicitly approved native campaign. Before the worker's token carried restricting
SIDs, this probe opened a low-integrity neighbour of the same user and created a child there,
outside the worker's Job. Now the open itself must be refused for three neighbours: an ordinary
low-integrity process, an untrusted-integrity process and a worker token of another attempt.
A medium-integrity process of this test is a diagnostic only: whether the worker can open it for
termination is recorded, not asserted.

Every neighbour is this test's own Python process in one kill-on-close Job. Only the low one runs;
the others stay suspended, because a process object answers an open the same way whether or not
its first thread ever ran, and an untrusted process is not known to start on its own.
"""
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes as W
import json
import os
import time
import uuid

import pytest

from plugins.symphony_pm.transport.types import Refused
from plugins.symphony_pm.transport.windows_token import LOW, MEDIUM
from tests.plugins.symphony_native_crash_support import BoundedApi, NativeRig, base_python, save_proof
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')

IDLE = 'import threading; threading.Event().wait(60)'
CONTRACT, DIAGNOSTIC = ('low', 'untrusted', 'other_attempt'), ('medium',)
UNTRUSTED = 0

WORKER = r'''
import ctypes as C, json, subprocess, sys, time
from ctypes import wintypes as W
from pathlib import Path
ready, targets, result, done = (Path(p) for p in sys.argv[1:5])
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
# opened only, never used, except that a create-process handle is tried as a substitute parent
RIGHTS = (('inject', 0x0008 | 0x0020 | 0x0002), ('create_process', 0x0080), ('terminate', 0x0001))

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

out = {'targets': {}}
ready.write_text('ready', encoding='utf-8')
end = time.monotonic() + 20
while not targets.exists() and time.monotonic() < end:
    time.sleep(0.05)
try:
    plan = json.loads(targets.read_text(encoding='utf-8'))
    for group in ('contract', 'diagnostic'):
        for name, pid in plan[group].items():
            record = {'opened': {}, 'errors': {}, 'child': {'created': False, 'stage': 'not-attempted'}}
            out['targets'][name] = record
            for right, access in RIGHTS:
                if group == 'diagnostic' and right != 'terminate':
                    continue
                handle = k.OpenProcess(access, False, pid)
                record['opened'][right] = bool(handle)
                if not handle:
                    record['errors'][right] = C.get_last_error()
                    continue
                if right == 'create_process':
                    record['child'] = child_of(handle)
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


def _neighbours(api, job, tmp_path, created):
    """Fills name -> (process handle, pid): one running low process and three that stay suspended."""
    environment = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR'}}
    tokens = {'low': lambda: api.derived_token((), 'S-1-16-4096'),  # the user's ordinary low-integrity process
              'untrusted': lambda: api.derived_token((), 'S-1-16-0'),
              'other_attempt': lambda: api.worker_token('escape-probe-other-attempt'),
              'medium': lambda: None}  # BoundedApi creates it with this test's own token
    with (tmp_path/'neighbour-in').open('w+b') as stdin, (tmp_path/'neighbour-log').open('w+b') as log:
        for name, build in tokens.items():
            token = build()
            try:
                process, thread, pid, _ = api.create_suspended(
                    [base_python(), '-I', '-B', '-c', IDLE], tmp_path, environment, (stdin, log, log), job, token)
            finally:
                if token is not None:
                    api.close(token)
            created[name] = (process, pid)
            try:
                if name == 'low':
                    api.resume(thread)
            finally:
                api.close(thread)


def test_restricted_worker_cannot_open_a_neighbour_to_create_or_inject_a_process(tmp_path):
    home = tmp_path/'profile'
    home.mkdir()
    rig = NativeRig(home)
    rig.native.api = BoundedApi(4)  # room for a child that lands in the worker's own Job after all
    targets, result, done = rig.work/'neighbours.json', rig.work/'escape.json', rig.work/'done'
    rig.argv = [base_python(), '-I', '-B', '-c', WORKER, str(rig.marker), str(targets), str(result), str(done), IDLE]
    api = BoundedApi(10)  # four neighbours, one console host, and room for whatever the worker creates there
    neighbour_job_id = 'HermesTestNeighbour-'+uuid.uuid4().hex
    neighbour_job = api.create_job(neighbour_job_id)
    neighbours, children = {}, {}
    record = {'children': {}}
    try:
        _neighbours(api, neighbour_job, tmp_path, neighbours)
        record['neighbours'] = {name: {'integrity': api.integrity(process),
                                       'restricted': bool(api.restricting_sids(process))}
                                for name, (process, _) in neighbours.items()}
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = rig.start(pool)
            wait_until(rig.marker.exists, timeout=15, what='escape probe worker start')
            receiver = rig.receiver()
            record['worker'] = {'integrity': api.integrity(receiver.process),
                                'restricting_sids': len(api.restricting_sids(receiver.process))}
            plan = {'contract': {name: neighbours[name][1] for name in CONTRACT},
                    'diagnostic': {name: neighbours[name][1] for name in DIAGNOSTIC}}
            temporary = targets.with_suffix('.tmp')
            temporary.write_text(json.dumps(plan), encoding='utf-8')
            temporary.replace(targets)
            wait_until(result.exists, timeout=25, what='escape probe result')
            observed = json.loads(result.read_text(encoding='utf-8'))
            record['attempt'] = observed
            for name, target in observed['targets'].items():
                if target['child'].get('created'):
                    # SYNCHRONIZE | QUERY_LIMITED_INFORMATION | TERMINATE on this test's own stray process
                    child = api.checked(api.fn('OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE)(
                        0x101001, False, target['child']['pid']))
                    children[name] = child
                    record['children'][name] = {'in_worker_job': api.in_job(child, receiver.job),
                                                'in_neighbour_job': api.in_job(child, neighbour_job),
                                                'integrity': api.integrity(child)}
            done.write_text('done', encoding='utf-8')
            future.result(timeout=45)
        record['outcome'] = rig.outcome
    finally:
        failures = []

        def step(action):
            try:
                return action()
            except (OSError, Refused) as exc:
                failures.append(repr(exc))

        for child in children.values():
            step(lambda child=child: api.terminate_process(child))  # a stray process, whichever Job holds it
        step(lambda: api.close_job(neighbour_job_id))  # kill-on-close stops every neighbour and its Job members
        record['stopped'] = {}
        for name, handle in (*((name, process) for name, (process, _) in neighbours.items()),
                             *(('child-of-'+name, child) for name, child in children.items())):
            record['stopped'][name] = step(lambda handle=handle: _signaled(api, handle, 10000))
            step(lambda handle=handle: api.close(handle))
        record['cleanup_failures'] = failures
        try:
            save_proof('escape-probe', record)
        finally:
            rig.close(time.monotonic()+10)
    assert record['outcome'] == {'returncode': 0, 'timed_out': False} and 'exception' not in record['attempt']
    assert {name: entry['integrity'] for name, entry in record['neighbours'].items()} == {
        'low': LOW, 'untrusted': UNTRUSTED, 'other_attempt': LOW, 'medium': MEDIUM}
    assert record['worker']['integrity'] == LOW and record['worker']['restricting_sids'] > 0
    for name in CONTRACT:
        target = record['attempt']['targets'][name]
        for right in ('create_process', 'inject'):
            assert target['opened'][right] is False and target['errors'][right] == 5, (name, right)
        assert target['child'] == {'created': False, 'stage': 'not-attempted'}, name
    assert record['children'] == {}
    assert not failures and all(record['stopped'].values())
