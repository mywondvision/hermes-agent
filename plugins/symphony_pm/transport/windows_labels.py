"""Mandatory labels on the owner process and its plugin data. Importing creates no OS resources.

The worker is low-integrity. An unlabeled owner process still lets it terminate, wait on and query
the owner, and plugin data without a no-read-up label lets it read permits and hold a lock on the
journal. Both labels are medium, so nothing changes for the user's ordinary processes.
"""
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import os

from .types import Refused
from .windows_token import MEDIUM

_LABEL, _KERNEL_OBJECT, _FILE_OBJECT = 0x10, 6, 1  # LABEL_SECURITY_INFORMATION, SE_OBJECT_TYPE values
_LABEL_ACE, _PERSISTENT_ACLS = 0x11, 0x8
_NO_WRITE, _NO_READ, _NO_EXECUTE = 0x1, 0x2, 0x4
PROCESS_POLICY, DATA_POLICY = _NO_WRITE | _NO_READ | _NO_EXECUTE, _NO_WRITE | _NO_READ
_PROCESS_LABEL, _DIRECTORY_LABEL, _FILE_LABEL = 'S:(ML;;NWNRNX;;;ME)', 'S:(ML;OICI;NRNW;;;ME)', 'S:(ML;;NRNW;;;ME)'


class LabelBoundary:
    """Mixed into WindowsApi next to TokenBoundary."""

    @contextmanager
    def _sacl(self, sddl):
        descriptor, present, sacl, defaulted = C.c_void_p(), W.BOOL(), C.c_void_p(), W.BOOL()
        self.checked(self.adv('ConvertStringSecurityDescriptorToSecurityDescriptorW',
            [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p], W.BOOL)(sddl, 1, C.byref(descriptor), None))
        try:
            self.checked(self.adv('GetSecurityDescriptorSacl',
                [C.c_void_p, C.POINTER(W.BOOL), C.POINTER(C.c_void_p), C.POINTER(W.BOOL)], W.BOOL)(
                descriptor, C.byref(present), C.byref(sacl), C.byref(defaulted)))
            yield sacl
        finally:
            self.fn('LocalFree', [C.c_void_p], C.c_void_p)(descriptor)

    def _set_label(self, name, target, kind, sddl):
        with self._sacl(sddl) as sacl:
            code = self.adv(name, [type(target), C.c_int, W.DWORD] + [C.c_void_p]*4, W.DWORD)(
                target, kind, _LABEL, None, None, None, sacl)
        if code:
            raise C.WinError(code)

    def _get_label(self, name, target, kind):
        """(integrity RID, policy mask) of an object's mandatory label, or None when it has none."""
        sacl, descriptor = C.c_void_p(), C.c_void_p()
        code = self.adv(name, [type(target), C.c_int, W.DWORD] + [C.c_void_p]*3
            + [C.POINTER(C.c_void_p)]*2, W.DWORD)(
            target, kind, _LABEL, None, None, None, C.byref(sacl), C.byref(descriptor))
        if code:
            raise C.WinError(code)
        try:
            if not sacl.value or C.cast(sacl, C.POINTER(C.c_ushort))[2] != 1:  # ACL.AceCount
                return None
            ace = sacl.value+8
            if C.c_ubyte.from_address(ace).value != _LABEL_ACE:
                return None
            sid = C.c_void_p(ace+8)
            count = self.adv('GetSidSubAuthorityCount', [C.c_void_p], C.POINTER(C.c_ubyte))(sid).contents.value
            rid = self.adv('GetSidSubAuthority', [C.c_void_p, W.DWORD], C.POINTER(W.DWORD))(sid, count-1).contents.value
            return rid, W.DWORD.from_address(ace+4).value
        finally:
            self.fn('LocalFree', [C.c_void_p], C.c_void_p)(descriptor)

    def path_label(self, path):
        return self._get_label('GetNamedSecurityInfoW', W.LPCWSTR(str(path)), _FILE_OBJECT)

    def process_label(self):
        return self._get_label('GetSecurityInfo', W.HANDLE(self._current_process()), _KERNEL_OBJECT)

    def protect_process(self):
        """Label this process so a lower-integrity one can neither write, read nor execute it."""
        if self.process_label() != (MEDIUM, PROCESS_POLICY):
            self._set_label('SetSecurityInfo', W.HANDLE(self._current_process()), _KERNEL_OBJECT, _PROCESS_LABEL)
        return self.process_label() == (MEDIUM, PROCESS_POLICY)

    def keeps_labels(self, directory):
        """Whether the volume holding `directory` stores security descriptors at all."""
        root, flags = C.create_unicode_buffer(1024), W.DWORD()
        self.checked(self.fn('GetVolumePathNameW', [W.LPCWSTR, W.LPWSTR, W.DWORD], W.BOOL)(str(directory), root, 1024))
        self.checked(self.fn('GetVolumeInformationW', [W.LPCWSTR, W.LPWSTR, W.DWORD, C.c_void_p, C.c_void_p,
            C.POINTER(W.DWORD), W.LPWSTR, W.DWORD], W.BOOL)(root.value, None, 0, None, None, C.byref(flags), None, 0))
        return bool(flags.value & _PERSISTENT_ACLS)

    def protect_directory(self, directory):
        """Label `directory` and everything already in it medium, no-read-up and no-write-up.

        New files inherit the label. Existing entries are relabelled only when they differ, and the
        result is read back, so a volume or entry that did not take the label is reported as False.
        """
        if not os.path.isdir(directory) or not self.keeps_labels(directory) or not self._labelled(
                str(directory), _DIRECTORY_LABEL):
            return False
        entries = []
        for parent, directories, files in os.walk(directory):
            entries += [(os.path.join(parent, name), _DIRECTORY_LABEL) for name in directories]
            entries += [(os.path.join(parent, name), _FILE_LABEL) for name in files]
        return all([self._labelled(path, sddl) for path, sddl in entries])

    def _labelled(self, path, sddl):
        try:
            if self.path_label(path) != (MEDIUM, DATA_POLICY):
                self._set_label('SetNamedSecurityInfoW', W.LPCWSTR(path), _FILE_OBJECT, sddl)
            return self.path_label(path) == (MEDIUM, DATA_POLICY)
        except FileNotFoundError:
            # A transient file (an SQLite sidecar, a closed capture file) went away after the walk;
            # whatever is created in its place inherits the directory's label.
            return not os.path.lexists(path)

    def guard_owner(self, data_directory):
        """Before any worker exists: this process is exactly medium, labelled, and so is its data.

        An elevated owner is refused: dropping privileges leaves its administrator group enabled
        in the worker's token, which this boundary does not remove.
        """
        try:
            guarded = (self.integrity(self._current_process()) == MEDIUM and self.protect_process()
                       and self.protect_directory(data_directory))
        except OSError as exc:
            raise Refused('WORKER_BOUNDARY_UNENFORCED') from exc
        if not guarded:
            raise Refused('WORKER_BOUNDARY_UNENFORCED')
