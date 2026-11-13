"""The worker's token boundary. Importing this module creates no OS resources.

A worker that shares the owner's token can open the owner process and duplicate its Job handle,
which defeats kill-on-close. Mandatory integrity is checked before the DACL, so a low-integrity
worker cannot open a medium-integrity owner for handle duplication, memory writes or thread
creation, and cannot write the owner's files.

Integrity alone leaves every other low-integrity object of the same user open: the worker's token
still carries the user's SID, so it could open a low neighbour process and create a child there,
outside its own Job. The token therefore also carries restricting SIDs. Windows checks such a
token twice, against its ordinary SIDs and against the restricting list, and grants only what
both checks grant. The list holds a SID unique to the attempt and the shared groups a process
needs to load at all; it holds neither the user nor the logon session, so an object that admits
only those is closed to the worker whatever its integrity.

The shared groups still read most of a disk whose root admits Users. The list therefore also
holds one SID common to every worker and to nothing else: a deny entry for it on a directory
closes that directory to workers only. Such an entry is not inherited, so it closes the subtree
only because the worker token holds no privilege at all, in particular not the one that bypasses
traverse checking.
"""
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import hashlib
import struct

from .types import Refused

LOW, MEDIUM = 0x1000, 0x2000  # mandatory integrity RIDs
_TOKEN_QUERY = 0x0008
_TOKEN_SOURCE = 0x0001 | 0x0002 | 0x0008 | 0x0080  # ASSIGN_PRIMARY | DUPLICATE | QUERY | ADJUST_DEFAULT
_DISABLE_MAX_PRIVILEGE, _SE_GROUP_INTEGRITY = 0x1, 0x20
_DEFAULT_DACL, _RESTRICTED_SIDS, _INTEGRITY_LEVEL, _MANDATORY_POLICY = 6, 11, 25, 27  # TOKEN_INFORMATION_CLASS
_NO_WRITE_UP = 0x1  # TOKEN_MANDATORY_POLICY_NO_WRITE_UP
_GENERIC_ALL, _GRANT_ACCESS = 0x10000000, 1
_LOW_SID = 'S-1-16-4096'
# Everyone, BUILTIN\Users and Authenticated Users reach system files and the interpreter; RESTRICTED
# is what this session's window station and desktop admit besides the logon session itself, and a
# process that cannot open them does not start.
SHARED_SIDS = ('S-1-1-0', 'S-1-5-32-545', 'S-1-5-11', 'S-1-5-12')
_PRIVILEGES = 3  # TokenPrivileges


def _resource_sid(text):
    """Resource-manager authority, four words of the digest of `text`."""
    words = struct.unpack('<4I', hashlib.sha256(text.encode('utf-8')).digest()[:16])
    return 'S-1-9-'+'-'.join(str(word) for word in words)


def attempt_sid(attempt):
    """The SID that stands for one attempt."""
    return _resource_sid('symphony_pm:attempt:'+attempt)


#: The SID every worker carries and nothing else does; a deny entry for it closes an object to workers.
WORKER_CLASS_SID = _resource_sid('symphony_pm:worker-class')


def restricting_sids(attempt):
    return (attempt_sid(attempt), WORKER_CLASS_SID, *SHARED_SIDS)


class SidAndAttributes(C.Structure):
    _fields_ = [('Sid', C.c_void_p), ('Attributes', W.DWORD)]


class Trustee(C.Structure):
    _fields_ = [('Multiple', C.c_void_p), ('Operation', C.c_int), ('Form', C.c_int), ('Type', C.c_int),
                ('Name', C.c_void_p)]


class ExplicitAccess(C.Structure):
    _fields_ = [('Permissions', W.DWORD), ('Mode', C.c_int), ('Inheritance', W.DWORD), ('Trustee', Trustee)]


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

    def _token_information(self, token, kind):
        get = self.adv('GetTokenInformation', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL)
        size = W.DWORD()
        get(token, kind, None, 0, C.byref(size))  # the size query fails by design
        self.checked(size.value)
        buffer = C.create_string_buffer(size.value)
        self.checked(get(token, kind, buffer, size, C.byref(size)))
        return buffer

    def _local_free(self, pointer):
        self.fn('LocalFree', [C.c_void_p], C.c_void_p)(pointer)

    @contextmanager
    def _sids(self, texts):
        """Binary SIDs for `texts`, freed on exit."""
        sids = []
        try:
            for text in texts:
                sid = C.c_void_p()
                self.checked(self.adv('ConvertStringSidToSidW', [W.LPCWSTR, C.POINTER(C.c_void_p)], W.BOOL)(
                    text, C.byref(sid)))
                sids.append(sid.value)
            yield sids
        finally:
            for sid in sids:
                self._local_free(sid)

    def _sid_text(self, sid):
        text = C.c_void_p()
        self.checked(self.adv('ConvertSidToStringSidW', [C.c_void_p, C.POINTER(C.c_void_p)], W.BOOL)(
            sid, C.byref(text)))
        try:
            return C.wstring_at(text.value)
        finally:
            self._local_free(text)

    @contextmanager
    def with_grant(self, acl, sid_text, access, inheritance):
        """A new ACL, freed on exit: `acl` plus one allow entry for `sid_text`."""
        entry, merged = ExplicitAccess(access, _GRANT_ACCESS, inheritance), C.c_void_p()
        with self._sids((sid_text,)) as (sid,):
            entry.Trustee.Name = sid  # TrusteeForm 0 is TRUSTEE_IS_SID
            code = self.adv('SetEntriesInAclW', [W.ULONG, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p)],
                            W.DWORD)(1, C.byref(entry), acl, C.byref(merged))
        if code:
            raise C.WinError(code)
        try:
            yield merged
        finally:
            self._local_free(merged)

    def integrity(self, process):
        """The mandatory integrity RID of a process's primary token."""
        token = self._process_token(process, _TOKEN_QUERY)
        try:
            buffer = self._token_information(token, _INTEGRITY_LEVEL)
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

    def restricting_sids(self, process):
        """The restricting SIDs of a process's primary token, as strings; empty for an ordinary token."""
        token = self._process_token(process, _TOKEN_QUERY)
        try:
            buffer = self._token_information(token, _RESTRICTED_SIDS)  # TOKEN_GROUPS
            count = W.DWORD.from_buffer(buffer).value
            if not count:
                return ()
            groups = (SidAndAttributes*count).from_buffer(buffer, C.sizeof(C.c_void_p))  # pointer-aligned array
            return tuple(self._sid_text(group.Sid) for group in groups)
        finally:
            self.close(token)

    def privilege_count(self, process):
        """How many privileges a process's primary token holds, enabled or not."""
        token = self._process_token(process, _TOKEN_QUERY)
        try:
            return W.DWORD.from_buffer(self._token_information(token, _PRIVILEGES)).value
        finally:
            self.close(token)

    def derived_token(self, restricting, integrity_sid, *, no_privileges=False):
        """A copy of this process's token with the given restricting SIDs and mandatory level. The
        caller closes it.

        By default every removable privilege is dropped, which keeps the one that bypasses traverse
        checking. With `no_privileges` every privilege the source holds is deleted, that one too.
        """
        source = self._process_token(self._current_process(), _TOKEN_SOURCE)
        try:
            token = W.HANDLE()
            flags, deleted, count = _DISABLE_MAX_PRIVILEGE, None, 0
            if no_privileges:
                held = self._token_information(source, _PRIVILEGES)  # TOKEN_PRIVILEGES: count, LUID_AND_ATTRIBUTES[]
                flags, count = 0, W.DWORD.from_buffer(held).value
                deleted = C.addressof(held)+C.sizeof(W.DWORD) if count else None
            with self._sids(restricting) as sids:
                listed = (SidAndAttributes*len(sids))(*((sid, 0) for sid in sids))
                self.checked(self.adv('CreateRestrictedToken', [W.HANDLE, W.DWORD, W.DWORD, C.c_void_p, W.DWORD,
                    C.c_void_p, W.DWORD, C.c_void_p, C.POINTER(W.HANDLE)], W.BOOL)(
                    source, flags, 0, None, count, deleted, len(sids), listed if sids else None,
                    C.byref(token)))
        finally:
            self.close(source)
        try:
            with self._sids((integrity_sid,)) as (sid,):
                label = SidAndAttributes(sid, _SE_GROUP_INTEGRITY)
                size = C.sizeof(label)+self.adv('GetLengthSid', [C.c_void_p], W.DWORD)(sid)
                self.checked(self.adv('SetTokenInformation', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL)(
                    token, _INTEGRITY_LEVEL, C.byref(label), size))
        except BaseException:
            self.close(token.value)
            raise
        return token.value

    def _admit_to_own_objects(self, token, sid_text):
        """Add `sid_text` to the token's default DACL.

        What the worker creates without an explicit descriptor (a child process, a pipe) takes that
        DACL. Without the attempt's SID there, the restricting check would lock the worker out of
        its own objects.
        """
        buffer = self._token_information(token, _DEFAULT_DACL)  # TOKEN_DEFAULT_DACL: one ACL pointer
        with self.with_grant(C.c_void_p.from_buffer(buffer).value, sid_text, _GENERIC_ALL, 0) as merged:
            self.checked(self.adv('SetTokenInformation', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL)(
                token, _DEFAULT_DACL, C.byref(merged), C.sizeof(merged)))

    def worker_token(self, attempt):
        """A low-integrity, restricted copy of this process's token for exactly this attempt.

        It is a restricted version of the caller's own primary token, so creating a process with
        it needs no privilege, no other account and no system setting. The caller closes it.
        """
        token = self.derived_token(restricting_sids(attempt), _LOW_SID, no_privileges=True)
        try:
            self._admit_to_own_objects(token, attempt_sid(attempt))
        except BaseException:
            self.close(token)
            raise
        return token

    def require_boundary(self, process, attempt):
        """Refuse unless the still-suspended worker is low, its token forbids writing up, holds no
        privilege and its restricting SIDs are exactly this attempt's: no user, no logon session,
        no other attempt.

        The owner's side of the boundary is `guard_owner`, which runs before any worker exists.
        """
        if (self.integrity(process) != LOW or not self.mandatory_policy(process) & _NO_WRITE_UP
                or self.privilege_count(process)
                or sorted(self.restricting_sids(process)) != sorted(restricting_sids(attempt))):
            raise Refused('WORKER_BOUNDARY_UNENFORCED')
