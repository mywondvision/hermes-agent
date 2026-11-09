"""Native permits from the existing host-owned, correlated human approval transport.

No approval hooks, yolo flags, model text or configuration booleans mint grants.
The caller must be the owning host and explicitly choose an already registered
transport. Nothing here discovers a recipient, installs a transport or sends audio.
"""
from __future__ import annotations

from dataclasses import asdict
import time

from hermes_cli.approval_transport import (
    ApprovalRequest, RegisteredApprovalTransport, invoke_approval_transport,
)
from hermes_cli.plugin_request_binding import AuthorityGrant, canonical_home
from tools import symphony_pm_adapter as pm
from .types import Permit, Refused, digest


class ApprovalSource:
    def __init__(self, owner, transport: RegisteredApprovalTransport, *, scheduler_id, clock=time.time):
        if (type(transport) is not RegisteredApprovalTransport
                or canonical_home(transport.profile_home) != owner.journal.home
                or not callable(transport.present) or not scheduler_id):
            raise Refused('PROFILE_APPROVAL_TRANSPORT_REQUIRED')
        self.owner, self.transport = owner, transport
        self.scheduler_id, self.clock = scheduler_id, clock

    def request_permit(self, request, permit: Permit, *, is_interrupted=None):
        """Explicit host action, never called by authority_source or by the model tool.

        One exact execution plan is displayed through the existing selected human
        channel. Permission to develop/test this code cannot reach this method.
        """
        now = self.clock()
        permit.validate(now)
        if (permit.identity.profile_home != self.owner.journal.home
                or permit.identity.namespace != self.owner.journal.namespace
                or permit.identity.request_id != request.request_id
                or permit.identity.payload_digest != request.payload_digest
                or permit.request_digest != request.request_digest
                or permit.project_ref != request.project_ref or permit.operation != request.operation):
            raise Refused('APPROVAL_REQUEST_BINDING')
        token = self.owner.token  # stale controller after a human wait cannot store a grant
        command = pm.canonical_bytes({
            'intent': dict(request.payload(), request_id=request.request_id),
            'profile_home': permit.identity.profile_home, 'session_id': permit.identity.session_id,
            'permit_ref': permit.ref, 'contract_digest': permit.contract_digest,
            'policy_ref': permit.policy_ref, 'scope_revision': permit.scope_revision,
            'expires_at': permit.expires_at, 'native_plan': asdict(permit.plan),
        }).decode('utf-8')
        approval = ApprovalRequest.create(
            command=command, description='One exact Symphony request and native execution plan',
            pattern_key='symphony-native:'+permit.binding_digest, pattern_keys=(),
            session_key=permit.identity.session_id, surface='symphony_pm',
            allow_session=False, allow_permanent=False,
            timeout_seconds=min(300.0, permit.expires_at-now))
        result = invoke_approval_transport(
            self.transport.present, approval, timeout_seconds=approval.timeout_seconds,
            is_interrupted=is_interrupted)
        if result.failure is not None or result.choice != 'once':
            return False
        self.owner.record_permit(token, permit, approval.request_id, approval.digest, self.clock())
        return True

    def __call__(self, identity):
        """Read a previously granted exact permit; never prompt, approve or retry."""
        try:
            token, permit = self.owner.find_permit(identity, self.clock())
        except Refused:
            return None
        now = self.clock()
        if now >= permit.expires_at:
            return None
        context = pm.TrustedPmContextV1(
            context_ref='context-'+digest(permit.ref)[:40], issuer_ref=permit.issuer_ref,
            project_ref=permit.project_ref, allowed_operations=(permit.operation,),
            request_id=permit.identity.request_id, policy_ref=permit.policy_ref,
            grant_ref=permit.ref, expires_at=permit.expires_at, now=now,
            max_age_seconds=30.0, scheduler_id=self.scheduler_id, scheduler_epoch=token.epoch,
            contract_digest=permit.contract_digest, approved_scope_revision=permit.scope_revision,
            approved_payload_digest=permit.identity.payload_digest,
            expected_job_ref=permit.job_ref, expected_job_revision=permit.job_revision)
        return AuthorityGrant(identity, permit.issuer_ref, permit.ref, permit.contract_digest,
                              permit.expires_at, context)
