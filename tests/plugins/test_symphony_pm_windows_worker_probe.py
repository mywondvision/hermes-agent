"""A low-integrity worker cannot reach the owner process or anything outside its own copy.

Requires an explicitly approved native campaign. The worker is this repository's own Python
script. It is handed the owner's real Job handle value, so a refusal here is the kernel's, not
ignorance of the value; finding that value by system handle enumeration is not exercised.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import time

import pytest

from plugins.symphony_pm.transport.windows_token import LOW
from tests.plugins.symphony_native_crash_support import NativeRig, base_python, save_proof
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')

PROBE = r'''
import ctypes as C, json, os, sys, time
from ctypes import wintypes as W
from pathlib import Path
ready, target, result, outside = (Path(p) for p in sys.argv[1:5])
k = C.WinDLL('kernel32', use_last_error=True)
k.OpenProcess.argtypes, k.OpenProcess.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
k.GetCurrentProcess.restype = W.HANDLE
k.DuplicateHandle.argtypes = [W.HANDLE, W.HANDLE, W.HANDLE, C.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD]
k.DuplicateHandle.restype = W.BOOL
k.CloseHandle.argtypes = [W.HANDLE]
out = {'owner_pid': os.getppid(), 'opened': {}, 'errors': {}, 'duplicated': False}
ready.write_text('ready', encoding='utf-8')
end = time.monotonic() + 20
while not target.exists() and time.monotonic() < end:
    time.sleep(0.05)
value = int(target.read_text(encoding='utf-8'))
# DUP_HANDLE; VM_OPERATION|VM_WRITE|CREATE_THREAD (code injection)
for name, access in (('dup_handle', 0x0040), ('inject', 0x0008 | 0x0020 | 0x0002), ('query_limited', 0x1000),
                     ('terminate', 0x0001)):  # opened only, never used; the last two are recorded, not asserted
    owner = k.OpenProcess(access, False, os.getppid())
    out['opened'][name] = bool(owner)
    if not owner:
        out['errors'][name] = C.get_last_error()
        continue
    if name == 'dup_handle':
        dup = W.HANDLE()
        out['duplicated'] = bool(k.DuplicateHandle(owner, value, k.GetCurrentProcess(), C.byref(dup), 0, False, 2))
        if out['duplicated']:
            k.CloseHandle(dup)
    k.CloseHandle(owner)
try:
    outside.write_text('worker was here', encoding='utf-8')
    out['wrote_outside'] = True
except OSError as exc:
    out['wrote_outside'], out['errors']['outside'] = False, exc.winerror
temporary = result.with_suffix('.tmp')
temporary.write_text(json.dumps(out), encoding='utf-8')
temporary.replace(result)
'''


def test_low_integrity_worker_cannot_open_the_owner_or_write_outside_its_copy(tmp_path):
    home = tmp_path/'profile'
    home.mkdir()
    rig = NativeRig(home)
    target, outside, result = tmp_path/'job-handle', tmp_path/'owner-side-file', rig.work/'probe.json'
    rig.argv = [base_python(), '-I', '-B', '-c', PROBE, str(rig.marker), str(target), str(result), str(outside)]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = rig.start(pool)
            wait_until(rig.marker.exists, timeout=15, what='probe worker start')
            receiver = rig.receiver()
            worker_integrity = rig.native.api.integrity(receiver.process)
            target.write_text(str(int(receiver.job)), encoding='utf-8')
            wait_until(result.exists, timeout=25, what='probe result')
            future.result(timeout=45)
        observed = json.loads(result.read_text(encoding='utf-8'))
        save_proof('worker-boundary-probe', {'probe': observed, 'worker': rig.outcome,
                                             'worker_integrity': worker_integrity})
        assert rig.outcome == {'returncode': 0, 'timed_out': False}
        assert worker_integrity == LOW
        assert observed['owner_pid'] == os.getpid()  # the refusals below are about this process
        assert observed['opened']['dup_handle'] is False and observed['errors']['dup_handle'] == 5
        assert observed['opened']['inject'] is False and observed['errors']['inject'] == 5
        assert observed['duplicated'] is False
        assert observed['wrote_outside'] is False and not outside.exists()
    finally:
        rig.close(time.monotonic()+10)
