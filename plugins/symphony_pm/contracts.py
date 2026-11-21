"""Host-facing types shared by canonical transport imports and profile-loaded plugin generations."""
from dataclasses import dataclass
from typing import Callable

from hermes_cli.plugin_request_binding import AuthorityGrant, RequestIdentity
from plugins.symphony_pm.adapter import SymphonyPmPort


class Refused(RuntimeError):
    """A closed admission/reconciliation boundary, never permission to retry."""


@dataclass(frozen=True)
class ServingBinding:
    port: SymphonyPmPort
    authority_source: Callable[[RequestIdentity], AuthorityGrant | None]
    trusted_issuers: tuple[str, ...]
    scheduler_id: str


@dataclass(frozen=True)
class ServingBootstrap:
    factory: Callable
