"""In-process AccessCheck with a duplicated worker token. Starts no process; reads no file content."""
import ctypes as C
from ctypes import wintypes as W
import json
import os
import sys

sys.path.insert(0, sys.argv[1])
from plugins.symphony_pm.transport.windows_api import WindowsApi  # noqa: E402

FILE_MAP = (0x120089, 0x120116, 0x1200A0, 0x1F01FF)
KEY_MAP = (0x20019, 0x20006, 0x20019, 0xF003F)
READ = {'file': 0x120089, 'key': 0x20019}
WRITE = {'file': 0x120116, 'key': 0x20006}

profile = os.environ['USERPROFILE']
targets = [
    ('file', r'D:\Claude\config\.credentials.json', 'secret: Claude credentials'),
    ('file', r'D:\AI\worktrees\qual-myw-61\.env', 'secret: another copy .env'),
    ('file', r'D:\AI\preserve', 'private packages dir'),
    ('file', profile + r'\.ssh', 'profile .ssh'),
    ('file', profile + r'\.codex', 'profile .codex'),
    ('file', r'D:\AI\worktrees\hermes-pm-ingress-20261031\.git\config', 'this repo .git/config'),
    ('file', r'D:\AI\worktrees\hermes-pm-ingress-20261031\pyproject.toml', 'this worktree file'),
    ('file', r'D:\AI\worktrees\kit-wave1-integration-20261031\README.md', 'another copy file'),
    ('file', r'D:\AI\envs\hermes-pm-ingress-test-20261106\Scripts\python.exe', 'interpreter'),
    ('file', r'C:\Windows\System32\kernel32.dll', 'system dll'),
    ('key', r'CURRENT_USER\Environment', 'HKCU Environment (user env vars)'),
    ('key', r'CURRENT_USER\Software', 'HKCU Software'),
]


class Mapping(C.Structure):
    _fields_ = [('r', W.DWORD), ('w', W.DWORD), ('x', W.DWORD), ('a', W.DWORD)]


def main():
    api = WindowsApi()
    token = api.worker_token('read-probe-20261113')
    imp = W.HANDLE()
    api.checked(api.adv('DuplicateToken', [W.HANDLE, C.c_int, C.POINTER(W.HANDLE)], W.BOOL)(token, 2, C.byref(imp)))
    get_sd = api.adv('GetNamedSecurityInfoW', [W.LPCWSTR, C.c_int, W.DWORD] + [C.c_void_p]*4
                     + [C.POINTER(C.c_void_p)], W.DWORD)
    check = api.adv('AccessCheck', [C.c_void_p, W.HANDLE, W.DWORD, C.POINTER(Mapping), C.c_void_p,
                                    C.POINTER(W.DWORD), C.POINTER(W.DWORD), C.POINTER(W.BOOL)], W.BOOL)
    out = []
    for kind, path, label in targets:
        sd = C.c_void_p()
        code = get_sd(path, 1 if kind == 'file' else 4, 0x1 | 0x2 | 0x4 | 0x10, None, None, None, None, C.byref(sd))
        if code:
            out.append({'target': label, 'error': code})
            continue
        row = {'target': label}
        for name, desired in (('read', READ[kind]), ('write', WRITE[kind])):
            privs = C.create_string_buffer(256)
            plen, granted, status = W.DWORD(256), W.DWORD(), W.BOOL()
            ok = check(sd, imp, desired, C.byref(Mapping(*(FILE_MAP if kind == 'file' else KEY_MAP))), privs,
                       C.byref(plen), C.byref(granted), C.byref(status))
            row[name] = ('granted' if status.value else 'denied') if ok else 'error %d' % C.get_last_error()
        api.fn('LocalFree', [C.c_void_p], C.c_void_p)(sd)
        out.append(row)
    api.close(imp.value)
    api.close(token)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
