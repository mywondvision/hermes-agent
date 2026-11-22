"""Isolated Desktop's two purpose-limited forwarding operations."""
from typing import Literal

from pydantic import StrictInt

from .base import Params, Result
from .canonical import CanonicalSessionRef
from .registry import method


class ForwardProbeParams(Params):
    session_id: str


class ForwardProbeResult(Result):
    version: Literal[1]
    route: Literal['ordinary', 'forward', 'hold']
    ref: CanonicalSessionRef | None = None
    generation: str | None = None
    mode: Literal['shared', 'personal'] | None = None


class ForwardSubmitParams(ForwardProbeParams):
    ref: CanonicalSessionRef
    generation: str
    submission_id: str
    text: str


class ForwardSubmitResult(Result):
    ref: CanonicalSessionRef
    submission_id: str
    admission_id: str
    status: Literal['queued', 'started', 'unknown', 'terminal']


class OperatorChallengeParams(ForwardProbeParams):
    action: Literal['submit', 'receipt', 'status', 'stop', 'stop_result', 'recover', 'stop_reconcile', 'stop_replace',
                    'apply_info', 'apply', 'archive', 'apply_result']
    submission_id: str
    installation_key: str
    window_key: str
    window_generation: int
    payload_sha256: str


class OperatorChallengeResult(Result):
    version: Literal[1]
    challenge_id: str
    message: str
    ref: CanonicalSessionRef
    window_generation: int


class OperatorExecuteParams(ForwardProbeParams):
    challenge_id: str
    installation_signature: str
    window_signature: str
    payload: dict[str, str | StrictInt]


class OperatorExecuteResult(Result):
    ref: CanonicalSessionRef | None = None
    submission_id: str | None = None
    admission_id: str | None = None
    status: Literal['queued', 'started', 'unknown', 'terminal'] | None = None
    attempt: dict[str, str] | None = None
    window_generation: int | None = None
    attempt_id: str | None = None
    phase: str | None = None
    observed_at: float | None = None
    evidence: str | None = None
    command_id: str | None = None
    previous_command_id: str | None = None
    state: str | None = None
    dispatched: bool | None = None
    local_stopped: bool | None = None
    project: str | None = None
    job: str | None = None
    capture_seq: int | None = None
    check_seq: int | None = None
    patch_sha256: str | None = None
    commit: str | None = None


method('prompt.forward_probe', params=ForwardProbeParams, result=ForwardProbeResult,
       doc='Classify the attached conversation without sending input.')
method('prompt.forward_submit', params=ForwardSubmitParams, result=ForwardSubmitResult,
       doc='Forward one typed input to its verified durable owner.')
method('operator.challenge', params=OperatorChallengeParams, result=OperatorChallengeResult,
       doc='Issue a purpose- and connection-bound challenge for a registered Desktop operator.')
method('operator.execute', params=OperatorExecuteParams, result=OperatorExecuteResult,
       doc='Accept a once-signed Desktop operator request through the verified owner.')
