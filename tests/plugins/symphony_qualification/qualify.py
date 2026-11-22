"""Fixed workloads from the isolated Linux qualification image."""
import os
import sys
import time
from pathlib import Path


def descendants():
    # All three processes overlap for independent PID/start/cgroup observation.
    child = os.fork()
    if child == 0:
        grandchild = os.fork()
        if grandchild == 0:
            os.setsid()
            print("grandchild", os.getpid(), flush=True)
            time.sleep(18)
            return
        print("child", os.getpid(), flush=True)
        time.sleep(8)
        return
    print("root", os.getpid(), flush=True)
    time.sleep(4)


def main(arguments):
    if dict(os.environ) != {"CODEX_SANDBOX_NETWORK_DISABLED": "1"}:
        raise SystemExit("inner environment mismatch")
    if arguments == ["write"]:
        with Path("/work/symphony-qualification.txt").open("xb") as stream:
            stream.write(b"symphony\n")
    elif arguments == ["descendants"]:
        descendants()
    elif arguments == ["wait"]:
        time.sleep(90)
    else:
        raise SystemExit("unknown qualification case")


if __name__ == "__main__":
    main(sys.argv[1:])
