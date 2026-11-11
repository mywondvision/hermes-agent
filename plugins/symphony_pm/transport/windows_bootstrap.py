"""Explicit assembly for the existing host lifecycle. Never registered automatically."""
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from hermes_cli.plugin_request_binding import canonical_home
from hermes_dir_acl import make_user_dir
from .bootstrap import TransportBootstrap
from .qualification import QualificationRecord, environment_digest
from .types import Refused, digest
from .windows_observer import WindowsQueryObserver
from .windows_ownership import WindowsOwnershipGuard
from .windows_receiver import WindowsNativeProvider


@dataclass(frozen=True)
class WindowsNativeFactory:
    profile_home: str
    namespace: str
    launcher: object
    environment: object
    command_result: object
    sanitize: object

    def __call__(self, scope):
        if canonical_home(scope.profile_home) != self.profile_home or scope.namespace != self.namespace:
            raise Refused('NATIVE_PROFILE_BINDING')
        directory = Path(scope.profile_home)/'plugin-data'/'symphony_pm'/digest(scope.namespace)[:32]/'capture'
        make_user_dir(directory, mode=0o700, parents=True, exist_ok=True)
        return WindowsNativeProvider(validate_call=self.launcher.validate_call,
            environment=self.environment, command_result=self.command_result,
            sanitize=self.sanitize, private_directory=directory)


def windows_transport(*, profile_home, namespace, launcher, environment, command_result, sanitize,
                      approval_transport, scheduler_id, controller, trusted_issuers, qualification):
    """Host must supply qualified kit objects and an exact approval source.

    environment is the owning profile's explicit child environment, never ambient
    os.environ. sanitize is kit's redact-then-clip function; command_result is its
    existing CommandResult. qualification names the exact driver build, executable,
    environment and issuers; any mismatch refuses. Constructing this declaration starts nothing.
    """
    if type(qualification) is not QualificationRecord:
        raise Refused('QUALIFICATION_RECORD')
    qualification.validate()
    home = canonical_home(profile_home)
    if (home != qualification.profile_home or namespace != qualification.namespace
            or scheduler_id != qualification.scheduler_id
            or tuple(trusted_issuers) != qualification.issuer_refs
            or launcher.qualification != qualification
            or environment_digest(environment) != qualification.environment_sha256):
        raise Refused('QUALIFICATION_BINDING')
    qualification.verify_executable()
    native = WindowsNativeFactory(home, namespace, launcher, MappingProxyType(dict(environment)), command_result, sanitize)
    return TransportBootstrap(WindowsOwnershipGuard, WindowsQueryObserver, native,
                              launcher, approval_transport, scheduler_id, controller, tuple(trusted_issuers))
