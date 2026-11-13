"""Test-only Windows crash helper. No service registration, model, or retry loop.

Running this file as a child requires an explicitly approved native campaign.
"""
from concurrent.futures import ThreadPoolExecutor
import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

# Isolated child Python intentionally has no ambient project import path.
if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli.plugin_request_binding import canonical_home
from hermes_dir_acl import make_user_dir
from hermes_platform.resolver import locate_command
from plugins.symphony_pm.transport.lifecycle import LifecycleOwner
from plugins.symphony_pm.transport.types import NativeBinding, ProcessIdentity, Refused
from plugins.symphony_pm.transport.windows_api import Accounting, WindowsApi
from plugins.symphony_pm.transport.windows_observer import WindowsQueryObserver
from plugins.symphony_pm.transport.windows_ownership import WindowsOwnershipGuard
from plugins.symphony_pm.transport.windows_receiver import WindowsNativeProvider
from plugins.symphony_pm.transport.attempt_workspace import grant, user_sid
from plugins.symphony_pm.transport.windows_read_boundary import FILE, DenyEntries, _split
from plugins.symphony_pm.transport.windows_token import attempt_sid
from tests.plugins.symphony_serving_support import approve
from tests.waiting import wait_until


def base_python():
    """Reuse the current interpreter's physical base, never a venv redirector."""
    raw = getattr(sys, '_base_executable', None)
    if not raw:
        raise Refused('EXISTING_BASE_INTERPRETER_REQUIRED')
    resolution = locate_command(str(Path(raw).resolve()))
    if not resolution.command:
        raise Refused('EXISTING_BASE_INTERPRETER_REQUIRED')
    return resolution.command[0]


def helper_command(script):
    # Same interpreter/dependencies as the approved runner, without another
    # process for the venv redirector. No install, probe or changed identity rule.
    bootstrap = ('import runpy,sys;sys.path[:]=' + repr(list(sys.path))
                 + ';script=sys.argv[1];sys.argv=sys.argv[1:];'
                 + 'runpy.run_path(script,run_name="__main__")')
    return [base_python(), '-I', '-B', '-c', bootstrap, str(script)]


def save_proof(name, body):
    destination = os.environ.get('HERMES_E2E_ARTIFACTS')
    if not destination:
        raise Refused('DURABLE_NATIVE_PROOF_PATH_REQUIRED')
    directory = Path(destination)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/(name+'.json')
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(body, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def allow_low_writes(directory):
    """Label one test-owned directory low so the low-integrity worker can write its own copy.

    A user may lower the label of an object they own without any privilege; nothing outside
    `directory` and no system setting changes.
    """
    api = WindowsApi()
    descriptor, present, sacl, defaulted = C.c_void_p(), W.BOOL(), C.c_void_p(), W.BOOL()
    api.checked(api.adv('ConvertStringSecurityDescriptorToSecurityDescriptorW',
        [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p], W.BOOL)(
        'S:(ML;OICI;NW;;;LW)', 1, C.byref(descriptor), None))
    try:
        api.checked(api.adv('GetSecurityDescriptorSacl',
            [C.c_void_p, C.POINTER(W.BOOL), C.POINTER(C.c_void_p), C.POINTER(W.BOOL)], W.BOOL)(
            descriptor, C.byref(present), C.byref(sacl), C.byref(defaulted)))
        code = api.adv('SetNamedSecurityInfoW', [W.LPCWSTR, C.c_int, W.DWORD] + [C.c_void_p]*4, W.DWORD)(
            str(directory), 1, 0x10, None, None, None, sacl)  # SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION
        if code:
            raise C.WinError(code)
    finally:
        api.fn('LocalFree', [C.c_void_p], C.c_void_p)(descriptor)


def current_user_sid():
    return user_sid(WindowsApi())


_TRAVERSE = 0x1000A0  # FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE


def admit_attempt(path, attempt, *, write=False, inherit=False):
    """Let one attempt's worker into one test-owned path: read and traverse, or modify with `write`.

    A restricted token is checked twice and must pass both times. A test directory admits its
    owner only through the owner-rights entry, and the kernel does not treat a restricted token as
    the owner, so the ordinary check needs the user's SID spelled out; the second check needs the
    attempt's SID. The worker token holds no privilege, so traversal is checked too: every test
    directory between the run's TEMP root and `path` gets a traverse-only entry for both SIDs.
    Only DACLs of test-owned directories under that root change; production has no such directory.
    """
    sids = (attempt_sid(attempt), current_user_sid())
    admit_ancestors([path], sids)
    for sid in sids:
        admit(path, sid, write=write, inherit=inherit)


_SHARED_ALIASES = {'BU', 'AU', 'WD', 'RC', 'S-1-5-32-545', 'S-1-5-11', 'S-1-1-0', 'S-1-5-12'}


def _shared_traverse(sddl):
    """Whether a directory already admits one of the worker's shared groups (so it is not test-private)."""
    return any(ace.split(';')[0] == 'A' and 'IO' not in ace.split(';')[1] and ace.split(';')[5] in _SHARED_ALIASES
               for ace in _split(sddl)[1])


def admit_ancestors(paths, sids):
    """Traverse-only entries for `sids` on the test-private directories above `paths`.

    Walking up from each target, every directory that admits none of the worker's shared groups
    (a pytest directory made owner-only) gets the entry; the walk stops at the first directory that
    admits them, which is where test-owned space ends, and never goes past TEMP. A child serving
    process has its own TEMP, so TEMP alone cannot bound the walk. Each entry is written to that
    one directory without propagation, once per (directory, SID).
    """
    raw = os.environ.get('TEMP')
    root, api, ancestors = Path(raw).resolve() if raw else None, WindowsApi(), {}
    for path in paths:
        for ancestor in Path(path).resolve().parents:
            if ancestor == root or ancestor.parent == ancestor:
                break
            if ancestor not in ancestors:
                ancestors[ancestor] = api.object_sddl(FILE, ancestor)
            if _shared_traverse(ancestors[ancestor]):
                break
    for ancestor, current in sorted(ancestors.items()):
        if _shared_traverse(current):
            continue
        updated = current
        for sid in sids:
            updated = with_allow(updated, sid, _TRAVERSE)
        if updated != current:
            api.write_dacl(FILE, ancestor, updated)


def with_allow(sddl, sid, mask):
    """The DACL with one explicit allow entry added after the explicit denies (canonical order)."""
    head, aces, tail = _split(sddl)
    entry = f'A;;0x{mask:x};;;{sid}'
    if entry.upper() in (ace.upper() for ace in aces):
        return sddl
    at = 0
    while at < len(aces) and aces[at].startswith('D;') and 'ID' not in aces[at].split(';')[1]:
        at += 1
    return head+''.join(f'({ace})' for ace in [*aces[:at], entry, *aces[at:]])+tail


def admit(path, sid, *, write=False, inherit=False):
    grant(WindowsApi(), path, sid, write=write, inherit=inherit)


def decoy_protected(directory):
    """One test-owned object every worker of this rig must not read: what `protected` lists.

    It carries the worker-class deny entry; the journal sits next to it and is the test's only record.
    """
    decoy = Path(directory)/'protected-decoy'
    decoy.mkdir(parents=True, exist_ok=True)
    DenyEntries(WindowsApi(), Path(directory)/'protected-decoy.journal.json').apply([(FILE, str(decoy))])
    return ((FILE, str(decoy)),)


def owner_environment(tmp_path, home, proofs):
    """The explicit environment of a test-owned serving process; nothing ambient but the system root."""
    environment = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR'}}
    child_home, child_temp = tmp_path/'child-home', tmp_path/'child-temp'
    for directory in (child_home, child_temp, child_home/'appdata', child_home/'local', child_home/'runtime'):
        directory.mkdir(parents=True, exist_ok=True)
    environment.update(HOME=str(child_home), USERPROFILE=str(child_home), HERMES_HOME=str(home),
        APPDATA=str(child_home/'appdata'), LOCALAPPDATA=str(child_home/'local'),
        TEMP=str(child_temp), TMP=str(child_temp), HERMES_RUNTIME_DIR=str(child_home/'runtime'),
        HERMES_DISABLE_LAZY_INSTALLS='1', UV_OFFLINE='1', PIP_CONFIG_FILE=os.devnull,
        PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', HERMES_E2E_ARTIFACTS=str(proofs))
    return environment


WAITING_WORKER = ('from pathlib import Path; import sys,threading; '
    'Path(sys.argv[1]).write_text("ready",encoding="utf-8"); threading.Event().wait(90)')
WORKER_WITH_DESCENDANT = ('from pathlib import Path; import subprocess,sys,threading; '
    'child=subprocess.Popen([sys.executable,"-I","-B","-c","import threading; threading.Event().wait(90)"]); '
    'Path(sys.argv[1]).write_text(str(child.pid),encoding="utf-8"); threading.Event().wait(90)')


class BoundedApi(WindowsApi):
    """The production Job with a test-sized process limit, plus the one thing production refuses:
    a test-owned owner process (token None) is created with this test's own token."""

    def __init__(self, limit=2):
        super().__init__()
        self.process_limit = limit

    def _spawn(self, token, signature, arguments):
        if token is None:
            self.checked(self.fn('CreateProcessW', signature, W.BOOL)(*arguments))
        else:
            super()._spawn(token, signature, arguments)


def capture_members(api, job, *, active_count, required_pids, handles, name):
    """Keep every observed exact member; image/name never grants or excludes control."""
    last = {}

    def complete():
        active, pids = api.snapshot(job)
        account = Accounting()
        api.checked(api.fn('QueryInformationJobObject',
            [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL)(
            job, 1, C.byref(account), C.sizeof(account), None))
        last.update(active=active, total=account.Total, pids=list(pids), members=[])
        return active == active_count and len(pids) == active_count and set(required_pids).issubset(pids)

    try:
        wait_until(complete, timeout=10, what='complete measured Job roster')
        if last['total'] > 4:
            raise Refused('CUMULATIVE_JOB_BUDGET_EXCEEDED')
        for pid in last['pids']:
            handle = api.open_process(pid)
            try:
                if not api.in_job(handle, job):
                    raise Refused('QUERY_MEMBER_CHANGED')
                identity = ProcessIdentity(pid, api.creation(handle))
                if identity not in handles:
                    handles[identity], handle = handle, None
                last['members'].append(asdict(identity))
            finally:
                if handle is not None:
                    api.close(handle)
        return last
    finally:
        save_proof(name, last)  # raw mismatch survives assertion/finally/temp cleanup


def close_queries(api, handles):
    records, failures = [], []
    for identity, handle in tuple(handles.items()):
        try:
            signaled = api.fn('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)(handle, 10000) == 0
            records.append({**asdict(identity), 'signaled': signaled})
            if not signaled:
                failures.append('QUIESCENCE_UNKNOWN')
        except (OSError, Refused) as exc:
            failures.append(type(exc).__name__)
        finally:
            try:
                api.close(handle)
            except OSError as exc:
                failures.append(type(exc).__name__)
    handles.clear()
    return records, failures


class NativeRig:
    """Actual providers, explicit synthetic command; no CodexDriver qualification claim."""
    def __init__(self, home, *, descendant=False):
        self.scope = SimpleNamespace(profile_home=canonical_home(home), namespace='symphony_pm:ingress', active=True)
        self.now = [time.time()]
        # The worker is low-integrity: the only place it can write is this one labelled directory.
        # `validate` admits the attempt's SID to it, to the current directory and to `readable`.
        self.readable = []
        self.work = Path(home)/'worker-copy'
        self.work.mkdir(parents=True, exist_ok=True)
        allow_low_writes(self.work)
        self.marker = self.work/'worker-ready'
        self.members = 3 if descendant else 2  # root + console host (+ one descendant)
        capture = Path(home)/'capture'
        make_user_dir(capture, mode=0o700, parents=True, exist_ok=True)
        self.argv = [base_python(), '-I', '-B', '-c',
                     WORKER_WITH_DESCENDANT if descendant else WAITING_WORKER, str(self.marker)]
        self.observer = WindowsQueryObserver(self.scope)
        self.native = WindowsNativeProvider(validate_call=self.validate,
            environment={k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR'}},
            protected=decoy_protected(home),
            command_result=lambda argv, code, **kw: SimpleNamespace(argv=argv, returncode=code, **kw),
            sanitize=lambda text: text, private_directory=capture,
            data_directory=Path(home)/'plugin-data'/'symphony_pm')
        self.native.api = BoundedApi(self.members)
        self.query_handles, self.roster, self.closed = {}, None, False
        self.creates = 0
        create = self.native.create_suspended

        def counted(*args):
            self.creates += 1
            return create(*args)
        self.native.create_suspended = counted
        self.guard = WindowsOwnershipGuard(self.scope)
        try:
            self.owner = LifecycleOwner(self.scope, self.guard, self.observer, self.native, self.launch,
                scheduler_id='serving-test', controller='controller-1')
        except BaseException:
            try:
                self.observer.close(0)
            finally:
                self.guard.close(0)
            raise

    def validate(self, ticket, argv, cwd, timeout, stdin_text):
        if (argv != tuple(self.argv) or cwd != ticket.plan.workspace or timeout != 30 or stdin_text != ''):
            raise Refused('SYNTHETIC_COMMAND_BINDING')
        self.ticket = ticket
        admit_attempt(cwd, ticket.attempt)
        admit_attempt(self.work, ticket.attempt, write=True, inherit=True)
        for path in self.readable:
            admit_attempt(path, ticket.attempt, inherit=True)

    def launch(self, ticket, request, runner):
        result = runner(self.argv, cwd=ticket.plan.workspace, timeout=30, stdin_text='')
        self.outcome = {'returncode': result.returncode, 'timed_out': result.timed_out}
        self.stderr = result.stderr[-2000:]  # a worker that could not start says why only here
        return self.outcome

    def start(self, pool):
        request, context, _ = approve(self)
        return pool.submit(self.owner.submit, request, context)

    def receiver(self):
        return next(iter(self.owner._receivers.values()))

    def ready(self):
        wait_until(self.marker.exists, timeout=15, what='own synthetic worker marker')
        receiver = self.receiver()
        self.roster = capture_members(self.native.api, receiver.job, active_count=self.members,
            required_pids=(receiver.binding.root.pid,), handles=self.query_handles,
            name='worker-'+receiver.ticket.attempt)
        if self.roster['total'] != self.members:
            raise Refused('WORKER_TOTAL_PROCESS_BUDGET')
        return receiver

    def close(self, deadline):
        if self.closed:
            return
        self.closed = True
        failures = []
        try:
            self.owner.close(deadline)
        except Exception as exc:  # health: allow BLE001 -- keep bounded terminal proof on owner cleanup failure
            failures.append(type(exc).__name__+': '+str(exc))
        finally:
            records, errors = close_queries(self.native.api, self.query_handles)
            failures.extend(errors)
            if hasattr(self, 'ticket'):
                save_proof('worker-terminal-'+self.ticket.attempt, {'terminal': records, 'errors': failures,
                    'outcome': getattr(self, 'outcome', None), 'stderr': getattr(self, 'stderr', None)})
        if failures:
            raise Refused('TEST_RIG_CLEANUP_UNKNOWN')


class CutApi(BoundedApi):
    def __init__(self, rig, cut, receipt):
        super().__init__(rig.members)
        self.rig, self.cut, self.receipt = rig, cut, receipt
        self.job_name = None

    def create_job(self, job_id):
        self.job_name = job_id
        return super().create_job(job_id)

    def pause(self, process=None):
        if process is not None:
            pid = self.fn('GetProcessId', [W.HANDLE], W.DWORD)(process)
            root = ProcessIdentity(pid, self.creation(process))
            binding = NativeBinding(self.rig.ticket, self.job_name, root, (root,))
        else:
            binding = self.rig.receiver().binding
        body = {'cut': self.cut, 'serving': asdict(self.rig.guard.identity), 'binding': asdict(binding),
                'worker_roster': self.rig.roster}
        # Parent sees only complete, flushed identity before opening query handles.
        temporary = self.receipt.with_suffix('.tmp')
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(body, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.receipt)
        release = self.receipt.with_suffix('.release')
        wait_until(release.exists, timeout=30, what='parent crash barrier release')
        if release.read_text(encoding='utf-8').strip() != 'CUT':
            raise Refused('PARENT_CUT_BARRIER_LOST')
        os._exit(71)  # deliberate serving crash; no Python cleanup or cancel ACK

    def create_suspended(self, argv, cwd, env, streams, job, token):
        created = super().create_suspended(argv, cwd, env, streams, job, token)
        if self.cut == 'after_create':
            self.pause(created[0])  # root exists inside its Job; binding is not yet durable
        return created

    def resume(self, thread):
        if self.cut == 'before_resume':
            self.pause()
        return super().resume(thread)

    def terminate(self, job):
        result = super().terminate(job)
        if self.cut == 'after_effect':
            self.pause()
        return result


def child_main(home, cut, receipt):
    if cut not in ('after_create', 'before_resume', 'after_effect', 'during_observer_read'):
        raise Refused('UNKNOWN_TEST_CUT')
    rig = NativeRig(home, descendant=cut == 'during_observer_read')
    rig.native.api = CutApi(rig, cut, Path(receipt))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = rig.start(pool)
            if cut == 'after_effect':
                receiver = rig.ready()
                rig.owner.cancel_internal(receiver.binding, 'PM_CANCEL')
            if cut == 'during_observer_read':
                receiver = rig.ready()
                # The crash lands inside the observer's query, while its Job duplicate is open.
                rig.observer.api.snapshot = lambda job: rig.native.api.pause()
                rig.owner.status(receiver.binding)
            future.result(timeout=45)
    finally:
        rig.close(time.monotonic()+10)


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit('Expected test-owned home, cut and receipt')
    child_main(*sys.argv[1:])
