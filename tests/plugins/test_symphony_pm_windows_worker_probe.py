"""A low-integrity worker cannot reach the labelled owner process or anything outside its own copy.

Requires an explicitly approved native campaign. The worker is this repository's own Python
script. It is handed the owner's real Job handle value, so a refusal here is the kernel's, not
ignorance of the value; finding that value by system handle enumeration is not exercised.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
import time

import pytest

from plugins.symphony_pm.transport.windows_labels import PROCESS_POLICY
from plugins.symphony_pm.transport.windows_token import LOW, MEDIUM
from tests.plugins.symphony_native_crash_support import NativeRig, base_python, save_proof
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')

# Every right is only opened, never used. The process label forbids write, read and execute up.
PROCESS_RIGHTS = {'dup_handle': 0x0040, 'inject': 0x0008 | 0x0020 | 0x0002, 'terminate': 0x0001,
                  'vm_read': 0x0010, 'suspend_resume': 0x0800, 'create_process': 0x0080,
                  'query_limited': 0x1000, 'synchronize': 0x00100000}
# A thread keeps its own default label, which this slice does not change: recorded, not asserted.
THREAD_RIGHTS = {'thread_terminate': 0x0001, 'thread_suspend_resume': 0x0002, 'thread_query_limited': 0x0800}

PROBE = r'''
import ctypes as C, json, os, sys, time
from ctypes import wintypes as W
from pathlib import Path
ready, target, result, outside = (Path(p) for p in sys.argv[1:5])
process_rights, thread_rights = json.loads(sys.argv[5]), json.loads(sys.argv[6])
k = C.WinDLL('kernel32', use_last_error=True)
k.OpenProcess.argtypes, k.OpenProcess.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
k.OpenThread.argtypes, k.OpenThread.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
k.GetCurrentProcess.restype = W.HANDLE
k.DuplicateHandle.argtypes = [W.HANDLE, W.HANDLE, W.HANDLE, C.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD]
k.DuplicateHandle.restype = W.BOOL
k.CloseHandle.argtypes = [W.HANDLE]
out = {'owner_pid': os.getppid(), 'opened': {}, 'errors': {}, 'duplicated': False}
ready.write_text('ready', encoding='utf-8')
end = time.monotonic() + 20
while not target.exists() and time.monotonic() < end:
    time.sleep(0.05)
job, thread = (int(value) for value in target.read_text(encoding='utf-8').split())
for name, access in process_rights.items():
    owner = k.OpenProcess(access, False, os.getppid())
    out['opened'][name] = bool(owner)
    if not owner:
        out['errors'][name] = C.get_last_error()
        continue
    if name == 'dup_handle':
        dup = W.HANDLE()
        out['duplicated'] = bool(k.DuplicateHandle(owner, job, k.GetCurrentProcess(), C.byref(dup), 0, False, 2))
        if out['duplicated']:
            k.CloseHandle(dup)
    k.CloseHandle(owner)
for name, access in thread_rights.items():
    handle = k.OpenThread(access, False, thread)
    out['opened'][name] = bool(handle)
    if handle:
        k.CloseHandle(handle)
    else:
        out['errors'][name] = C.get_last_error()
try:
    outside.write_text('worker was here', encoding='utf-8')
    out['wrote_outside'] = True
except OSError as exc:
    out['wrote_outside'], out['errors']['outside'] = False, [exc.errno, exc.winerror]
temporary = result.with_suffix('.tmp')
temporary.write_text(json.dumps(out), encoding='utf-8')
temporary.replace(result)
'''


def test_low_integrity_worker_cannot_open_the_owner_or_write_outside_its_copy(tmp_path):
    home = tmp_path/'profile'
    home.mkdir()
    rig = NativeRig(home)
    target, outside, result = rig.work/'job-handle', tmp_path/'owner-side-file', rig.work/'probe.json'
    rig.argv = [base_python(), '-I', '-B', '-c', PROBE, str(rig.marker), str(target), str(result), str(outside),
                json.dumps(PROCESS_RIGHTS), json.dumps(THREAD_RIGHTS)]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = rig.start(pool)
            wait_until(rig.marker.exists, timeout=15, what='probe worker start')
            receiver = rig.receiver()
            worker_integrity = rig.native.api.integrity(receiver.process)
            target.write_text(f'{int(receiver.job)} {threading.main_thread().native_id}', encoding='utf-8')
            wait_until(result.exists, timeout=25, what='probe result')
            future.result(timeout=45)
        observed = json.loads(result.read_text(encoding='utf-8'))
        owner_label = rig.native.api.process_label()
        save_proof('worker-boundary-probe', {'probe': observed, 'worker': rig.outcome,
            'worker_integrity': worker_integrity, 'owner_label': owner_label})
        assert rig.outcome == {'returncode': 0, 'timed_out': False}
        assert worker_integrity == LOW and owner_label == (MEDIUM, PROCESS_POLICY)
        assert observed['owner_pid'] == os.getpid()  # the refusals below are about this process
        for name in PROCESS_RIGHTS:
            assert observed['opened'][name] is False and observed['errors'][name] == 5, name
        assert observed['duplicated'] is False
        assert observed['wrote_outside'] is False and not outside.exists()
    finally:
        rig.close(time.monotonic()+10)
