"""Task-local Windows supervision. No model calls, queue, or production registration.

The root is created suspended, bound to a non-breakaway kill-on-close Job Object,
then resumed. Named-job readback can run in a separate query-only observer process.
This is bounded process-lifetime supervision, not a restartable OS handle service.
"""
import ctypes as C
from ctypes import wintypes as W
import json
import subprocess
import time

import psutil
from cancel_control import CancelRejected, DispatchPermit, NativeCancelGate

K = C.WinDLL("kernel32", use_last_error=True)
HANDLE, DWORD, BOOL = W.HANDLE, W.DWORD, W.BOOL


class BasicLimit(C.Structure):
    _fields_ = [("ProcessTime", C.c_int64), ("JobTime", C.c_int64), ("Flags", DWORD),
                ("MinWS", C.c_size_t), ("MaxWS", C.c_size_t), ("ActiveLimit", DWORD),
                ("Affinity", C.c_size_t), ("Priority", DWORD), ("Scheduling", DWORD)]


class IO(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in ("RO", "WO", "OO", "RT", "WT", "OT")]


class ExtendedLimit(C.Structure):
    _fields_ = [("Basic", BasicLimit), ("IO", IO), ("ProcessMemory", C.c_size_t),
                ("JobMemory", C.c_size_t), ("PeakProcessMemory", C.c_size_t),
                ("PeakJobMemory", C.c_size_t)]


class Accounting(C.Structure):
    _fields_ = [(name, C.c_int64) for name in ("User", "Kernel", "PeriodUser", "PeriodKernel")] + [
        (name, DWORD) for name in ("PageFaults", "Total", "Active", "Terminated")]


class PidList(C.Structure):
    _fields_ = [("Assigned", DWORD), ("Count", DWORD), ("Pids", C.c_size_t * 256)]


def signature(name, args, result):
    fn = getattr(K, name)
    fn.argtypes, fn.restype = args, result
    return fn


create_job = signature("CreateJobObjectW", [C.c_void_p, W.LPCWSTR], HANDLE)
open_job = signature("OpenJobObjectW", [DWORD, BOOL, W.LPCWSTR], HANDLE)
set_job = signature("SetInformationJobObject", [HANDLE, C.c_int, C.c_void_p, DWORD], BOOL)
query_job = signature("QueryInformationJobObject", [HANDLE, C.c_int, C.c_void_p, DWORD, C.c_void_p], BOOL)
assign_job = signature("AssignProcessToJobObject", [HANDLE, HANDLE], BOOL)
terminate_job = signature("TerminateJobObject", [HANDLE, W.UINT], BOOL)
close_handle = signature("CloseHandle", [HANDLE], BOOL)
open_thread = signature("OpenThread", [DWORD, BOOL, DWORD], HANDLE)
resume_thread = signature("ResumeThread", [HANDLE], DWORD)
open_process = signature("OpenProcess", [DWORD, BOOL, DWORD], HANDLE)
wait_handle = signature("WaitForSingleObject", [HANDLE, DWORD], DWORD)
get_times = signature("GetProcessTimes", [HANDLE] + [C.POINTER(W.FILETIME)] * 4, BOOL)


def checked(value):
    if not value:
        raise C.WinError(C.get_last_error())
    return value


def creation_ticks(handle):
    times = [W.FILETIME() for _ in range(4)]
    checked(get_times(handle, *(C.byref(t) for t in times)))
    return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)


def snapshot(handle):
    a, ids = Accounting(), PidList()
    checked(query_job(handle, 1, C.byref(a), C.sizeof(a), None))
    checked(query_job(handle, 3, C.byref(ids), C.sizeof(ids), None))
    members = []
    for pid in ids.Pids[:ids.Count]:
        try:
            p = psutil.Process(pid)
            members.append({"pid": pid, "start": p.create_time(), "name": p.name(), "ppid": p.ppid()})
        except psutil.NoSuchProcess:
            members.append({"pid": pid, "exited_during_read": True})
    return {"observed_at": time.time(), "active": a.Active, "total": a.Total,
            "terminated": a.Terminated, "pids": list(ids.Pids[:ids.Count]), "members": members}


def observe(target):
    """Separate process opens query-only job + synchronize/query process handles.

    No receipt flags and no terminate rights are used. Exact creation FILETIME protects
    against PID reuse. An absent PID is recorded separately from a signaled live handle.
    """
    if not isinstance(target.get("known_processes"), list) or not target["known_processes"]:
        raise CancelRejected("EMPTY_PROCESS_EVIDENCE")
    job = checked(open_job(4, False, target["job_object"]))
    try:
        result = snapshot(job)
        records = []
        for expected in target["known_processes"]:
            hp = open_process(0x100000 | 0x1000, False, expected["pid"])
            if not hp:
                err = C.get_last_error()
                records.append({**expected, "absent": err == 87, "error": err})
                continue
            try:
                ticks = creation_ticks(hp)
                records.append({**expected, "actual_start_ticks": ticks,
                                "identity_matches": ticks == expected["start_ticks"],
                                "signaled": wait_handle(hp, 0) == 0})
            finally:
                checked(close_handle(hp))
        result["known_processes"] = records
        result["tree_stopped"] = result["active"] == 0 and not result["pids"] and all(
            r.get("absent") or (r.get("identity_matches") and r.get("signaled")) for r in records)
        return result
    finally:
        checked(close_handle(job))


class WindowsJob:
    def __init__(self, name):
        self.name = name
        self.handle = checked(create_job(None, name))
        if C.get_last_error() == 183:
            checked(close_handle(self.handle))
            raise RuntimeError("Job already exists; never attach to an unrelated process tree")
        limits = ExtendedLimit()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway flags.
        checked(set_job(self.handle, 9, C.byref(limits), C.sizeof(limits)))
        self.process = None
        self.known = {}
        self.process_handles = {}
        self._cancel_gate = NativeCancelGate()
        self._cancel_route = None
        self._closed = False

    def start_suspended(self, argv, cwd, env, stdout, stderr):
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                        stdout=stdout, stderr=stderr, close_fds=True,
                                        creationflags=0x4 | 0x08000000)
        try:
            checked(assign_job(self.handle, int(self.process._handle)))
            self.capture()
            return {"pid": self.process.pid, "start_ticks": creation_ticks(int(self.process._handle)),
                    "job_object": self.name}
        except BaseException:
            self.process.kill()  # still suspended; no model instructions have executed
            self.process.wait(timeout=10)
            raise

    def resume(self, prompt):
        threads = psutil.Process(self.process.pid).threads()
        if len(threads) != 1:
            raise RuntimeError("Unexpected suspended process threads")
        ht = checked(open_thread(2, False, threads[0].id))
        try:
            if resume_thread(ht) == 0xFFFFFFFF:
                raise C.WinError(C.get_last_error())
        finally:
            checked(close_handle(ht))
        self.process.stdin.write(prompt.encode("utf-8"))
        self.process.stdin.close()

    def capture(self):
        result = snapshot(self.handle)
        for pid in result["pids"]:
            if pid in self.process_handles:
                continue
            hp = open_process(0x100000 | 0x1000, False, pid)
            if hp:
                self.process_handles[pid] = hp
                self.known[pid] = {"pid": pid, "start_ticks": creation_ticks(hp)}
        return result

    def terminal_handles(self):
        return {str(pid): wait_handle(h, 0) == 0 for pid, h in self.process_handles.items()}

    def check_binding(self, target):
        if (self.process is None or str(self.process.pid) != target.process or
                creation_ticks(int(self.process._handle)) != target.start):
            raise CancelRejected("NATIVE_HANDLE_BINDING")

    def set_cancel_route(self, route):
        if self._cancel_route is not None:
            raise CancelRejected("CANCEL_ROUTE_ALREADY_BOUND")
        self._cancel_route = route

    def cancel_once(self, permit=None):
        if not isinstance(permit, DispatchPermit):
            raise CancelRejected("DURABLE_PERMIT_REQUIRED")
        self.check_binding(permit.target)
        return self._cancel_gate.dispatch(permit, lambda: checked(terminate_job(self.handle, 123)))

    def observer_target(self):
        self.capture()
        return {"job_object": self.name, "known_processes": list(self.known.values())}

    def close(self):
        if self._closed:
            return
        try:
            if self.process is not None:
                if self._cancel_route is not None:
                    self._cancel_route("EMERGENCY_CLEANUP")
                elif snapshot(self.handle)["active"]:
                    raise CancelRejected("ACTIVE_CLOSE_WITHOUT_DURABLE_ROUTE_UNKNOWN")
        finally:
            # Storage/lease/observer errors cannot suppress kernel kill-on-close.
            # Failure remains UNKNOWN; releasing handles is not a terminal receipt.
            self._closed = True
            failures = []
            for handle in [self.handle, *self.process_handles.values()]:
                try:
                    checked(close_handle(handle))
                except OSError as exc:
                    failures.append(exc)
            self.process_handles.clear()
            if failures:
                raise CancelRejected("HANDLE_RELEASE_UNKNOWN") from failures[0]


if __name__ == "__main__":
    import pathlib
    import sys
    print(json.dumps(observe(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))), indent=2))
