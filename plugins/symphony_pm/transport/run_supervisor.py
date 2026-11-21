"""One service-owned worker. SQLite owns admission and recovery; the mailbox cannot replay."""
from contextvars import Context
import logging
import threading
import time

from agent.memory_provider import spawn_context_thread
from agent.secret_scope import set_multiplex_context
from hermes_constants import set_hermes_home_override
from .types import Refused


def profile_thread(home, target, name):
    # Build the captured context from the service identity. Bootstrap may itself run during a turn.
    def create():
        set_hermes_home_override(home)
        # Even the process's own profile must not fall back to process credentials here.
        set_multiplex_context(True)
        return spawn_context_thread(target, name=name)
    return Context().run(create)


class RunSupervisor:
    def __init__(self, owner):
        self.owner = owner
        self.condition = threading.Condition()
        self.pending = None
        self.stopping = False
        self.cleanup = None
        self.cleanup_failed = False
        self.thread = profile_thread(owner.scope.profile_home, self._run, 'symphony-run-supervisor')
        self.thread.start()

    def offer(self, token, ticket, request):
        with self.condition:
            if self.stopping or not self.thread.is_alive() or self.pending is not None:
                raise Refused('SUPERVISOR_UNAVAILABLE')
            self.pending = (token, ticket, request)
            self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopping or self.pending is not None)
                if self.stopping:
                    return
                work, self.pending = self.pending, None
            try:
                self.owner.execute_reserved(*work)
            except (Exception, SystemExit) as exc:  # health: allow BLE001 -- one failed ticket cannot kill the worker
                logging.getLogger(__name__).warning('Symphony launch %s ended with %s',
                                                    work[1].attempt, type(exc).__name__)

    def close(self, deadline):
        with self.condition:
            if not self.stopping:
                self.stopping = self.owner._closing = True
                self.pending = None
                self.condition.notify()
                self.cleanup = profile_thread(self.owner.scope.profile_home, lambda: self._close_owned(deadline),
                                              'symphony-supervisor-shutdown')
                self.cleanup.start()
        self.cleanup.join(max(0, deadline-time.monotonic()))
        if self.cleanup.is_alive():
            raise Refused('OWNED_CLEANUP_PENDING')
        if self.cleanup_failed:
            raise Refused('OWNED_CLEANUP_INCOMPLETE')

    def _close_owned(self, deadline):
        try:
            try:
                self.owner.close_resources(deadline)
            finally:
                # A late preparer may still own a workspace. Keep the OS guard until it has unwound.
                self.thread.join()
                self.owner.guard.close(deadline)
        except (Exception, SystemExit) as exc:  # health: allow BLE001 -- report failure to the serving host
            self.cleanup_failed = True
            logging.getLogger(__name__).error('Symphony cleanup ended with %s', type(exc).__name__)
