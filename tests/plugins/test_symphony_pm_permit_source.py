"""Fixture-only human transport tests; no real UI/message, model or native execution."""
from dataclasses import replace

import pytest

from hermes_cli.approval_transport import RegisteredApprovalTransport
from plugins.symphony_pm.transport.approval import ApprovalSource
from plugins.symphony_pm.transport.types import Refused
from tests.plugins.symphony_serving_support import approve, rig


def _source(r, present, home=None):
    transport = RegisteredApprovalTransport('fixture-ui', present, 'fixture-transport', home or r.scope.profile_home)
    return ApprovalSource(r.owner, transport, scheduler_id='serving-test', clock=lambda: r.now[0])


def test_native_permit_requires_correlated_once_decision_and_exact_plan(tmp_path):
    r = rig(tmp_path)
    try:
        request, _, template = approve(r)
        permit = replace(template, ref='fresh-permit')
        seen = []

        def present(approval):
            seen.append(approval)
            assert approval.allowed_choices == ('once', 'deny')
            assert permit.plan.model in approval.command and permit.plan.environment_ref in approval.command
            return approval.respond('once')

        source = _source(r, present)
        assert source.request_permit(request, permit)
        # Two different permits for the same identity are ambiguous, never silently selected.
        assert source(permit.identity) is None
        assert len(seen) == 1 and r.native.created == []
    finally:
        r.owner.close(0)


def test_mismatched_or_denied_approval_never_creates_permit(tmp_path):
    r = rig(tmp_path)
    try:
        request, _, template = approve(r)
        permit = replace(template, ref='denied-permit', identity=replace(template.identity, request_id='other-request'))
        called = []
        source = _source(r, lambda approval: called.append(approval))
        with pytest.raises(Refused, match='APPROVAL_REQUEST_BINDING'):
            source.request_permit(request, permit)
        assert called == []
        valid = replace(template, ref='denied-permit')
        assert not _source(r, lambda approval: approval.respond('deny')).request_permit(request, valid)
        assert not _source(r, lambda approval: replace(approval.respond('once'), request_digest='0'*64)).request_permit(request, valid)
        assert r.native.created == []
    finally:
        r.owner.close(0)


def test_stale_controller_during_human_wait_cannot_install_grant(tmp_path):
    r = rig(tmp_path)
    try:
        request, _, template = approve(r)
        permit = replace(template, ref='stale-permit')

        def present(approval):
            r.owner.rotate_controller('controller-2')
            return approval.respond('once')

        with pytest.raises(Refused, match='WRITER_FENCED'):
            _source(r, present).request_permit(request, permit)
        assert r.native.created == []
    finally:
        r.owner.close(0)


def test_other_profile_transport_is_refused_before_present(tmp_path):
    r = rig(tmp_path)
    try:
        with pytest.raises(Refused, match='PROFILE_APPROVAL_TRANSPORT_REQUIRED'):
            _source(r, lambda _: pytest.fail('foreign transport called'), home=str(tmp_path/'foreign'))
    finally:
        r.owner.close(0)


def test_authority_lookup_reads_exact_grant_without_prompting(tmp_path):
    r = rig(tmp_path)
    try:
        _, _, permit = approve(r)
        source = _source(r, lambda _: pytest.fail('lookup must not prompt'))
        grant = source(permit.identity)
        assert grant is not None and grant.identity == permit.identity
        assert grant.value.scheduler_epoch == r.owner.token.epoch
        assert source(replace(permit.identity, session_id='foreign')) is None
        r.now[0] = permit.expires_at
        assert source(permit.identity) is None
        assert r.native.created == []
    finally:
        r.owner.close(0)
