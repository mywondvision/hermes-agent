"""The Codex client process: the user's own token inside a kill-on-close Job. Importing creates nothing.

The worker path (`windows_receiver`) gives every process a low, restricted token, and the Codex
client cannot run under it: its own Windows sandbox needs the user's token to set up. Here the
client is the trusted part and the sandbox it applies to the commands it runs is the boundary;
the owner proves that boundary with `codex sandbox` before any model is started. What this module
keeps from the worker path is everything that does not depend on the token: the root is born
suspended inside its Job, resumes only through the lifecycle owner, and the Job's last handle
kills the client and every descendant. Its environment is explicit and carries no name that
looks like a secret, because sandboxed commands inherit what the client was given.
"""
from dataclasses import dataclass
import ctypes as C
from ctypes import wintypes as W
import secrets
import tempfile
import threading
import time

from .types import NativeBinding, ProcessIdentity, Refused, finite, job_object_name
from .windows_api import WindowsApi
from .windows_receiver import WindowsReceiver, require_plain_environment
from .windows_token import _TOKEN_SOURCE

_WAIT_TIMEOUT = 258


@dataclass(frozen=True)
class CommandOutcome:
    argv: tuple
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def command_outcome(argv, returncode, *, stdout, stderr, timed_out):
    return CommandOutcome(tuple(argv), returncode, stdout, stderr, timed_out)


class ClientApi(WindowsApi):
    """The production Job, sized for a Codex client tree, and a spawn with the user's own token.

    The token is an unchanged restricted copy of this process's token, a child token: assigning
    it to a new process needs no privilege, and nothing about it is wider than the owner itself.
    """
    process_limit, memory_limit = 64, 6 << 30

    def client_token(self):
        source = self._process_token(self._current_process(), _TOKEN_SOURCE)
        try:
            token = W.HANDLE()
            self.checked(self.adv('CreateRestrictedToken', [W.HANDLE, W.DWORD, W.DWORD, C.c_void_p, W.DWORD,
                C.c_void_p, W.DWORD, C.c_void_p, C.POINTER(W.HANDLE)], W.BOOL)(
                source, 0, 0, None, 0, None, 0, None, C.byref(token)))
            return token.value
        finally:
            self.close(source)


class CodexClientReceiver(WindowsReceiver):
    """WindowsReceiver's resume, wait, cancel and close, around a client created with the user's token."""

    def __init__(self, api, ticket, argv, cwd, stdin_text, *, environment, command_result, sanitize,
                 private_directory):
        self.api, self.ticket = api, ticket
        self.command_result, self.sanitize = command_result, sanitize
        self.lock = threading.RLock()
        self.job = self.process = self.thread = self.job_id = None
        self.closed = self.resumed = self.spent = False
        self.files, self.argv = [], tuple(argv)
        require_plain_environment(environment)
        try:
            for _ in range(3):
                self.files.append(tempfile.TemporaryFile(dir=private_directory))
            self.files[0].write((stdin_text or '').encode('utf-8'))
            self.files[0].seek(0)
            self.job_id = job_object_name(ticket)
            self.job = api.create_job(self.job_id)
            token = api.client_token()
            try:
                self.process, self.thread, pid, _ = api.create_suspended(
                    argv, cwd, environment, self.files, self.job, token)
            finally:
                api.close(token)
            root = ProcessIdentity(pid, api.creation(self.process))
            self.binding = NativeBinding(ticket, self.job_id, root, (root,))
            self.binding.validate()
        except BaseException:
            self.close_fail_safe(time.monotonic())
            raise


class CodexClientProvider:
    """NativeProvider for the Codex client; the existing worker provider is untouched."""

    def __init__(self, *, validate_call, environment, sanitize, private_directory, api=None):
        self.validate_call, self.environment = validate_call, dict(environment)
        require_plain_environment(self.environment)
        self.sanitize, self.private_directory = sanitize, private_directory
        self.api = api or ClientApi()

    def validate_driver_call(self, ticket, argv, cwd, timeout, stdin_text):
        if not finite(timeout) or timeout <= 0:
            raise Refused('BOUNDED_NATIVE_TIMEOUT_REQUIRED')
        self.validate_call(ticket, tuple(argv), cwd, timeout, stdin_text)

    def create_suspended(self, ticket, argv, cwd, stdin_text):
        return CodexClientReceiver(self.api, ticket, argv, cwd, stdin_text, environment=self.environment,
                                   command_result=command_outcome, sanitize=self.sanitize,
                                   private_directory=self.private_directory)

    def run_unbound(self, argv, cwd, timeout, streams):
        """One model-free client command (the sandbox precheck) in its own kill-on-close Job.

        `streams` are three open files: stdin, stdout, stderr. Returns (exit code or None, timed out).
        Leaving this function closes the Job, so nothing it started outlives it.
        """
        job_id = 'Local\\HermesSymphonyPrecheck-'+secrets.token_hex(8)
        job = self.api.create_job(job_id)
        process = thread = None
        try:
            token = self.api.client_token()
            try:
                process, thread, _, _ = self.api.create_suspended(argv, cwd, self.environment, streams, job, token)
            finally:
                self.api.close(token)
            self.api.resume(thread)
            wait = self.api.fn('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)
            result = wait(process, max(1, int(timeout*1000)))
            if result == _WAIT_TIMEOUT:
                return None, True
            if result != 0:
                raise Refused('PRECHECK_WAIT_UNKNOWN')
            return self.api.exit_code(process), False
        finally:
            self.api.close_job(job_id)
            for handle in (thread, process):
                if handle is not None:
                    self.api.close(handle)
