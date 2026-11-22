"""Test-stand wiring only. No plugin discovery, PM field or user-config switch."""
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path

from plugins.symphony_pm.contracts import ServingBootstrap
from plugins.symphony_pm.transport.linux_bootstrap import LinuxTransportBootstrap
from plugins.symphony_pm.transport.linux_launcher import LinuxLauncher
from plugins.symphony_pm.transport.linux_sandbox import LinuxQualification, file_digest, in_image
from plugins.symphony_pm.transport.types import Refused, sha


PROFILE = "symphony-qualify-local-v1"
CASES = ("write", "descendants", "wait")
PROGRAM = Path(__file__).with_name("stand_probe.py").read_bytes().decode("utf-8")
PROGRAM_SHA = "f2c58aeef43592785c3410210881e468137060e793edf4f1bcee8fa6d4b2b52f"
CODEX_SHA = "167c0148a849d2444f1b5a7fb5f8bb2de1de5ae13a2a504b833fc765980f5cd9"
POLICY_SHA = "b5ca627735987f47dafc24525f71114fdd04a9e62f234881d1176dad4965392d"
FIXTURE = "/usr/libexec/hermes-symphony/qualify.py"


@dataclass(frozen=True)
class StandQualification(LinuxQualification):
    fixture_sha256: str

    def valid_command_environment(self):
        return self.command_environment == ("CODEX_SANDBOX_NETWORK_DISABLED",)

    def validate(self):
        super().validate()
        if (self.permission_profile != PROFILE or self.environment_ref != PROFILE
                or self.namespace != "symphony_pm:ingress-linux"
                or self.executable != "/usr/bin/codex" or self.probe_python != "/usr/bin/python3"
                or not sha(self.fixture_sha256, 64) or len(self.hashes) != 7
                or self.hashes[4] != CODEX_SHA or self.hashes[6] != POLICY_SHA
                or hashlib.sha256(PROGRAM.encode()).hexdigest() != PROGRAM_SHA):
            raise Refused("LINUX_QUALIFICATION_STAND")

    def verify(self):
        super().verify()
        if file_digest(in_image(self.runtime, FIXTURE)) != self.fixture_sha256:
            raise Refused("LINUX_QUALIFICATION_FIXTURE")


@dataclass(frozen=True)
class StandCase:
    manifest: bytes
    manifest_sha256: str
    case: str

    def verify(self, qualification):
        if hashlib.sha256(self.manifest).hexdigest() != self.manifest_sha256:
            raise Refused("LINUX_QUALIFICATION_MANIFEST")
        expected = {
            "format": "symphony-qualification-worker/1",
            "cases": list(CASES),
            "fixture_sha256": qualification.fixture_sha256,
            "policy_sha256": POLICY_SHA,
            "image_sha256": qualification.image_sha256,
            "codex_sha256": CODEX_SHA,
            "python_sha256": qualification.hashes[5],
            "probe_sha256": PROGRAM_SHA,
        }
        if json.loads(self.manifest) != expected or self.case not in CASES:
            raise Refused("LINUX_QUALIFICATION_MANIFEST")


class StandLauncher(LinuxLauncher):
    def __init__(self, qualification, settings, case, owner_check):
        if type(qualification) is not StandQualification or type(case) is not StandCase:
            raise Refused("LINUX_QUALIFICATION_STAND")
        super().__init__(qualification, settings, owner_check=owner_check)
        case.verify(qualification)
        self.case = case

    def admission(self):
        self.case.verify(self.qualification)
        super().admission()

    def _call(self, ticket, request, workspace, targets):
        if os.path.lexists(workspace.src / ".codex"):
            raise Refused("LINUX_QUALIFICATION_PROJECT_CONFIG")
        call = super()._call(ticket, request, workspace, targets)
        q = self.qualification
        prefix = (q.executable, "sandbox", "-P", PROFILE, "-C", "/work", "--", q.probe_python, "-I", "-B")
        probe_argv = (*prefix, "-c", PROGRAM, call.challenge, json.dumps(targets))
        return replace(call, argv=(*prefix, FIXTURE, self.case.case), probe_argv=probe_argv, prompt="")


def stand_binding(qualification, settings, case, *, owner_check, approval_transport, controller):
    launcher = StandLauncher(qualification, settings, case, owner_check)
    return ServingBootstrap(LinuxTransportBootstrap(launcher, approval_transport, controller))
