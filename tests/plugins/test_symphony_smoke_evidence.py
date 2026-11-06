"""Negative fixtures for the published smoke packet; no model, OS effect or network."""
import hashlib
import json
from pathlib import Path

import pytest

ART = Path(__file__).resolve().parents[2] / ".artifacts/live-smoke-20261106"


@pytest.fixture(scope="module")
def checks():
    import sys
    sys.path.insert(0, str(ART))
    import evidence_checks
    yield evidence_checks
    sys.path.remove(str(ART))


@pytest.fixture(scope="module")
def original(checks):
    reader = checks.GitPacket(ART.parents[1])
    checks.EvidenceVerifier(reader).verify()
    return dict(reader.cache)


class Packet:
    def __init__(self, files, checks):
        self.files = dict(files)
        self.checks = checks

    def read(self, name):
        return self.files[self.checks.relative(name)]

    def obj(self, name):
        return json.loads(self.read(name))

    def put(self, name, value):
        self.files[name] = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()

    def change(self, name, fn):
        value = self.obj(name)
        fn(value)
        self.put(name, value)

    def reseal(self):
        # Tests deliberately trust a NEW synthetic fixture pin, so semantic checks
        # must reject inconsistent identities even when hashes are internally valid.
        run, prefix = self.checks.RUN, self.checks.PREFIX
        artifacts = self.obj(run + "artifact-hashes.json")
        for name in artifacts:
            artifacts[name] = hashlib.sha256(self.read(run + name)).hexdigest()
        self.put(run + "artifact-hashes.json", artifacts)
        pub = self.obj(prefix + "PUBLICATION_MANIFEST.json")
        for name in pub["included"]:
            pub["included"][name] = hashlib.sha256(self.read(name)).hexdigest()
        self.put(prefix + "PUBLICATION_MANIFEST.json", pub)
        return {"publication_hash": hashlib.sha256(self.read(prefix + "PUBLICATION_MANIFEST.json")).hexdigest(),
                "evidence_hash": hashlib.sha256(self.read(run + "artifact-hashes.json")).hexdigest()}


@pytest.fixture
def packet(original, checks):
    return Packet(original, checks)


def test_published_immutable_packet_passes_without_running_any_worker(packet, checks):
    result = checks.EvidenceVerifier(packet).verify()
    assert result["controller_restart"] == "NOT_PROVEN" and result["cancel_acceptance"] == "HOLD"


def test_published_bytes_tampering_rejected_before_semantic_checks(packet, checks):
    packet.files[checks.RUN + "attempt-1/result.json"] = b"{}"
    with pytest.raises(checks.InvalidEvidence, match="HASH:"):
        checks.EvidenceVerifier(packet).verify()


@pytest.mark.parametrize("kind", ["publication", "artifact"])
def test_empty_hash_manifests_never_pass_even_with_trusted_fixture_pin(packet, checks, kind):
    if kind == "publication":
        packet.change(checks.PREFIX + "PUBLICATION_MANIFEST.json", lambda x: x["included"].clear())
    else:
        packet.put(checks.RUN + "artifact-hashes.json", {})
    pins = packet.reseal()
    with pytest.raises(checks.InvalidEvidence, match="EMPTY_HASH_MANIFEST"):
        checks.EvidenceVerifier(packet, **pins).verify()


@pytest.mark.parametrize("kind", ["known", "expected", "handles", "events", "ledger", "running"])
def test_empty_proof_collections_are_not_vacuously_true(packet, checks, kind):
    run = checks.RUN
    if kind == "known":
        packet.change(run + "attempt-2/terminal-observer.json", lambda x: x["proof"].update(known_processes=[]))
    elif kind == "expected":
        packet.change(run + "attempt-2/observer-target.json", lambda x: x.update(known_processes=[]))
    elif kind == "handles":
        packet.change(run + "cancel-verdict.json", lambda x: x.update(retained_handles_signaled={}))
    elif kind == "events":
        packet.files[run + "events.jsonl"] = b""
    elif kind == "ledger":
        packet.change(run + "ledger-readback.json", lambda x: x.update(launches=[]))
    else:
        packet.change(run + "attempt-2/running-observer.json", lambda x: x["proof"].update(active=0, pids=[]))
    with pytest.raises(checks.InvalidEvidence, match="EMPTY_"):
        checks.EvidenceVerifier(packet, **packet.reseal()).verify()


@pytest.mark.parametrize("kind", ["job", "attempt", "native_pid", "root_start", "request", "session",
                                  "effect", "cancel_pid", "cancel_ledger_request", "time", "reservation"])
def test_broken_cross_file_identity_or_lifecycle_rejected_after_resealing(packet, checks, kind):
    run = checks.RUN
    if kind in ("job", "attempt"):
        packet.change(run + "first-verdict.json", lambda x: x.update({kind: "foreign"}))
    elif kind == "native_pid":
        packet.change(run + "attempt-1/binding.json", lambda x: x.update(pid=9999))
    elif kind == "root_start":
        packet.change(run + "attempt-2/observer-target.json", lambda x: x["known_processes"][0].update(start_ticks="foreign"))
    elif kind == "effect":
        def break_effect(x):
            target = json.loads(x["effects"][0]["target"])
            target["attempt"] = "foreign"
            x["effects"][0]["target"] = json.dumps(target)
        packet.change(run + "ledger-readback.json", break_effect)
    elif kind == "cancel_ledger_request":
        def break_request(x):
            for row in x["operations"]:
                if row["request_id"] == "cancel-redelivery-new-id":
                    row["job"] = "foreign"
        packet.change(run + "ledger-readback.json", break_request)
    elif kind == "session":
        events = [json.loads(line) for line in packet.read(run + "attempt-1/stdout.jsonl").splitlines()]
        events[0]["thread_id"] = "foreign-thread"
        packet.files[run + "attempt-1/stdout.jsonl"] = ("\n".join(json.dumps(e) for e in events) + "\n").encode()
    elif kind == "reservation":
        packet.change(run + "cancel-verdict.json", lambda x: x["replay"]["view"].update(reservation="RELEASED"))
    else:
        events = [json.loads(line) for line in packet.read(run + "events.jsonl").splitlines()]
        if kind == "request":
            response = next(e for e in events if e["event"] == "PM_RESPONSE")
            response["payload"]["request_id"] = "foreign-request"
        else:
            native = next(e for e in events if e["event"] == "NATIVE_CANCEL_DISPATCH")
            native["pid" if kind == "cancel_pid" else "at"] = "9999" if kind == "cancel_pid" else 0
        packet.files[run + "events.jsonl"] = ("\n".join(json.dumps(e) for e in events) + "\n").encode()
    with pytest.raises(checks.InvalidEvidence):
        checks.EvidenceVerifier(packet, **packet.reseal()).verify()


@pytest.mark.parametrize("path", ["../outside", "a/../../outside", "C:/outside", "/outside", "a\\..\\outside"])
def test_artifact_path_cannot_escape_packet(checks, path):
    with pytest.raises(checks.InvalidEvidence):
        checks.relative(path)
