"""The provider variant exercises the live relay and exact client-only paths from an actual Codex sandbox."""
import re

from .linux_carrier import HOME, PORT, SOCKET
from .linux_probe import BODY, REPORT
from .linux_sandbox import verify_report
from .types import Refused, digest

PROGRAM = BODY + f'client_home = {HOME!r}\nconnector = {SOCKET!r}\nrelay_port = {PORT}\n' + r'''
import errno, stat
with socket.socket() as client_socket:
    client_socket.settimeout(1)
    endpoint_denied = client_socket.connect_ex(('127.0.0.1', relay_port)) in (
        errno.ECONNREFUSED, errno.ENETUNREACH, errno.EACCES, errno.EPERM, errno.EHOSTUNREACH)
def closed(fd):
    try:
        os.fstat(fd)
    except OSError as exc:
        return exc.errno == errno.EBADF
    return False
with open('/proc/self/environ', 'rb') as source:
    initial = [entry.decode('utf-8').split('=', 1) for entry in source.read().split(b'\x00') if entry]
launch_env = dict(initial)
runtime_env = dict(os.environ)
if 'LC_CTYPE' not in launch_env and runtime_env.get('LC_CTYPE') in ('C.UTF-8', 'C.utf8'):
    del runtime_env['LC_CTYPE']
carrier = {'endpoint_denied': endpoint_denied,
           'home_denied': all(denied(p, os.O_RDONLY) for p in
                              (client_home, client_home+'/auth.json', client_home+'/config.toml')),
           'connector_hidden': not os.path.lexists(connector),
           'closed_fds': (all(closed(int(n)) for n in os.listdir('/proc/self/fd') if int(n) > 2)
                          and not any(stat.S_ISSOCK(os.fstat(n).st_mode) for n in (0,1,2))),
           'environment_exact': (len(initial) == len(launch_env) and runtime_env == launch_env
                                 and launch_env == json.loads(sys.argv[3]))}
''' + 'worker = ' + REPORT + "\nworker['environment'] = sorted(launch_env)\nprint(json.dumps({'worker': worker, 'carrier': carrier}))\n"


def verify(report, scope, binding, q, challenge, owner_net):
    if (type(scope) is not dict or set(scope) != {'unit', 'invocation', 'net'}
            or (scope['unit'], scope['invocation']) != (binding.unit, binding.invocation_id)
            or type(scope['net']) is not str or not re.fullmatch(r'net:\[[0-9]+\]', scope['net'])
            or scope['net'] == owner_net or type(report) is not dict or set(report) != {'worker', 'carrier'}):
        raise Refused('LINUX_PROVIDER_PROBE')
    worker = report['worker']
    verify_report(worker, q, challenge, owner_net)
    checks = report['carrier']
    if (worker['network_namespace'] == scope['net'] or type(checks) is not dict
            or set(checks) != {'endpoint_denied', 'home_denied', 'connector_hidden', 'closed_fds', 'environment_exact'}
            or any(v is not True for v in checks.values())):
        raise Refused('LINUX_PROVIDER_WORKER_EXPOSED')
    return digest([report, scope])
