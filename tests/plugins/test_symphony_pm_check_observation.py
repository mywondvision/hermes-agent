"""An owner-check observation must identify the capture it actually checked."""
import pytest

from plugins.symphony_pm.transport.types import Refused
from tests.plugins.symphony_serving_support import approve, rig


def test_check_observation_binds_attempt_source_patch_and_capture_sequence(tmp_path):
    r = rig(tmp_path)
    try:
        request, context, _ = approve(r)
        ticket, _ = r.owner.submit(request, context)
        token, journal = r.owner.token, r.owner.journal
        patch = 'f' * 64
        captured = journal.append_observation(token, ticket, {'kind': 'CAPTURE', 'patch': {'sha256': patch}})
        ref = {'attempt': ticket.attempt, 'seq': captured, 'sha256': patch}
        checked = {'attempt': ticket.attempt, 'source_sha': ticket.plan.source_sha, 'patch_sha256': patch}
        seq = journal.next_observation_seq(token, ticket)
        for wrong_ref, wrong_check in (
                ({**ref, 'seq': seq}, checked),
                ({**ref, 'attempt': 'another-attempt'}, checked),
                ({**ref, 'sha256': 'e' * 64}, {**checked, 'patch_sha256': 'e' * 64}),
                (ref, {**checked, 'source_sha': 'e' * 40})):
            with pytest.raises(Refused, match='CHECK_CAPTURE_BINDING'):
                journal.append_observation(token, ticket, {'kind': 'CHECK', 'capture_ref': wrong_ref,
                                                           'check': wrong_check}, expected_seq=seq)
        assert journal.append_observation(token, ticket, {'kind': 'CHECK', 'capture_ref': ref,
                                                          'check': checked}, expected_seq=seq) == seq
    finally:
        r.owner.close(0)
