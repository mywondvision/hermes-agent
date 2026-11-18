"""One `codex exec` per launch ticket, the way the two pilots ran it. Importing creates nothing.

The attempt gets a copy of the tree at the plan's commit, without `.git`, in the one directory the
Codex permission profile lets its commands write (`<attempts root>/active`). Before any model runs,
`codex sandbox` executes a probe under that same profile; any access outside the attempt, a
reachable loopback port or a secret-looking variable stops the launch there. The model run is the
ticket's single native create, through the lifecycle owner's runner. The probe is offered to the
model as well, but only observed: a model may decline it, and that does not stop the run.

The result is data for the owner: the patch the attempt produced, its SHA-256 and the outcome of
an owner-side check in a separate copy (apply, then the host's commands, including tests that need
Git, which cannot run inside the sandbox). Nothing is applied to the repository. Once a ticket has
a native binding this launcher returns a result instead of raising, so the outcome reaches the
journal even when the run, capture or check failed.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
from types import MappingProxyType

from hermes_cli.plugin_request_binding import canonical_home
from hermes_dir_acl import make_user_dir
from hermes_platform.resolver import locate_command
from . import attempt_workspace as ws
from .qualification import _open_denying_writers, environment_digest
from .types import Refused, binding_from_json, digest, finite, identifier, sha
from .windows_receiver import _SECRET_NAME, require_plain_environment

#: The plan's model and effort when both come from the qualified `config.toml`, not from flags.
CONFIG_SELECTED = 'codex-config'
ACTIVE = 'active'

PROBE = r'''
import json, os, socket, sys
from pathlib import Path
mode, report, auth, home, outside, port = sys.argv[1:7]
out = {'mode': mode, 'steps': []}
def step(name, action):
    record = {'name': name}
    try:
        record.update(action() or {}); record.setdefault('ok', True)
    except OSError as exc:
        record.update(ok=False, errno=exc.errno, winerror=getattr(exc, 'winerror', None))
    except Exception as exc:
        record.update(ok=False, exception=type(exc).__name__)
    out['steps'].append(record)
def opened(path, mode='rb'):
    with open(path, mode):
        pass
def env():
    names = sorted(os.environ)
    suspicious = sorted(n for n, v in os.environ.items() if v.startswith(('eyJ', 'sk-')))
    return {'secret_like_names': [n for n in names if any(w in n.upper() for w in ('KEY', 'TOKEN', 'SECRET', 'AUTH', 'PASS'))],
            'suspicious_values': suspicious}
def connect():
    with socket.create_connection(('127.0.0.1', int(port)), timeout=5):
        pass
step('env', env)
step('read-auth', lambda: opened(auth))
step('list-codex-home', lambda: {'count': len(os.listdir(home))})
step('read-outside', lambda: opened(Path(outside)/'canary.txt'))
step('write-outside', lambda: opened(Path(outside)/'probe-write.txt', 'ab'))
step('loopback', connect)
Path(report).write_text(json.dumps(out), encoding='utf-8')
print('PROBE_DONE', mode)
'''

PREAMBLE = r'''
You are working in a copy of the hermes-agent repository at a fixed commit, without .git. Network is disabled for
commands and your write access is limited to this directory and its temp folder.

STEP 1 (first, if you are willing): run this read-only boundary check of your own sandbox and do not modify it:
{probe}
Then continue.

STEP 2, the task below. Tests that call git cannot run inside this sandbox; the owner runs them afterwards in a
separate copy, so do not try to work around that. Keep the existing style (short docstrings that explain why, no
defensive clutter). Finish with a short summary of the change and the final test result.

--- task ---
{goal}
--- end of task ---
'''

_DENIED = ('read-auth', 'list-codex-home', 'read-outside', 'write-outside', 'loopback')


def probe_ok(report):
    """Every outside access refused and no secret-looking variable visible to sandboxed commands."""
    steps = {s.get('name'): s for s in report.get('steps', ())}
    env = steps.get('env') or {}
    return (env.get('ok') is True and not env.get('secret_like_names') and not env.get('suspicious_values')
            and all(steps.get(name, {}).get('ok') is False for name in _DENIED))


def client_environment(source, *, codex_home, client_home):
    """The client's explicit environment: `source` without secret-looking names, its own HOME and TEMP.

    Commands the sandbox runs inherit from this, so nothing here may carry a credential; the
    client's own login lives in the file store of `codex_home`, which the profile denies.
    """
    environment = {k: v for k, v in source.items() if not _SECRET_NAME.search(k)}
    home = Path(client_home)
    environment.setdefault('SYSTEMDRIVE', 'C:')  # the MXC sandbox will not start without it
    environment.update(CODEX_HOME=str(codex_home), USERPROFILE=str(home), HOME=str(home),
                       APPDATA=str(home/'appdata'), LOCALAPPDATA=str(home/'local'),
                       TEMP=str(home/'temp'), TMP=str(home/'temp'))
    require_plain_environment(environment)
    return environment


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class CodexExecQualification:
    """The Codex client build, its home and profile, the client environment and the issuers that were qualified.

    The model and effort are whatever the hashed `config.toml` selects; the plan names them
    `CONFIG_SELECTED`, so a changed configuration is a refused launch, not a different model.
    """
    profile_home: str
    namespace: str
    scheduler_id: str
    executable: str
    executable_sha256: str
    codex_home: str
    config_sha256: str
    permission_profile: str
    environment_ref: str
    environment_sha256: str
    issuer_refs: tuple

    def validate(self):
        if (type(self.profile_home) is not str or not Path(self.profile_home).is_absolute()
                or self.profile_home != canonical_home(self.profile_home)
                or not all(identifier(v) for v in (self.namespace, self.scheduler_id, self.environment_ref,
                                                   self.permission_profile))
                or not all(sha(v, 64) for v in (self.executable_sha256, self.config_sha256, self.environment_sha256))
                or type(self.executable) is not str or not Path(self.executable).is_absolute()
                or Path(self.executable).suffix.lower() != '.exe'
                or type(self.codex_home) is not str or not Path(self.codex_home).is_absolute()
                or type(self.issuer_refs) is not tuple or not self.issuer_refs
                or not all(identifier(v) for v in self.issuer_refs)
                or len(set(self.issuer_refs)) != len(self.issuer_refs)):
            raise Refused('CODEX_QUALIFICATION_RECORD')

    @contextmanager
    def held_executable(self):
        """The hashed bytes are the launched bytes: no writer can open the file while this is held."""
        with _open_denying_writers(self.executable) as stream:
            hasher = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1 << 20), b''):
                hasher.update(chunk)
            if hasher.hexdigest() != self.executable_sha256:
                raise Refused('QUALIFIED_EXECUTABLE_CHANGED')
            yield

    def verify_executable(self):
        with self.held_executable():
            pass

    def verify_config(self):
        if file_sha256(Path(self.codex_home)/'config.toml') != self.config_sha256:
            raise Refused('CODEX_CONFIG_CHANGED')


@dataclass(frozen=True)
class AttemptSettings:
    """Host paths and the owner-side check. `checks(paths)` returns (name, argv) pairs run in the check copy.

    `command_prefix` sits between the executable and Codex's arguments; only a test's stand-in
    client (an interpreter plus a script) sets it.
    """
    repository: str
    attempts_root: str
    probe_python: str
    checks: object
    check_environment: object
    command_prefix: tuple = ()
    exec_timeout: float = 40*60
    precheck_timeout: float = 180
    check_timeout: float = 1200

    def validate(self):
        if (not all(type(v) is str and Path(v).is_absolute() for v in (
                self.repository, self.attempts_root, self.probe_python))
                or not callable(self.checks) or type(self.command_prefix) is not tuple
                or not all(finite(v) and v > 0 for v in (self.exec_timeout, self.precheck_timeout,
                                                         self.check_timeout))):
            raise Refused('ATTEMPT_SETTINGS')

    @property
    def active(self):
        return Path(self.attempts_root)/ACTIVE


def clean_artifacts(src):
    """Interpreter, pytest and PowerShell caches are by-products of running things, never part of a patch."""
    removed = []
    stray = src/'Microsoft'/'Windows'/'PowerShell'
    if stray.is_dir():
        shutil.rmtree(stray)
        removed.append('Microsoft/Windows/PowerShell')
        for parent in (src/'Microsoft'/'Windows', src/'Microsoft'):
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
    for name in ('__pycache__', '.pytest_cache'):
        for path in sorted(src.rglob(name), reverse=True):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
                removed.append(path.relative_to(src).as_posix())
    return removed


@contextmanager
def _loopback():
    """A listening loopback port the probe must fail to reach; the backlog completes a connect without accept."""
    listener = socket.socket()
    try:
        listener.bind(('127.0.0.1', 0))
        listener.listen(4)
        yield listener.getsockname()[1]
    finally:
        listener.close()


class _ProbeWatch:
    """Observes the in-exec probe report while the model runs; a found access stops the attempt."""

    def __init__(self, report, stop_attempt):
        self.report, self.stop_attempt = report, stop_attempt
        self.verdict, self.stop_outcome = None, None
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, name='symphony-probe-watch', daemon=True)

    def _read(self):
        try:
            return json.loads(self.report.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def _judge(self, running=True):
        data = self._read()
        if data is None:
            return False
        self.verdict = 'PROBE_OK' if probe_ok(data) else 'PROBE_VIOLATION'
        if self.verdict == 'PROBE_VIOLATION' and running:
            try:
                self.stop_outcome = self.stop_attempt()
            except Exception as exc:  # health: allow BLE001 -- recorded; the time limit still bounds the run
                self.stop_outcome = 'STOP_FAILED:'+type(exc).__name__
        return True

    def _run(self):
        while not self.done.wait(2):
            if self._judge():
                return

    def finish(self):
        self.done.set()
        self.thread.join(10)
        if self.verdict is None:
            self._judge(running=False)  # the run is over; a late report is evidence, not a stop
        return self.verdict or 'PROBE_NOT_RUN'


class CodexExecLauncher:
    """The launcher LifecycleOwner calls; its single runner call is the `codex exec` model run."""

    def __init__(self, qualification, settings, *, environment, sanitize=lambda text: text):
        if type(qualification) is not CodexExecQualification or type(settings) is not AttemptSettings:
            raise Refused('CODEX_QUALIFICATION_RECORD')
        qualification.validate()
        settings.validate()
        environment = MappingProxyType(dict(environment))
        require_plain_environment(environment)
        if environment_digest(environment) != qualification.environment_sha256:
            raise Refused('QUALIFICATION_BINDING')
        self.qualification, self.settings = qualification, settings
        self.environment, self.sanitize = environment, sanitize
        self.lock, self.commands = threading.RLock(), {}

    def validate_call(self, ticket, argv, cwd, timeout, stdin_text):
        with self.lock:
            if self.commands.get(ticket.attempt) != (ticket, tuple(argv), str(Path(cwd).resolve()), timeout, stdin_text):
                raise Refused('QUALIFIED_DRIVER_CALL_REQUIRED')

    def _bind(self, ticket, runner):
        q, plan = self.qualification, ticket.plan
        owner = runner.owner
        if (ticket.namespace != q.namespace or owner.scope.namespace != q.namespace
                or canonical_home(owner.scope.profile_home) != q.profile_home):
            raise Refused('LAUNCHER_PROFILE_BINDING')
        if (Path(plan.workspace) != self.settings.active/'src' or plan.environment_ref != q.environment_ref
                or plan.model != CONFIG_SELECTED or plan.effort != CONFIG_SELECTED):
            raise Refused('CODEX_EXEC_PLAN_BINDING')
        q.verify_config()
        resolution = locate_command('git')
        if not resolution.command:
            raise Refused('OWNER_GIT_UNAVAILABLE')
        return resolution.command[0]

    def __call__(self, ticket, request, runner):
        git = self._bind(ticket, runner)
        active = self.settings.active
        try:
            active.mkdir()
        except FileExistsError as exc:
            raise Refused('ATTEMPT_ACTIVE_EXISTS') from exc
        try:
            return self._attempt(git, ticket, request, runner, active)
        finally:
            if active.exists():
                self._settle(active, ticket.attempt)

    def _attempt(self, git, ticket, request, runner, active):
        q, s = self.qualification, self.settings
        # A short name keeps the shadow Git directory and the check copy inside path limits.
        data = (Path(q.profile_home)/'plugin-data'/'symphony_pm'/digest(q.namespace)[:32]/'attempts'
                / digest(ticket.attempt)[:12])
        make_user_dir(data, mode=0o700, parents=True, exist_ok=False)
        workspace = ws.Workspace(ticket.attempt, active)
        for directory in (workspace.src, workspace.temp, active/'probe', data/'outside'):
            directory.mkdir()
        (data/'outside'/'canary.txt').write_text('canary-'+os.urandom(16).hex(), encoding='utf-8')
        (active/'probe'/'probe.py').write_text(PROBE, encoding='utf-8')
        ws.export(git, s.repository, ticket.plan.source_sha, workspace.src)
        capture = ws.Capture(git, s.repository, ticket.plan.source_sha, workspace, data/'shadow', data/'results')
        capture.baseline()
        record = {'attempt': ticket.attempt, 'source_sha': ticket.plan.source_sha,
                  'codex_config_sha256': q.config_sha256}
        with _loopback() as port:
            def probe(mode):
                return [s.probe_python, '-I', '-B', str(active/'probe'/'probe.py'), mode,
                        str(active/(mode+'-report.json')), str(Path(q.codex_home)/'auth.json'), q.codex_home,
                        str(data/'outside'), str(port)]
            record['precheck'] = self._precheck(runner.owner.native, data, workspace, probe('precheck'))
            if record['precheck']['verdict'] != 'PROBE_OK':
                self._write(data, record)
                raise Refused('CODEX_PRECHECK_REFUSED')
            (active/'precheck-report.json').unlink()
            prompt = PREAMBLE.format(probe=subprocess.list2cmdline(probe('inexec')), goal=request.goal)
            record.update(self._exec(ticket, runner, data, workspace, prompt))
        if record['stop'] is None:
            record.update(self._capture(git, capture, workspace, data, ticket.plan.source_sha))
        record['workspace'] = self._settle(active, ticket.attempt)
        self._write(data, record)
        return record

    def _precheck(self, native, data, workspace, command):
        q, s = self.qualification, self.settings
        argv = [q.executable, *s.command_prefix, 'sandbox', '-C', str(workspace.src), '-P', q.permission_profile,
                '--', *command]
        names = ('precheck.in', 'precheck.out', 'precheck.err')
        (data/names[0]).write_bytes(b'')
        streams = [(data/names[0]).open('rb'), (data/names[1]).open('w+b'), (data/names[2]).open('w+b')]
        try:
            with q.held_executable():
                exit_code, timed_out = native.run_unbound(argv, workspace.src, s.precheck_timeout, streams)
        finally:
            for stream in streams:
                stream.close()
        report = workspace.path/'precheck-report.json'
        try:
            data_report = json.loads(report.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data_report = None
        verdict = 'PROBE_NOT_RUN' if data_report is None else 'PROBE_OK' if probe_ok(data_report) else 'PROBE_VIOLATION'
        return {'exit_code': exit_code, 'timed_out': timed_out, 'verdict': verdict, 'report': data_report}

    def _exec(self, ticket, runner, data, workspace, prompt):
        q, s = self.qualification, self.settings
        argv = (q.executable, *s.command_prefix, 'exec', '--skip-git-repo-check', '--ephemeral', '--json',
                '--strict-config', '-o', str(data/'last-message.txt'), '-C', str(workspace.src),
                '-c', f'default_permissions="{q.permission_profile}"', '-')
        cwd = str(workspace.src)
        with self.lock:
            if ticket.attempt in self.commands:
                raise Refused('CODEX_ATTEMPT_ALREADY_BOUND')
            self.commands[ticket.attempt] = (ticket, argv, str(Path(cwd).resolve()), s.exec_timeout, prompt)
        watch = _ProbeWatch(workspace.path/'inexec-report.json', lambda: self._stop(ticket, runner.owner))
        watch.thread.start()
        outcome, stop = None, None
        try:
            with q.held_executable():
                outcome = runner(list(argv), cwd=cwd, timeout=s.exec_timeout, stdin_text=prompt)
        except Refused as exc:
            stop = str(exc)
        except Exception as exc:  # health: allow BLE001 -- a bound run's outcome must still reach the journal
            stop = 'RUN_FAILED:'+type(exc).__name__
        finally:
            with self.lock:
                self.commands.pop(ticket.attempt, None)
            verdict = watch.finish()
        stdout = getattr(outcome, 'stdout', '') or ''
        (data/'events.jsonl').write_text(stdout, encoding='utf-8', newline='\n')
        (data/'exec.err').write_text(getattr(outcome, 'stderr', '') or '', encoding='utf-8', newline='\n')
        events = []
        for line in stdout.splitlines():
            if line.startswith('{'):
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
        if verdict == 'PROBE_VIOLATION':
            stop = 'PROBE_VIOLATION'
        elif outcome is not None and outcome.timed_out:
            stop = 'TIME_LIMIT'
        return {'exit_code': getattr(outcome, 'returncode', None), 'stop': stop, 'inexec_probe': verdict,
                'probe_stop': watch.stop_outcome,
                'usage': [e.get('usage') for e in events if e.get('type') == 'turn.completed'],
                'commands': sum(1 for e in events if e.get('type') == 'item.started'
                                and (e.get('item') or {}).get('type') == 'command_execution')}

    @staticmethod
    def _stop(ticket, owner):
        _, row = owner.journal.read_launch(owner.token, ticket.project, ticket.job, ticket.revision)
        if row['binding'] is None:
            return 'UNBOUND'
        binding = binding_from_json(json.loads(row['binding']))
        return owner.cancel_internal(binding, 'EMERGENCY_CLEANUP', detail='PROBE_VIOLATION').state

    def _capture(self, git, capture, workspace, data, source_sha):
        cleaned = clean_artifacts(workspace.src)
        try:
            result = capture.result()
        except Refused as exc:
            return {'cleaned': cleaned, 'patch': {'refused': str(exc)}}
        patch = {'path': str(result.patch), 'sha256': result.sha256, 'paths': list(result.paths)}
        if not result.paths:
            return {'cleaned': cleaned, 'patch': patch, 'check': None}
        return {'cleaned': cleaned, 'patch': patch, 'check': self._check(git, data, result, source_sha)}

    def _check(self, git, data, result, source_sha):
        """Apply to a fresh copy of the commit and run the host's commands there; the attempt copy is not reused."""
        s = self.settings
        copy = data/'check'
        copy.mkdir()
        ws.export(git, s.repository, source_sha, copy)
        environment = ws._clean_environment(GIT_CEILING_DIRECTORIES=str(data))
        applied = subprocess.run([git, 'apply', '--binary', str(result.patch)], cwd=copy, capture_output=True,
                                 env=environment, timeout=ws.GIT_TIMEOUT, check=False)
        outcome = {'apply': applied.returncode, 'commands': {}}
        if applied.returncode == 0:
            for name, argv in s.checks(tuple(result.paths)):
                done = subprocess.run(list(argv), cwd=copy, capture_output=True, timeout=s.check_timeout,
                                      env=dict(s.check_environment), check=False)
                log = data/f'check-{name}.log'
                log.write_bytes(done.stdout+b'\n--- stderr ---\n'+done.stderr)
                outcome['commands'][name] = done.returncode
        outcome['ok'] = applied.returncode == 0 and all(code == 0 for code in outcome['commands'].values())
        return outcome

    def _settle(self, active, attempt):
        """Keep the attempt under its own name; while something still holds it, it stays `active`."""
        target = Path(self.settings.attempts_root)/attempt
        try:
            active.rename(target)
        except OSError:
            return {'path': str(active), 'renamed': False}
        return {'path': str(target), 'renamed': True}

    @staticmethod
    def _write(data, record):
        (data/'owner-record.json').write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding='utf-8')
