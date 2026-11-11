"""Query-only process coverage. No terminate handles and no persistent Job handles."""
from dataclasses import asdict
import threading
import time

from .lifecycle import ObserverHandshake, ProcessReadback, StopReadback
from .types import ProcessIdentity, Refused, digest
from .windows_api import WindowsApi


class WindowsQueryObserver:
    def __init__(self, scope):
        self.api, self.lock = WindowsApi(), threading.RLock()
        self.scope, self.owner, self.generation = scope, None, None
        self.handles, self.covered = {}, {}
        self.closed, self.loss_handler = False, None

    def set_loss_handler(self, callback):
        self.loss_handler = callback

    def _process(self, identity):
        if identity not in self.handles:
            handle = self.api.open_process(identity.pid)
            try:
                if self.api.creation(handle) != identity.creation:
                    raise Refused('PROCESS_IDENTITY_CHANGED')
            except BaseException:
                self.api.close(handle)
                raise
            self.handles[identity] = handle
        return self.handles[identity]

    def _query(self, target):
        binding = target.binding
        binding.validate()
        # A retained query Job handle would postpone kill-on-close. Only process
        # query handles survive calls; a missing Job is UNKNOWN, never empty proof.
        job = self.api.open_job(binding.job_object)
        try:
            root = self._process(binding.root)
            if not self.api.in_job(root, job):
                raise Refused('JOB_IDENTITY_UNKNOWN')
            active, pids = self.api.snapshot(job)
            members = []
            for pid in pids:
                handle = self.api.open_process(pid)
                try:
                    identity = ProcessIdentity(pid, self.api.creation(handle))
                    if not self.api.in_job(handle, job):
                        raise Refused('JOB_MEMBER_CHANGED')
                    if identity not in self.handles:
                        self.handles[identity], handle = handle, None
                    members.append(identity)
                finally:
                    if handle is not None:
                        self.api.close(handle)
            required = tuple(dict.fromkeys((*target.known_processes, *members)))
            records = tuple(ProcessReadback(p, self.api.wait(self._process(p))) for p in required)
            return active, tuple(members), records
        finally:
            self.api.close(job)

    def _lost(self):
        if self.loss_handler is not None:
            self.loss_handler()

    def handshake(self, owner, generation, bindings):
        try:
            with self.lock:
                if self.closed or owner.profile_home != self.scope.profile_home:
                    raise Refused('OBSERVER_CLOSED')
                if owner.namespace != self.scope.namespace:
                    raise Refused('OBSERVER_SCOPE')
                for target in bindings:
                    self._query(target)
                self.owner, self.generation = owner, generation
                self.covered = {t.binding: t for t in bindings}
                return ObserverHandshake(owner, generation, time.time(), tuple(digest(asdict(t)) for t in bindings))
        except Exception:
            self._lost()
            raise

    def read(self, target, epoch, generation):
        try:
            with self.lock:
                if self.closed or generation != self.generation or target.binding not in self.covered:
                    raise Refused('OBSERVER_COVERAGE_UNKNOWN')
                active, members, records = self._query(target)
                return StopReadback(target.binding, epoch, generation, time.time(), True, active, members, records)
        except Exception:
            self._lost()
            raise

    def close(self, deadline):
        with self.lock:
            self.closed = True
            handles, self.handles = self.handles, {}
            failures = []
            for handle in handles.values():
                try:
                    self.api.close(handle)
                except OSError as exc:
                    failures.append(exc)
            if failures:
                raise Refused('OBSERVER_CLOSE_UNKNOWN') from failures[0]
