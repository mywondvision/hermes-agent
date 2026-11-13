"""The worker's token boundary. Importing this module creates no OS resources.

A worker that shares the owner's token can open the owner process and duplicate its Job handle,
which defeats kill-on-close. Mandatory integrity is checked before the DACL, so a low-integrity
worker cannot open a medium-integrity owner for handle duplication, memory writes or thread
creation, and cannot write the owner's files. It can still read whatever the user can read.
"""
import ctypes as C
from ctypes import wintypes as W

from .types import Refused

LOW, MEDIUM = 0x1000, 0x2000  # mandatory integrity RIDs
_TOKEN_QUERY = 0x0008
_TOKEN_SOURCE = 0x0001 | 0x0002 | 0x0008 | 0x0080  # ASSIGN_PRIMARY | DUPLICATE | QUERY | ADJUST_DEFAULT
_DISABLE_MAX_PRIVILEGE, _INTEGRITY_LEVEL, _SE_GROUP_INTEGRITY = 0x1, 25, 0x20
_MANDATORY_POLICY, _NO_WRITE_UP = 27, 0x1  # TokenMandatoryPolicy, TOKEN_MANDATORY_POLICY_NO_WRITE_UP


class SidAndAttributes(C.Structure):
    _fields_ = [('Sid', C.c_void_p), ('Attributes', W.DWORD)]


class TokenBoundary:
    """Mixed into WindowsApi; `adv` resolves advapi32 the way `fn` resolves kernel32."""

    def adv(self, name, args, result):
        if self.A is None:
            self.A = C.WinDLL('advapi32', use_last_error=True)
        fn = getattr(self.A, name)
        fn.argtypes, fn.restype = args, result
        return fn

    def _current_process(self):
        return self.fn('GetCurrentProcess', [], W.HANDLE)()

    def _process_token(self, process, access):
        token = W.HANDLE()
        self.checked(self.adv('OpenProcessToken', [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL)(
            process, access, C.byref(token)))
        return token.value

    def integrity(self, process):
        """The mandatory integrity RID of a process's primary token."""
        token = self._process_token(process, _TOKEN_QUERY)
        try:
            get = self.adv('GetTokenInformation',
                           [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL)
            size = W.DWORD()
            get(token, _INTEGRITY_LEVEL, None, 0, C.byref(size))  # the size query fails by design
            self.checked(size.value)
            buffer = C.create_string_buffer(size.value)
            self.checked(get(token, _INTEGRITY_LEVEL, buffer, size, C.byref(size)))
            sid = C.cast(buffer, C.POINTER(SidAndAttributes)).contents.Sid
            count = self.adv('GetSidSubAuthorityCount', [C.c_void_p], C.POINTER(C.c_ubyte))(sid).contents.value
            return self.adv('GetSidSubAuthority', [C.c_void_p, W.DWORD], C.POINTER(W.DWORD))(
                sid, count-1).contents.value
        finally:
            self.close(token)

    def mandatory_policy(self, process):
        """The mandatory policy bits of a process's primary token."""
        token = self._process_token(process, _TOKEN_QUERY)
        try:
            policy, size = W.DWORD(), W.DWORD()
            self.checked(self.adv('GetTokenInformation',
                [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL)(
                token, _MANDATORY_POLICY, C.byref(policy), C.sizeof(policy), C.byref(size)))
            return policy.value
        finally:
            self.close(token)

    def worker_token(self):
        """A low-integrity copy of this process's token with every removable privilege dropped.

        It is a restricted version of the caller's own primary token, so creating a process with
        it needs no privilege, no other account and no system setting. The caller closes it.
        """
        source = self._process_token(self._current_process(), _TOKEN_SOURCE)
        try:
            token = W.HANDLE()
            self.checked(self.adv('CreateRestrictedToken', [W.HANDLE, W.DWORD, W.DWORD, C.c_void_p, W.DWORD,
                C.c_void_p, W.DWORD, C.c_void_p, C.POINTER(W.HANDLE)], W.BOOL)(
                source, _DISABLE_MAX_PRIVILEGE, 0, None, 0, None, 0, None, C.byref(token)))
        finally:
            self.close(source)
        try:
            sid = C.c_void_p()
            self.checked(self.adv('ConvertStringSidToSidW', [W.LPCWSTR, C.POINTER(C.c_void_p)], W.BOOL)(
                'S-1-16-4096', C.byref(sid)))
            try:
                label = SidAndAttributes(sid.value, _SE_GROUP_INTEGRITY)
                size = C.sizeof(label)+self.adv('GetLengthSid', [C.c_void_p], W.DWORD)(sid)
                self.checked(self.adv('SetTokenInformation', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL)(
                    token, _INTEGRITY_LEVEL, C.byref(label), size))
            finally:
                self.fn('LocalFree', [C.c_void_p], C.c_void_p)(sid)
        except BaseException:
            self.close(token.value)
            raise
        return token.value

    def require_boundary(self, process):
        """Refuse unless the still-suspended worker is low and its token forbids writing up.

        The owner's side of the boundary is `guard_owner`, which runs before any worker exists.
        """
        if self.integrity(process) != LOW or not self.mandatory_policy(process) & _NO_WRITE_UP:
            raise Refused('WORKER_BOUNDARY_UNENFORCED')
