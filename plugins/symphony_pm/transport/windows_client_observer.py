"""Query a Codex client's unnamed Job without opening MXC member processes."""
from contextlib import ExitStack

from .lifecycle import ProcessReadback
from .types import Refused, job_object_name
from .windows_observer import WindowsQueryObserver


class WindowsClientJobObserver(WindowsQueryObserver):
    def _query(self, target):
        binding = target.binding
        binding.validate()
        if binding.job_object != job_object_name(binding.ticket):
            raise Refused('JOB_IDENTITY_UNKNOWN')
        with ExitStack() as handles:
            job = self.api.open_job(binding.job_object)
            handles.callback(self.api.close, job)
            records = []
            for identity in target.known_processes:
                process = self.api.open_process(identity.pid)
                handles.callback(self.api.close, process)
                if self.api.creation(process) != identity.creation:
                    raise Refused('PROCESS_IDENTITY_CHANGED')
                signaled = self.api.wait(process)
                # The suspended root was verified in this very Job before resume. A reaped
                # process may no longer report membership; its retained identity must still match.
                if identity == binding.root and not (signaled and binding in self.covered):
                    if not self.api.in_job(process, job):
                        raise Refused('JOB_IDENTITY_UNKNOWN')
                records.append(ProcessReadback(identity, signaled))
            active, pids = self.api.snapshot(job)
            return active, pids, tuple(records)
