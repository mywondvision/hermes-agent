"""Owner-issued MXC qualification for checking a captured patch without a model."""
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
from threading import Lock
from weakref import WeakValueDictionary

from .qualification import _open_denying_writers
from .types import Refused, digest

PROFILE = 'symphony-owner-check'
_WORKSPACE_LOCKS = WeakValueDictionary()
_WORKSPACE_REGISTRY_LOCK = Lock()


@dataclass(frozen=True)
class CheckCommand:
    name: str
    tool: str
    arguments: tuple
    requires_git: bool = False


DIFF_CHECK = CheckCommand("git-diff-check", "git", ("diff", "--check", "<source_sha>", "<candidate_sha>"), True)


def check_plan_fingerprint(profile, commands):
    """Bind the ordered executable plan to the exact owner qualification used to run it."""
    if not isinstance(profile, OwnerCheckProfile):
        raise Refused('CHECK_PROFILE_MISSING')
    names = set()
    for command in commands:
        if (not isinstance(command, CheckCommand) or not isinstance(command.name, str)
                or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,80}', command.name)
                or command.name in names or type(command.requires_git) is not bool
                or (command.tool not in ('python', 'ruff') and command != DIFF_CHECK) or type(command.arguments) is not tuple
                or not all(isinstance(arg, str) for arg in command.arguments)):
            raise Refused('CHECK_COMMAND_POLICY')
        names.add(command.name)
    return digest({'profile': PROFILE, 'revisions': profile.revisions(),
                   'paths': [str(p) for p in (profile.root, profile.client, profile.python, profile.ruff, profile.git)],
                   'commands': [asdict(command) for command in commands]})


@dataclass(frozen=True)
class OwnerCheckProfile:
    """The home, empty client working directory and sole writable directory belong to this profile."""
    root: Path
    client: Path
    python: Path
    ruff: Path
    git: Path
    client_sha256: str
    python_sha256: str
    ruff_sha256: str
    git_sha256: str
    config_sha256: str

    @property
    def home(self):
        return self.root/'codex'

    @property
    def active(self):
        return self.root/'active'

    @property
    def anchor(self):
        return self.root/'cwd'

    def config_bytes(self):
        """The effective policy has a single writable directory. Project config is never a client cwd."""
        def quoted(value):
            return json.dumps(str(value), ensure_ascii=False)
        permissions = {
            ':root': 'deny', ':minimal': 'read', self.anchor: 'read', self.active: 'write',
            self.python.parent: 'read', self.ruff: 'read',
        }
        entries = [f'{quoted(path)} = {quoted(access)}' for path, access in permissions.items()]
        lines = [
            f'default_permissions = "{PROFILE}"', 'approval_policy = "never"', 'web_search = "disabled"',
            '[windows]', 'sandbox = "mxc"', '[shell_environment_policy]', 'inherit = "core"',
            'ignore_default_excludes = false', '[shell_environment_policy.set]',
            f'TEMP = {quoted(self.active/"temp")}', f'TMP = {quoted(self.active/"temp")}',
            'PYTHONDONTWRITEBYTECODE = "1"', 'PYTHONUTF8 = "1"',
            f'[permissions.{PROFILE}.filesystem]', *entries,
            f'[permissions.{PROFILE}.network]', 'enabled = false',
        ]
        return ('\n'.join(lines)+'\n').encode('utf-8')

    def environment(self, source):
        """Never forward a caller's PATH, home, Python settings or arbitrary environment names."""
        selected = {k.upper(): v for k, v in source.items() if k.upper() in ('SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE')}
        if 'SYSTEMROOT' not in selected:
            raise Refused('CHECK_SYSTEM_ROOT')
        selected.setdefault('SYSTEMDRIVE', Path(selected['SYSTEMROOT']).drive)
        user = self.root/'user'
        selected.update(PATH=str(Path(selected['SYSTEMROOT'])/'System32'), CODEX_HOME=str(self.home),
                        HOME=str(user), USERPROFILE=str(user), APPDATA=str(user/'appdata'),
                        LOCALAPPDATA=str(user/'local'), TMP=str(user/'temp'), TEMP=str(user/'temp'))
        return selected

    @contextmanager
    def qualified(self, git, *, outside, workspace=True):
        pins = [(self.client, self.client_sha256), (self.python, self.python_sha256),
                (self.ruff, self.ruff_sha256), (self.git, self.git_sha256),
                (self.home/'config.toml', self.config_sha256)]
        if (Path(git) != self.git or not self.anchor.is_dir() or any(self.anchor.iterdir())
                or self.root.resolve() != self.root or self.anchor.resolve() != self.anchor
                or self.home.resolve() != self.home or not self.root.is_absolute()
                or self.config_sha256 != hashlib.sha256(self.config_bytes()).hexdigest()
                or any(p.resolve() != p or not p.is_absolute() or p.is_relative_to(self.active) for p, _ in pins)
                or any(self.root.is_relative_to(p) or p.is_relative_to(self.root) for p in outside)):
            raise Refused('CHECK_PROFILE_BINDING')
        with ExitStack() as held:
            if workspace:
                # Distinct profile objects for this single-owner workspace share the same lane.
                with _WORKSPACE_REGISTRY_LOCK:
                    lane = _WORKSPACE_LOCKS.setdefault(self.root, Lock())
                held.enter_context(lane)
                if self.active.exists():
                    raise Refused('CHECK_WORKSPACE_UNSETTLED')
            for path, expected in pins:
                stream = held.enter_context(_open_denying_writers(str(path)))
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    raise Refused('CHECK_QUALIFICATION_CHANGED')
            yield

    def revisions(self):
        return {'profile': self.config_sha256, 'client': self.client_sha256, 'python': self.python_sha256,
                'ruff': self.ruff_sha256, 'git': self.git_sha256}
