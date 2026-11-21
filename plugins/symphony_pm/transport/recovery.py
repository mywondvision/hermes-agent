"""Owner-side evidence for a predecessor transition. A process death alone is not a stop proof."""
from dataclasses import dataclass
import json

from .types import digest, sha

WINDOWS_JOB_CONTRACT = 'windows-job-kill-on-close'
PROVEN_STOP = ('OWNER_DEAD', 'BOOT_CHANGED', 'JOB_EMPTY')
_CLAIMED = {'CREATE_INTENT', 'BOUND', 'RESUME_INTENT'}


@dataclass(frozen=True)
class PredecessorProof:
    owner_digest: str
    host_id: str
    kind: str
    source: str
    contract: str


@dataclass(frozen=True)
class LegacyLaunchOwnership:
    """An owning host may independently identify one pre-v2 launch using archived evidence."""
    ticket_digest: str
    owner_digest: str
    contract: str
    host_id: str
    source: str


def recovered_state(row, previous, current_boot, proof, legacy):
    """Keep uncertain launches held across repeated claims; a new epoch supplies no new evidence."""
    unknown = ('HELD_UNKNOWN', None, None, 'PREDECESSOR_STOP_UNPROVEN')
    if type(proof) is not PredecessorProof or previous is None or not proof.source or not proof.host_id:
        return unknown
    owner = digest(previous)
    if owner != proof.owner_digest or proof.kind not in ('OWNER_DEAD', 'BOOT_CHANGED'):
        return unknown
    contract, host_id = row['ownership_contract'], row['host_id']
    source = proof.source
    if row['owner_identity'] is None:
        ticket_digest = digest(json.loads(row['ticket']))
        matches = [item for item in legacy if type(item) is LegacyLaunchOwnership and
                   (item.ticket_digest, item.owner_digest) == (ticket_digest, owner) and sha(item.source, 64)]
        if len(matches) != 1:
            return unknown
        contract, host_id = matches[0].contract, matches[0].host_id
        source = source + ':' + matches[0].source
    if not host_id or host_id != proof.host_id:
        return unknown
    if (proof.kind == 'BOOT_CHANGED' and row['boot_id'] and row['boot_id'] != current_boot
            and contract and contract == proof.contract):
        return ('STOPPED', 'BOOT_CHANGED', source, None)
    if row['owner_identity'] is not None and row['owner_identity'] != owner:
        return unknown
    if row['state'] == 'LAUNCH_INTENT' and row['binding'] is None and proof.kind == 'OWNER_DEAD':
        return ('NOT_STARTED', None, source, None)
    if row['state'] not in _CLAIMED | {'LAUNCH_INTENT'}:
        return unknown
    if not contract or contract != proof.contract:
        return unknown
    return ('STOPPED', proof.kind, source, None)
