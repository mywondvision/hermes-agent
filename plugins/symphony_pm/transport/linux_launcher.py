"""Ticket-bound Linux client and precheck. The untrusted command sees only /work in the project."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import threading
import uuid

from .linux_control import message, publish
from . import linux_carrier as provider
from .linux_host import SystemdManager, private_directory
from .linux_identity import unit_for
from .linux_probe import PROGRAM
from .recapture import archive_path, attempt_data, directory_identity
from .types import Refused, digest, finite, identifier


@dataclass(frozen=True)
class LinuxSettings:
    project_ref: str
    repository: str
    attempts_root: str
    state: str
    exec_timeout: float
    probe_timeout: float
    minimum_disk_free: int
    minimum_memory_free: int
    maximum_attempt_volume_bytes: int

    @property
    def active(self):
        return Path(self.attempts_root)/'active'

    def validate(self):
        if (not identifier(self.project_ref) or not all(Path(p).is_absolute() for p in
                                                        (self.repository, self.attempts_root, self.state))
                or not all(finite(t) and t > 0 for t in (self.exec_timeout, self.probe_timeout))
                or not all(type(n) is int and n > 0 for n in (self.minimum_memory_free, self.minimum_disk_free,
                                                               self.maximum_attempt_volume_bytes))):
            raise Refused('LINUX_ATTEMPT_SETTINGS')
        private_directory(self.state)
        private_directory(self.attempts_root)


@dataclass(frozen=True)
class LinuxCall:
    ticket: object
    argv: tuple
    workspace: str
    timeout: float
    prompt: str
    challenge: str
    probe_argv: tuple
    probe_timeout: float


def marker_targets(launcher):
    """Read provisioned harmless markers at each actual protected root before using its probe."""
    from .linux_sandbox import in_image
    q, s = launcher.qualification, launcher.settings
    marker = '.symphony-qualification-marker'
    codex = '/etc/symphony/codex/' + marker
    roots = {'deny_control': Path(s.state), 'deny_source': Path(s.repository),
             'deny_neighbor': Path(s.attempts_root)/'qualification-neighbor',
             'deny_journal': Path(q.profile_home)/'plugin-data'/'symphony_pm'/digest(q.namespace)[:32],
             'deny_credentials': in_image(q.runtime, codex).parent}
    if len({path.resolve(strict=True) for path in roots.values()}) != len(roots):
        raise Refused('LINUX_PROBE_BOUNDARY_ALIAS')
    hidden, fingerprints = {}, {}
    for purpose, directory in roots.items():
        receipt = directory/marker
        if (receipt.resolve(strict=True) != receipt or not receipt.is_file()
                or receipt.stat().st_size != len(b'symphony-qualification-marker\n')
                or receipt.read_bytes() != b'symphony-qualification-marker\n'):
            raise Refused('LINUX_PROBE_MARKER')
        hidden[purpose] = codex if purpose == 'deny_credentials' else str(receipt)
        fingerprints[purpose] = (str(receipt), hidden[purpose], hashlib.sha256(receipt.read_bytes()).hexdigest())
    if digest(fingerprints) != q.marker_set_sha256:
        raise Refused('LINUX_PROBE_MARKERS_CHANGED')
    hidden['deny_owner_processes'] = f'/proc/{os.getpid()}/root' + hidden['deny_control']
    return hidden


@contextmanager
def canaries(launcher):
    hidden = marker_targets(launcher)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        yield {'hidden': hidden, 'port': listener.getsockname()[1]}


class LinuxLauncher:
    def __init__(self, qualification, settings, *, owner_check=None, provider_evidence_sha256=None):
        provider.require_sealed_primary(qualification, provider_evidence_sha256)
        qualification.validate()
        self.qualification, self.settings = qualification, settings
        self.owner_check = owner_check
        self.calls, self.lock = {}, threading.RLock()

    @property
    def active(self):
        return self.settings.active

    def job_coverage_for(self, guard):
        self.qualification.verify()
        return self.qualification.host_id == guard.recovery_host_id

    def never_created_workspace_is_clear(self, ticket):
        paths = (self.active, archive_path(self.settings.attempts_root, ticket.attempt),
                 attempt_data(self.qualification, ticket), Path(self.settings.state)/unit_for(ticket))
        for path in paths:
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return False
            return False
        return True

    def reconcile_check(self, owner):
        from .linux_check import reconcile_check
        reconcile_check(self, owner)

    def require_check_slot_clear(self):
        from .linux_check import check_marker
        try:
            check_marker(self).lstat()
        except FileNotFoundError:
            pass
        else:
            raise Refused('CHECK_STOP_UNPROVEN')

    def workspace_clear(self):
        try:
            self.settings.active.lstat()
        except FileNotFoundError:
            pass
        else:
            return False
        # An unresolved create survives loss of the active name or a human release.
        for directory in Path(self.settings.state).glob('hermes-symphony@*.service'):
            if not (directory/'retire').is_file():
                return False
        return True

    def require_project(self, project_ref):
        if project_ref != self.settings.project_ref:
            raise Refused('LINUX_PROJECT_BINDING')

    def admission(self):
        self.require_check_slot_clear()
        self.qualification.verify()
        self.settings.validate()
        if digest(asdict(self.settings)) != self.qualification.settings_sha256:
            raise Refused('LINUX_QUALIFIED_SETTINGS_CHANGED')
        marker_targets(self)
        q, s = self.qualification, self.settings
        manager = SystemdManager(q, s.state)
        manager.verify_configuration('hermes-symphony@' + '0'*64 + '.service')
        controllers = Path('/sys/fs/cgroup/cgroup.controllers').read_text(encoding='ascii').split()
        if not {'cpu', 'memory', 'pids'} <= set(controllers):
            raise Refused('LINUX_CGROUP_CONTROLLERS')
        attempts = Path(s.attempts_root)
        volume = os.statvfs(attempts)
        if (not os.path.ismount(attempts) or volume.f_blocks * volume.f_frsize > s.maximum_attempt_volume_bytes
                or any(attempts.stat().st_dev == Path(path).stat().st_dev
                       for path in (q.profile_home, s.state, s.repository))):
            raise Refused('LINUX_ATTEMPT_DISK_LIMIT')
        memory = Path('/proc/meminfo').read_text(encoding='ascii').splitlines()
        available = next(int(line.split()[1]) * 1024 for line in memory if line.startswith('MemAvailable:'))
        if (available < self.settings.minimum_memory_free or any(
                shutil.disk_usage(p).free < self.settings.minimum_disk_free for p in
                (self.settings.state, self.settings.attempts_root, self.qualification.profile_home))):
            raise Refused('LINUX_OWNER_CAPACITY')

    def lookup(self, ticket):
        with self.lock:
            call = self.calls.get(ticket.attempt)
            if call is None or call.ticket != ticket:
                raise Refused('LINUX_CALL_NOT_BOUND')
            return call

    def validate_call(self, ticket, argv, cwd, timeout, stdin_text):
        call = self.lookup(ticket)
        if (tuple(argv), str(cwd), timeout, stdin_text) != (call.argv, call.workspace, call.timeout, call.prompt):
            raise Refused('QUALIFIED_DRIVER_CALL_REQUIRED')

    def _call(self, ticket, request, workspace, targets):
        q = self.qualification
        challenge = uuid.uuid4().hex
        probe = (q.executable, 'sandbox', '-P', q.permission_profile, '-C', '/work', '--',
                 q.probe_python, '-I', '-B', '-c', PROGRAM, challenge, json.dumps(targets))
        argv = provider.primary_exec(q)
        prompt = json.dumps({'goal': request.goal, 'raw_constraints': list(request.raw_constraints)},
                            ensure_ascii=False, allow_nan=False)
        return LinuxCall(ticket, argv, str(workspace.src), self.settings.exec_timeout, prompt,
                         challenge, probe, self.settings.probe_timeout)

    def __call__(self, ticket, request, runner):
        from . import linux_workspace
        q, plan = self.qualification, ticket.plan
        self.require_project(ticket.project)
        if (ticket.namespace != q.namespace or runner.owner.scope.profile_home != q.profile_home
                or Path(plan.workspace) != self.settings.active/'src'
                or (plan.model, plan.effort, plan.environment_ref) != ('codex-config', 'codex-config', q.environment_ref)):
            raise Refused('LINUX_PLAN_BINDING')
        self.admission()
        self.settings.active.mkdir(mode=0o700, exist_ok=False)
        try:
            _, workspace, capture = linux_workspace.prepare(self, ticket)
            with canaries(self) as targets:
                call = self._call(ticket, request, workspace, targets)
                with self.lock:
                    self.calls[ticket.attempt] = call
                try:
                    return self._run(ticket, runner, capture, call)
                finally:
                    with self.lock:
                        self.calls.pop(ticket.attempt, None)
        except BaseException:
            _, row = runner.owner.journal.read_launch(runner.token, ticket.project, ticket.job, ticket.revision)
            if row['state'] == 'LAUNCH_INTENT':
                self._archive(ticket)
            raise

    def _run(self, ticket, runner, capture, call):
        from .linux_workspace import capture_patch
        result = {'exit_code': None, 'stop': None}
        try:
            outcome = runner(list(call.argv), cwd=call.workspace, timeout=call.timeout, stdin_text=call.prompt)
            result['exit_code'] = outcome.returncode
            result['stop'] = 'TIME_LIMIT' if outcome.timed_out else None
        except Exception as exc:  # health: allow BLE001 -- every bound client must enter the stop coordinator
            result['stop'] = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        stopped, causes = runner.owner.finish_client_job(runner.token, ticket)
        if 'PM_CANCEL' in causes:
            result['stop'] = 'PM_CANCEL'
        result['workspace'] = {'path': str(self.settings.active), 'renamed': False}
        if stopped:
            if self.settle_workspace(ticket):
                result['workspace'] = {'path': str(archive_path(self.settings.attempts_root, ticket.attempt)),
                                       'renamed': True}
                # The capture object still points at active. Rebind the workspace after its atomic freeze.
                from .attempt_workspace import Workspace
                capture.workspace = Workspace(ticket.attempt, Path(result['workspace']['path']))
                try:
                    result['patch'] = capture_patch(capture)
                    if result['patch']['paths']:
                        from .linux_check import check_patch
                        result['check'] = check_patch(self, ticket, runner, capture, result['patch'],
                                                      completed=result['stop'] is None and result['exit_code'] == 0)
                except (Refused, OSError) as exc:
                    result['capture_error'] = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        return result

    def _archive(self, ticket):
        active, archive = self.settings.active, archive_path(self.settings.attempts_root, ticket.attempt)
        if os.path.lexists(archive):
            raise Refused('LINUX_ARCHIVE_EXISTS')
        active.rename(archive)
        return archive

    def settle_workspace(self, ticket):
        data = attempt_data(self.qualification, ticket)
        active = self.settings.active
        archive = archive_path(self.settings.attempts_root, ticket.attempt)
        control = Path(self.settings.state)/unit_for(ticket)
        try:
            receipt = message(data, 'workspace-id.json')
            if os.path.lexists(active):
                if directory_identity(active) != receipt:
                    return False
                self._archive(ticket)
            if directory_identity(archive) != receipt:
                return False
            from .linux_workspace import seal_workspace
            seal_workspace(self, ticket, archive)
            if control.exists() and not (control/'retire').exists():
                publish(control, 'retire', message(control, 'ready')['invocation_id'])
            return True
        except (OSError, ValueError, Refused):
            return False

    def recapture(self, journal, token, ticket, row):
        from .linux_workspace import recapture
        return recapture(self, journal, token, ticket, row)
