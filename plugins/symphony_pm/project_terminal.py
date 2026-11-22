"""Project-scoped Windows command prompt inside an owner-configured MXC sandbox."""
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile

from agent.terminal_env_provider import TerminalEnvironmentProvider
from hermes_constants import get_hermes_home

from .transport.types import Refused


class ProjectTerminal(TerminalEnvironmentProvider):
    name = 'symphony_project_mxc'
    display_name = 'Symphony project terminal (Windows MXC)'
    env_description = 'Windows cmd.exe in the project directory of an MXC sandbox'

    def __init__(self, ctx):
        self.ctx = ctx
        self.profile = Path(get_hermes_home()).resolve()
        self.root = self.profile / 'plugin-data' / 'symphony_pm' / 'project-terminal'
        self.project = self.root / 'project'

    @property
    def default_cwd(self):
        return self.project.as_posix()

    def guard_tool(self, tool_name, args):
        # This opt-in profile has one path for general execution. The PM tool
        # retains its own authority checks; older profiles never register this hook.
        from .adapter import TOOL_NAME
        if tool_name == TOOL_NAME:
            return None
        if tool_name == 'terminal' and not (args.get('pty') or args.get('background')):
            from tools.terminal_scope import terminal_env
            if terminal_env('TERMINAL_ENV', 'local') == self.name and self.is_available():
                return None
        return {'action': 'block', 'message': 'PROJECT_TERMINAL_PROFILE_REQUIRED'}

    def config_bytes(self, shell, python):
        # Grant the shell runtime read access. The project is the sole writable subtree.
        def quote(path):
            return json.dumps(str(path), ensure_ascii=False)
        return ('default_permissions = "symphony-project"\napproval_policy = "never"\n'
                'web_search = "disabled"\n[windows]\nsandbox = "mxc"\n'
                '[shell_environment_policy]\ninherit = "core"\n[shell_environment_policy.set]\n'
                f'TMP = {quote(self.project / ".tmp")}\nTEMP = {quote(self.project / ".tmp")}\n'
                f'PATH = {quote(shell.parent)}\n'
                '[permissions.symphony-project.filesystem]\n":root" = "deny"\n":minimal" = "read"\n'
                f'{quote(self.root / "anchor")} = "read"\n{quote(shell)} = "read"\n'
                f'{quote(python.parent)} = "read"\n'
                f'{quote(self.project)} = "write"\n[permissions.symphony-project.network]\nenabled = false\n'
                ).encode()

    def _paths(self):
        if os.name != 'nt' or Path(get_hermes_home()).resolve() != self.profile:
            raise Refused('PROJECT_TERMINAL_PROFILE')
        settings = self.ctx.get_config('project_terminal', {})
        client, shell, python = (Path(settings[key]) for key in ('client', 'shell', 'python'))
        anchor = self.root / 'anchor'
        policy = self.root / 'codex' / 'config.toml'
        if (shell != Path(os.environ['SystemRoot']) / 'System32' / 'cmd.exe' or client.name.lower() != 'codex.exe'
                or python.name.lower() != 'python.exe' or python.parent == Path(python.anchor)
                or any(not path.is_absolute() or path.resolve() != path for path in (client, shell, python, self.project, anchor, policy))
                or not self.project.is_dir() or not anchor.is_dir() or any(anchor.iterdir())
                or not client.is_file() or not shell.is_file() or not python.is_file() or not policy.is_file()
                or client.is_relative_to(self.profile) or shell.is_relative_to(self.profile)
                or python.is_relative_to(self.profile) or self.profile.is_relative_to(python.parent)
                or client.is_relative_to(python.parent) or client.is_relative_to(shell.parent)):
            raise Refused('PROJECT_TERMINAL_BINDING')
        return settings, client, shell, python, policy

    def is_available(self):
        try:
            _, _, shell, python, policy = self._paths()
            return policy.read_bytes() == self.config_bytes(shell, python)
        except (OSError, KeyError, TypeError, ValueError, Refused):
            return False

    @contextmanager
    def qualified(self):
        from .transport.qualification import _open_denying_writers
        settings, client, shell, python, policy = self._paths()
        with ExitStack() as locks:
            for path, expected in ((client, settings['client_sha256']), (shell, settings['shell_sha256']),
                                   (python, settings['python_sha256']),
                                   (policy, hashlib.sha256(self.config_bytes(shell, python)).hexdigest())):
                handle = locks.enter_context(_open_denying_writers(str(path)))
                if hashlib.file_digest(handle, 'sha256').hexdigest() != expected:
                    raise Refused('PROJECT_TERMINAL_QUALIFICATION_CHANGED')
            yield client, shell, python

    def create_environment(self, *, cwd, **kwargs):
        if not self.is_available():
            raise Refused('PROJECT_TERMINAL_UNAVAILABLE')
        return MxcEnvironment(self)


class MxcEnvironment:
    uses_host_filesystem = False
    is_local = False

    def __init__(self, provider):
        self.provider = provider
        self.cwd = provider.default_cwd

    def execute(self, command, cwd='', *, timeout=None, stdin_data=None, **kwargs):
        from .transport.codex_client import CodexClientProvider
        profile = self.provider
        # Hermes may offer a desktop workspace as the initial cwd. Only paths in this
        # profile's project can be forwarded. Cycles and redirects fail before spawn.
        try:
            directory = (profile.project / (cwd or self.cwd)).resolve()
            if not directory.is_relative_to(profile.project) or not directory.is_dir():
                raise Refused('PROJECT_TERMINAL_CWD')
            duration = min(float(timeout if timeout is not None else 60), 300)
            if not 0 < duration <= 300:
                raise Refused('PROJECT_TERMINAL_TIMEOUT')
            with profile.qualified() as (client, shell, python), ExitStack() as stack:
                system = Path(os.environ['SystemRoot'])
                user = profile.root / 'user'
                env = dict(SYSTEMROOT=str(system), WINDIR=str(system), SYSTEMDRIVE=system.drive,
                           PATH=str(system / 'System32'), HOME=str(user), USERPROFILE=str(user),
                           APPDATA=str(user / 'appdata'), LOCALAPPDATA=str(user / 'local'),
                           TEMP=str(user / 'temp'), TMP=str(user / 'temp'), CODEX_HOME=str(profile.root / 'codex'))
                native = CodexClientProvider(validate_call=None, environment=env, sanitize=None, private_directory=user)
                streams = [stack.enter_context(tempfile.TemporaryFile(dir=user)) for _ in range(3)]
                streams[0].write((stdin_data or '').encode('utf-8'))
                streams[0].seek(0)
                anchor = profile.root / 'anchor'
                # Enter the project only inside MXC. Pass the native shell its command line
                # verbatim; the outer client's argv quoting is for an ordinary Windows program.
                entry = ('import subprocess,sys; shell,cwd,command=sys.argv[1:]; '
                         'raise SystemExit(subprocess.call(f\'"{shell}" /d /v:off /s /c "{command}"\', '
                         'executable=shell,cwd=cwd))')
                argv = [str(client), 'sandbox', '-C', str(anchor), '-P', 'symphony-project', '--',
                        str(python), '-I', '-S', '-c', entry, str(shell), str(directory), command]
                try:
                    code, expired = native.run_unbound(argv, str(anchor), duration, streams)
                except Exception as exc:  # health: allow BLE001 -- prevent core retry after an uncertain native call
                    return dict(output=f'Project terminal outcome unknown ({type(exc).__name__}).', returncode=125)
                output = []
                for stream in streams[1:]:
                    stream.seek(0)
                    output.append(stream.read(131073))
                data = b'\n'.join(chunk[:131072] for chunk in output if chunk)
                try:
                    text = data.decode('utf-8')
                except UnicodeDecodeError:
                    text = data.decode('oem', 'replace')
                return dict(output=text, returncode=124 if expired else code, hermes_timed_out=expired)
        except (Refused, OSError, RuntimeError, KeyError, TypeError, ValueError) as exc:
            # Return a terminal result. The caller retries exceptions as transient transport errors.
            return dict(output=f'Project terminal unavailable ({exc}).', returncode=125)

    def cleanup(self):
        """Each synchronous execution closes its native Job before returning."""
