"""What a worker may not read, and the reversible deny entries that say so. Importing creates nothing.

Low integrity stops a worker writing up, not reading. On a disk whose root admits Users, the shared
SIDs in the worker's restricting list read almost everything. An object is closed to workers by
one deny entry for `WORKER_CLASS_SID`: no ordinary token carries that SID, so nothing else changes.

The entry is not inherited and is written with the legacy per-object call, so no child object is
rewritten. It closes the subtree only together with a worker token that holds no privilege
(`worker_token`), because traversal is then checked at every directory on the path. `AccessCheck`
looks at one object and cannot show that; only a real process can.

Before a worker exists, `require_read_boundary` checks every listed object: the entry is present
and the worker token is refused read. An empty list is a refusal, never an open default.
"""
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import re

from .types import Refused
from .windows_token import WORKER_CLASS_SID

FILE, KEY = 'file', 'key'
_OBJECT_TYPE = {FILE: 1, KEY: 4}  # SE_FILE_OBJECT, SE_REGISTRY_KEY
_READ = {FILE: 0x1200A9, KEY: 0x20019}  # read, list and traverse; KEY_READ
_MAPPING = {FILE: (0x120089, 0x120116, 0x1200A0, 0x1F01FF), KEY: (0x20019, 0x20006, 0x20019, 0xF003F)}
_DACL = 0x4  # DACL_SECURITY_INFORMATION
_HKEY_CURRENT_USER = 0x80000001
_KEY_ACCESS = 0x00020000 | 0x00040000  # READ_CONTROL | WRITE_DAC
_ACE = re.compile(r'\(([^()]*)\)')


class _Mapping(C.Structure):
    _fields_ = [('r', W.DWORD), ('w', W.DWORD), ('x', W.DWORD), ('a', W.DWORD)]


# --- SDDL of one DACL: pure text, no OS call ---------------------------------------------------

def _split(sddl):
    """(text before the DACL aces, list of ace bodies, text after them) of one SDDL string."""
    start = sddl.index('D:')
    end = sddl.find('S:', start)
    end = len(sddl) if end < 0 else end
    head, body = sddl[:start+2], sddl[start+2:end]
    first = body.find('(')
    flags = body if first < 0 else body[:first]
    return head+flags, _ACE.findall(body[len(flags):]), sddl[end:]


def _is_class_deny(ace):
    fields = ace.split(';')
    return len(fields) == 6 and fields[0] == 'D' and fields[5].upper() == WORKER_CLASS_SID.upper()


def deny_entry(kind):
    return f'D;;0x{_READ[kind]:x};;;{WORKER_CLASS_SID}'


def has_class_deny(sddl):
    """A deny entry for the worker class that applies to the object itself (not inherit-only)."""
    return any(_is_class_deny(ace) and 'IO' not in ace.split(';')[1] for ace in _split(sddl)[1])


def with_class_deny(sddl, kind):
    """The DACL with the worker-class deny entry first, as canonical order puts explicit denies."""
    head, aces, tail = _split(sddl)
    if any(_is_class_deny(ace) for ace in aces):
        return sddl
    return head+''.join(f'({ace})' for ace in [deny_entry(kind), *aces])+tail


def without_class_deny(sddl):
    head, aces, tail = _split(sddl)
    return head+''.join(f'({ace})' for ace in aces if not _is_class_deny(ace))+tail


# --- The OS side, mixed into WindowsApi -------------------------------------------------------

class ReadBoundary:

    def object_sddl(self, kind, target):
        """Owner, group and DACL of one object as SDDL; never a registry value."""
        descriptor, text, size = C.c_void_p(), C.c_void_p(), W.DWORD()
        code = self.adv('GetNamedSecurityInfoW', [W.LPCWSTR, C.c_int, W.DWORD] + [C.c_void_p]*4
                        + [C.POINTER(C.c_void_p)], W.DWORD)(
            str(target), _OBJECT_TYPE[kind], 0x1 | 0x2 | _DACL, None, None, None, None, C.byref(descriptor))
        if code:
            raise C.WinError(code)
        try:
            self.checked(self.adv('ConvertSecurityDescriptorToStringSecurityDescriptorW',
                [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_void_p), C.POINTER(W.DWORD)], W.BOOL)(
                descriptor, 1, 0x1 | 0x2 | _DACL, C.byref(text), C.byref(size)))
            try:
                return C.wstring_at(text.value)
            finally:
                self._local_free(text)
        finally:
            self._local_free(descriptor)

    @contextmanager
    def _descriptor(self, sddl):
        descriptor = C.c_void_p()
        self.checked(self.adv('ConvertStringSecurityDescriptorToSecurityDescriptorW',
            [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p], W.BOOL)(sddl, 1, C.byref(descriptor), None))
        try:
            yield descriptor
        finally:
            self._local_free(descriptor)

    def write_dacl(self, kind, target, sddl):
        """Replace the DACL of exactly this object; nothing below it is rewritten.

        The per-object call drops the auto-inherited flag unless it is requested explicitly; losing
        it would change how a later propagation from the parent treats this object.
        """
        with self._descriptor(sddl) as descriptor:
            if 'AI' in _split(sddl)[0].split('D:', 1)[1]:
                # SE_DACL_AUTO_INHERIT_REQ | SE_DACL_AUTO_INHERITED
                self.checked(self.adv('SetSecurityDescriptorControl', [C.c_void_p, W.WORD, W.WORD], W.BOOL)(
                    descriptor, 0x0500, 0x0500))
            if kind == FILE:
                self.checked(self.adv('SetFileSecurityW', [W.LPCWSTR, W.DWORD, C.c_void_p], W.BOOL)(
                    str(target), _DACL, descriptor))
                return
            root, _, sub = str(target).partition('\\')
            if root != 'CURRENT_USER':
                raise Refused('READ_BOUNDARY_KEY_SCOPE')
            key = W.HANDLE()
            code = self.adv('RegOpenKeyExW', [W.HANDLE, W.LPCWSTR, W.DWORD, W.DWORD, C.POINTER(W.HANDLE)], W.LONG)(
                _HKEY_CURRENT_USER, sub, 0, _KEY_ACCESS, C.byref(key))
            if code:
                raise C.WinError(code)
            try:
                code = self.adv('RegSetKeySecurity', [W.HANDLE, W.DWORD, C.c_void_p], W.LONG)(
                    key, _DACL, descriptor)
                if code:
                    raise C.WinError(code)
            finally:
                self.adv('RegCloseKey', [W.HANDLE], W.LONG)(key)

    def token_reads(self, token, kind, target):
        """Whether `token` would be granted read on this one object (no traversal is modelled)."""
        impersonation = W.HANDLE()
        self.checked(self.adv('DuplicateToken', [W.HANDLE, C.c_int, C.POINTER(W.HANDLE)], W.BOOL)(
            token, 2, C.byref(impersonation)))  # SecurityImpersonation
        descriptor = C.c_void_p()
        try:
            code = self.adv('GetNamedSecurityInfoW', [W.LPCWSTR, C.c_int, W.DWORD] + [C.c_void_p]*4
                            + [C.POINTER(C.c_void_p)], W.DWORD)(
                str(target), _OBJECT_TYPE[kind], 0x1 | 0x2 | _DACL | 0x10, None, None, None, None,
                C.byref(descriptor))
            if code:
                raise C.WinError(code)
            privileges, length = C.create_string_buffer(256), W.DWORD(256)
            granted, status = W.DWORD(), W.BOOL()
            self.checked(self.adv('AccessCheck', [C.c_void_p, W.HANDLE, W.DWORD, C.POINTER(_Mapping), C.c_void_p,
                C.POINTER(W.DWORD), C.POINTER(W.DWORD), C.POINTER(W.BOOL)], W.BOOL)(
                descriptor, impersonation, _READ[kind], C.byref(_Mapping(*_MAPPING[kind])), privileges,
                C.byref(length), C.byref(granted), C.byref(status)))
            return bool(status.value)
        finally:
            if descriptor.value:
                self._local_free(descriptor)
            self.close(impersonation.value)

    def require_read_boundary(self, token, protected):
        """Before any worker exists: every listed object carries the entry and refuses this token."""
        try:
            if not protected or not all(has_class_deny(self.object_sddl(kind, target))
                                        and not self.token_reads(token, kind, target)
                                        for kind, target in protected):
                raise Refused('READ_BOUNDARY_UNENFORCED')
        except (OSError, ValueError) as exc:  # ValueError: a descriptor without a DACL
            raise Refused('READ_BOUNDARY_UNENFORCED') from exc


# --- Reversible application -------------------------------------------------------------------

class DenyEntries:
    """Add or remove the worker-class deny entry on listed objects, with a snapshot and a journal.

    The journal records `planned -> applying -> applied -> reverting -> reverted` per object and
    holds SDDL only (owner, group, DACL; no registry value). Both directions are idempotent, so an
    interrupted run is finished by running the same direction again. Revert removes only entries
    for the worker class; if the rest of the DACL no longer matches the snapshot, somebody else
    changed it, and the snapshot is reported, never written over it.
    """

    def __init__(self, api, journal):
        self.api, self.journal = api, Path(journal)
        self.state = json.loads(self.journal.read_text(encoding='utf-8')) if self.journal.exists() else {}

    def _save(self):
        temporary = self.journal.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(self.state, stream, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.journal)

    def _mark(self, kind, target, **fields):
        self.state.setdefault(f'{kind}:{target}', {'kind': kind, 'target': str(target)}).update(fields)
        self._save()

    def apply(self, objects):
        for kind, target in objects:
            current = self.api.object_sddl(kind, target)
            entry = self.state.get(f'{kind}:{target}')
            if entry is None or entry.get('state') == 'reverted':
                self._mark(kind, target, before=without_class_deny(current), state='planned')
            self._mark(kind, target, state='applying')
            if not has_class_deny(current):
                self.api.write_dacl(kind, target, with_class_deny(current, kind))
            after = self.api.object_sddl(kind, target)
            if not has_class_deny(after) or without_class_deny(after) != self.state[f'{kind}:{target}']['before']:
                raise Refused('DENY_ENTRY_NOT_APPLIED')
            self._mark(kind, target, state='applied')

    def revert(self, objects=None):
        """Remove the entry from `objects`, or from every object the journal knows."""
        drift = []
        listed = objects if objects is not None else [(e['kind'], e['target']) for e in self.state.values()]
        for kind, target in listed:
            self._mark(kind, target, state='reverting')
            current = self.api.object_sddl(kind, target)
            if has_class_deny(current) or any(_is_class_deny(a) for a in _split(current)[1]):
                self.api.write_dacl(kind, target, without_class_deny(current))
            after = self.api.object_sddl(kind, target)
            if any(_is_class_deny(a) for a in _split(after)[1]):
                raise Refused('DENY_ENTRY_NOT_REVERTED')
            before = self.state[f'{kind}:{target}'].get('before')
            if before is not None and after != before:
                drift.append(f'{kind}:{target}')
            self._mark(kind, target, state='reverted')
        if drift:
            raise Refused('DACL_DRIFT_SINCE_SNAPSHOT')
