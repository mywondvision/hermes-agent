"""Use the existing qualified kit driver; no CLI probes, scheduler or fallback runner."""
from dataclasses import dataclass
from pathlib import Path
import threading
from hermes_platform.resolver import locate_command
from hermes_cli.plugin_request_binding import canonical_home

from .qualification import QualificationRecord
from .types import Refused


@dataclass(frozen=True)
class PreparedCodexAttempt:
    driver: object
    pack: object
    prompt: str
    environment_ref: str
    validate_result: object
    kit_revision: str


class CodexLauncher:
    """Host supplies already qualified, exclusively owned kit CodexDriver objects.

    The host's factory must resolve executables through hermes_platform and bind
    the canonical validator and explicit environment before any execution permit.
    Missing qualification cannot trigger probe/readiness calls in this path.
    """
    def __init__(self, prepared_attempt, *, qualification):
        if type(qualification) is not QualificationRecord:
            raise Refused('QUALIFICATION_RECORD')
        qualification.validate()
        self.prepared_attempt, self.qualification = prepared_attempt, qualification
        self.profile_home, self.namespace = qualification.profile_home, qualification.namespace
        self.lock, self.commands, self.used_drivers = threading.RLock(), {}, set()

    def validate_call(self, ticket, argv, cwd, timeout, stdin_text):
        with self.lock:
            expected = self.commands.get(ticket.attempt)
            if expected != (ticket, tuple(argv), str(Path(cwd).resolve()), timeout, stdin_text):
                raise Refused('QUALIFIED_DRIVER_CALL_REQUIRED')

    def __call__(self, ticket, request, runner):
        if (ticket.namespace != self.namespace or runner.owner.scope.namespace != self.namespace
                or canonical_home(runner.owner.scope.profile_home) != self.profile_home):
            raise Refused('LAUNCHER_PROFILE_BINDING')
        prepared = self.prepared_attempt(ticket, request)
        if type(prepared) is not PreparedCodexAttempt:
            raise Refused('QUALIFIED_CODEX_ATTEMPT_REQUIRED')
        driver, pack, q = prepared.driver, prepared.pack, self.qualification
        probe = driver.probed
        target = driver._target  # cached resolution only; resolve() may otherwise search the host
        if (driver.runtime != 'codex' or probe is None or probe.readiness != 'ready'
                or driver.model != ticket.plan.model or driver.effort != ticket.plan.effort
                or prepared.environment_ref != ticket.plan.environment_ref
                or prepared.kit_revision != q.kit_revision or prepared.environment_ref != q.environment_ref
                or Path(pack.worktree).resolve() != Path(ticket.plan.workspace).resolve()
                or pack.checkpoint_sha != ticket.plan.source_sha or pack.peer_runtime != 'codex'
                or pack.peer_spawn_allowed or not callable(prepared.validate_result)
                or not target or not Path(target).is_absolute() or Path(target).suffix.lower() != '.exe'
                or Path(target).resolve() != Path(q.executable).resolve()):
            raise Refused('QUALIFIED_CODEX_ATTEMPT_REQUIRED')
        with q.held_executable():
            # Held until the launch returns: the driver executes this exact path.
            return self._launch_held(ticket, runner, prepared, target)

    def _launch_held(self, ticket, runner, prepared, target):
        driver, pack = prepared.driver, prepared.pack
        probe = driver.probed
        probe.require(*driver.required_features)
        resolution = locate_command(target)
        if not resolution.command or Path(resolution.command[0]).resolve() != Path(target).resolve():
            raise Refused('QUALIFIED_EXECUTABLE_UNAVAILABLE')
        with self.lock:
            if id(driver) in self.used_drivers or ticket.attempt in self.commands:
                raise Refused('CODEX_DRIVER_ALREADY_BOUND')
            self.used_drivers.add(id(driver))
            driver.runner = runner
        try:
            # Syntax, strict schema projection, model/effort/budget/bypass checks
            # remain owned by the existing driver. This call uses its cached probe.
            args = driver.implementation_argv(pack, prepared.prompt)
            argv = (target, *args)
            driver.reject_bypass(argv)
            with self.lock:
                self.commands[ticket.attempt] = (ticket, argv, str(Path(pack.worktree).resolve()),
                                                  driver.timeout, prepared.prompt)
            run = driver.launch_implementation(pack, prepared.prompt)
            if run['returncode'] != 0 or run['timedOut']:
                raise Refused('CODEX_RESULT_UNKNOWN')
            result = driver.collect_result(pack)
            if result is None:
                raise Refused('CODEX_RESULT_MISSING')
            prepared.validate_result(result)
            return result
        finally:
            with self.lock:
                self.commands.pop(ticket.attempt, None)
            driver.cleanup(pack)
