"""One attempt's own workspace and its result as a checked patch. Importing creates nothing.

The worker gets a copy of the tree at a fixed commit, without `.git`, plus its own HOME and TEMP,
all in one new directory. Only that directory is labelled low, so a low-integrity worker can write
there and nowhere else; no existing object is relabelled.

After the worker has stopped, the owner reads the copy as data. Nothing in it is executed: the
diff is taken by Git with a Git directory, index and configuration that live outside the copy, and
before that the copy is walked without following links. A link, a hard link or anything named like
Git's own files is a refusal, not a filtered result. The patch is checked to apply to the fixed
commit in the owner's repository through a separate index; it is never applied here.
"""
from dataclasses import dataclass
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile

from .types import Refused
from .windows_token import attempt_sid

_LOW_LABEL, _FROZEN_LABEL = 'S:(ML;OICI;NW;;;LW)', 'S:(ML;OICI;NW;;;ME)'
_MODIFY, _READ = 0x1301BF, 0x1200A9
_REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
PATCH_LIMIT = 16 << 20
GIT_TIMEOUT = 600


@dataclass(frozen=True)
class Workspace:
    attempt: str
    path: Path

    @property
    def src(self):
        return self.path/'src'

    @property
    def home(self):
        return self.path/'home'

    @property
    def temp(self):
        return self.path/'temp'

    def environment(self, base):
        """The worker's explicit environment: `base` plus HOME and TEMP inside this workspace."""
        home = str(self.home)
        return {**base, 'HOME': home, 'USERPROFILE': home, 'APPDATA': str(self.home/'appdata'),
                'LOCALAPPDATA': str(self.home/'local'), 'TEMP': str(self.temp), 'TMP': str(self.temp)}


# --- Windows: access and labels -----------------------------------------------------------------

def user_sid(api):
    token = api._process_token(api._current_process(), 0x0008)  # TOKEN_QUERY
    try:
        buffer = api._token_information(token, 1)  # TokenUser: SID_AND_ATTRIBUTES first
        return api._sid_text(C.c_void_p.from_buffer(buffer).value)
    finally:
        api.close(token)


def grant(api, path, sid, *, write=False, inherit=False, access=None):
    """Add one allow entry for `sid` to `path` (modify, read-execute or `access`), keeping a
    protected DACL protected."""
    dacl, descriptor = C.c_void_p(), C.c_void_p()
    code = api.adv('GetNamedSecurityInfoW', [W.LPCWSTR, C.c_int, W.DWORD, C.c_void_p, C.c_void_p,
        C.POINTER(C.c_void_p), C.c_void_p, C.POINTER(C.c_void_p)], W.DWORD)(
        str(path), 1, 4, None, None, C.byref(dacl), None, C.byref(descriptor))
    if code:
        raise C.WinError(code)
    try:
        control, revision = W.WORD(), W.DWORD()
        api.checked(api.adv('GetSecurityDescriptorControl',
            [C.c_void_p, C.POINTER(W.WORD), C.POINTER(W.DWORD)], W.BOOL)(descriptor, C.byref(control), C.byref(revision)))
        protected = 0x80000000 if control.value & 0x1000 else 0  # PROTECTED_DACL_SECURITY_INFORMATION
        mask = access if access is not None else _MODIFY if write else _READ
        with api.with_grant(dacl.value, sid, mask, 3 if inherit else 0) as merged:
            code = api.adv('SetNamedSecurityInfoW', [W.LPCWSTR, C.c_int, W.DWORD] + [C.c_void_p]*4, W.DWORD)(
                str(path), 1, 4 | protected, None, None, merged, None)
        if code:
            raise C.WinError(code)
    finally:
        api._local_free(descriptor)


def create(api, root, attempt):
    """A new workspace for `attempt` under `root`; an existing one is never reused.

    The directory is named by a short digest of the attempt, so deep trees stay well inside path limits.
    """
    path = Path(root)/hashlib.sha256(attempt.encode('utf-8')).hexdigest()[:12]
    try:
        path.mkdir()
    except FileExistsError as exc:
        raise Refused('ATTEMPT_WORKSPACE_EXISTS') from exc
    workspace = Workspace(attempt, path)
    for directory in (workspace.src, workspace.home, workspace.temp, workspace.home/'appdata',
                      workspace.home/'local'):
        directory.mkdir()
    # The kernel never treats a restricted token as an object's owner, so both checks need an entry.
    for sid in (user_sid(api), attempt_sid(attempt)):
        grant(api, path, sid, write=True, inherit=True)
    api._set_label('SetNamedSecurityInfoW', W.LPCWSTR(str(path)), 1, _LOW_LABEL)
    if api.path_label(path) != (0x1000, 0x1):
        raise Refused('ATTEMPT_WORKSPACE_LABEL')
    return workspace


def freeze(api, workspace):
    """After capture: relabel the whole workspace medium, so no low process writes it again."""
    api._set_label('SetNamedSecurityInfoW', W.LPCWSTR(str(workspace.path)), 1, _FROZEN_LABEL)
    return api.path_label(workspace.path) == (0x2000, 0x1)


# --- Git: copy in, patch out --------------------------------------------------------------------

def _clean_environment(**extra):
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0', **extra)
    return environment


def export(git, repository, sha, destination, paths=()):
    """Extract the tree of `sha` into `destination` as data; `.git` is not part of an archive."""
    command = [git, '-C', str(repository), '-c', 'core.autocrlf=false', 'archive', '--format=tar', sha,
               *(('--', *paths) if paths else ())]
    with subprocess.Popen(command, stdout=subprocess.PIPE, env=_clean_environment()) as process:
        with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
            archive.extractall(destination, filter='data')
        if process.wait(timeout=GIT_TIMEOUT):
            raise Refused('ATTEMPT_EXPORT_FAILED')


class Shadow:
    """A Git directory and index owned by the owner, pointed at the copy only as a work tree."""

    _SAFE = ('core.autocrlf=false', 'core.safecrlf=false', 'core.filemode=false', 'core.symlinks=false',
             'core.fsmonitor=false', 'core.untrackedCache=false', 'core.longpaths=true')

    def __init__(self, git, directory):
        self.git, self.directory = git, Path(directory)
        self.repository, self.hooks = self.directory/'repo.git', self.directory/'no-hooks'
        self.directory.mkdir(parents=True, exist_ok=False)
        self.hooks.mkdir()
        subprocess.run([git, 'init', '--bare', '-q', str(self.repository)], env=_clean_environment(),
                       check=True, timeout=GIT_TIMEOUT)

    def run(self, work_tree, index, *args, stdout=subprocess.PIPE):
        options = [item for pair in (('-c', value) for value in (*self._SAFE, 'core.hooksPath='+str(self.hooks)))
                   for item in pair]
        done = subprocess.run([self.git, *options, *args], cwd=str(work_tree), stdout=stdout,
                              stderr=subprocess.PIPE, timeout=GIT_TIMEOUT, check=False, env=_clean_environment(
                                  GIT_DIR=str(self.repository), GIT_WORK_TREE=str(work_tree),
                                  GIT_INDEX_FILE=str(self.directory/index)))
        if done.returncode:
            raise Refused('ATTEMPT_GIT_FAILED')
        return done.stdout

    def tree(self, work_tree, index):
        self.run(work_tree, index, 'add', '--all', '--force', '--', '.')
        return self.run(work_tree, index, 'write-tree').decode('ascii').strip()


def require_plain_tree(root):
    """Walk without following anything; refuse links, hard links and Git's own names."""
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in (*directories, *files):
            path = os.path.join(parent, name)
            info = os.lstat(path)
            if getattr(info, 'st_file_attributes', 0) & _REPARSE or stat.S_ISLNK(info.st_mode):
                raise Refused('ATTEMPT_REPARSE_POINT')
            if name.lower() == '.git':
                raise Refused('ATTEMPT_POLICY')
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise Refused('ATTEMPT_HARDLINK')


def require_patch_policy(patch, paths, limit=PATCH_LIMIT):
    if len(patch) > limit:
        raise Refused('ATTEMPT_PATCH_LIMIT')
    for path in paths:
        if any(part.lower().startswith('.git') for part in path.split('/')):
            raise Refused('ATTEMPT_POLICY')
    for line in patch.split(b'\n'):
        if line.startswith((b'old mode ', b'new mode ')) or line.startswith(b'new file mode 12') \
                or line.startswith(b'deleted file mode 12') or line.startswith(b'new file mode 16'):
            raise Refused('ATTEMPT_POLICY')


def require_applies(git, repository, sha, patch_file, index, hooks):
    """The patch applies to `sha` in the owner's repository, checked against a separate index only."""
    environment = _clean_environment(GIT_INDEX_FILE=str(index))
    options = ('-c', 'core.autocrlf=false', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath='+str(hooks))
    for args in (('read-tree', sha), ('apply', '--cached', '--check', '--binary', str(patch_file))):
        done = subprocess.run([git, '-C', str(repository), *options, *args],
                              capture_output=True, env=environment, timeout=GIT_TIMEOUT, check=False)
        if done.returncode:
            raise Refused('ATTEMPT_PATCH_DOES_NOT_APPLY')


@dataclass(frozen=True)
class Result:
    patch: Path
    sha256: str
    paths: tuple
    baseline_matches_source: bool


class Capture:
    """Baseline before the worker, patch after it. The shadow and results are owner-private."""

    def __init__(self, git, repository, sha, workspace, shadow_directory, results):
        self.git, self.repository, self.sha, self.workspace = git, Path(repository), sha, workspace
        self.shadow, self.results = Shadow(git, shadow_directory), Path(results)
        self.base = None

    def baseline(self):
        require_plain_tree(self.workspace.src)
        self.base = self.shadow.tree(self.workspace.src, 'base.index')
        source = subprocess.run([self.git, '-C', str(self.repository), 'rev-parse', self.sha+'^{tree}'],
                                capture_output=True, env=_clean_environment(), timeout=GIT_TIMEOUT, check=True)
        self.source_tree = source.stdout.decode('ascii').strip()
        return self.base

    def result(self, limit=PATCH_LIMIT):
        if self.base is None:
            raise Refused('ATTEMPT_BASELINE_MISSING')
        require_plain_tree(self.workspace.src)
        after = self.shadow.tree(self.workspace.src, 'after.index')
        names = self.shadow.run(self.workspace.src, 'after.index', 'diff', '--name-only', '-z', '--no-renames',
                                self.base, after)
        paths = tuple(sorted(p for p in names.decode('utf-8').split('\0') if p))
        patch = self.shadow.run(self.workspace.src, 'after.index', 'diff', '--binary', '--full-index',
                                '--no-renames', '--no-ext-diff', '--no-textconv', self.base, after)
        require_patch_policy(patch, paths, limit)
        self.results.mkdir(parents=True, exist_ok=True)
        target = self.results/(self.workspace.attempt+'.patch')
        with target.open('xb') as stream:
            stream.write(patch)
        require_applies(self.git, self.repository, self.sha, target, self.shadow.directory/'verify.index',
                        self.shadow.hooks)
        digest = hashlib.sha256(patch).hexdigest()
        result = Result(target, digest, paths, self.base == self.source_tree)
        (self.results/(self.workspace.attempt+'.json')).write_text(json.dumps({
            'attempt': self.workspace.attempt, 'source_sha': self.sha, 'sha256': digest, 'paths': list(paths),
            'base_tree': self.base, 'after_tree': after, 'baseline_matches_source': result.baseline_matches_source,
        }, indent=1), encoding='utf-8')
        return result
