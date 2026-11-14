"""One no-model probe of Codex's own Windows sandbox (MXC) on test-owned decoys. Requires an explicit native grant.

`codex sandbox` runs a Python probe under an explicit `windows.sandbox=mxc` with a temporary CODEX_HOME made
here; the existing Codex homes are neither used nor changed. The profile denies the whole disk, reads the
platform minimum, the probe's interpreter and one open decoy, writes only its own attempt directory, and has
no command network. The Codex client runs in a kill-on-close Job of this test with an explicit environment
that carries decoy secrets.

Every observation is recorded first and asserted after the proof is saved. Passing proves only the command
sandbox boundary on decoys on this PC, not MCP, web search, connectors or other client capabilities, and not
isolation from other processes of the same user.
"""
from concurrent.futures import ThreadPoolExecutor
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import uuid
import winreg

import pytest

from plugins.symphony_pm.transport.windows_read_boundary import FILE
from plugins.symphony_pm.transport.windows_receiver import _SECRET_NAME
from tests.plugins.symphony_native_crash_support import BoundedApi, base_python, current_user_sid, save_proof
from tests.plugins.test_symphony_pm_windows_attempt_workspace import (
    DISK_LIKE, _by_id_owner, _decoy, _key, _owner_opens, _owner_reads_key, _primary_fingerprint,
)
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')

CODEX = Path(r'D:\Codex\npm-global\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64'
             r'\vendor\x86_64-pc-windows-msvc\bin\codex.exe')
CODEX_HOMES = (Path(r'D:\Codex\codex-home'), Path(r'C:\OpenAI\CodexHome'))
DECOY_NAMES = ('OPENAI_API_KEY', 'SYMPHONY_DECOY_TOKEN')

PROBE = r'''
import ctypes as C, hashlib, json, os, socket, subprocess, sys, time, winreg
from ctypes import wintypes as W
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
k = C.WinDLL('kernel32', use_last_error=True)
out = {'steps': []}
def step(name, action):
    record = {'seq': len(out['steps'])+1, 'name': name}
    try:
        record.update(action() or {})
        record.setdefault('ok', True)
    except OSError as exc:
        record.update(ok=False, winerror=getattr(exc, 'winerror', None), errno=exc.errno)
    except Exception as exc:
        record.update(ok=False, exception=type(exc).__name__)
    out['steps'].append(record)
def opened(path, mode):
    with open(path, mode):
        pass
class Id(C.Structure):
    _fields_ = [('size', W.DWORD), ('kind', C.c_int), ('id', C.c_ubyte*16)]
def by_id(file_id):
    k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    k.CreateFileW.restype = W.HANDLE
    hint = k.CreateFileW(plan['attempt'], 0x80, 7, None, 3, 0x02000000, None)
    if hint in (None, -1, 0xFFFFFFFFFFFFFFFF):
        raise C.WinError(C.get_last_error())
    try:
        k.OpenFileById.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, W.DWORD, C.c_void_p, W.DWORD]
        k.OpenFileById.restype = W.HANDLE
        handle = k.OpenFileById(hint, C.byref(Id(C.sizeof(Id), 2, (C.c_ubyte*16)(*int(file_id).to_bytes(16, 'little')))),
                                0x80000000, 7, None, 0)
        if handle in (None, -1, 0xFFFFFFFFFFFFFFFF):
            raise C.WinError(C.get_last_error())
        k.CloseHandle(handle)
    finally:
        k.CloseHandle(hint)
def hard_link(link, target):
    k.CreateHardLinkW.argtypes = [W.LPCWSTR, W.LPCWSTR, C.c_void_p]
    if not k.CreateHardLinkW(link, target, None):
        raise C.WinError(C.get_last_error())
def connect(host, port):
    with socket.create_connection((host, port), timeout=5):
        pass
attempt = Path(plan['attempt'])
step('e1-env', lambda: {'names': sorted(os.environ), 'decoys_visible': [n for n in plan['decoy_names'] if n in os.environ]})
step('p1-modify', lambda: (attempt/'src'/'file.txt').open('ab').write(b'probe\n') and None)
step('p1-create', lambda: (attempt/'src'/'new.txt').write_bytes(b'new\n') and None)
step('p1-delete', lambda: (attempt/'src'/'gone.txt').unlink())
def child():
    done = subprocess.run([sys.executable, '-I', '-B', '-c', "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'child'); print('child-ok')",
                           str(attempt/'src'/'child.txt')], capture_output=True, timeout=60)
    return {'ok': done.returncode == 0 and b'child-ok' in done.stdout, 'rc': done.returncode}
step('p1-child', child)
for name, path in plan['write_denied'].items():
    step('n1-'+name, lambda path=path: opened(path, 'ab'))
step('n2-read-closed', lambda: opened(plan['closed_canary'], 'rb'))
step('n2-list-closed', lambda: {'count': len(os.listdir(plan['closed_root']))})
step('n2-read-open', lambda: {'sha256': hashlib.sha256(Path(plan['open_canary']).read_bytes()).hexdigest()})
step('n3-id-closed', lambda: by_id(plan['closed_id']))
step('n3-id-open', lambda: by_id(plan['open_id']))
step('n4-link-closed', lambda: hard_link(str(attempt/'hl-closed'), plan['closed_canary']))
def key():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, plan['key'], 0, winreg.KEY_READ):
        pass
step('r1-key', key)
step('l1-loopback', lambda: connect('127.0.0.1', plan['port']))
step('l1-external', lambda: connect('1.1.1.1', 443))
def linger():
    process = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'import time; time.sleep(120)'])
    return {'pid': process.pid}
step('s1-child', linger)
report = attempt/'report.json'
report.with_suffix('.tmp').write_text(json.dumps(out), encoding='utf-8')
report.with_suffix('.tmp').replace(report)
time.sleep(120)
'''

EXPECTED = ['e1-env', 'p1-modify', 'p1-create', 'p1-delete', 'p1-child', 'n1-primary_file', 'n1-primary_git_config',
            'n1-neighbour_file', 'n2-read-closed', 'n2-list-closed', 'n2-read-open', 'n3-id-closed', 'n3-id-open',
            'n4-link-closed', 'r1-key', 'l1-loopback', 'l1-external', 's1-child']


MAPPINGS = (r"(Get-ChildItem 'HKCU:\Software\Classes\Local Settings\Software\Microsoft\Windows"
            r"\CurrentVersion\AppContainer\Mappings' -ErrorAction SilentlyContinue | Measure-Object).Count")
PACKAGES = (r"$l=(Get-ItemProperty 'HKCU:\Volatile Environment').LOCALAPPDATA; "
            r"(Get-ChildItem (Join-Path $l 'Packages') -ErrorAction SilentlyContinue | Measure-Object).Count")


def _toml_path(path):
    return "'"+str(path)+"'"


def _codex_home(directory, attempt, readable):
    home = directory/'codex-home'
    home.mkdir()
    lines = ['default_permissions = "symphony-probe"', '', '[windows]', 'sandbox = "mxc"', '',
             '[permissions.symphony-probe.filesystem]', '":root" = "deny"', '":minimal" = "read"',
             f'{_toml_path(attempt)} = "write"', *(f'{_toml_path(p)} = "read"' for p in readable), '',
             '[permissions.symphony-probe.network]', 'enabled = false', '']
    (home/'config.toml').write_text('\n'.join(lines), encoding='utf-8')
    return home


def _client_environment(home, client_home, canary):
    """The client starts from this test's environment, already reduced by the runner, never the user's own.

    Names that look secret are dropped, profile directories point into the test and the decoys are added, so
    the only secret-like names the client can pass on are the decoys.
    """
    env = {k: v for k, v in os.environ.items() if not _SECRET_NAME.search(k)}
    env.setdefault('SYSTEMDRIVE', os.path.splitdrive(env.get('SYSTEMROOT', 'C:\\Windows'))[0] or 'C:')
    env.update(CODEX_HOME=str(home), USERPROFILE=str(client_home), HOME=str(client_home),
               APPDATA=str(client_home/'appdata'), LOCALAPPDATA=str(client_home/'local'),
               TEMP=str(client_home/'temp'), TMP=str(client_home/'temp'), **{name: canary for name in DECOY_NAMES})
    return env


def _probe_python(directory):
    """A copy of the probe's interpreter outside the worktree; the profile grants read on test-owned files only."""
    source = Path(base_python()).resolve().parent
    target = directory/'probe-python'
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(
        'site-packages', 'include', 'libs', 'tcl', 'Scripts', '__pycache__', 'test', 'idlelib', 'tkinter'))
    return target/'python.exe'


def _host_state(git):
    def ps(command):
        # Console output is in the OEM code page (local account names are not ASCII here); bytes are
        # compared by digest, never decoded as UTF-8.
        done = subprocess.run(['powershell', '-NoProfile', '-Command', command], capture_output=True, check=False,
                              timeout=180)
        return hashlib.sha256(done.stdout.strip()).hexdigest() if done.returncode == 0 else f'error {done.returncode}'
    configs = {str(h): hashlib.sha256((h/'config.toml').read_bytes()).hexdigest()
               for h in CODEX_HOMES if (h/'config.toml').exists()}
    return {'users': ps('(Get-LocalUser | Select-Object -ExpandProperty Name | Sort-Object) -join ","'),
            'firewall_rules': ps('(Get-NetFirewallRule -ErrorAction SilentlyContinue | Measure-Object).Count'),
            # A persistent AppContainer profile created by the sandbox would show in these two (read-only).
            'appcontainer_mappings': ps(MAPPINGS), 'packages': ps(PACKAGES),
            'codex_configs': configs, 'worktree': _primary_fingerprint(git)}


def _wait(api, process, seconds):
    return api.fn('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)(process, int(seconds*1000)) == 0


def _pid_state(api, pid):
    handle = api.fn('OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE)(0x101000, False, pid)
    if not handle:
        return {'open': False, 'error': C.get_last_error()}
    try:
        return {'open': True, 'signaled': _wait(api, handle, 0)}
    finally:
        api.close(handle)


def _tail(path, size=4000):
    return path.read_bytes()[-size:].decode('utf-8', 'replace') if path.exists() else None


class Client:
    """One client command in its own kill-on-close Job; everything that explains a failure is kept."""

    def __init__(self, api, argv, cwd, env, job_id, directory, home):
        self.api, self.job_id, self.home = api, job_id, home
        self.paths = [directory/(job_id+suffix) for suffix in ('.in', '.out', '.err')]
        self.streams = [path.open('w+b') for path in self.paths]
        job = api.create_job(job_id)
        self.open = True
        self.process, thread, self.pid, _ = api.create_suspended(argv, cwd, env, self.streams, job, None)
        api.resume(thread)
        api.close(thread)
        self.in_job = api.in_job(self.process, job)

    def finish(self, wait):
        finished = _wait(self.api, self.process, wait)
        return {'finished': finished, 'exit': self.api.exit_code(self.process) if finished else None,
                'in_job': self.in_job}

    def close(self):
        if self.open:
            self.open = False
            self.api.close_job(self.job_id)  # kill-on-close: the whole tree this Job holds
        stopped = _wait(self.api, self.process, 15)
        self.api.close(self.process)
        for stream in self.streams:
            stream.close()
        return stopped

    def output(self):
        return {'stdout': _tail(self.paths[1]), 'stderr': _tail(self.paths[2]),
                'sandbox_log': _tail(self.home/'.sandbox'/'sandbox.log')}


def _m0(api, codex, profile, attempt, env, run, directory, home, label):
    client = Client(api, [*codex, '-P', profile, '--', 'cmd.exe', '/d', '/c', 'echo MXC_OK'], attempt, env,
                    f'SymphonyMxc{label}-{run}', directory, home)
    record = client.finish(120)
    client.close()
    record.update(client.output())
    record['ok'] = record['finished'] and record['exit'] == 0 and 'MXC_OK' in (record['stdout'] or '')
    return record


def _sddl(api, paths):
    return {str(p): api.object_sddl(FILE, p) for p in paths}


def test_codex_mxc_sandbox_on_decoys(tmp_path):
    git = shutil.which('git')
    api, user, run = BoundedApi(32), current_user_sid(), uuid.uuid4().hex[:12]
    canary = 'canary-'+secrets.token_hex(16)
    proof = {'run': run, 'codex': str(CODEX), 'controls': {}, 'm0a': None, 'm0b': None, 'probe': None, 's1': {}}
    keys_root = 'Software\\SymphonySmoke-'+run
    proof['host_before'] = _host_state(git)
    listener = socket.socket()
    listener.settimeout(300)  # a blocked accept never outlives the test
    listener.bind(('127.0.0.1', 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    accepted = []
    pool = ThreadPoolExecutor(max_workers=1)
    clients = []
    try:
        pool.submit(lambda: accepted.append(listener.accept()[0]))
        # --- stand ------------------------------------------------------------------------------
        closed_canary = _decoy(api, tmp_path/'decoy-closed', user, canary.encode())
        open_canary = _decoy(api, tmp_path/'decoy-open', user, b'open control\n')
        _key(api, keys_root+'\\Closed', user, canary)
        primary = tmp_path/'primary'
        primary.mkdir()
        api.write_dacl(FILE, primary, 'O:'+user+'G:'+user+DISK_LIKE.format(user=user))
        (primary/'.git').mkdir()
        (primary/'file.txt').write_bytes(b'primary\n')
        (primary/'.git'/'config').write_bytes(b'[core]\n')
        neighbour = tmp_path/'attempts'/'neighbour'
        neighbour.mkdir(parents=True)
        (neighbour/'file.txt').write_bytes(b'neighbour\n')
        attempt = tmp_path/'attempts'/'attempt'
        (attempt/'src').mkdir(parents=True)
        (attempt/'src'/'file.txt').write_bytes(b'file\n')
        (attempt/'src'/'gone.txt').write_bytes(b'gone\n')
        python = _probe_python(tmp_path)
        profile_paths = (attempt, python.parent, tmp_path/'decoy-open')
        home = _codex_home(tmp_path, attempt, profile_paths[1:])
        proof['sddl_before'] = _sddl(api, profile_paths)
        targets = {'primary_file': primary/'file.txt', 'primary_git_config': primary/'.git'/'config',
                   'neighbour_file': neighbour/'file.txt'}
        proof['controls'] = {
            'write': {n: _owner_opens(p, 'r+b') for n, p in targets.items()},
            'read_closed': _owner_opens(closed_canary, 'rb'), 'by_id_closed': _by_id_owner(closed_canary),
            'key': _owner_reads_key(keys_root+'\\Closed'),
        }
        with socket.create_connection(('127.0.0.1', port), timeout=5):
            proof['controls']['loopback'] = True
        wait_until(lambda: accepted, timeout=5, what='owner loopback control')
        pool.submit(lambda: accepted.append(listener.accept()[0]))
        client_home = tmp_path/'client-home'
        for sub in ('', 'appdata', 'local', 'temp'):
            (client_home/sub).mkdir(parents=True, exist_ok=True)
        env = _client_environment(home, client_home, canary)
        proof['client_env_names'] = sorted(env)
        codex = [str(CODEX), 'sandbox', '-C', str(attempt), '-c', 'windows.sandbox="mxc"']
        # --- M0a: the vendor's own check; M0b: the same under this profile -------------------------
        proof['m0a'] = _m0(api, codex, ':workspace', attempt, env, run, tmp_path, home, 'M0a')
        if not proof['m0a']['ok']:
            pytest.fail('MXC_UNAVAILABLE_STOP')
        proof['m0b'] = _m0(api, codex, 'symphony-probe', attempt, env, run, tmp_path, home, 'M0b')
        if not proof['m0b']['ok']:
            pytest.fail('MXC_PROFILE_STOP')
        # --- the probe --------------------------------------------------------------------------
        plan = {'attempt': str(attempt), 'decoy_names': list(DECOY_NAMES), 'closed_canary': str(closed_canary),
                'closed_root': str(tmp_path/'decoy-closed'), 'open_canary': str(open_canary),
                'closed_id': os.stat(closed_canary).st_ino, 'open_id': os.stat(open_canary).st_ino,
                'key': keys_root+'\\Closed', 'port': port, 'write_denied': {n: str(p) for n, p in targets.items()}}
        plan_file = tmp_path/'decoy-open'/'plan.json'  # readable in the profile; not the closed decoy
        plan_file.write_text(json.dumps(plan), encoding='utf-8')
        client = Client(api, [*codex, '-P', 'symphony-probe', '--', str(python), '-I', '-B', '-c', PROBE,
                              str(plan_file)], attempt, env, 'SymphonyMxcProbe-'+run, tmp_path, home)
        clients.append(client)
        report = attempt/'report.json'
        try:
            wait_until(report.exists, timeout=180, what='probe report')
        except AssertionError:
            pass  # recorded as a missing report and asserted after the proof is saved
        probe = json.loads(report.read_text(encoding='utf-8')) if report.exists() else None
        proof['probe'] = probe
        child = next((s.get('pid') for s in (probe or {}).get('steps', []) if s['name'] == 's1-child'), None)
        proof['s1'] = {'in_job': client.in_job, 'client_alive_before': not _wait(api, client.process, 0),
                       'child_before': _pid_state(api, child) if child else None}
        proof['s1']['client_stopped'] = client.close()
        clients.remove(client)
        if child:
            try:
                wait_until(lambda: not _pid_state(api, child).get('open') or _pid_state(api, child).get('signaled'),
                           timeout=15, what='probe child stop')
            except AssertionError:
                pass  # recorded below and asserted after the proof is saved
        proof['s1']['child_after'] = _pid_state(api, child) if child else None
        proof['probe_output'] = client.output()
        texts = [json.dumps(proof['probe']), json.dumps(proof['probe_output']), json.dumps(proof['m0a']),
                 json.dumps(proof['m0b'])]
        proof['canary_leaked'] = any(canary in text for text in texts)
        proof['files_after'] = sorted(p.name for p in (attempt/'src').iterdir())
        proof['outside_unchanged'] = {n: p.read_bytes() == {'primary_file': b'primary\n', 'primary_git_config': b'[core]\n',
                                                              'neighbour_file': b'neighbour\n'}[n] for n, p in targets.items()}
        proof['sddl_after'] = _sddl(api, profile_paths)
    finally:
        for client in clients:
            try:
                client.close()
            except Exception as exc:  # health: allow BLE001 -- the proof records every cleanup failure
                proof.setdefault('cleanup_errors', []).append(repr(exc))
        for sub in (keys_root+'\\Closed', keys_root):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except OSError as exc:
                proof.setdefault('cleanup_errors', []).append(f'{sub}: {exc.winerror}')
        listener.close()
        pool.shutdown(wait=False, cancel_futures=True)
        proof['host_after'] = _host_state(git)
        save_proof('codex-mxc-probe', proof)

    # --- verdicts, after the proof is saved ---------------------------------------------------------
    controls = proof['controls']
    assert all(controls['write'].values()) and controls['read_closed'] and controls['by_id_closed']
    assert controls['key'] and controls['loopback']
    assert proof['host_after'] == proof['host_before'], 'H1: host changed'
    assert proof['sddl_after'] == proof['sddl_before'], 'H1: the sandbox changed rights on a profile path'
    assert proof['canary_leaked'] is False and all(proof['outside_unchanged'].values())
    probe = proof['probe']
    assert probe is not None and [s['name'] for s in probe['steps']] == EXPECTED
    steps = {s['name']: s for s in probe['steps']}
    assert steps['e1-env']['decoys_visible'] == []
    for name in ('p1-modify', 'p1-create', 'p1-delete', 'p1-child', 'n2-read-open'):
        assert steps[name]['ok'] is True, (name, steps[name])
    assert steps['n2-read-open']['sha256'] == hashlib.sha256(b'open control\n').hexdigest()
    for name in ('n1-primary_file', 'n1-primary_git_config', 'n1-neighbour_file', 'n2-read-closed', 'n2-list-closed',
                 'n3-id-closed', 'n4-link-closed', 'l1-loopback', 'l1-external'):
        assert steps[name]['ok'] is False, (name, steps[name])
    assert proof['files_after'] == ['child.txt', 'file.txt', 'new.txt']
    assert proof['s1']['client_stopped'] is True
    assert proof['s1']['child_after'] is not None and (proof['s1']['child_after'].get('open') is False
                                                       or proof['s1']['child_after'].get('signaled') is True)
