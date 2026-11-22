"""Offline fixture for accepting one guest host key. No network or SSH client."""
import base64
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import secrets
import sqlite3
import time

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_ssh_public_key


def _request_id(challenge):
    return hashlib.sha256(challenge.encode()).hexdigest()


@dataclass(frozen=True)
class Claim:
    challenge: str
    lease: str
    boot: str
    kind: str
    value: str

    def payload(self):
        return json.dumps(["offline-guest-host-key-v2", _request_id(self.challenge), self.lease,
                           self.boot, self.kind, self.value], ensure_ascii=True,
                          separators=(",", ":")).encode("ascii")


@dataclass(frozen=True)
class Witness:
    # Independent fixtures: the provider identifies the running lease, and a
    # separate trusted observation identifies the boot belonging to that lease.
    running_lease: str | None
    boot_lease: str | None
    boot: str | None


def initialize(path):
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE pending (
                request TEXT PRIMARY KEY, lease TEXT NOT NULL, boot TEXT NOT NULL,
                verifier BLOB NOT NULL, expires INTEGER NOT NULL, held INTEGER NOT NULL DEFAULT 0,
                UNIQUE (lease, boot)
            );
            CREATE TABLE binding (
                request TEXT PRIMARY KEY REFERENCES pending(request),
                public_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                payload_hash TEXT NOT NULL, signature BLOB NOT NULL
            );
            CREATE TABLE assertion (
                request TEXT NOT NULL REFERENCES pending(request), fingerprint TEXT NOT NULL,
                payload BLOB NOT NULL, signature BLOB NOT NULL,
                PRIMARY KEY (request, fingerprint)
            );
            CREATE TABLE journal (request TEXT, reason TEXT NOT NULL, payload_hash TEXT);
        """)


def register(path, lease, boot, verifier: Ed25519PublicKey, expires):
    """Verifier comes from the trusted test registrar, never from a delivery."""
    if not lease or not boot:
        raise ValueError("missing enrollment identity")
    challenge = secrets.token_hex(32)
    request = _request_id(challenge)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO pending(request,lease,boot,verifier,expires) VALUES(?,?,?,?,?)",
                   (request, lease, boot, verifier.public_bytes_raw(), expires))
    return challenge


def identify(line):
    """Accept only a canonical single OpenSSH public key and fingerprint its wire blob."""
    try:
        public = load_ssh_public_key(line.encode("ascii"))
        if not isinstance(public, Ed25519PublicKey):
            return None
        if public.public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii") != line:
            return None
        digest = hashlib.sha256(base64.b64decode(line.split()[1], validate=True)).digest()
        return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")
    except (ValueError, TypeError, UnicodeError, UnsupportedAlgorithm):
        return None


def _check_source(pending, claim, signature, witness, clock):
    if pending is None:
        return "unknown_request"
    try:
        Ed25519PublicKey.from_public_bytes(pending["verifier"]).verify(signature, claim.payload())
    except (InvalidSignature, TypeError):
        return "unauthenticated"
    if pending["held"]:
        return "held"
    if clock() >= pending["expires"]:
        return "expired"
    if witness.running_lease is None:
        return "provider_unknown"
    if claim.lease != pending["lease"] or witness.running_lease != pending["lease"]:
        return "lease_mismatch"
    if not witness.boot or not witness.boot_lease:
        return "boot_unknown"
    if (witness.boot_lease, witness.boot) != (pending["lease"], pending["boot"]) or claim.boot != pending["boot"]:
        return "boot_mismatch"
    return None


def _bind(db, request, claim, signature, presented, payload_hash):
    if claim.kind == "public_key":
        signed_fp = identify(claim.value)
    elif claim.kind == "fingerprint" and valid_fingerprint(claim.value):
        signed_fp = claim.value
    else:
        return "invalid_claim"
    if not signed_fp:
        return "invalid_key"
    # Record the signed statement independently of what the connection presents.
    # A different authenticated fingerprint is a conflict even before any binding exists.
    db.execute("""INSERT INTO assertion(request,fingerprint,payload,signature) VALUES(?,?,?,?)
                  ON CONFLICT(request,fingerprint) DO NOTHING""",
               (request, signed_fp, claim.payload(), signature))
    if db.execute("SELECT 1 FROM assertion WHERE request=? AND fingerprint<>?", (request, signed_fp)).fetchone():
        db.execute("UPDATE pending SET held=1 WHERE request=?", (request,))
        return "conflict"
    if identify(presented) != signed_fp:
        return "presented_key_mismatch"
    if db.execute("SELECT 1 FROM binding WHERE request=?", (request,)).fetchone():
        return "already_bound"
    db.execute("INSERT INTO binding VALUES(?,?,?,?,?)",
               (request, presented, signed_fp, payload_hash, signature))
    return "accepted"


def valid_fingerprint(value):
    if not value.startswith("SHA256:") or len(value) != 50:
        return False
    try:
        raw = base64.b64decode(value[7:] + "=", validate=True)
    except ValueError:
        return False
    return base64.b64encode(raw).decode("ascii").rstrip("=") == value[7:]


def deliver(path, claim: Claim, signature: bytes, witness: Witness, presented: str, *, clock=time.time):
    """Serialize the decision, first evidence, conflict hold and receipt in one commit."""
    request = _request_id(claim.challenge)
    payload_hash = hashlib.sha256(claim.payload()).hexdigest()
    with closing(sqlite3.connect(path)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        pending = db.execute("SELECT * FROM pending WHERE request=?", (request,)).fetchone()
        reason = _check_source(pending, claim, signature, witness, clock)
        if reason is None:
            reason = _bind(db, request, claim, signature, presented, payload_hash)
        db.execute("INSERT INTO journal VALUES(?,?,?)", (request, reason, payload_hash))
    return reason
