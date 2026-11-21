"""Select once at startup of a dedicated Linux profile; declaring a transport starts no service."""
from dataclasses import dataclass

from hermes_cli.plugin_request_binding import canonical_home
from .bootstrap import _Resources, serve_owner
from .linux_control import LinuxObserver
from .linux_host import LinuxOwnershipGuard, SystemdManager
from .linux_launcher import LinuxLauncher, LinuxSettings
from .linux_native import LinuxNativeProvider
from .linux_sandbox import LinuxQualification
from .types import Refused


@dataclass(frozen=True)
class LinuxTransportBootstrap:
    launcher: LinuxLauncher
    approval_transport: object
    controller: str

    def __call__(self, scope):
        from .linux_lifecycle import LinuxLifecycleOwner

        q, settings = self.launcher.qualification, self.launcher.settings
        if (scope.namespace != 'symphony_pm:ingress-linux' or scope.profile_home != q.profile_home
                or scope.namespace != q.namespace):
            raise Refused('LINUX_PROFILE_BINDING')
        q.verify()
        guard = LinuxOwnershipGuard(scope, settings.state, q.installation_id)
        resources = _Resources(guard, None)
        scope.on_close(resources.close)
        manager = SystemdManager(q, settings.state)
        observer = LinuxObserver(settings.state, manager, guard.recovery_host_id)
        resources.observer = observer
        provider = LinuxNativeProvider(self.launcher, observer)
        owner = LinuxLifecycleOwner(scope, guard, observer, provider, self.launcher,
                                    scheduler_id=q.scheduler_id, controller=self.controller)
        resources.owner = owner
        return serve_owner(owner, self.approval_transport, q.issuer_refs)


def linux_exec_transport(*, profile_home, namespace, qualification, settings, approval_transport,
                         scheduler_id, controller, trusted_issuers, owner_check=None, provider_evidence_sha256=None):
    if namespace != 'symphony_pm:ingress-linux':
        raise Refused('LINUX_PROFILE_BINDING')
    if type(qualification) is not LinuxQualification or type(settings) is not LinuxSettings:
        raise Refused('LINUX_QUALIFICATION')
    launcher = LinuxLauncher(qualification, settings, owner_check=owner_check,
                             provider_evidence_sha256=provider_evidence_sha256)
    if (canonical_home(profile_home), namespace, scheduler_id, tuple(trusted_issuers)) != (
            qualification.profile_home, qualification.namespace, qualification.scheduler_id, qualification.issuer_refs):
        raise Refused('LINUX_PROFILE_BINDING')
    return LinuxTransportBootstrap(launcher, approval_transport, controller)
