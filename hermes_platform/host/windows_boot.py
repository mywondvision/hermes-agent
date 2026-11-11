"""Windows boot incarnation, queried without shell commands or environment input."""
import ctypes as C
from ctypes import wintypes as W
import uuid


def boot_identity():
    # SYSTEM_BOOT_ENVIRONMENT_INFORMATION starts with the boot GUID. Fail closed
    # on unsupported kernels; clock arithmetic is not a durable boot identity.
    query = C.WinDLL('ntdll').NtQuerySystemInformation
    query.argtypes = [C.c_int, C.c_void_p, W.ULONG, C.POINTER(W.ULONG)]
    query.restype = W.LONG
    buffer, size = C.create_string_buffer(32), W.ULONG()
    status = query(90, buffer, C.sizeof(buffer), C.byref(size))
    if status < 0 or size.value < 16 or buffer.raw[:16] == bytes(16):
        raise OSError('Windows boot identity unavailable')
    return str(uuid.UUID(bytes_le=buffer.raw[:16]))
