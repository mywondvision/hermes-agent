"""Lazy Windows handle primitives; importing this module creates no OS resources."""
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import subprocess
import threading

from .types import Refused
from .windows_token import TokenBoundary
from hermes_platform.host.facts import os_family

_CREATE_LOCK = threading.Lock()
# Process-wide, shared by every WindowsApi instance and subclass. A Job has no kernel name so that
# nothing outside this process can open it by name; lookup by id is therefore only possible in the
# owning process, and the lock keeps a lookup from duplicating a handle value that was just closed.
_JOBS, _JOBS_LOCK = {}, threading.Lock()
_JOB_QUERY = 0x0004  # JOB_OBJECT_QUERY
_HANDLE_LIST, _JOB_LIST = 0x00020002, 0x0002000D  # PROC_THREAD_ATTRIBUTE_{HANDLE,JOB}_LIST
# CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT
_CREATE_FLAGS = 0x4 | 0x08000000 | 0x00080000 | 0x400
_STARTF_USESTDHANDLES = 0x100


class Accounting(C.Structure):
    _fields_ = [(n, C.c_int64) for n in ('User', 'Kernel', 'PeriodUser', 'PeriodKernel')] + [
        (n, W.DWORD) for n in ('Faults', 'Total', 'Active', 'Terminated')]


class BasicLimit(C.Structure):
    _fields_ = [('ProcessTime', C.c_int64), ('JobTime', C.c_int64), ('Flags', W.DWORD),
                ('MinWS', C.c_size_t), ('MaxWS', C.c_size_t), ('ActiveLimit', W.DWORD),
                ('Affinity', C.c_size_t), ('Priority', W.DWORD), ('Scheduling', W.DWORD)]


class IoCounters(C.Structure):
    _fields_ = [(n, C.c_uint64) for n in ('RO', 'WO', 'OO', 'RT', 'WT', 'OT')]


class ExtendedLimit(C.Structure):
    _fields_ = [('Basic', BasicLimit), ('IO', IoCounters), ('ProcessMemory', C.c_size_t),
                ('JobMemory', C.c_size_t), ('PeakProcessMemory', C.c_size_t), ('PeakJobMemory', C.c_size_t)]


class StartupInfoW(C.Structure):
    _fields_ = [('cb', W.DWORD), ('lpReserved', W.LPWSTR), ('lpDesktop', W.LPWSTR), ('lpTitle', W.LPWSTR)] + [
        (n, W.DWORD) for n in ('dwX', 'dwY', 'dwXSize', 'dwYSize', 'dwXCountChars', 'dwYCountChars',
                               'dwFillAttribute', 'dwFlags')] + [
        ('wShowWindow', W.WORD), ('cbReserved2', W.WORD), ('lpReserved2', C.c_void_p),
        ('hStdInput', W.HANDLE), ('hStdOutput', W.HANDLE), ('hStdError', W.HANDLE)]


class StartupInfoExW(C.Structure):
    _fields_ = [('StartupInfo', StartupInfoW), ('lpAttributeList', C.c_void_p)]


class ProcessInformation(C.Structure):
    _fields_ = [('hProcess', W.HANDLE), ('hThread', W.HANDLE), ('dwProcessId', W.DWORD), ('dwThreadId', W.DWORD)]


class WindowsApi(TokenBoundary):
    A = None  # advapi32, loaded on first token call

    def __init__(self):
        if os_family() != 'win32':
            raise Refused('WINDOWS_HOST_REQUIRED')
        self.K = C.WinDLL('kernel32', use_last_error=True)

    def fn(self, name, args, result):
        fn = getattr(self.K, name)
        fn.argtypes, fn.restype = args, result
        return fn

    @staticmethod
    def checked(value):
        if not value:
            raise C.WinError(C.get_last_error())
        return value

    def close(self, handle):
        self.checked(self.fn('CloseHandle', [W.HANDLE], W.BOOL)(handle))

    def open_process(self, pid):
        return self.checked(self.fn('OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE)(0x101000, False, pid))

    def creation(self, handle):
        values = [W.FILETIME() for _ in range(4)]
        self.checked(self.fn('GetProcessTimes', [W.HANDLE]+[C.POINTER(W.FILETIME)]*4, W.BOOL)(
            handle, *(C.byref(v) for v in values)))
        return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)

    def wait(self, handle):
        value = self.fn('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)(handle, 0)
        if value not in (0, 258):
            raise Refused('PROCESS_WAIT_UNKNOWN')
        return value == 0

    def exit_code(self, handle):
        value = W.DWORD()
        self.checked(self.fn('GetExitCodeProcess', [W.HANDLE, C.POINTER(W.DWORD)], W.BOOL)(handle, C.byref(value)))
        return value.value

    def open_job(self, job_id):
        """A short-lived query-only duplicate of the registered Job; the caller closes it."""
        import _winapi
        with _JOBS_LOCK:
            if job_id not in _JOBS:
                raise Refused('JOB_IDENTITY_UNKNOWN')
            current = _winapi.GetCurrentProcess()
            return _winapi.DuplicateHandle(current, _JOBS[job_id], current, _JOB_QUERY, False, 0)

    def create_job(self, job_id):
        with _JOBS_LOCK:
            if job_id in _JOBS:
                raise Refused('JOB_NAME_COLLISION')
            job = self.checked(self.fn('CreateJobObjectW', [C.c_void_p, W.LPCWSTR], W.HANDLE)(None, None))
            try:
                limits = ExtendedLimit()
                limits.Basic.Flags = 0x2000
                self.checked(self.fn('SetInformationJobObject', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL)(
                    job, 9, C.byref(limits), C.sizeof(limits)))
            except BaseException:
                self.close(job)
                raise
            _JOBS[job_id] = job
            return job

    def close_job(self, job_id):
        """The only way a registered Job handle is closed."""
        with _JOBS_LOCK:
            if job_id not in _JOBS:
                raise Refused('JOB_IDENTITY_UNKNOWN')
            self.close(_JOBS.pop(job_id))

    def in_job(self, process, job):
        value = W.BOOL()
        self.checked(self.fn('IsProcessInJob', [W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)], W.BOOL)(
            process, job, C.byref(value)))
        return bool(value.value)

    def snapshot(self, job):
        query = self.fn('QueryInformationJobObject', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL)
        account = Accounting()
        self.checked(query(job, 1, C.byref(account), C.sizeof(account), None))

        class Pids(C.Structure):
            _fields_ = [('Assigned', W.DWORD), ('Count', W.DWORD), ('Values', C.c_size_t*4096)]

        pids = Pids()
        self.checked(query(job, 3, C.byref(pids), C.sizeof(pids), None))
        if pids.Count > 4096 or pids.Count != pids.Assigned:
            raise Refused('JOB_MEMBERSHIP_UNKNOWN')
        return account.Active, tuple(pids.Values[:pids.Count])

    def terminate(self, job):
        self.checked(self.fn('TerminateJobObject', [W.HANDLE, W.UINT], W.BOOL)(job, 123))
        return True

    def terminate_process(self, process):
        self.checked(self.fn('TerminateProcess', [W.HANDLE, W.UINT], W.BOOL)(process, 123))

    def resume(self, thread):
        value = self.fn('ResumeThread', [W.HANDLE], W.DWORD)(thread)
        if value != 1:
            raise Refused('SUSPENSION_STATE_UNKNOWN')

    @contextmanager
    def attribute_list(self, handles, job):
        """Inherit exactly `handles` and be born inside exactly `job`; creates no process."""
        listed, jobs = (W.HANDLE*len(handles))(*handles), (W.HANDLE*1)(job)  # must outlive the list
        size = C.c_size_t()
        init = self.fn('InitializeProcThreadAttributeList', [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)], W.BOOL)
        init(None, 2, 0, C.byref(size))  # the size query fails by design
        self.checked(size.value)
        buffer = (C.c_size_t*((size.value+C.sizeof(C.c_size_t)-1)//C.sizeof(C.c_size_t)))()
        self.checked(init(buffer, 2, 0, C.byref(size)))
        try:
            update = self.fn('UpdateProcThreadAttribute', [C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p,
                                                           C.c_size_t, C.c_void_p, C.c_void_p], W.BOOL)
            for attribute, value in ((_HANDLE_LIST, listed), (_JOB_LIST, jobs)):
                self.checked(update(buffer, 0, attribute, value, C.sizeof(value), None, None))
            yield buffer
        finally:
            self.fn('DeleteProcThreadAttributeList', [C.c_void_p], None)(buffer)

    def _create_process(self, argv, cwd, env, handles, job, token):
        env = dict(env)
        if any('\0' in part for part in (*argv, str(cwd), *env, *env.values())):
            raise Refused('CREATE_EMBEDDED_NUL')  # the kernel would silently truncate at the NUL
        block = ''.join(f'{key}={value}\0' for key, value in env.items()) or '\0'
        startup, info = StartupInfoExW(), ProcessInformation()
        basic = startup.StartupInfo
        basic.cb, basic.dwFlags = C.sizeof(startup), _STARTF_USESTDHANDLES
        basic.hStdInput, basic.hStdOutput, basic.hStdError = handles
        with self.attribute_list(handles, job) as attributes:
            startup.lpAttributeList = C.addressof(attributes)
            signature = [W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD, C.c_void_p,
                         W.LPCWSTR, C.c_void_p, C.c_void_p]
            arguments = (argv[0], C.create_unicode_buffer(subprocess.list2cmdline(argv)), None, None, True,
                         _CREATE_FLAGS, C.create_unicode_buffer(block+'\0'), str(cwd), C.byref(startup),
                         C.byref(info))
            if token is None:
                self.checked(self.fn('CreateProcessW', signature, W.BOOL)(*arguments))
            else:
                self.checked(self.adv('CreateProcessAsUserW', [W.HANDLE, *signature], W.BOOL)(token, *arguments))
        return info.hProcess, info.hThread, info.dwProcessId, info.dwThreadId

    def _discard(self, created):
        """Terminate and close exactly this just-created, still-suspended process."""
        failures = []
        try:
            self.terminate_process(created[0])
        except OSError as exc:
            failures.append(exc)
        for handle in created[:2]:
            try:
                self.close(handle)
            except OSError as exc:
                failures.append(exc)
        return failures

    def create_suspended(self, argv, cwd, env, streams, job, token):
        """The root is born inside `job`: no window exists between creation and Job membership.

        `token` is the primary token the root is born with. None keeps the caller's own, which is
        only right for a process that is itself an owner; a worker always gets `worker_token()`.
        """
        import _winapi
        import msvcrt
        duplicates, created, contained = [], None, False
        with _CREATE_LOCK:
            try:
                for stream in streams:
                    duplicates.append(_winapi.DuplicateHandle(_winapi.GetCurrentProcess(),
                        msvcrt.get_osfhandle(stream.fileno()), _winapi.GetCurrentProcess(), 0, True,
                        _winapi.DUPLICATE_SAME_ACCESS))
                created = self._create_process(argv, cwd, env, duplicates, job, token)
                if not self.in_job(created[0], job):
                    raise Refused('CREATE_OUTSIDE_JOB')
                contained = True
            finally:
                failures = []
                for handle in duplicates:
                    try:
                        self.close(handle)
                    except OSError as exc:
                        failures.append(exc)
                if created is not None and (failures or not contained):
                    failures.extend(self._discard(created))
                if failures:
                    raise Refused('CREATE_HANDLE_CLEANUP_UNKNOWN') from failures[0]
        return created
