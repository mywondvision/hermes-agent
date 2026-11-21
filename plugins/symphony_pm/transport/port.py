"""Existing PM Protocol projected from one durable lifecycle owner, never a queue."""
from plugins.symphony_pm import adapter as pm
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
            ticket, fresh = self.owner.submit(request, context, background=True)
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
            ticket, _, body = self.owner.control_target(request, context)
            if body is None:
                raise Refused('STATUS_OBSERVATION_REQUIRED')
            view = body['view']
        except Refused as exc:
            return pm.PmStatusReceipt(**common, job_ref=request.job_ref,
                                     job_revision=request.expected_job_revision, attempt_ref='',
                                     phase='UNKNOWN', uncertainty=(str(exc),))
        common['observed_at'] = view['observed_at']
        return pm.PmStatusReceipt(**common, job_ref=ticket.job, job_revision=ticket.revision,
                                 attempt_ref=ticket.attempt, phase=view['phase'], evidence_refs=(view['evidence_ref'],),
                                 retention=tuple(map(tuple, view['retention'])), uncertainty=tuple(view['uncertainty']))

    def request_cancel(self, request, context):
        common = self._common(request, context)
        try:
            with self.owner.lock:
                ticket, _, body = self.owner.control_target(request, context)
                result = self.owner.stop_by_owner(ticket.project, ticket.job, ticket.revision,
                                                  request_id=request.request_id, detail=request.reason)
        except Refused as exc:
            return pm.PmCancelReceipt(**common, job_ref=request.job_ref,
                                     job_revision=request.expected_job_revision, outcome='UNKNOWN',
                                     reservation_state='POSSIBLY_CONSUMED', uncertainty=(str(exc),))
        # A query-only local-stop proof is not an acknowledgement of a cancel
        # effect. Keep the shared receipt UNKNOWN unless this call got an ACK.
        outcome = result.state if result.state in ('CANCEL_REQUESTED', 'ALREADY_REQUESTED') else 'UNKNOWN'
        claims = ('worker_stopped', 'descendants_stopped') if body and body['job_empty'] else ()
        return pm.PmCancelReceipt(**common, job_ref=ticket.job, job_revision=ticket.revision,
                                 outcome=outcome, reservation_state='POSSIBLY_CONSUMED',
                                 claims=claims, evidence_refs=(body['view']['evidence_ref'],) if claims else (),
                                 retention=(('reservation', result.reservation),),
                                 uncertainty=('EXTERNAL_AND_BILLING_UNKNOWN',))
