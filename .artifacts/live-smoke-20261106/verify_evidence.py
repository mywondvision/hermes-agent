"""Read-only, stdlib-only verification of published smoke evidence. No worker launch."""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN = HERE / "approved-retry-1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verify():
    manifest = read(RUN / "artifact-hashes.json")
    for name, expected in manifest.items():
        path = RUN.joinpath(*name.replace("\\", "/").split("/"))
        assert path.resolve().is_relative_to(RUN.resolve()), name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
    first = read(RUN / "first-verdict.json")
    cancel = read(RUN / "cancel-verdict.json")
    source = read(RUN / "workspace-1/input.json")
    result = read(RUN / "attempt-1/result.json")
    assert result == {"marker": source["marker"], "value": source["value"], "sum": sum(source["numbers"])}
    assert first["status"] == "PASS" and first["result"] == result
    events = [json.loads(line) for line in (RUN / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    starts = [e for e in events if e["event"] == "NATIVE_STARTED"]
    cancel_calls = [e for e in events if e["event"] == "NATIVE_CANCEL_DISPATCH"]
    assert len(starts) == 2 and len(cancel_calls) == 1
    assert {e["attempt"] for e in starts} == {first["attempt"], cancel["attempt"]}
    assert cancel_calls[0]["attempt"] == cancel["attempt"]
    assert next(e["at"] for e in events if e["event"] == "FIRST_VERIFIED") < starts[1]["at"]
    assert cancel["lost_reply"]["outcome"] == "UNKNOWN"
    assert cancel["lost_reply"]["issues"] == ["RESPONSE_LOST"]
    assert cancel["replay"]["view"]["scheduler_outcome"] == "ALREADY_REQUESTED"
    assert cancel["new_id_replay"]["view"]["scheduler_outcome"] == "ALREADY_REQUESTED"
    assert cancel["native_cancel_calls"] == 1 and cancel["native_launches_total"] == 3
    running = read(RUN / "attempt-2/running-observer.json")["proof"]
    terminal = read(RUN / "attempt-2/terminal-observer.json")["proof"]
    assert running["active"] > 0 and not running["tree_stopped"]
    assert terminal["active"] == 0 and terminal["pids"] == [] and terminal["tree_stopped"]
    assert terminal["observed_at"] > cancel_calls[0]["at"] > running["observed_at"]
    assert all(cancel["retained_handles_signaled"].values())
    assert all(p.get("absent") or (p["identity_matches"] and p["signaled"]) for p in terminal["known_processes"])
    ledger = read(RUN / "ledger-readback.json")
    assert len(ledger["launches"]) == 3 and len(ledger["effects"]) == 1
    assert {r["state"] for r in ledger["launches"]} == {
        "FAILED_CONFIG_HOLD", "TERMINAL_SUCCESS", "TERMINAL_LOCAL_CANCEL_HOLD"}
    assert ledger["effects"][0]["state"] == "CANCEL_REQUESTED"
    assert cancel["status"] == "HOLD"
    assert cancel["external_effects_settled"] == cancel["billing_settled"] == "UNKNOWN"
    for name, expected in (("offline.txt", "90 tests passed, 0 failed"),
                           ("regressions.txt", "54 tests passed, 0 failed")):
        raw = (HERE / name).read_bytes()
        text = raw.decode("utf-16" if raw.startswith(b"\xff\xfe") else "utf-8-sig")
        assert expected in text, name
    print(json.dumps({"recorded_evidence_consistent": True, "hashed_files": len(manifest),
                      "offline_recorded_pass": 144, "new_native_starts": len(starts),
                      "overall_native_attempts": 3, "native_cancel_calls": 1,
                      "local_tree_stop_and_dedupe": "PASS", "cancel_acceptance": "HOLD",
                      "billing_settled": "UNKNOWN", "external_effects_settled": "UNKNOWN",
                      "scope": "checks recorded artifacts; does not rerun or independently reproduce live events"}, indent=2))


if __name__ == "__main__":
    verify()
