"""One attempt workspace, end to end, on test-owned decoys only. Requires an explicit native grant.

No model, no driver, no real secret, no change to any real root, key, worktree or `.git`. Three
synthetic workers run with the production worker token:

- A works in its own copy of the fixed commit, runs child tools and probes every boundary the CR
  names: foreign writes, reads by path, by file id and through a hard link, the registry;
- B leaves a junction in its copy; C writes under `.git` and fails. Their captures must refuse.

Every refusal by path has a control that the same worker passes on an identical decoy without the
deny entry, so a refusal cannot come from a path the worker could not reach anyway. A refusal counts
only with error 5. The closed canary is a random string that must never show up anywhere.

Opening by file id has no path, so the deny entry on the decoy's root cannot be what refuses it;
what is asserted is that the closed canary is refused (the owner, with its privileges, opens both).
Whether the open canary opens by id is recorded, not asserted: a worker without the traverse
privilege may well be refused both. The hard-link probes are refused by integrity as much as by the
deny entry and are recorded as such.

Passing proves the mechanism on decoys. It says nothing about the protection of real secrets.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from types import SimpleNamespace
import uuid
import winreg

import pytest

from hermes_platform.resolver import locate_command
from plugins.symphony_pm.transport import attempt_workspace as ws
from plugins.symphony_pm.transport.types import Refused
from plugins.symphony_pm.transport.windows_read_boundary import FILE, KEY, DenyEntries, has_class_deny
from plugins.symphony_pm.transport.windows_receiver import WindowsReceiver
from plugins.symphony_pm.transport.windows_token import LOW, attempt_sid, restricting_sids
from tests.plugins.symphony_native_crash_support import (
    BoundedApi, admit_ancestors, base_python, current_user_sid, save_proof,
)
from tests.plugins.symphony_serving_support import approve, rig

pytestmark = pytest.mark.platforms('windows')

REPOSITORY = Path(__file__).resolve().parents[2]
SOURCE = '0a46b77117ca21b3a7123afb8a650604fb7365ce'
EXPORT_LIMIT_SECONDS = 600
# What the root of a data disk on this host admits: Users read, Authenticated Users modify.
DISK_LIKE = 'D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1301bf;;;AU)(A;OICI;0x1200a9;;;BU)(A;OICI;FA;;;{user})'
# What HKCU\Environment admits on this host: the user, SYSTEM, administrators, and RESTRICTED read.
ENVIRONMENT_LIKE = 'D:P(A;;KA;;;SY)(A;;KA;;;BA)(A;;KA;;;{user})(A;;KR;;;RC)'

WORKER = r'''
import ctypes as C, hashlib, json, os, subprocess, sys, winreg
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

def opened(path, mode):  # opening is the probe; nothing is read from a closed target
    with open(path, mode):
        pass

def digest(path):
    return {'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()}

def key(sub, read=False):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_READ) as handle:
        if read:
            return {'sha256': hashlib.sha256(winreg.QueryValueEx(handle, 'value')[0].encode()).hexdigest()}

class Id(C.Structure):
    _fields_ = [('size', W.DWORD), ('kind', C.c_int), ('id', C.c_ubyte*16)]

def by_id(file_id):
    k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    k.CreateFileW.restype = W.HANDLE
    hint = k.CreateFileW(plan['src'], 0x80, 7, None, 3, 0x02000000, None)  # own directory, same volume
    if hint in (None, -1, 0xFFFFFFFFFFFFFFFF):
        raise C.WinError(C.get_last_error())
    try:
        descriptor = Id(C.sizeof(Id), 2, (C.c_ubyte*16)(*int(file_id).to_bytes(16, 'little')))
        k.OpenFileById.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, W.DWORD, C.c_void_p, W.DWORD]
        k.OpenFileById.restype = W.HANDLE
        handle = k.OpenFileById(hint, C.byref(descriptor), 0x80000000, 7, None, 0)
        if handle in (None, -1, 0xFFFFFFFFFFFFFFFF):
            raise C.WinError(C.get_last_error())
        k.CloseHandle(handle)
    finally:
        k.CloseHandle(hint)

def hard_link(link, target):
    k.CreateHardLinkW.argtypes = [W.LPCWSTR, W.LPCWSTR, C.c_void_p]
    if not k.CreateHardLinkW(link, target, None):
        raise C.WinError(C.get_last_error())

src, home, temp = Path(plan['src']), Path(plan['home']), Path(plan['temp'])
step('env', lambda: {'env': {n: os.environ.get(n) for n in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP')},
                     'names': sorted(os.environ)})
mode = plan['mode']
if mode == 'A':
    step('p1-modify', lambda: (src/'README.md').open('ab').write(b'\nsmoke: worker line\n') and None)
    step('p1-create', lambda: ((src/'smoke').mkdir(), (src/'smoke'/'new.txt').write_bytes(b'created by worker\n')) and None)
    step('p1-delete', lambda: (src/'LICENSE').unlink())
    step('p1-home', lambda: (home/'h.txt').write_bytes(b'home\n') and None)
    step('p1-temp', lambda: (temp/'t.txt').write_bytes(b'temp\n') and None)
    def child_python():
        done = subprocess.run([sys.executable, '-I', '-B', '-c',
            "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'child\\n'); print('child-ok')",
            str(src/'smoke'/'child.txt')], capture_output=True, timeout=60)
        print('P2-python:', done.stdout.decode().strip(), flush=True)
        return {'ok': done.returncode == 0, 'rc': done.returncode}
    step('p2-python', child_python)
    def child_git():
        done = subprocess.run([plan['git'], '--version'], capture_output=True, timeout=60)
        print('P2-git:', done.stdout.decode().strip(), flush=True)
        return {'ok': done.returncode == 0 and done.stdout.startswith(b'git version'), 'rc': done.returncode}
    step('p2-git', child_git)
    for name, path in plan['write_denied'].items():
        if name == 'plugin_data':
            step('n1-'+name, lambda path=path: opened(Path(path)/'worker-probe.txt', 'xb'))
        else:
            step('n1-'+name, lambda path=path: opened(path, 'ab'))
    step('n2-read-closed-canary', lambda: opened(plan['closed_canary'], 'rb'))
    step('n2-read-open-canary', lambda: digest(plan['open_canary']))
    step('n2-list-closed-root', lambda: {'count': len(os.listdir(plan['closed_root']))})
    step('n2-list-open-root', lambda: {'count': len(os.listdir(plan['open_root']))})
    step('n2-key-closed', lambda: key(plan['closed_key']))
    step('n2-key-open', lambda: key(plan['open_key'], read=True))
    step('n2-list-neighbour', lambda: {'count': len(os.listdir(plan['neighbour']))})
    step('n3-id-closed-canary', lambda: by_id(plan['closed_id']))
    step('n3-id-open-canary', lambda: by_id(plan['open_id']))
    step('n4-link-closed-canary', lambda: hard_link(str(home/'hl-closed'), plan['closed_canary']))
    step('n4-link-open-canary', lambda: hard_link(str(home/'hl-open'), plan['open_canary']))
elif mode == 'B':
    import _winapi
    step('b-junction', lambda: _winapi.CreateJunction(plan['closed_root'], str(src/'jlink')))
    step('b-read-through', lambda: opened(src/'jlink'/'inner'/'canary.txt', 'rb'))
    step('b-junction-open', lambda: _winapi.CreateJunction(plan['open_root'], str(src/'jopen')))
    step('b-read-through-open', lambda: digest(src/'jopen'/'inner'/'canary.txt'))
elif mode == 'C':
    step('c-hook', lambda: ((src/'.git'/'hooks').mkdir(parents=True), (src/'.git'/'hooks'/'pre-commit').write_bytes(b'exit 0\n')) and None)
    step('c-edit', lambda: (src/'README.md').open('ab').write(b'\nfrom C\n') and None)
report = home/'report.json'
report.with_suffix('.tmp').write_text(json.dumps(out), encoding='utf-8')
report.with_suffix('.tmp').replace(report)
sys.exit(3 if mode == 'C' else 0)
'''

EXPECTED = {
    'A': ['env', 'p1-modify', 'p1-create', 'p1-delete', 'p1-home', 'p1-temp', 'p2-python', 'p2-git',
          'n1-primary_file', 'n1-primary_git_config', 'n1-neighbour_file', 'n1-plugin_data',
          'n2-read-closed-canary', 'n2-read-open-canary', 'n2-list-closed-root', 'n2-list-open-root',
          'n2-key-closed', 'n2-key-open', 'n2-list-neighbour', 'n3-id-closed-canary', 'n3-id-open-canary',
          'n4-link-closed-canary', 'n4-link-open-canary'],
    'B': ['env', 'b-junction', 'b-read-through', 'b-junction-open', 'b-read-through-open'],
    'C': ['env', 'c-hook', 'c-edit'],
}


def _decoy(api, root, user, content):
    root.mkdir()
    api.write_dacl(FILE, root, 'O:'+user+'G:'+user+DISK_LIKE.format(user=user))
    (root/'inner').mkdir()
    canary = root/'inner'/'canary.txt'
    canary.write_bytes(content)
    return canary


def _key(api, sub, user, value):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, sub) as handle:
        winreg.SetValueEx(handle, 'value', 0, winreg.REG_SZ, value)
    api.write_dacl(KEY, 'CURRENT_USER\\'+sub, 'O:'+user+'G:'+user+ENVIRONMENT_LIKE.format(user=user))
    return 'CURRENT_USER\\'+sub


def _owner_reads_key(sub):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_READ) as handle:
        return bool(winreg.QueryValueEx(handle, 'value')[0])


def _owner_creates(directory):
    probe = Path(directory)/'owner-probe.txt'
    probe.write_bytes(b'')
    probe.unlink()
    return True


def _owner_opens(path, mode):
    with open(path, mode):
        return True


def _by_id_owner(path):
    import ctypes as C
    from ctypes import wintypes as W
    k = C.WinDLL('kernel32', use_last_error=True)
    k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    k.CreateFileW.restype = W.HANDLE
    hint = k.CreateFileW(str(Path(path).parent), 0x80, 7, None, 3, 0x02000000, None)
    if hint in (None, -1, 0xFFFFFFFFFFFFFFFF):
        raise C.WinError(C.get_last_error())

    class Id(C.Structure):
        _fields_ = [('size', W.DWORD), ('kind', C.c_int), ('id', C.c_ubyte*16)]
    try:
        k.OpenFileById.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, W.DWORD, C.c_void_p, W.DWORD]
        k.OpenFileById.restype = W.HANDLE
        handle = k.OpenFileById(hint, C.byref(Id(C.sizeof(Id), 2, (C.c_ubyte*16)(*os.stat(path).st_ino.to_bytes(16, 'little')))),
                                0x80000000, 7, None, 0)
        opened = handle not in (None, -1, 0xFFFFFFFFFFFFFFFF)
        if opened:
            k.CloseHandle(handle)
        return opened
    finally:
        k.CloseHandle(hint)


def _launch(api, ticket, workspace, plan, protected, data_directory, capture_directory, environment):
    plan_file = workspace.home/'plan.json'
    plan_file.write_text(json.dumps(plan), encoding='utf-8')
    argv = [base_python(), '-I', '-B', '-c', WORKER, str(plan_file)]
    capture_directory.mkdir()
    receiver = WindowsReceiver(api, ticket, argv, workspace.src, '', environment=workspace.environment(environment),
        protected=protected, command_result=lambda argv, code, **kw: SimpleNamespace(returncode=code, **kw),
        sanitize=lambda text: text, private_directory=capture_directory, data_directory=data_directory)
    try:
        token = {'integrity': api.integrity(receiver.process), 'privileges': api.privilege_count(receiver.process),
                 'restricting_exact': sorted(api.restricting_sids(receiver.process)) == sorted(restricting_sids(ticket.attempt)),
                 'write_up_forbidden': bool(api.mandatory_policy(receiver.process) & 1)}
        receiver.resume('', authorize=lambda action: action())
        result = receiver.wait_result(argv, 240)
    finally:
        receiver.close_fail_safe(time.monotonic()+10)
    report_file = workspace.home/'report.json'
    report = json.loads(report_file.read_text(encoding='utf-8')) if report_file.exists() else None
    return token, result, report


def _ticket(tmp_path, name):
    r = rig(tmp_path/('journal-'+name))
    request, context, _ = approve(r)
    ticket, _, _ = r.owner.journal.reserve_launch(r.owner.token, request, context, r.now[0])
    return r, ticket, Path(r.owner.journal.path).parents[1]


def _primary_fingerprint(git):
    """Content of the real worktree's changes, tracked and untracked; read-only (no optional locks)."""
    def run(*args, stdin=None):
        return subprocess.run([git, '--no-optional-locks', '-C', str(REPOSITORY), *args], input=stdin,
                              capture_output=True, check=True, env=ws._clean_environment()).stdout
    tracked = run('diff', 'HEAD', '--binary', '--no-ext-diff', '--no-textconv')
    # This run's own evidence directory is written while the test runs; a nested repository is
    # listed as a directory and is compared by name only.
    own = Path(os.environ['HERMES_E2E_ARTIFACTS']).resolve().parent
    own = own.relative_to(REPOSITORY).as_posix()+'/' if own.is_relative_to(REPOSITORY) else None
    listed = [p for p in run('ls-files', '-o', '--exclude-standard', '-z').decode('utf-8').split('\0')
              if p and not (own and p.startswith(own))]
    files = '\n'.join(p for p in listed if not p.endswith('/')).encode('utf-8')
    hashes = run('hash-object', '--stdin-paths', stdin=files+b'\n') if files else b''
    untracked = '\0'.join(listed).encode('utf-8')
    return {'head': run('rev-parse', 'HEAD').strip().decode(), 'tracked': hashlib.sha256(tracked).hexdigest(),
            'untracked': hashlib.sha256(untracked+hashes).hexdigest()}


def test_attempt_workspace_on_decoys(tmp_path):
    git = locate_command('git').command[0]
    api, user, run = BoundedApi(6), current_user_sid(), uuid.uuid4().hex[:12]
    canary = ('canary-'+secrets.token_hex(16)).encode()
    proof = {'source': SOURCE, 'run': run, 'controls': {}, 'workers': {}, 'captures': {}}
    status_before = _primary_fingerprint(git)
    rigs, deny, keys_root = [], DenyEntries(api, tmp_path/'deny-journal.json'), 'Software\\SymphonySmoke-'+run
    protected, snapshot = (), {}
    try:
        # --- the stand: decoys, keys, a fake primary copy, three workspaces ------------------
        closed_canary = _decoy(api, tmp_path/'decoy-closed', user, canary)
        open_canary = _decoy(api, tmp_path/'decoy-open', user, b'open control\n')
        closed_key = _key(api, keys_root+'\\Closed', user, canary.decode())
        _key(api, keys_root+'\\Open', user, 'open control')
        protected = ((FILE, str(tmp_path/'decoy-closed')), (KEY, closed_key))
        snapshot = {f'{k}:{t}': api.object_sddl(k, t) for k, t in protected}
        deny.apply(protected)
        primary = tmp_path/'primary'
        primary.mkdir()
        api.write_dacl(FILE, primary, 'O:'+user+'G:'+user+DISK_LIKE.format(user=user))
        (primary/'.git').mkdir()
        (primary/'file.txt').write_bytes(b'primary\n')
        (primary/'.git'/'config').write_bytes(b'[core]\n')
        attempts, spaces, captures = tmp_path/'attempts', {}, {}
        attempts.mkdir()
        tickets = {}
        for name in 'ABC':
            r, ticket, data = _ticket(tmp_path, name)
            rigs.append(r)
            tickets[name] = (ticket, data)
            spaces[name] = ws.create(api, attempts, ticket.attempt)
        data_a = tickets['A'][1]
        # Traverse-only entries on the test directories above every target, written before the
        # copy exists and without propagation, once per (directory, SID).
        admit_ancestors([spaces[n].path for n in 'ABC'] + [tmp_path/'decoy-closed', tmp_path/'decoy-open',
                        primary, data_a], [s for n in 'ABC' for s in (attempt_sid(tickets[n][0].attempt),)] + [user])
        started = time.monotonic()
        ws.export(git, REPOSITORY, SOURCE, spaces['A'].src)
        proof['export_seconds'] = round(time.monotonic()-started, 1)
        for name in 'BC':
            ws.export(git, REPOSITORY, SOURCE, spaces[name].src, paths=('README.md',))
        for name in 'ABC':
            captures[name] = ws.Capture(git, REPOSITORY, SOURCE, spaces[name], tmp_path/'owner'/name/'shadow',
                                        tmp_path/'owner'/'results')
            captures[name].baseline()
        targets = {'primary_file': primary/'file.txt', 'primary_git_config': primary/'.git'/'config',
                   'neighbour_file': spaces['B'].src/'README.md', 'plugin_data': data_a}
        # --- owner-side controls: every target exists and opens for the owner ---------------
        proof['controls'] = {
            'write': {n: _owner_opens(p, 'r+b') if n != 'plugin_data' else _owner_creates(p) for n, p in targets.items()},
            'list_neighbour': bool(os.listdir(spaces['B'].path)),
            'read_closed_canary': _owner_opens(closed_canary, 'rb'),
            'by_id_closed': _by_id_owner(closed_canary), 'by_id_open': _by_id_owner(open_canary),
            'key_closed': _owner_reads_key(keys_root+'\\Closed'),
            'deny_present': all(has_class_deny(api.object_sddl(k, t)) for k, t in protected),
        }
        link = tmp_path/'owner-link'
        os.link(open_canary, link)
        proof['controls']['hard_link'] = link.exists()
        link.unlink()
        base = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR'}}
        base['PATH'] = os.pathsep.join((str(Path(git).parent), os.path.join(base.get('SYSTEMROOT', 'C:/Windows'), 'System32')))
        common = {'git': git, 'closed_canary': str(closed_canary), 'open_canary': str(open_canary),
                  'closed_root': str(tmp_path/'decoy-closed'), 'open_root': str(tmp_path/'decoy-open'),
                  'closed_key': keys_root+'\\Closed', 'open_key': keys_root+'\\Open',
                  'closed_id': os.stat(closed_canary).st_ino, 'open_id': os.stat(open_canary).st_ino,
                  'neighbour': str(spaces['B'].path), 'write_denied': {n: str(p) for n, p in targets.items()}}
        # --- the three workers ---------------------------------------------------------------
        outputs = []
        for name in 'ABC':
            space = spaces[name]
            plan = {**common, 'mode': name, 'src': str(space.src), 'home': str(space.home), 'temp': str(space.temp)}
            token, result, report = _launch(api, tickets[name][0], space, plan, protected, tickets[name][1],
                                            tmp_path/('capture-'+name), base)
            outputs += [result.stdout, result.stderr, json.dumps(report)]
            proof['workers'][name] = {'token': token, 'returncode': result.returncode,
                                      'stdout': result.stdout[-2000:], 'stderr': result.stderr[-2000:], 'report': report}
        for name in 'ABC':
            try:
                result = captures[name].result()
                proof['captures'][name] = {'paths': list(result.paths), 'sha256': result.sha256,
                                           'baseline_matches_source': result.baseline_matches_source}
                outputs.append(result.patch.read_bytes().decode('utf-8', 'replace'))
            except Refused as exc:
                proof['captures'][name] = {'refused': str(exc)}
        proof['frozen_A'] = ws.freeze(api, spaces['A'])
        proof['canary_leaked'] = any(canary.decode() in text for text in outputs)
    finally:
        # --- R2: the stand's own deny entries and keys go back exactly ------------------------
        restore = {}
        try:
            deny.revert()
            restore['sddl_restored'] = {f'{k}:{t}': api.object_sddl(k, t) == snapshot.get(f'{k}:{t}')
                                        for k, t in protected}
        except (Refused, OSError) as exc:
            restore['revert_error'] = repr(exc)
        for sub in (keys_root+'\\Closed', keys_root+'\\Open', keys_root):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except OSError as exc:
                restore.setdefault('key_errors', []).append(f'{sub}: {exc.winerror}')
        try:
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, keys_root).Close()
            restore['keys_removed'] = False
        except OSError:
            restore['keys_removed'] = True
        proof['restore'] = restore
        proof['primary_status_unchanged'] = _primary_fingerprint(git) == status_before
        for r in rigs:
            r.owner.close(0)
        save_proof('attempt-workspace', proof)

    # --- assertions, after the proof is saved ------------------------------------------------
    controls = proof['controls']
    assert all(controls['write'].values()) and controls['read_closed_canary'] and controls['key_closed']
    assert controls['by_id_closed'] and controls['by_id_open'] and controls['hard_link'] and controls['deny_present']
    assert controls['list_neighbour']
    assert proof['export_seconds'] < EXPORT_LIMIT_SECONDS
    for name in 'ABC':
        worker = proof['workers'][name]
        assert worker['token'] == {'integrity': LOW, 'privileges': 0, 'restricting_exact': True,
                                   'write_up_forbidden': True}, name
        report = worker['report']
        assert report is not None and [s['name'] for s in report['steps']] == EXPECTED[name], name
        assert [s['seq'] for s in report['steps']] == list(range(1, len(EXPECTED[name])+1)), name
        env = report['steps'][0]['env']
        assert all(Path(value).is_relative_to(spaces[name].path) for value in env.values()), name
    steps = {s['name']: s for s in proof['workers']['A']['report']['steps']}
    assert proof['workers']['A']['returncode'] == 0
    for name in ('p1-modify', 'p1-create', 'p1-delete', 'p1-home', 'p1-temp', 'p2-python', 'p2-git',
                 'n2-read-open-canary', 'n2-list-open-root', 'n2-key-open'):
        assert steps[name]['ok'] is True, (name, steps[name])
    assert steps['n2-read-open-canary']['sha256'] == hashlib.sha256(b'open control\n').hexdigest()
    assert 'P2-python: child-ok' in proof['workers']['A']['stdout'] and 'P2-git: git version' in proof['workers']['A']['stdout']
    for name in ('n1-primary_file', 'n1-primary_git_config', 'n1-neighbour_file', 'n1-plugin_data',
                 'n2-read-closed-canary', 'n2-list-closed-root', 'n2-key-closed', 'n2-list-neighbour',
                 'n3-id-closed-canary', 'n4-link-closed-canary', 'n4-link-open-canary'):
        assert steps[name]['ok'] is False and steps[name].get('winerror') == 5, (name, steps[name])
    b_steps = {s['name']: s for s in proof['workers']['B']['report']['steps']}
    assert b_steps['b-junction']['ok'] is True and b_steps['b-junction-open']['ok'] is True
    assert b_steps['b-read-through-open']['sha256'] == hashlib.sha256(b'open control\n').hexdigest()
    assert b_steps['b-read-through']['ok'] is False and b_steps['b-read-through'].get('winerror') == 5
    assert proof['workers']['C']['returncode'] == 3
    assert proof['captures']['A']['paths'] == ['LICENSE', 'README.md', 'smoke/child.txt', 'smoke/new.txt']
    assert proof['captures']['B'] == {'refused': 'ATTEMPT_REPARSE_POINT'}
    assert proof['captures']['C'] == {'refused': 'ATTEMPT_POLICY'} and spaces['C'].path.is_dir()
    assert proof['frozen_A'] is True and proof['canary_leaked'] is False
    assert proof['restore'].get('sddl_restored') and all(proof['restore']['sddl_restored'].values())
    assert proof['restore']['keys_removed'] is True and 'revert_error' not in proof['restore']
    assert proof['primary_status_unchanged'] is True
