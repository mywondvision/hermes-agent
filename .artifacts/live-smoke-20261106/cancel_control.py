"""One spent cancel ticket for PM, watchdog and explicit cleanup; no runnable queue.

Uses the existing Owner effects table. Connections are per call/thread. A committed
DISPATCH_INTENT is never reissued, including when the process died before the OS call.
Kernel kill-on-close remains a separate OS fail-safe, never an acknowledgement.
"""
from dataclasses import asdict, dataclass
import json
import math
import threading
import time

CAUSES = ("PM_CANCEL", "TIMEOUT", "EMERGENCY_CLEANUP")
RESERVATION = "POSSIBLY_CONSUMED_HELD"


class CancelRejected(RuntimeError):
    pass


class DispatchPermit:
    """In-process one-use capability issued only after durable admission (not an ACL)."""
    def __init__(self, target, authorize):
        self.target = target
        self.effect_key = target.effect_key
        self._authorize = authorize
        self._spent = False
        self._lock = threading.Lock()

    def consume(self):
        with self._lock:
            if self._spent:
                raise CancelRejected("PERMIT_SPENT")
            self._authorize()
            self._spent = True


class NativeCancelGate:
    """Receiver guard: even an exception/lost response spends this handle's send slot."""
    def __init__(self):
        self._lock = threading.Lock()
        self.effect_key = None
        self.dispatch_count = 0

    def dispatch(self, permit, native_call):
        if not isinstance(permit, DispatchPermit):
            raise CancelRejected("DURABLE_PERMIT_REQUIRED")
        with self._lock:
            if self.effect_key is not None:
                if self.effect_key != permit.effect_key:
                    raise CancelRejected("HANDLE_EFFECT_MISMATCH")
                return False
            permit.consume()
            self.effect_key = permit.effect_key
            self.dispatch_count += 1
            native_call()
            return True


@dataclass(frozen=True)
class CancelResult:
    effect_key: str
    cause: str
    state: str
    dispatched: bool = False
    local_stopped: bool = False
    hold: bool = True
    reservation: str = RESERVATION


def terminal_readback(proof, target, now, earliest=0, max_age=60):
    """Require exact target + nonempty root process evidence, not all([])."""
    if not isinstance(proof, dict) or proof.get("target") != asdict(target):
        return False
    at = proof.get("observed_at")
    known = proof.get("known_processes")
    if (type(at) not in (float, int) or not math.isfinite(at) or
            not earliest <= at <= now or now - at > max_age or
            not isinstance(known, list) or not known):
        return False
    roots = [p for p in known if isinstance(p, dict) and str(p.get("pid")) == target.process
             and p.get("start_ticks") == target.start]
    if len(roots) != 1:
        return False
    stopped = all(isinstance(p, dict) and (p.get("absent") is True and p.get("error") == 87 or
                  p.get("identity_matches") is True and p.get("signaled") is True) for p in known)
    return (stopped and proof.get("active") == 0 and proof.get("pids") == []
            and proof.get("tree_stopped") is True)


class CancellationCoordinator:
    def __init__(self, owner_factory, token, receiver, observer, context_factory, *, clock=time.time, hook=None):
        self.owner_factory, self.token = owner_factory, token
        self.receiver, self.observer, self.context_factory = receiver, observer, context_factory
        self.clock, self.hook = clock, hook or (lambda _point: None)

    def _open(self):
        owner = self.owner_factory()
        # Bounded SQLite contention wait, not a native retry. No connection crosses threads.
        owner.db.execute("PRAGMA busy_timeout=1000")
        owner.db.execute("CREATE TABLE IF NOT EXISTS cancel_causes("
                         "effect_key TEXT,request_id TEXT,cause TEXT,detail TEXT,observed_at REAL,"
                         "PRIMARY KEY(effect_key,request_id,cause))")
        owner.db.execute("CREATE TABLE IF NOT EXISTS cancel_readbacks("
                         "effect_key TEXT PRIMARY KEY,observed_at REAL,proof TEXT)")
        return owner

    def _reason(self, owner, target, request, cause):
        with owner.transaction():
            owner._authorize(self.token, self.clock())
            row = owner._job(target.project, target.job, target.revision)
            if row["target"] != json.dumps(asdict(target)):
                raise CancelRejected("TARGET_BINDING")
            owner.db.execute("INSERT OR IGNORE INTO cancel_causes VALUES(?,?,?,?,?)",
                             (target.effect_key, request.request_id, cause, request.reason, self.clock()))

    def _observe(self, owner, target):
        try:
            proof = self.observer(target)
        except Exception as exc:
            proof = {"observer_error": type(exc).__name__}
        at = self.clock()
        with owner.transaction():
            owner._authorize(self.token, at)
            owner.db.execute("INSERT OR REPLACE INTO cancel_readbacks VALUES(?,?,?)",
                             (target.effect_key, at, json.dumps(proof)))
        return proof

    def cancel(self, target, cause, *, request=None, context=None):
        from tests.fixtures.symphony_pm_ingress.owner import Fence
        from tools import symphony_pm_adapter as pm
        if cause not in CAUSES:
            raise CancelRejected("CAUSE")
        if request is None:
            request = pm.CancelIntent("internal-" + cause.lower() + "-" + target.attempt,
                                      target.project, target.job, target.revision, "supervisor:" + cause)
            payload = dict(request.payload(), request_id=request.request_id)
            context = self.context_factory(payload, self.token)
        owner = self._open()
        try:
            actual = owner.target(request, context, self.token, self.clock())
            if actual != target:
                raise CancelRejected("TARGET_BINDING")
            self._reason(owner, target, request, cause)
            self.hook("before_intent")
            spent = owner.snapshot(target)
            proof = self._observe(owner, target)
            earliest = 0 if spent is None else spent["cancel_at"]
            stopped = terminal_readback(proof, target, self.clock(), earliest, context.max_age_seconds)
            if spent is not None or stopped:
                if spent is not None:
                    # Persist a new transport request's binding without claiming its spent ticket.
                    owner.prepare_cancel(request, context, self.token, target, None, self.clock())
                state = "LOCAL_STOPPED_HOLD" if stopped else "UNKNOWN"
                return CancelResult(target.effect_key, cause, state, local_stopped=stopped)
            if self.receiver is None:
                return CancelResult(target.effect_key, cause, "UNKNOWN")
            self.receiver.check_binding(target)
            fence = Fence(target, self.token.epoch, "local-exact-handle-binding", self.clock())
            claimed = owner.prepare_cancel(request, context, self.token, target, fence, self.clock())
            if not claimed:
                # Another connection won after our earlier read. Never dispatch its ticket.
                proof = self._observe(owner, target)
                stopped = terminal_readback(proof, target, self.clock(), 0, context.max_age_seconds)
                return CancelResult(target.effect_key, cause, "LOCAL_STOPPED_HOLD" if stopped else "UNKNOWN",
                                    local_stopped=stopped)
            self.hook("after_intent")
            permit = DispatchPermit(target, lambda: owner._authorize(self.token, self.clock()))
            try:
                dispatched = self.receiver.cancel_once(permit)
            except Exception:
                owner.finish_dispatch(self.token, target, self.clock(), "UNKNOWN")
                return CancelResult(target.effect_key, cause, "UNKNOWN")
            self.hook("after_effect")
            state = "CANCEL_REQUESTED" if dispatched else "UNKNOWN"
            owner.finish_dispatch(self.token, target, self.clock(), state)
            self.hook("after_result")
            return CancelResult(target.effect_key, cause, state, dispatched=dispatched)
        finally:
            owner.close()
