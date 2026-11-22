"""Record the ordinary owner subprocess interface without running any patch code on the host."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from hermes_platform.resolver import locate_command
from plugins.symphony_pm.transport.owner_check_profile import CheckCommand, OwnerCheckProfile


def check_plan(_paths):
    return (CheckCommand('value', 'python', ('-c', 'import pkg.value as v; assert v.VALUE == 2')),)


@contextmanager
def owner_client(r, *, plan=check_plan, refusal=False):
    root = r.attempts.parent/'check-profile'
    for folder in ('codex', 'cwd', 'user/temp', 'user/appdata', 'user/local'):
        (root/folder).mkdir(parents=True)
    exe = Path(sys.executable).resolve()
    git = Path(locate_command('git').command[0]).resolve()
    py_hash = hashlib.sha256(exe.read_bytes()).hexdigest()
    q = OwnerCheckProfile(root, exe, exe, exe, git, py_hash, py_hash, py_hash,
                          hashlib.sha256(git.read_bytes()).hexdigest(), '')
    config = q.config_bytes()
    (q.home/'config.toml').write_bytes(config)
    q = replace(q, config_sha256=hashlib.sha256(config).hexdigest())
    r.launcher.settings = replace(r.launcher.settings, owner_check=q, checks=plan)
    recorded, run = [], subprocess.run

    def dispatch(argv, *args, **kwargs):
        argv = [str(arg) for arg in argv]
        recorded.append((argv, set(kwargs.get('env') or ())))
        if argv[:2] == [str(q.client), 'sandbox']:
            if refusal:
                raise PermissionError
            # The stand-in inspects data only. Native coverage executes real pytest and Ruff in MXC.
            value = (q.active/'src/pkg/value.py').read_bytes()
            return subprocess.CompletedProcess(argv, int(value != b'VALUE = 2\n'))
        return run(argv, *args, **kwargs)

    with patch.object(subprocess, 'run', dispatch):
        yield recorded, q
