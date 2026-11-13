"""Same in-process AccessCheck, for candidate restricting lists. Starts no process, reads no content."""
import ctypes as C, json, sys
from ctypes import wintypes as W
sys.path.insert(0, sys.argv[1])
import read_probe as P
from plugins.symphony_pm.transport.windows_api import WindowsApi
from plugins.symphony_pm.transport.windows_token import attempt_sid
A = attempt_sid('read-probe-20261113')
VARIANTS = {
  'current(att,Everyone,Users,AU,RESTRICTED)': (A,'S-1-1-0','S-1-5-32-545','S-1-5-11','S-1-5-12'),
  'drop AU': (A,'S-1-1-0','S-1-5-32-545','S-1-5-12'),
  'drop AU+Users': (A,'S-1-1-0','S-1-5-12'),
  'att+RESTRICTED only': (A,'S-1-5-12'),
}
keep = {'secret: Claude credentials','secret: another copy .env','HKCU Environment (user env vars)','interpreter','system dll','this worktree file'}
api = WindowsApi()
get_sd = api.adv('GetNamedSecurityInfoW',[W.LPCWSTR,C.c_int,W.DWORD]+[C.c_void_p]*4+[C.POINTER(C.c_void_p)],W.DWORD)
check = api.adv('AccessCheck',[C.c_void_p,W.HANDLE,W.DWORD,C.POINTER(P.Mapping),C.c_void_p,C.POINTER(W.DWORD),C.POINTER(W.DWORD),C.POINTER(W.BOOL)],W.BOOL)
res = {}
for name, sids in VARIANTS.items():
    tok = api.derived_token(sids, 'S-1-16-4096'); imp = W.HANDLE()
    api.checked(api.adv('DuplicateToken',[W.HANDLE,C.c_int,C.POINTER(W.HANDLE)],W.BOOL)(tok,2,C.byref(imp)))
    row = {}
    for kind, path, label in P.targets:
        if label not in keep: continue
        sd = C.c_void_p(); get_sd(path, 1 if kind=='file' else 4, 0x17, None,None,None,None, C.byref(sd))
        pr = C.create_string_buffer(256); pl, g, s = W.DWORD(256), W.DWORD(), W.BOOL()
        check(sd, imp, P.READ[kind], C.byref(P.Mapping(*(P.FILE_MAP if kind=='file' else P.KEY_MAP))), pr, C.byref(pl), C.byref(g), C.byref(s))
        row[label] = 'R' if s.value else '-'
        api.fn('LocalFree',[C.c_void_p],C.c_void_p)(sd)
    api.close(imp.value); api.close(tok); res[name] = row
print(json.dumps(res, ensure_ascii=False, indent=1))
