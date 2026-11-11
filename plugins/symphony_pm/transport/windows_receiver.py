"""Exact owned native handles; no terminate-handle recovery or retry route."""
from ctypes import wintypes as W
import os
import tempfile
import threading
import time

from .types import NativeBinding, ProcessIdentity, Refused, finite, job_object_name
from .windows_api import WindowsApi


class WindowsReceiver:
    def __init__(self, api, ticket, argv, cwd, stdin_text, *, environment,
                 command_result, sanitize, private_directory):
        self.api, self.ticket = api, ticket
        self.command_result, self.sanitize = command_result, sanitize
        self.lock = threading.RLock()
        self.job = self.process = self.thread = self.job_id = None
        self.closed = self.resumed = self.spent = False
        self.files, self.argv = [], tuple(argv)
        try:
            for _ in range(3):
                self.files.append(tempfile.TemporaryFile(dir=private_directory))
            self.files[0].write((stdin_text or '').encode('utf-8'))
            self.files[0].seek(0)
            self.job_id = job_object_name(ticket)
            self.job = api.create_job(self.job_id)
            self.process, self.thread, pid, _ = api.create_suspended(
                argv, cwd, environment, self.files, self.job)
            root = ProcessIdentity(pid, api.creation(self.process))
            self.binding = NativeBinding(ticket, self.job_id, root, (root,))
            self.binding.validate()
        except BaseException:
            # The root is born inside the kill-on-close Job, so dropping the last Job handle
            # terminates it; no exact-process terminate is needed or allowed here.
            self.close_fail_safe(time.monotonic())
            raise

    def check_binding(self, binding):
        if (self.closed or binding != self.binding
                or self.api.creation(self.process) != binding.root.creation
                or not self.api.in_job(self.process, self.job)):
            raise Refused('NATIVE_HANDLE_BINDING')

    def resume(self, stdin_text=None, *, authorize):
        with self.lock:
            self.check_binding(self.binding)
            if self.resumed:
                raise Refused('RESUME_SLOT_SPENT')
            self.resumed = True
            authorize(lambda: self.api.resume(self.thread))
            self.api.close(self.thread)
            self.thread = None

    def cancel_once(self, binding, *, authorize):
        with self.lock:
            self.check_binding(binding)
            if self.spent or not callable(authorize):
                raise Refused('CANCEL_SLOT_SPENT_OR_UNAUTHORIZED')
            self.spent = True
            return authorize(lambda: self.api.terminate(self.job)) is True

    def wait_result(self, argv, timeout, timed_out=False):
        if tuple(argv) != self.argv or not finite(timeout) or timeout <= 0:
            raise Refused('RESULT_COMMAND_BINDING')
        with self.lock:
            self.check_binding(self.binding)
            query = self.api.open_process(self.binding.root.pid)
            try:
                if self.api.creation(query) != self.binding.root.creation:
                    raise Refused('RESULT_PROCESS_IDENTITY')
            except BaseException:
                self.api.close(query)
                raise
        try:
            self._wait(query, timeout)
            with self.lock:
                if self.closed:
                    raise Refused('RESULT_CLOSED_UNKNOWN')
                output = []
                for stream in self.files[1:]:
                    stream.seek(0)
                    value = stream.read(16*1024*1024+1)
                    if len(value) > 16*1024*1024:
                        raise Refused('NATIVE_OUTPUT_LIMIT_UNKNOWN')
                    output.append(self.sanitize(value.decode('utf-8', 'replace')))
                return self.command_result(self.argv, self.api.exit_code(query),
                    stdout=output[0], stderr=output[1], timed_out=timed_out)
        finally:
            self.api.close(query)

    def _wait(self, query, timeout):
        # Kernel wait is bounded; output goes to private files, never pipe buffers
        # that could block the worker while owner holds its lifecycle lock.
        wait = self.api.fn('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)
        end = time.monotonic()+timeout
        while True:
            with self.lock:
                if self.closed:
                    raise Refused('RESULT_CLOSED_UNKNOWN')
                if any(os.fstat(stream.fileno()).st_size > 16*1024*1024 for stream in self.files[1:]):
                    raise TimeoutError('NATIVE_CAPTURE_LIMIT')
            remaining = end-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('NATIVE_DRAIN_TIMEOUT')
            result = wait(query, min(max(1, int(remaining*1000)), 200))
            if result == 0:
                break
            if result != 258:
                raise Refused('NATIVE_WAIT_UNKNOWN')

    def close_fail_safe(self, deadline):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            failures = []
            # The Job registry owns its handle, so only close_job may release it. A refused create
            # leaves `job` None, so a colliding id's real owner is never closed from here.
            for attr, release in (('job', lambda _: self.api.close_job(self.job_id)),
                                  ('thread', self.api.close), ('process', self.api.close)):
                handle = getattr(self, attr)
                if handle is not None:
                    try:
                        release(handle)
                    except (OSError, Refused) as exc:  # a missing registry entry must not strand the other handles
                        failures.append(exc)
                    setattr(self, attr, None)
            for stream in self.files:
                try:
                    stream.close()
                except OSError as exc:
                    failures.append(exc)
            if failures:
                raise Refused('OWNED_HANDLE_CLOSE_UNKNOWN') from failures[0]


class WindowsNativeProvider:
    def __init__(self, *, validate_call, environment, command_result, sanitize, private_directory):
        # All inputs are host-owned objects bound to the qualified kit attempt.
        self.validate_call = validate_call
        self.environment = dict(environment)
        self.options = dict(command_result=command_result, sanitize=sanitize, private_directory=private_directory)
        self.api = WindowsApi()

    def validate_driver_call(self, ticket, argv, cwd, timeout, stdin_text):
        if not finite(timeout) or timeout <= 0:
            raise Refused('BOUNDED_NATIVE_TIMEOUT_REQUIRED')
        self.validate_call(ticket, tuple(argv), cwd, timeout, stdin_text)

    def create_suspended(self, ticket, argv, cwd, stdin_text):
        return WindowsReceiver(self.api, ticket, argv, cwd, stdin_text,
                               environment=self.environment, **self.options)
