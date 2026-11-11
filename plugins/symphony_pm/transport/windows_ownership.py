"""Process-lifetime exclusive file ownership; lease expiry never grants takeover."""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import threading
import uuid

from hermes_cli.plugin_request_binding import canonical_home
from hermes_dir_acl import make_user_dir
from hermes_platform.host.windows_boot import boot_identity
from .types import ProcessIdentity, Refused, ServingIdentity, digest
from .windows_api import WindowsApi


class WindowsOwnershipGuard:
    def __init__(self, scope):
        self.lock = threading.RLock()
        self.api, self.handle = WindowsApi(), None
        home = canonical_home(scope.profile_home)
        directory = Path(home)/'plugin-data'/'symphony_pm'/digest(scope.namespace)[:32]
        make_user_dir(directory, mode=0o700, parents=True, exist_ok=True)
        create = self.api.fn('CreateFileW', [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p,
                                           W.DWORD, W.DWORD, W.HANDLE], W.HANDLE)
        handle = create(str(directory/'owner.lock'), 0x80000000 | 0x40000000, 0, None, 4, 0x80, None)
        if handle == C.c_void_p(-1).value:
            raise Refused('OS_OWNER_UNAVAILABLE')
        self.handle = handle
        try:
            process = self.api.open_process(os.getpid())
            try:
                identity = ProcessIdentity(os.getpid(), self.api.creation(process))
            finally:
                self.api.close(process)
            self.identity = ServingIdentity(home, scope.namespace, boot_identity(), identity, uuid.uuid4().hex)
            self.identity.validate()
        except BaseException:
            self.close(0)
            raise

    def assert_owned(self):
        with self.lock:
            if self.handle is None or os.getpid() != self.identity.process.pid:
                raise Refused('OS_OWNER_LOST')

    def require_predecessor_dead(self, previous):
        with self.lock:
            self.assert_owned()
            if previous['boot_id'] != self.identity.boot_id:
                raise Refused('BOOT_CHANGED_HOLD')
            expected = ProcessIdentity(**previous['process'])
            try:
                handle = self.api.open_process(expected.pid)
            except OSError as exc:
                if exc.winerror == 87:
                    return
                raise Refused('PREDECESSOR_DEATH_UNKNOWN') from exc
            try:
                if self.api.creation(handle) != expected.creation:
                    raise Refused('PREDECESSOR_IDENTITY_UNKNOWN')
                if not self.api.wait(handle):
                    raise Refused('PREDECESSOR_STILL_ALIVE')
            finally:
                self.api.close(handle)

    def close(self, deadline):
        with self.lock:
            if self.handle is not None:
                handle, self.handle = self.handle, None
                self.api.close(handle)
