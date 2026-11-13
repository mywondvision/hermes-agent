"""Does a deny ACE for a SID that only sits in the restricting list deny the worker? Own temp dir only."""
import ctypes as C, sys, json, subprocess
from ctypes import wintypes as W
sys.path.insert(0, sys.argv[1])
import read_probe as P
from plugins.symphony_pm.transport.windows_api import WindowsApi
from plugins.symphony_pm.transport.windows_token import attempt_sid, SHARED_SIDS
CLASS = 'S-1-9-1-2-3-4'  # throwaway class SID for this probe
d = sys.argv[2]
api = WindowsApi()
def check(sids):
    tok = api.derived_token(sids, 'S-1-16-4096'); imp = W.HANDLE()
    api.checked(api.adv('DuplicateToken',[W.HANDLE,C.c_int,C.POINTER(W.HANDLE)],W.BOOL)(tok,2,C.byref(imp)))
    sd = C.c_void_p(); api.adv('GetNamedSecurityInfoW',[W.LPCWSTR,C.c_int,W.DWORD]+[C.c_void_p]*4+[C.POINTER(C.c_void_p)],W.DWORD)(d,1,0x17,None,None,None,None,C.byref(sd))
    pr = C.create_string_buffer(256); pl,g,s = W.DWORD(256),W.DWORD(),W.BOOL()
    api.adv('AccessCheck',[C.c_void_p,W.HANDLE,W.DWORD,C.POINTER(P.Mapping),C.c_void_p,C.POINTER(W.DWORD),C.POINTER(W.DWORD),C.POINTER(W.BOOL)],W.BOOL)(sd,imp,0x120089,C.byref(P.Mapping(*P.FILE_MAP)),pr,C.byref(pl),C.byref(g),C.byref(s))
    api.close(imp.value); api.close(tok); return 'granted' if s.value else 'denied'
A = attempt_sid('deny-probe')
before = {'without class SID': check((A,*SHARED_SIDS)), 'with class SID': check((A,CLASS,*SHARED_SIDS))}
from plugins.symphony_pm.transport.windows_token import ExplicitAccess
dacl, desc = C.c_void_p(), C.c_void_p()
assert not api.adv('GetNamedSecurityInfoW',[W.LPCWSTR,C.c_int,W.DWORD,C.c_void_p,C.c_void_p,C.POINTER(C.c_void_p),C.c_void_p,C.POINTER(C.c_void_p)],W.DWORD)(d,1,4,None,None,C.byref(dacl),None,C.byref(desc))
e, merged = ExplicitAccess(0x1200A9, 3, 0), C.c_void_p()  # DENY_ACCESS, read+traverse, no inheritance
with api._sids((CLASS,)) as (sid,):
    e.Trustee.Name = sid
    assert not api.adv('SetEntriesInAclW',[W.ULONG,C.c_void_p,C.c_void_p,C.POINTER(C.c_void_p)],W.DWORD)(1,C.byref(e),dacl,C.byref(merged))
sdbuf = C.create_string_buffer(64)
api.checked(api.adv('InitializeSecurityDescriptor',[C.c_void_p,W.DWORD],W.BOOL)(sdbuf,1))
api.checked(api.adv('SetSecurityDescriptorDacl',[C.c_void_p,W.BOOL,C.c_void_p,W.BOOL],W.BOOL)(sdbuf,1,merged,0))
api.checked(api.adv('SetFileSecurityW',[W.LPCWSTR,W.DWORD,C.c_void_p],W.BOOL)(d,4,sdbuf))
after = {'without class SID': check((A,*SHARED_SIDS)), 'with class SID': check((A,CLASS,*SHARED_SIDS))}
print(json.dumps({'before_deny': before, 'after_deny_for_class_sid': after}, indent=1))
