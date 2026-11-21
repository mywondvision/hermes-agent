"""One `codex exec` per launch ticket, the way the two pilots ran it. Importing creates nothing.

The attempt gets a copy of the tree at the plan's commit, without `.git`, in the one directory the
Codex permission profile lets its commands write (`<attempts root>/active`). Before any model runs,
`codex sandbox` executes a probe under that same profile; any access outside the attempt, a
reachable loopback port or a secret-looking variable stops the launch there. The model run is the
ticket's single native create, through the lifecycle owner's runner. The probe is offered to the
model as well, but only observed: a model may decline it, and that does not stop the run.

The result is data for the owner: the patch the attempt produced, its SHA-256 and the outcome of
an owner-side check in a separate copy under its own MXC profile. Git-dependent tests are deferred
until the owner's CR-8 decision. Nothing is applied to the repository. Once a ticket has
a native binding this launcher returns a result instead of raising, so the outcome reaches the
journal even when the run, capture or check failed.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
from types import MappingProxyType

from hermes_cli.plugin_request_binding import canonical_home
from hermes_dir_acl import make_user_dir
from hermes_platform.resolver import locate_command
from plugins.symphony_pm.contracts import LaunchExpired
from . import attempt_workspace as ws
from .codex_exec_check import capture_result, clean_artifacts as clean_artifacts
from .codex_exec_probe import PROBE, _loopback, _ProbeWatch, precheck, probe_ok as probe_ok
from .qualification import _open_denying_writers, environment_digest
from .recapture import LegacyArchive, archive_path, attempt_data, directory_identity, recapture
from .types import Refused, binding_from_json, finite, identifier, sha
from .windows_receiver import require_plain_environment

#: The plan's model and effort when both come from the qualified `config.toml`, not from flags.
CONFIG_SELECTED = 'codex-config'
ACTIVE = 'active'

PREAMBLE = r'''
You are working in a copy of the hermes-agent repository at a fixed commit, without .git. Network is disabled for
commands and your write access is limited to this directory and its temp folder.

STEP 1 (first, if you are willing): run this read-only boundary check of your own sandbox and do not modify it:
{probe}
Then continue.

STEP 2, the task below. The owner defers tests that call git until the CR-8 decision.
Keep the existing style (short docstrings that explain why, no
defensive clutter). Finish with a short summary of the change and the final test result.

--- task ---
{goal}
--- end of task ---
'''

#: What the client takes from the owner's environment. A name filter alone let a webhook URL through
#: in the first native run (`*_WEBHOOK`): a credential need not be named like one, so this is an allow-list.
CLIENT_ENVIRONMENT = frozenset((
    'ALLUSERSPROFILE', 'COMMONPROGRAMFILES', 'COMMONPROGRAMFILES(X86)', 'COMMONPROGRAMW6432', 'COMPUTERNAME',
    'COMSPEC', 'HOMEDRIVE', 'HOMEPATH', 'NUMBER_OF_PROCESSORS', 'OS', 'PATH', 'PATHEXT', 'PROCESSOR_ARCHITECTURE',
    'PROCESSOR_IDENTIFIER', 'PROCESSOR_LEVEL', 'PROCESSOR_REVISION', 'PROGRAMDATA', 'PROGRAMFILES',
    'PROGRAMFILES(X86)', 'PROGRAMW6432', 'PUBLIC', 'SYSTEMDRIVE', 'SYSTEMROOT', 'USERDOMAIN', 'USERNAME', 'WINDIR',
))


def client_environment(source, *, codex_home, client_home):
    """The client's explicit environment: allow-listed names from `source`, its own HOME and TEMP.

    Commands the sandbox runs may inherit from this, so nothing here may carry a credential; the
    client's own login lives in the file store of `codex_home`, which the profile denies.
    """
    environment = {k: v for k, v in source.items() if k.upper() in CLIENT_ENVIRONMENT}
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
class ClientJobCoverage:
    """Owning host's attestation of MXC membership in this client's Job on the measured stand."""
    host_id: str
    executable_sha256: str
    config_sha256: str
    permission_profile: str
    evidence_sha256: str


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
    job_coverage: ClientJobCoverage | None = None

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
    """Host paths and the owner-issued check plan. `checks(paths)` returns CheckCommand objects for MXC.

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
    legacy_archive: LegacyArchive | None = None
    owner_check: object = None

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

    def job_coverage_for(self, guard):
        q = self.qualification
        evidence = q.job_coverage
        return bool(type(evidence) is ClientJobCoverage and sha(evidence.evidence_sha256, 64)
                    and evidence.host_id and evidence.host_id == getattr(guard, 'recovery_host_id', None)
                    and (evidence.executable_sha256, evidence.config_sha256, evidence.permission_profile) ==
                        (q.executable_sha256, q.config_sha256, q.permission_profile))

    def validate_call(self, ticket, argv, cwd, timeout, stdin_text):
        with self.lock:
            if self.commands.get(ticket.attempt) != (ticket, tuple(argv), str(Path(cwd).resolve()), timeout, stdin_text):
                raise Refused('QUALIFIED_DRIVER_CALL_REQUIRED')

    def typed_plan(self):
        from .owner_check_profile import check_plan_fingerprint
        from .types import digest
        import tomllib
        q, s = self.qualification, self.settings
        config = (Path(q.codex_home)/'config.toml').read_bytes()
        if hashlib.sha256(config).hexdigest() != q.config_sha256:
            raise Refused('CODEX_CONFIG_CHANGED')
        settings = tomllib.loads(config.decode('utf-8'))
        if 'profile' in settings:
            profile = settings.get('profiles', {}).get(settings['profile'])
            if not isinstance(profile, dict):
                raise Refused('EXACT_EXECUTOR_SELECTION_REQUIRED')
            settings = dict(settings, **profile)
        selected = (settings.get('model'), settings.get('model_reasoning_effort'))
        if not all(isinstance(value, str) and value.strip() for value in selected):
            raise Refused('EXACT_EXECUTOR_SELECTION_REQUIRED')
        checks = tuple(s.checks(()))
        return dict(repository=str(Path(s.repository).resolve()), workspace=str(s.active/'src'),
                    model=CONFIG_SELECTED, effort=CONFIG_SELECTED, environment_ref=q.environment_ref,
                    configured_model=selected[0], configured_effort=selected[1],
                    executor_sha256=q.executable_sha256, config_sha256=q.config_sha256, qualification_sha256=digest(asdict(q)),
                    checks=[asdict(c) for c in checks], check_policy_sha256=check_plan_fingerprint(s.owner_check, checks),
                    exec_timeout=s.exec_timeout, precheck_timeout=s.precheck_timeout, check_timeout=s.check_timeout)

    def _typed_settings(self, ticket):
        s = self.settings
        if not getattr(ticket.plan, 'typed_details', ''):
            return s
        from .owner_check_profile import check_plan_fingerprint
        reviewed = json.loads(ticket.plan.typed_details)
        current = json.loads(json.dumps(self.typed_plan()))
        if any(reviewed.get(k) != v for k, v in current.items()):
            raise Refused('TYPED_EXECUTOR_CHANGED')
        def checks(paths):
            commands = tuple(s.checks(paths))
            if check_plan_fingerprint(s.owner_check, commands) != reviewed['check_policy_sha256']:
                raise Refused('TYPED_CHECK_PLAN_CHANGED')
            return commands
        return replace(s, checks=checks)

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
        except BaseException:
            # Before a create intent the workspace is ours alone. After that cut the journal
            # and a proven local stop govern recovery, including failures during capture.
            _, row = runner.owner.journal.read_launch(runner.token, ticket.project, ticket.job, ticket.revision)
            if row['state'] in ('LAUNCH_INTENT', 'NOT_STARTED'):
                self._settle(active, ticket.attempt)
            raise

    def _attempt(self, git, ticket, request, runner, active):
        q, s = self.qualification, self._typed_settings(ticket)
        data = attempt_data(q, ticket)
        make_user_dir(data, mode=0o700, parents=True, exist_ok=False)
        (data/'workspace-id.json').write_text(json.dumps(directory_identity(active)), encoding='utf-8')
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
            record['precheck'] = precheck(q, s, runner.owner.native, data, workspace, probe('precheck'))
            if record['precheck']['verdict'] != 'PROBE_OK':
                self._write(data, record)
                raise Refused('CODEX_PRECHECK_REFUSED')
            (active/'precheck-report.json').unlink()
            task = request.goal
            if getattr(ticket.plan, 'typed_details', ''):
                task = json.dumps({'goal': task, 'raw_constraints': list(request.raw_constraints)}, ensure_ascii=False)
            prompt = PREAMBLE.format(probe=subprocess.list2cmdline(probe('inexec')), goal=task)
            try:
                record.update(self._exec(ticket, runner, data, workspace, prompt))
            except LaunchExpired:
                raise
            except BaseException:
                runner.owner.finish_client_job(runner.token, ticket)
                raise
            else:
                stopped, causes = runner.owner.finish_client_job(runner.token, ticket)
        if 'PM_CANCEL' in causes and record['stop'] is None:
            record['stop'] = 'PM_CANCEL'
        if stopped:
            try:
                record.update(capture_result(s, git, capture, workspace, data, ticket.plan.source_sha,
                                             verify=record['stop'] is None and record['exit_code'] == 0))
            except Exception as exc:  # health: allow BLE001 -- capture failure is an observation after proven stop
                record['patch'] = {'refused': 'CAPTURE_FAILED:'+type(exc).__name__}
            record['workspace'] = self._settle(active, ticket.attempt)
        else:
            record['workspace'] = {'path': str(active), 'renamed': False}
        self._write(data, record)
        return record

    def _exec(self, ticket, runner, data, workspace, prompt):
        q, s = self.qualification, self.settings
        selection = ()
        if getattr(ticket.plan, 'typed_details', ''):
            reviewed = json.loads(ticket.plan.typed_details)
            selection = ('-c', 'model=' + json.dumps(reviewed['configured_model'], ensure_ascii=False),
                         '-c', 'model_reasoning_effort=' + json.dumps(reviewed['configured_effort'], ensure_ascii=False))
        argv = (q.executable, *s.command_prefix, 'exec', '--skip-git-repo-check', '--ephemeral', '--json',
                '--strict-config', '-o', str(data/'last-message.txt'), '-C', str(workspace.src),
                '-c', f'default_permissions="{q.permission_profile}"', *selection, '-')
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
        except LaunchExpired:
            raise
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

    def _settle(self, active, attempt):
        """Keep the attempt under its own name; while something still holds it, it stays `active`."""
        target = archive_path(self.settings.attempts_root, attempt)
        try:
            active.rename(target)
        except OSError:
            return {'path': str(active), 'renamed': False}
        return {'path': str(target), 'renamed': True}

    def workspace_clear(self):
        try:
            self.settings.active.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    def recapture(self, journal, token, ticket, row):
        return recapture(self, journal, token, ticket, row)

    def settle_workspace(self, ticket):
        """Recover a stopped owner's directory only if its original file identity still matches."""
        if self.workspace_clear():
            return True
        active = self.settings.active
        try:
            recorded = json.loads((attempt_data(self.qualification, ticket)/'workspace-id.json').read_text('utf-8'))
            if directory_identity(active) != recorded:
                return False
            return self._settle(active, ticket.attempt)['renamed']
        except (OSError, ValueError, Refused):
            return False

    @staticmethod
    def _write(data, record):
        (data/'owner-record.json').write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding='utf-8')
