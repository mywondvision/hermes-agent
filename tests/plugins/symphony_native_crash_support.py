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
from plugins.symphony_pm.transport.windows_api import Accounting, ExtendedLimit, WindowsApi
from plugins.symphony_pm.transport.windows_observer import WindowsQueryObserver
from plugins.symphony_pm.transport.windows_ownership import WindowsOwnershipGuard
from plugins.symphony_pm.transport.windows_receiver import WindowsNativeProvider
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


class BoundedApi(WindowsApi):
    def __init__(self, limit=2):
        super().__init__()
        self.limit = limit

    def create_job(self, name):
        job = super().create_job(name)
        try:
            limits = ExtendedLimit()
            limits.Basic.Flags, limits.Basic.ActiveLimit = 0x2000 | 0x8, self.limit
            self.checked(self.fn('SetInformationJobObject',
                [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL)(job, 9, C.byref(limits), C.sizeof(limits)))
            return job
        except BaseException:
            self.close(job)
            raise


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
    def __init__(self, home):
        self.scope = SimpleNamespace(profile_home=canonical_home(home), namespace='symphony_pm:ingress', active=True)
        self.now = [time.time()]
        self.marker = Path(home)/'worker-ready'
        capture = Path(home)/'capture'
        make_user_dir(capture, mode=0o700, parents=True, exist_ok=True)
        self.argv = [base_python(), '-I', '-B', '-c',
            'from pathlib import Path; import sys,threading; '
            'Path(sys.argv[1]).write_text("ready",encoding="utf-8"); threading.Event().wait(90)',
            str(self.marker)]
        self.observer = WindowsQueryObserver(self.scope)
        self.native = WindowsNativeProvider(validate_call=self.validate,
            environment={k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR'}},
            command_result=lambda argv, code, **kw: SimpleNamespace(argv=argv, returncode=code, **kw),
            sanitize=lambda text: text, private_directory=capture)
        self.native.api = BoundedApi(2)
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

    def launch(self, ticket, request, runner):
        result = runner(self.argv, cwd=ticket.plan.workspace, timeout=30, stdin_text='')
        return {'returncode': result.returncode, 'timed_out': result.timed_out}

    def start(self, pool):
        request, context, _ = approve(self)
        return pool.submit(self.owner.submit, request, context)

    def receiver(self):
        return next(iter(self.owner._receivers.values()))

    def ready(self):
        wait_until(self.marker.exists, timeout=15, what='own synthetic worker marker')
        receiver = self.receiver()
        self.roster = capture_members(self.native.api, receiver.job, active_count=2,
            required_pids=(receiver.binding.root.pid,), handles=self.query_handles,
            name='worker-'+receiver.ticket.attempt)
        if self.roster['total'] != 2:
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
                save_proof('worker-terminal-'+self.ticket.attempt, {'terminal': records, 'errors': failures})
        if failures:
            raise Refused('TEST_RIG_CLEANUP_UNKNOWN')


class CutApi(BoundedApi):
    def __init__(self, rig, cut, receipt):
        super().__init__()
        self.rig, self.cut, self.receipt = rig, cut, receipt
        self.job_name = None

    def create_job(self, name):
        self.job_name = name
        return super().create_job(name)

    def pause(self, process=None):
        if process is not None:
            from ctypes import wintypes as W
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

    def create_suspended(self, argv, cwd, env, streams, job):
        created = super().create_suspended(argv, cwd, env, streams, job)
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
    if cut not in ('after_create', 'before_resume', 'after_effect'):
        raise Refused('UNKNOWN_TEST_CUT')
    rig = NativeRig(home)
    rig.native.api = CutApi(rig, cut, Path(receipt))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = rig.start(pool)
            if cut == 'after_effect':
                receiver = rig.ready()
                rig.owner.cancel_internal(receiver.binding, 'PM_CANCEL')
            future.result(timeout=45)
    finally:
        rig.close(time.monotonic()+10)


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit('Expected test-owned home, cut and receipt')
    child_main(*sys.argv[1:])
