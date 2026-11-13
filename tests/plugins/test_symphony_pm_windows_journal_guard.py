"""A low-integrity worker can neither read, change nor lock the owner's journal and plugin data.

Requires an explicitly approved native campaign. The worker is this repository's own Python
script and the files are this test's own profile; every attempt is ordered so that one that
unexpectedly succeeded does the least damage first (read, lock, open for write, rename, delete).
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time

import pytest

from plugins.symphony_pm.transport.windows_labels import DATA_POLICY
from plugins.symphony_pm.transport.windows_token import MEDIUM
from tests.plugins.symphony_native_crash_support import NativeRig, base_python, save_proof
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')

ATTEMPTS = ('read', 'lock', 'overwrite', 'rename', 'delete')

WORKER = r'''
import ctypes as C, json, os, sys, time
from ctypes import wintypes as W
from pathlib import Path
ready, targets, result = (Path(p) for p in sys.argv[1:4])
k = C.WinDLL('kernel32', use_last_error=True)
k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
k.CreateFileW.restype = W.HANDLE
k.CloseHandle.argtypes = [W.HANDLE]
k.LockFile.argtypes, k.LockFile.restype = [W.HANDLE] + [W.DWORD]*4, W.BOOL
INVALID = W.HANDLE(-1).value

def opened(path, access):
    handle = k.CreateFileW(path, access, 7, None, 3, 0, None)  # share everything, OPEN_EXISTING
    return (None, C.get_last_error()) if handle in (None, INVALID) else (handle, 0)

def attempt(action, path):
    if action in ('read', 'overwrite', 'lock'):
        handle, error = opened(path, 0x40000000 if action == 'overwrite' else 0x80000000)
        if handle is None:
            return {'done': False, 'winerror': error}
        done = True
        if action == 'lock':
            done = bool(k.LockFile(handle, 0, 0, 1, 0))
            error = 0 if done else C.get_last_error()
        k.CloseHandle(handle)
        return {'done': done, 'winerror': error}
    try:
        if action == 'rename':
            os.rename(path, path + '.moved')
            os.rename(path + '.moved', path)
        else:
            os.remove(path)
        return {'done': True, 'winerror': 0}
    except OSError as exc:
        return {'done': False, 'winerror': exc.winerror}

ready.write_text('ready', encoding='utf-8')
end = time.monotonic() + 20
while not targets.exists() and time.monotonic() < end:
    time.sleep(0.05)
plan = json.loads(targets.read_text(encoding='utf-8'))
out = {'files': {}, 'listing': {}}
for directory in plan['directories']:
    try:
        out['listing'][directory] = {'done': True, 'count': len(os.listdir(directory))}
    except OSError as exc:
        out['listing'][directory] = {'done': False, 'winerror': exc.winerror}
for path in plan['files']:
    out['files'][path] = {action: attempt(action, path) for action in plan['attempts']}
temporary = result.with_suffix('.tmp')
temporary.write_text(json.dumps(out), encoding='utf-8')
temporary.replace(result)
'''


def test_low_integrity_worker_cannot_read_change_or_lock_the_journal(tmp_path):
    home = tmp_path/'profile'
    home.mkdir()
    rig = NativeRig(home)
    targets, result = rig.work/'targets.json', rig.work/'journal-guard.json'
    rig.argv = [base_python(), '-I', '-B', '-c', WORKER, str(rig.marker), str(targets), str(result)]
    journal = Path(rig.owner.journal.path)  # the journal's own spelling of the profile path
    data = journal.parents[1]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = rig.start(pool)
            wait_until(rig.marker.exists, timeout=15, what='journal guard worker start')
            # SQLite's transient sidecars come and go while the owner writes; they are not targets
            files = sorted(str(Path(parent)/name) for parent, _, names in os.walk(data) for name in names
                           if not name.endswith(('-journal', '-wal', '-shm')))
            directories = sorted({str(data), *(str(Path(path).parent) for path in files)})
            plan = {'files': files, 'directories': directories, 'attempts': ATTEMPTS}
            temporary = targets.with_suffix('.tmp')
            temporary.write_text(json.dumps(plan), encoding='utf-8')
            temporary.replace(targets)
            wait_until(result.exists, timeout=25, what='journal guard result')
            future.result(timeout=45)  # the owner still records the outcome in the journal after the attempts
        observed = json.loads(result.read_text(encoding='utf-8'))
        labels = {path: rig.native.api.path_label(path) for path in (*directories, *files) if Path(path).exists()}
        save_proof('journal-guard', {'attempts': observed, 'labels': labels, 'worker': rig.outcome})
        assert rig.outcome == {'returncode': 0, 'timed_out': False}
        assert str(journal) in files and journal.exists()
        assert labels and all(label == (MEDIUM, DATA_POLICY) for label in labels.values())
        for directory in directories:
            assert observed['listing'][directory] == {'done': False, 'winerror': 5}, directory
        for path in files:
            for action in ATTEMPTS:
                assert observed['files'][path][action] == {'done': False, 'winerror': 5}, (path, action)
        _, row = rig.owner.journal.read_launch(rig.owner.token, rig.ticket.project, rig.ticket.job,
                                               rig.ticket.revision)
        assert row['result'] is not None  # the journal took the owner's write that followed the attempts
    finally:
        rig.close(time.monotonic()+10)
