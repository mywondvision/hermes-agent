"""Fixed qualification precheck executed as Python -I -B -c inside the test stand."""
import errno
import json
import os
import socket
import sys


def denied(path, flags):
    try:
        fd = os.open(path, flags | os.O_NONBLOCK)
    except (PermissionError, FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return True
    except OSError as exc:
        if exc.errno == errno.EROFS:
            return True
        raise
    os.close(fd)
    return False


def writable(path, challenge):
    with open(path, "xb") as stream:
        stream.write(challenge.encode("ascii"))
    os.unlink(path)
    return True


def network_denied(port):
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        print("socket", exc.errno, file=sys.stderr)
        return exc.errno == errno.EPERM
    with client:
        client.settimeout(1)
        try:
            client.connect(("127.0.0.1", port))
        except OSError as exc:
            print("connect", exc.errno, file=sys.stderr)
            return exc.errno == errno.EPERM
    print("connected", file=sys.stderr)
    return False


def report(challenge, targets):
    checks = {key: denied(path, os.O_RDONLY) for key, path in targets["hidden"].items()}
    checks.update(
        write_workspace=writable("/work/.symphony-probe-" + challenge, challenge),
        write_tmp=writable("/tmp/.symphony-probe-" + challenge, challenge),
        deny_runtime_write=denied(sys.executable, os.O_WRONLY),
        deny_cgroup=denied("/sys/fs/cgroup/cgroup.procs", os.O_WRONLY),
        deny_network=network_denied(targets["port"]),
    )
    with open("/proc/net/dev", encoding="ascii") as stream:
        interfaces = sorted(line.split(":", 1)[0].strip() for line in stream.read().splitlines()[2:])
    return {"challenge": challenge, "checks": checks, "environment": sorted(os.environ),
            "network_namespace": os.readlink("/proc/self/ns/net"), "interfaces": interfaces}


if __name__ == "__main__":
    print(json.dumps(report(sys.argv[1], json.loads(sys.argv[2]))))
