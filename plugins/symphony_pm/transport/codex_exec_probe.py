"""MXC probe program, precheck and observation of the in-exec report."""
from contextlib import contextmanager
import json
import socket
import threading
from hermes_constants import get_hermes_home
from .run_supervisor import profile_thread


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
    return {'names': names, 'name_count': len(os.environ)}
def connect():
    with socket.create_connection(('127.0.0.1', int(port)), timeout=5):
        pass
step('env', env)
step('read-auth', lambda: opened(auth))
step('list-codex-home', lambda: {'count': len(os.listdir(home))})
step('read-outside', lambda: opened(Path(outside)/'canary.txt'))
step('write-outside', lambda: opened(Path(outside)/'probe-write.txt', 'ab'))
step('loopback', connect)
destination = Path(report)
pending = destination.with_suffix(f'.{os.getpid()}.tmp')
pending.write_text(json.dumps(out), encoding='utf-8')
pending.replace(destination)
print('PROBE_DONE', mode)
'''

_DENIED = ('read-auth', 'list-codex-home', 'read-outside', 'write-outside', 'loopback')
# Qualified Windows/client fields, the Symphony profile's Python settings and Codex's network flag.
# This policy is established before running the probe. Never extend it from an observed report.
_COMMAND_NAMES = frozenset("""
    ALLUSERSPROFILE APPDATA CODEX_HOME CODEX_SANDBOX_NETWORK_DISABLED COMMONPROGRAMFILES
    COMMONPROGRAMFILES(X86) COMMONPROGRAMW6432 COMPUTERNAME COMSPEC HOME HOMEDRIVE HOMEPATH LOCALAPPDATA
    NUMBER_OF_PROCESSORS OS PATH PATHEXT PROCESSOR_ARCHITECTURE PROCESSOR_IDENTIFIER PROCESSOR_LEVEL
    PROCESSOR_REVISION PROGRAMDATA PROGRAMFILES PROGRAMFILES(X86) PROGRAMW6432 PUBLIC PYTHONDONTWRITEBYTECODE
    PYTHONPATH PYTHONUTF8 SYSTEMDRIVE SYSTEMROOT TEMP TMP USERDOMAIN USERNAME USERPROFILE WINDIR
""".split())
_REQUIRED_NAMES = frozenset(('SYSTEMROOT', 'SYSTEMDRIVE', 'TEMP', 'TMP'))


def _environment_ok(step):
    names = step.get('names')
    if (set(step) != {'name', 'ok', 'names', 'name_count'} or step['ok'] is not True
            or type(names) is not list or type(step['name_count']) is not int or step['name_count'] != len(names)
            or not all(type(name) is str and name.isascii() for name in names)):
        return False
    canonical = {name.upper() for name in names}
    return len(canonical) == len(names) and _REQUIRED_NAMES <= canonical <= _COMMAND_NAMES


def _access_denied(step):
    errno, winerror = step.get('errno'), step.get('winerror')
    if (not step.keys() <= {'name', 'ok', 'errno', 'winerror'} or step['ok'] is not False
            or (errno is not None and type(errno) is not int)):
        return False
    if winerror is None:
        return errno in (1, 13)
    # Windows maps sharing violations and other failures onto the same POSIX errno.
    allowed = (5, 10013) if step['name'] == 'loopback' else (5,)
    return type(winerror) is int and winerror in allowed


def probe_ok(report):
    """Require one complete environment inventory and an access denial at each boundary."""
    if type(report) is not dict or set(report) != {'mode', 'steps'} or report['mode'] not in ('precheck', 'inexec'):
        return False
    records = report.get('steps')
    if type(records) is not list or len(records) != len(_DENIED) + 1:
        return False
    if not all(type(s) is dict and type(s.get('name')) is str and type(s.get('ok')) is bool for s in records):
        return False
    steps = {s['name']: s for s in records}
    return (steps.keys() == {'env', *_DENIED} and _environment_ok(steps['env'])
            and all(_access_denied(steps[name]) for name in _DENIED))


def _unique_fields(pairs):
    record = dict(pairs)
    if len(record) != len(pairs):
        raise ValueError('duplicate probe field')
    return record


def _read_report(path, mode):
    try:
        # The qualified inventory fits in a few KiB. Bound reads of a worker-writable file on the owner.
        with path.open('rb') as stream:
            encoded = stream.read(65537)
        if len(encoded) > 65536:
            return 'PROBE_VIOLATION', None
        report = json.loads(encoded.decode('utf-8'), object_pairs_hook=_unique_fields)
    except FileNotFoundError:
        return 'PROBE_NOT_RUN', None
    except (OSError, ValueError, RecursionError):
        return 'PROBE_VIOLATION', None
    if not probe_ok(report) or report['mode'] != mode:
        return 'PROBE_VIOLATION', None
    return 'PROBE_OK', report


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
        self.thread = profile_thread(get_hermes_home(), self._run, 'symphony-probe-watch')

    def _judge(self, running=True):
        verdict, _ = _read_report(self.report, 'inexec')
        if verdict == 'PROBE_NOT_RUN':
            return False
        self.verdict = verdict
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


def precheck(q, s, native, data, workspace, command):
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
    verdict, data_report = _read_report(workspace.path/'precheck-report.json', 'precheck')
    if timed_out is not False or type(exit_code) is not int or exit_code != 0:
        verdict = 'PROBE_VIOLATION'
    return {'exit_code': exit_code, 'timed_out': timed_out, 'verdict': verdict, 'report': data_report}
