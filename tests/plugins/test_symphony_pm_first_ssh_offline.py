"""Offline first-SSH trust experiment. All identities and independent observations are fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import base64
import hashlib
import multiprocessing
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.plugins.symphony_qualification import first_ssh_offline as stand


LEASE = "fixture-lease"
BOOT = "fixture-boot"
NOW = 1000
WITNESS = stand.Witness(LEASE, LEASE, BOOT)


def public_line(private):
    return private.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()


def rows(path, table):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()


@pytest.fixture
def enrollment(tmp_path, record_property):
    with TemporaryDirectory(dir=tmp_path) as local:
        path = Path(local) / "receiver.sqlite"
        signer = Ed25519PrivateKey.generate()
        guest = Ed25519PrivateKey.generate()
        stand.initialize(path)
        challenge = stand.register(path, LEASE, BOOT, signer.public_key(), NOW + 300)
        claim = stand.Claim(challenge, LEASE, BOOT, "public_key", public_line(guest))
        yield path, signer, claim
        material = path.read_bytes()
        for forbidden in (signer.private_bytes_raw(), guest.private_bytes_raw(), challenge.encode()):
            assert forbidden not in material
        journal = [r[1] for r in rows(path, "journal")]
        count = len(rows(path, "binding"))
        assert count == journal.count("accepted") <= 1
        record_property("offline_observations", {"bindings": count, "decisions": journal})
    assert not path.exists()


def deliver(path, signer, claim, *, presented=None, witness=WITNESS, now=NOW):
    key = claim.value if presented is None else presented
    return stand.deliver(path, claim, signer.sign(claim.payload()), witness, key, clock=lambda: now)


@pytest.mark.parametrize("mode", ["public_key", "fingerprint"])
def test_authenticated_key_or_fingerprint(enrollment, mode):
    path, signer, original = enrollment
    key = original.value
    blob = base64.b64decode(key.split()[1])
    expected_fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    claim = original if mode == "public_key" else replace(original, kind=mode, value=expected_fp)
    assert deliver(path, signer, claim, presented=key) == "accepted"
    binding, = rows(path, "binding")
    assert binding[1:4] == (key, expected_fp, hashlib.sha256(claim.payload()).hexdigest())
    signer.public_key().verify(binding[4], claim.payload())
    assert deliver(path, signer, claim, presented=key) == "already_bound"
    assert rows(path, "binding") == [binding]


@pytest.mark.parametrize("bound", [False, True], ids=["empty", "existing"])
@pytest.mark.parametrize("case,reason", [
    ("other_claim_lease", "lease_mismatch"), ("other_provider_lease", "lease_mismatch"),
    ("old_boot", "boot_mismatch"), ("old_claim_boot", "boot_mismatch"),
    ("unrelated_boot_witness", "boot_mismatch"), ("unobserved_boot", "boot_unknown"),
    ("unobserved_lease", "provider_unknown"), ("expired", "expired"),
    ("unregistered_request", "unknown_request"), ("wrong_signer", "unauthenticated"),
    ("altered_message", "unauthenticated"), ("wrong_full_key", "presented_key_mismatch"),
    ("changed_public_key", "presented_key_mismatch"), ("garbage_key", "invalid_key"),
    ("malformed_fingerprint", "invalid_claim"),
])
def test_refusal_preserves_prior_binding(enrollment, case, reason, bound):
    path, signer, original = enrollment
    if bound:
        assert deliver(path, signer, original) == "accepted"
    first = rows(path, "binding")
    other = Ed25519PrivateKey.generate()
    presented = public_line(other) if case in ("wrong_full_key", "changed_public_key") else original.value
    changes = {"other_claim_lease": {"lease": "other"}, "old_claim_boot": {"boot": "old"},
               "unregistered_request": {"challenge": "other"}, "altered_message": {"value": public_line(other)},
               "garbage_key": {"value": "ssh-ed25519 garbage"},
               "wrong_full_key": {"kind": "fingerprint", "value": stand.identify(original.value)},
               "malformed_fingerprint": {"kind": "fingerprint", "value": "SHA256:!"}}
    claim = replace(original, **changes.get(case, {}))
    observers = {"other_provider_lease": {"running_lease": "other"}, "old_boot": {"boot": "old"},
                 "unrelated_boot_witness": {"boot_lease": "other"}, "unobserved_boot": {"boot": None},
                 "unobserved_lease": {"running_lease": None}}
    witness = replace(WITNESS, **observers.get(case, {}))
    signature = (other if case == "wrong_signer" else signer).sign(
        original.payload() if case == "altered_message" else claim.payload())
    assert stand.deliver(path, claim, signature, witness, presented,
                         clock=lambda: NOW + (300 if case == "expired" else 0)) == reason
    assert rows(path, "journal")[-1][1] == reason
    assert rows(path, "binding") == first


@pytest.mark.parametrize("conflicting", [False, True])
def test_simultaneous_deliveries(enrollment, conflicting):
    path, signer, claim = enrollment
    second = replace(claim, value=public_line(Ed25519PrivateKey.generate())) if conflicting else claim
    barrier = Barrier(2)

    def submit(message):
        barrier.wait(timeout=10)
        return deliver(path, signer, message)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, [claim, second]))
    assert sorted(outcomes) == ["accepted", "conflict" if conflicting else "already_bound"]
    first, = rows(path, "binding")
    winning_claim = [claim, second][outcomes.index("accepted")]
    assert first[1] == winning_claim.value
    assert first[3] == hashlib.sha256(winning_claim.payload()).hexdigest()
    signer.public_key().verify(first[4], winning_claim.payload())
    assert deliver(path, signer, winning_claim) == ("held" if conflicting else "already_bound")
    assert rows(path, "binding") == [first]


def crash_delivery(path, claim, signature, before_commit):
    if before_commit:
        connect = sqlite3.connect

        def armed(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.create_function("power_loss", 0, lambda: os._exit(43))
            return db

        sqlite3.connect = armed
    stand.deliver(path, claim, signature, WITNESS, claim.value, clock=lambda: NOW)
    os._exit(44)


@pytest.mark.parametrize("before_commit", [True, False])
def test_recovery_after_process_exit(enrollment, before_commit):
    path, signer, claim = enrollment
    if before_commit:
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TRIGGER crash BEFORE INSERT ON journal BEGIN SELECT power_loss(); END")
    child = multiprocessing.get_context("spawn").Process(
        target=crash_delivery, args=(path, claim, signer.sign(claim.payload()), before_commit))
    child.start()
    child.join(30)
    try:
        assert child.exitcode == (43 if before_commit else 44)
    finally:
        if child.is_alive():
            child.kill()
            child.join(10)
        child.close()
    assert all(len(rows(path, table)) == int(not before_commit) for table in ("assertion", "binding", "journal"))
    if before_commit:
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("DROP TRIGGER crash")
    assert deliver(path, signer, claim) == ("accepted" if before_commit else "already_bound")


def test_one_registration_per_lease_boot(enrollment):
    path, signer, claim = enrollment
    with pytest.raises(sqlite3.IntegrityError):
        stand.register(path, LEASE, BOOT, Ed25519PrivateKey.generate().public_key(), NOW + 999)
    assert len(rows(path, "pending")) == 1
    assert deliver(path, signer, claim) == "accepted"


@pytest.mark.parametrize("contradiction", [True, False])
def test_first_signed_fingerprint_survives_mismatched_presentation(enrollment, contradiction):
    path, signer, original = enrollment
    first = replace(original, kind="fingerprint", value=stand.identify(original.value))
    other = replace(original, value=public_line(Ed25519PrivateKey.generate()))
    assert deliver(path, signer, first, presented=other.value) == "presented_key_mismatch"
    assert not rows(path, "binding")
    assert deliver(path, signer, first, presented=other.value) == "presented_key_mismatch"
    following = other if contradiction else original
    assert deliver(path, signer, following) == ("conflict" if contradiction else "accepted")
    if contradiction:
        assert deliver(path, signer, original) == "held"
        assert deliver(path, signer, other) == "held"
        assert not rows(path, "binding")
    else:
        assert rows(path, "binding")[0][1] == original.value
    evidence = rows(path, "assertion")
    expected = [first, other] if contradiction else [first]
    assert len(evidence) == len(expected)
    for (_, fingerprint, payload, signature), message in zip(evidence, expected):
        assert fingerprint == (message.value if message.kind == "fingerprint" else stand.identify(message.value))
        assert payload == message.payload()
        signer.public_key().verify(signature, payload)


def test_unverified_message_cannot_seed_conflict(enrollment):
    path, signer, original = enrollment
    other = Ed25519PrivateKey.generate()
    claim = replace(original, value=public_line(other))
    assert deliver(path, other, claim) == "unauthenticated"
    assert deliver(path, signer, claim, witness=replace(WITNESS, boot=None)) == "boot_unknown"
    assert not rows(path, "assertion")
    assert deliver(path, signer, original) == "accepted"
