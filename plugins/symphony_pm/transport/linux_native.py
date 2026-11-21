"""Systemd attempt provider. Native side effects have one entry and a fenced, one-use file gate."""
from dataclasses import dataclass
import os
from pathlib import Path
import time

from .linux_control import message, publish, wait_message
from .linux_host import private_directory, process_identity
from .linux_identity import CONTRACT, unit_for
from .linux_sandbox import verify_report
from .types import Refused


def owner_network_namespace():
    return os.readlink('/proc/self/ns/net')


@dataclass(frozen=True)
class LinuxResult:
    returncode: int
    timed_out: bool


class LinuxReceiver:
    def __init__(self, observer, binding, launcher, *, call=None):
        self.observer, self.binding, self.launcher, self.call = observer, binding, launcher, call
        self.directory = observer.directory(binding.ticket)
        self._authorize_exec = None

    def check_binding(self, binding):
        if binding != self.binding:
            raise Refused('LINUX_RECEIVER_BINDING')
        os.close(self.observer._open(binding))

    def resume(self, stdin_text=None, *, authorize):
        if self.call is None or self.call.prompt != stdin_text or self._authorize_exec is not None:
            raise Refused('LINUX_RESUME_WITHHELD')
        self.check_binding(self.binding)
        self.launcher.qualification.verify()
        authorize(lambda: publish(self.directory, 'probe', self.binding.invocation_id))
        self._authorize_exec = authorize

    def _precheck(self, deadline):
        authorize, self._authorize_exec = self._authorize_exec, None
        if authorize is None:
            raise Refused('LINUX_RESUME_WITHHELD')
        probe_deadline = min(deadline, time.monotonic() + self.call.probe_timeout)
        while not (self.directory/'probe-exit').exists():
            if (self.directory/'cancel').exists() or (self.directory/'exit').exists():
                return
            remaining = probe_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('LINUX_PRECHECK_TIMEOUT')
            time.sleep(min(remaining, 0.04))
        if (self.directory/'cancel').exists():
            return
        code = message(self.directory, 'probe-exit')
        if code != 0 or (self.directory/'probe-output').stat().st_size > 65536:
            raise Refused('LINUX_PRECHECK_REFUSED')
        report_hash = verify_report(message(self.directory, 'probe-output'), self.launcher.qualification,
                                    self.call.challenge, owner_network_namespace())
        publish(self.directory, 'probe-receipt', {'sha256': report_hash,
                'qualification': self.launcher.qualification.fingerprint})
        self.check_binding(self.binding)
        self.launcher.qualification.verify()
        authorize(lambda: publish(self.directory, 'exec', self.binding.invocation_id))

    def cancel_once(self, binding, *, authorize):
        self.check_binding(binding)
        def close_launch():
            publish(self.directory, 'cancel', binding.invocation_id)
            return True
        return authorize(close_launch)

    def wait_result(self, argv, timeout, timed_out=False):
        if self.call is None or tuple(argv) != self.call.argv:
            raise Refused('LINUX_RESULT_TARGET')
        deadline = time.monotonic() + timeout
        if not timed_out:
            self._precheck(deadline)
        code = wait_message(self.directory, 'exit', deadline)
        if type(code) is not int or not -127 <= code <= 255:
            raise Refused('LINUX_RESULT_UNKNOWN')
        return LinuxResult(code, timed_out)

    def close_fail_safe(self, deadline):
        # A local receiver owns no live FD. The service has its own finite RuntimeMaxSec,
        # KillMode=control-group and non-restarting template, including if the writer dies.
        self._authorize_exec = None
        self.call = None


class LinuxNativeProvider:
    ownership_contract = CONTRACT

    def __init__(self, launcher, observer):
        self.launcher, self.observer = launcher, observer

    def validate_driver_call(self, ticket, argv, cwd, timeout, stdin_text):
        self.launcher.validate_call(ticket, argv, cwd, timeout, stdin_text)

    def create_suspended(self, ticket, argv, cwd, stdin_text):
        call = self.launcher.lookup(ticket)
        self.launcher.validate_call(ticket, argv, cwd, call.timeout, stdin_text)
        q = self.launcher.qualification
        self.launcher.admission()
        directory = self.observer.directory(ticket)
        directory.mkdir(mode=0o700, exist_ok=False)
        private_directory(directory)
        with (directory/'stdin').open('xb') as stream:
            stream.write(call.prompt.encode('utf-8'))
        publish(directory, 'spec', {'runtime': q.runtime, 'bwrap': q.bwrap,
                                    'workspace': call.workspace, 'argv': call.argv,
                                    'probe_argv': call.probe_argv, 'probe_timeout': call.probe_timeout})
        self.observer.manager.start(unit_for(ticket))
        wait_message(directory, 'ready', time.monotonic() + 15)
        binding = self.observer.identity(ticket)
        receiver = LinuxReceiver(self.observer, binding, self.launcher, call=call)
        receiver.check_binding(binding)
        if (process_identity(binding.root.pid) != binding.root
                or '0::'+binding.cgroup_path not in os.fsdecode(Path(f'/proc/{binding.root.pid}/cgroup').read_bytes()).splitlines()):
            raise Refused('LINUX_ROOT_MEMBERSHIP')
        return receiver

    def recover_receiver(self, binding):
        receiver = LinuxReceiver(self.observer, binding, self.launcher)
        receiver.check_binding(binding)
        return receiver
