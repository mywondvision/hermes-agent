"""Immutable record of the driver build, executable, environment and issuers that were qualified."""
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path

from hermes_cli.plugin_request_binding import canonical_home
from .types import Refused, digest, identifier, sha
from .windows_api import WindowsApi

_GENERIC_READ, _FILE_SHARE_READ, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL = 0x80000000, 0x1, 3, 0x80


def environment_digest(environment) -> str:
    """The value QualificationRecord.environment_sha256 must carry for this child environment."""
    return digest(sorted(environment.items()))


def _open_denying_writers(path):
    """Read handle that refuses every concurrent write, replace and delete until it is closed.

    FILE_SHARE_READ alone is deliberate: the image loader's later open needs only read/execute access.
    """
    import msvcrt
    api = WindowsApi()
    create = api.fn('CreateFileW', [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE],
                    W.HANDLE)
    handle = create(path, _GENERIC_READ, _FILE_SHARE_READ, None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
    if handle == C.c_void_p(-1).value:
        raise Refused('QUALIFIED_EXECUTABLE_CHANGED')
    # The CRT file object owns the handle from here; closing it closes the handle exactly once.
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), 'rb')  # windows-footgun: ok


@dataclass(frozen=True)
class QualificationRecord:
    """Evidence identity only: nothing here executes, probes or selects the executable."""
    profile_home: str
    namespace: str
    scheduler_id: str
    kit_revision: str
    executable: str
    executable_sha256: str
    environment_ref: str
    environment_sha256: str
    issuer_refs: tuple[str, ...]

    def validate(self):
        if (type(self.profile_home) is not str or not Path(self.profile_home).is_absolute()
                or self.profile_home != canonical_home(self.profile_home)
                or not all(identifier(v) for v in (self.namespace, self.scheduler_id, self.environment_ref))
                or not sha(self.kit_revision, 40) or not sha(self.executable_sha256, 64)
                or not sha(self.environment_sha256, 64)
                or type(self.executable) is not str or not Path(self.executable).is_absolute()
                or Path(self.executable).suffix.lower() != '.exe'
                or type(self.issuer_refs) is not tuple or not self.issuer_refs
                or not all(identifier(v) for v in self.issuer_refs)
                or len(set(self.issuer_refs)) != len(self.issuer_refs)):
            raise Refused('QUALIFICATION_RECORD')

    @contextmanager
    def held_executable(self):
        """Hash the executable through a handle that stays open, so the verified bytes are the launched bytes.

        Hashing a path and executing it later leaves a window for a swap; holding a no-write/no-delete
        handle across the launch closes it. Windows only: elsewhere WindowsApi refuses.
        """
        with _open_denying_writers(self.executable) as stream:
            hasher = hashlib.sha256()
            try:
                for chunk in iter(lambda: stream.read(1 << 20), b''):
                    hasher.update(chunk)
            except OSError:
                raise Refused('QUALIFIED_EXECUTABLE_CHANGED') from None
            if hasher.hexdigest() != self.executable_sha256:
                raise Refused('QUALIFIED_EXECUTABLE_CHANGED')
            yield

    def verify_executable(self):
        """One-shot check; a launch must use held_executable so the bytes cannot change afterwards."""
        with self.held_executable():
            pass

    @property
    def digest(self):
        return digest(asdict(self))
