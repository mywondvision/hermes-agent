"""Explicit assembly of the Codex client transport. Never registered automatically.

The client observer accounts for MXC members via the owned Job. The worker transport retains its
per-process observer. Constructing the declaration checks the qualification and starts nothing.
"""
from dataclasses import dataclass
from pathlib import Path

from hermes_cli.plugin_request_binding import canonical_home
from hermes_dir_acl import make_user_dir
from .bootstrap import TransportBootstrap
from .codex_client import CodexClientProvider
from .codex_exec_launcher import CodexExecLauncher
from .types import Refused, digest
from .windows_client_observer import WindowsClientJobObserver
from .windows_ownership import WindowsOwnershipGuard


@dataclass(frozen=True)
class CodexClientFactory:
    profile_home: str
    namespace: str
    launcher: CodexExecLauncher

    def __call__(self, scope):
        if canonical_home(scope.profile_home) != self.profile_home or scope.namespace != self.namespace:
            raise Refused('NATIVE_PROFILE_BINDING')
        directory = Path(scope.profile_home)/'plugin-data'/'symphony_pm'/digest(scope.namespace)[:32]/'capture'
        make_user_dir(directory, mode=0o700, parents=True, exist_ok=True)
        return CodexClientProvider(validate_call=self.launcher.validate_call, environment=self.launcher.environment,
                                   sanitize=self.launcher.sanitize, private_directory=directory)


def codex_exec_transport(*, profile_home, namespace, launcher, approval_transport, scheduler_id, controller,
                         trusted_issuers, migrate_v1=False):
    """The host supplies a qualified launcher and an exact approval source; any mismatch refuses."""
    if type(launcher) is not CodexExecLauncher:
        raise Refused('CODEX_QUALIFICATION_RECORD')
    q = launcher.qualification
    home = canonical_home(profile_home)
    if (home != q.profile_home or namespace != q.namespace or scheduler_id != q.scheduler_id
            or tuple(trusted_issuers) != q.issuer_refs):
        raise Refused('QUALIFICATION_BINDING')
    q.verify_executable()
    q.verify_config()
    return TransportBootstrap(WindowsOwnershipGuard, WindowsClientJobObserver, CodexClientFactory(home, namespace, launcher),
                              launcher, approval_transport, scheduler_id, controller, tuple(trusted_issuers), migrate_v1)
