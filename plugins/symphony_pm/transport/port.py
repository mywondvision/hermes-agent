"""Existing PM Protocol projected from one durable lifecycle owner, never a queue."""
from tools import symphony_pm_adapter as pm
from .types import Refused


class ServingPort:
    def __init__(self, owner):
        self.owner = owner

    def _common(self, request, context):
        # Adapter contract uses the request's logical tick; OS readbacks retain
        # their independent wall timestamps and are never minted from this field.
        return dict(contract_version=pm.CONTRACT_VERSION, contract_digest=context.contract_digest,
                    request_id=request.request_id, payload_digest=request.payload_digest,
                    project_ref=request.project_ref, issuer_ref=context.issuer_ref,
                    scheduler_id=self.owner.scheduler_id, scheduler_epoch=context.scheduler_epoch,
                    observed_at=context.now)

    def submit_intent(self, request, context):
        common = self._common(request, context)
        try:
            ticket, fresh = self.owner.submit(request, context)
        except Refused as exc:
            reason = str(exc)
            outcome = 'CONFLICT' if reason == 'CONFLICT' else 'UNKNOWN'
            return pm.PmSubmissionReceipt(**common, outcome=outcome, uncertainty=(reason,))
        except Exception:  # health: allow BLE001 -- launcher failures never justify another launch ticket
            return pm.PmSubmissionReceipt(**common, outcome='UNKNOWN', uncertainty=('LAUNCH_OUTCOME_UNKNOWN',))
        return pm.PmSubmissionReceipt(**common, outcome='SUBMITTED' if fresh else 'DUPLICATE',
                                      job_ref=ticket.job, job_revision=ticket.revision)

    def get_status(self, request, context):
        common = self._common(request, context)
        try:
            binding = self.owner.control_target(request, context)
            phase, evidence = self.owner.status(binding)
        except Refused as exc:
            return pm.PmStatusReceipt(**common, job_ref=request.job_ref,
                                     job_revision=request.expected_job_revision, attempt_ref='',
                                     phase='UNKNOWN', uncertainty=(str(exc),))
        return pm.PmStatusReceipt(**common, job_ref=binding.ticket.job, job_revision=binding.ticket.revision,
                                 attempt_ref=binding.ticket.attempt, phase=phase,
                                 evidence_refs=evidence,
                                 retention=(('reservation', 'POSSIBLY_CONSUMED_HELD'),),
                                 uncertainty=('EXTERNAL_AND_BILLING_UNKNOWN',))

    def request_cancel(self, request, context):
        common = self._common(request, context)
        try:
            binding = self.owner.control_target(request, context)
            result = self.owner.cancel_internal(binding, 'PM_CANCEL', request_id=request.request_id,
                                                detail=request.reason)
        except Refused as exc:
            return pm.PmCancelReceipt(**common, job_ref=request.job_ref,
                                     job_revision=request.expected_job_revision, outcome='UNKNOWN',
                                     reservation_state='POSSIBLY_CONSUMED', uncertainty=(str(exc),))
        # A query-only local-stop proof is not an acknowledgement of a cancel
        # effect. Keep the shared receipt UNKNOWN unless this call got an ACK.
        outcome = 'CANCEL_REQUESTED' if result.dispatched else 'UNKNOWN'
        return pm.PmCancelReceipt(**common, job_ref=binding.ticket.job, job_revision=binding.ticket.revision,
                                 outcome=outcome, reservation_state='POSSIBLY_CONSUMED',
                                 retention=(('reservation', result.reservation),),
                                 uncertainty=('EXTERNAL_AND_BILLING_UNKNOWN',))
