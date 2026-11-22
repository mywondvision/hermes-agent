"""Isolated Desktop's two purpose-limited forwarding operations."""
from typing import Literal

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
    mode: Literal['shared'] | None = None


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


method('prompt.forward_probe', params=ForwardProbeParams, result=ForwardProbeResult,
       doc='Classify the attached conversation without sending input.')
method('prompt.forward_submit', params=ForwardSubmitParams, result=ForwardSubmitResult,
       doc='Forward one typed input to its verified durable owner.')
