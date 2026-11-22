"""A fixed program run by the same qualified Codex command sandbox as the approved work."""

from string import Template

from .linux_sandbox import GUEST_SCRATCH

BODY = Template(r'''
import json, os, socket, sys
challenge, targets = sys.argv[1], json.loads(sys.argv[2])

def denied(path, flags):
    try:
        fd = os.open(path, flags | os.O_NONBLOCK)
    except (PermissionError, FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return True
    except OSError as exc:
        import errno
        if exc.errno == errno.EROFS:
            return True
        raise
    os.close(fd)
    return False

def writable(path):
    with open(path, 'xb') as f:
        f.write(challenge.encode('ascii'))
    os.unlink(path)
    return True

checks = {key: denied(path, os.O_RDONLY) for key, path in targets['hidden'].items()}
checks.update(write_workspace=writable('/work/.symphony-probe-' + challenge),
              write_tmp=writable('$guest_scratch/.symphony-probe-' + challenge),
              deny_runtime_write=denied(sys.executable, os.O_WRONLY),
              deny_cgroup=denied('/sys/fs/cgroup/cgroup.procs', os.O_WRONLY))
with socket.socket() as sock:
    sock.settimeout(1)
    checks['deny_network'] = sock.connect_ex(('127.0.0.1', targets['port'])) != 0
with open('/proc/net/dev', encoding='ascii') as f:
    interfaces = sorted(line.split(':', 1)[0].strip() for line in f.read().splitlines()[2:])
''').substitute(guest_scratch=GUEST_SCRATCH)
REPORT = """{'challenge': challenge, 'checks': checks, 'environment': sorted(os.environ),
                  'network_namespace': os.readlink('/proc/self/ns/net'), 'interfaces': interfaces}"""
PROGRAM = BODY + 'print(json.dumps(' + REPORT + '))\n'
