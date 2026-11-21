"""Single-writer Linux lifecycle, including query-only takeover of a surviving service."""
from collections import Counter
from dataclasses import asdict
import json

from .lifecycle import LifecycleOwner, ProcessReadback
from .linux_identity import CONTRACT, LinuxBinding, LinuxStopReadback
from .linux_journal import LinuxJournal
from .types import Refused, finite


class LinuxLifecycleOwner(LifecycleOwner):
    journal_type = LinuxJournal
    binding_type = LinuxBinding

    def __init__(self, *args, **kwargs):
        self._submitting = Counter()
        super().__init__(*args, **kwargs)
        self.journal.clock = self.clock
        self.journal.check_slot = getattr(self.launcher, 'require_check_slot_clear', None)
        # The service host activates scope only after its factory returns.
        self._reconcile_owned()

    def _coverage_verified(self):
        return (self.native.ownership_contract == CONTRACT
                and callable(getattr(self.launcher, 'job_coverage_for', None))
                and self.launcher.job_coverage_for(self.guard) is True)

    def _settle_workspaces(self):
        for ticket in self.journal.unsettled_workspaces(self.token):
            if ticket.request_digest in self._submitting:
                continue
            _, row = self.journal.read_launch(self.token, ticket.project, ticket.job, ticket.revision)
            if (row['state'] == 'NOT_STARTED' and self.launcher.never_created_workspace_is_clear(ticket)
                    or self.launcher.settle_workspace(ticket)):
                self.journal.workspace_settled(self.token, ticket)

    def replace_observer(self, observer):
        with self.lock:
            try:
                super().replace_observer(observer)
            finally:
                if self.observer is observer:
                    self.native.observer = observer
                    for receiver in self._receivers.values():
                        receiver.observer = observer

    def status_row(self, ticket, row):
        if row['state'] == 'HELD_UNKNOWN' and row['binding']:
            self.reconcile()
            return self.journal.read_launch(self.token, ticket.project, ticket.job, ticket.revision)[1]
        return row

    def status(self, binding):
        self.reconcile()
        return super().status(binding)

    def _admit_submit(self, request, context):
        super()._admit_submit(request, context)
        self.launcher.require_project(request.project_ref)
        self.reconcile()
        self.launcher.admission()

    def execute_reserved(self, token, ticket, request):
        with self.lock:
            self._submitting[request.request_digest] += 1
        try:
            return super().execute_reserved(token, ticket, request)
        finally:
            with self.lock:
                self._submitting[request.request_digest] -= 1
                if not self._submitting[request.request_digest]:
                    del self._submitting[request.request_digest]

    def _bindings(self):
        try:
            bindings = super()._bindings()
        except (AttributeError, KeyError, TypeError, ValueError, Refused) as exc:
            self.observer_lost()
            raise Refused('OBSERVER_COVERAGE_UNKNOWN') from exc
        if any((b.host_id, b.boot_id) != (self.guard.recovery_host_id, self.guard.identity.boot_id)
               for b in bindings):
            self.observer_lost()
            raise Refused('OBSERVER_COVERAGE_UNKNOWN')
        return bindings

    def check_policy(self, ticket, row, patch, check):
        from .linux_check_host import LinuxCheckHost, LinuxCheckProfile, LinuxOwnerCheck
        from .linux_check import saved_commands
        checker = self.launcher.owner_check
        if (type(checker) is not LinuxOwnerCheck or type(checker.profile) is not LinuxCheckProfile
                or not isinstance(checker.host, LinuxCheckHost)):
            raise Refused('QUALIFIED_CHECKER_REQUIRED')
        profile = checker.profile
        profile.validate()
        epoch = json.loads(row['proof_source'])['epoch']
        return profile.commands, (profile.launch_fingerprint == self.launcher.qualification.fingerprint
                and check.get('revisions') == {'profile': profile.fingerprint, 'evidence': profile.evidence_sha256}
                and 'plan_sha256' not in check and saved_commands(self.launcher, ticket, epoch, patch, check))

    def _finished_client_job(self, row):
        return self.journal.proven_stop(row)

    def _job_accounting(self, binding):
        if not self._observer_available or not self._coverage_verified():
            raise Refused('OBSERVER_COVERAGE_UNKNOWN')
        proof = self.observer.read(binding, self.token.epoch, self.generation)
        now = self.clock()
        effect = self.journal.effect(self.token, binding)
        if (type(proof) is not LinuxStopReadback or proof.binding != binding
                or proof.source != 'linux-cgroup.events/1' or type(proof.epoch) is not int
                or proof.epoch != self.token.epoch or type(proof.generation) is not int
                or proof.generation != self.generation or not finite(proof.observed_at)
                or not 0 <= now-proof.observed_at <= 30 or (effect and proof.observed_at < effect['cancel_at'])
                or type(proof.launch_closed) is not bool or type(proof.populated) is not int
                or proof.populated not in (0, 1) or not proof.processes or not all(
                    type(p) is ProcessReadback and p.identity.valid() and type(p.signaled) is bool
                    for p in proof.processes)):
            raise Refused('LINUX_READBACK_UNKNOWN')
        required = set(binding.known_processes) | set(self.journal.known_members(self.token, binding))
        observed = {p.identity for p in proof.processes}
        if not required <= observed or len(observed) != len(proof.processes):
            raise Refused('LINUX_PROCESS_COVERAGE')
        self.journal.record_members(self.token, binding, tuple(observed))
        self.journal.save_readback(self.token, binding, self.generation, now, asdict(proof))
        if proof.launch_closed and proof.populated == 0 and all(p.signaled for p in proof.processes):
            return 'STOPPED'
        return 'RUNNING' if proof.populated == 1 else 'HELD_UNKNOWN'

    def reconcile(self):
        """Only a fresh read of the original payload can settle a spent launch on this boot."""
        with self.lock:
            self._check()
            self._reconcile_owned()

    def _reconcile_owned(self):
        self.guard.assert_owned()
        for row in self.journal.unfinished(self.token):
            if row['state'] != 'HELD_UNKNOWN' or row['ownership_contract'] != CONTRACT or not row['binding']:
                continue
            try:
                self._refresh_observer()
                binding = self.journal.decode_binding(json.loads(row['binding']))
                if self._local_stop(binding):
                    self.journal.finish_job_stop(self.token, binding.ticket, self.generation, stopped=True)
                    self._receivers.pop(binding.ticket.attempt, None)
                    continue
                if binding.ticket.attempt not in self._receivers:
                    receiver = self.native.recover_receiver(binding)
                    if receiver.binding == binding:
                        self._receivers[binding.ticket.attempt] = receiver
            except (OSError, TimeoutError, ValueError, TypeError, KeyError, Refused):
                # Loss of the controller, a recycled unit or an unknown binding keeps its slot.
                continue
        reconcile_check = getattr(self.launcher, 'reconcile_check', None)
        if reconcile_check is not None:
            reconcile_check(self)
        self._settle_workspaces()
