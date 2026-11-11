"""Diagnostic, not a pass/fail contract: can a worker reach its own Job through the owner process?

Requires an explicitly approved native campaign. The worker is this repository's own Python
script; it only duplicates the one Job handle the test names, rewrites the limits it read
unchanged, and exits. Finding the handle value by system handle enumeration is not exercised.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import time

import pytest

from tests.plugins.symphony_native_crash_support import NativeRig, base_python, save_proof
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')

PROBE = r'''
import ctypes as C, json, os, sys, time
from ctypes import wintypes as W
from pathlib import Path
ready, target, result = (Path(p) for p in sys.argv[1:4])
k = C.WinDLL('kernel32', use_last_error=True)
k.OpenProcess.argtypes, k.OpenProcess.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
k.GetCurrentProcess.restype = W.HANDLE
k.DuplicateHandle.argtypes = [W.HANDLE, W.HANDLE, W.HANDLE, C.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD]
k.DuplicateHandle.restype = W.BOOL
k.IsProcessInJob.argtypes, k.IsProcessInJob.restype = [W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)], W.BOOL
k.QueryInformationJobObject.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p]
k.QueryInformationJobObject.restype = W.BOOL
k.SetInformationJobObject.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD]
k.SetInformationJobObject.restype = W.BOOL
k.CloseHandle.argtypes = [W.HANDLE]
out = {'owner_pid': os.getppid(), 'open_owner': False, 'duplicated': False, 'is_own_job': None,
       'limits_read': None, 'limits_rewritten_unchanged': None, 'errors': {}}
ready.write_text('ready', encoding='utf-8')
end = time.monotonic() + 20
while not target.exists() and time.monotonic() < end:
    time.sleep(0.05)
value = int(target.read_text(encoding='utf-8'))
owner = k.OpenProcess(0x0040, False, os.getppid())  # PROCESS_DUP_HANDLE
if not owner:
    out['errors']['open_owner'] = C.get_last_error()
else:
    out['open_owner'] = True
    dup = W.HANDLE()
    if not k.DuplicateHandle(owner, value, k.GetCurrentProcess(), C.byref(dup), 0, False, 2):  # SAME_ACCESS
        out['errors']['duplicate'] = C.get_last_error()
    else:
        out['duplicated'] = True
        inside = W.BOOL()
        if k.IsProcessInJob(k.GetCurrentProcess(), dup, C.byref(inside)):
            out['is_own_job'] = bool(inside.value)
        else:
            out['errors']['is_process_in_job'] = C.get_last_error()
        limits = (C.c_byte * 144)()  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION on x64
        out['limits_read'] = bool(k.QueryInformationJobObject(dup, 9, limits, 144, None))
        if out['limits_read']:
            out['limits_rewritten_unchanged'] = bool(k.SetInformationJobObject(dup, 9, limits, 144))
            if not out['limits_rewritten_unchanged']:
                out['errors']['set_information'] = C.get_last_error()
        else:
            out['errors']['query_information'] = C.get_last_error()
        k.CloseHandle(dup)
    k.CloseHandle(owner)
temporary = result.with_suffix('.tmp')
temporary.write_text(json.dumps(out), encoding='utf-8')
temporary.replace(result)
'''


def test_capture_whether_worker_can_duplicate_its_job_handle_from_the_owner(tmp_path):
    home = tmp_path/'profile'
    home.mkdir()
    target, result = tmp_path/'job-handle', tmp_path/'probe.json'
    rig = NativeRig(home)
    rig.argv = [base_python(), '-I', '-B', '-c', PROBE, str(rig.marker), str(target), str(result)]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = rig.start(pool)
            wait_until(rig.marker.exists, timeout=15, what='probe worker start')
            # The value an attacker would find by enumerating the owner's handles.
            target.write_text(str(int(rig.receiver().job)), encoding='utf-8')
            wait_until(result.exists, timeout=25, what='probe result')
            _, fresh = future.result(timeout=45)
        observed = json.loads(result.read_text(encoding='utf-8'))
        save_proof('worker-job-handle-probe', observed)
        assert fresh
        assert isinstance(observed['open_owner'], bool) and isinstance(observed['duplicated'], bool)
    finally:
        rig.close(time.monotonic()+10)
