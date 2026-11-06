"""Read-only evidence checks and fresh OS queries; never instantiate a sender/owner."""
import ctypes
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / ".artifacts/live-smoke-20261106"))
import winjob as w


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


checks = []


def check(name, condition):
    checks.append({"check": name, "status": "PASS" if condition else "FAIL"})


execution_dirs = list((OUT / "sandbox").glob("execution-*"))
check("exactly_one_probe_execution", len(execution_dirs) == 1)
work = execution_dirs[0]
target = read(work / "target.json")
c1 = read(work / "c1-identity.json")
c2 = read(work / "c2-result.json")
result = read(work / "result.json")
expected = read(work / "observer-target.json")
calls = [json.loads(x) for x in (work / "native-calls.jsonl").read_text().splitlines() if x]
native = read(work / "native-return.json")
db_path = work / "owner.sqlite"
db_hash = sha(db_path)
db = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
db.row_factory = sqlite3.Row
tables = {}
for table in ["writers", "operations", "jobs", "effects", "cancel_causes", "cancel_readbacks"]:
    tables[table] = [dict(row) for row in db.execute('SELECT * FROM "' + table + '"')]
db.close()
save("durable-readback.json", {"read_only": True, "database": str(db_path.relative_to(ROOT)),
     "database_sha256": db_hash, "tables": tables})
effect = tables["effects"][0]
proof = json.loads(tables["cancel_readbacks"][0]["proof"])
effect_key = effect["effect_key"]
check("probe_exit_and_parent_assertions", read(OUT / "invocation-result.json")["exit_code"] == 0 and result["mechanical_controller_restart"] == "PASS")
check("separate_controllers_and_new_epoch", c1["pid"] != c2["controller_pid"] and c2["old_controller_pid"] == c1["pid"] and c2["epoch"] == 2)
check("pre_admission_exact_c1_handshake", read(work / "parent-accepted-c1-identity.json") == c1)
check("query_only_handles_nonempty_and_cover_all", read(work / "parent-holds-query-handles.json") == {"query_only": True, "handles": len(expected["known_processes"]) + 1})
check("one_native_entry_and_ack_same_effect", len(calls) == 1 and calls[0]["effect_key"] == effect_key == native["effect_key"] and native["acknowledged"] is True)
check("one_job_one_effect_original_dispatch_epoch", len(tables["jobs"]) == len(tables["effects"]) == 1 and effect["dispatch_epoch"] == 1)
check("spent_intent_remains_unknown_without_terminal_settlement", effect["state"] == "DISPATCH_INTENT" and effect["proof"] is None and effect["proof_epoch"] is None)
check("same_target_job_attempt_pid_start_after_restart", target == c2["target"] == json.loads(effect["target"]) == json.loads(tables["jobs"][0]["target"]) == proof["target"])
check("request_job_revision_effect_links", len(tables["operations"]) == 2 and all(row["job"] == target["job"] and row["revision"] == target["revision"] for row in tables["operations"]) and tables["operations"][0]["request_id"] == "restart-submit" and tables["operations"][1]["effect_key"] == effect_key)
check("new_writer_epoch_two", len(tables["writers"]) == 1 and tables["writers"][0]["owner"] == "C2" and tables["writers"][0]["epoch"] == 2)
check("reconcile_no_sender_or_new_dispatch", c2["receiver"] is None and c2["cancel"]["dispatched"] is False and c2["new_worker_launches"] == 0)
check("hold_and_reservation_retained", c2["cancel"]["state"] == "LOCAL_STOPPED_HOLD" and c2["cancel"]["hold"] is True and c2["cancel"]["reservation"] == "POSSIBLY_CONSUMED_HELD" and result["overall"] == "HOLD")
known = proof["known_processes"]
check("nonempty_exact_root_descendant_os_proof", len(known) >= 2 and len(known) == len(expected["known_processes"]) and any(str(p["pid"]) == target["process"] and p["start_ticks"] == target["start"] for p in known) and all(p["identity_matches"] is True and p["signaled"] is True and p["start_ticks"] == p["actual_start_ticks"] for p in known) and proof["active"] == 0 and proof["pids"] == [] and proof["tree_stopped"] is True)
check("native_effect_and_readback_link", tables["cancel_readbacks"][0]["effect_key"] == effect_key and proof["observed_at"] >= effect["cancel_at"] and tables["cancel_causes"][0]["effect_key"] == effect_key)
observations = [json.loads(x) for x in (OUT / "process-observations.jsonl").read_text().splitlines() if x]
check("independent_monitor_observed_both_controllers_and_workers", {c1["pid"], c2["controller_pid"], *[p["pid"] for p in known]}.issubset({p["pid"] for p in observations}))

# No process killing and no cancel route: fresh query/synchronize handles only.
os_queries = []
for identity in [dict(p, role="worker_job_member") for p in expected["known_processes"]] + [dict(c1, role="C1")]:
    row = {**identity, "observed_at": time.time()}
    ctypes.set_last_error(0)
    handle = w.open_process(0x100000 | 0x1000, False, identity["pid"])
    if not handle:
        row["open_error"] = ctypes.get_last_error()
        row["status"] = "PASS" if row["open_error"] == 87 else "UNKNOWN"
        row["observation"] = "PID absent" if row["status"] == "PASS" else "query unavailable"
    else:
        try:
            row["actual_start_ticks"] = w.creation_ticks(handle)
            row["wait_result"] = w.wait_handle(handle, 0)
            row["status"] = "PASS" if row["actual_start_ticks"] != identity["start_ticks"] or row["wait_result"] == 0 else "FAIL"
            row["observation"] = "PID reused; original identity absent" if row["actual_start_ticks"] != identity["start_ticks"] else "exact identity queried"
        finally:
            w.checked(w.close_handle(handle))
    os_queries.append(row)
save("post-exit-os-readback.json", {"query_only": True, "no_cancel_or_kill": True, "processes": os_queries,
     "limitation": "post-exit queries complement C2 positive Job/process proof; no remote or billing assertion"})
check("post_exit_known_worker_and_c1_identities_terminal_or_absent", all(p["status"] == "PASS" for p in os_queries))

preflight = read(OUT / "preflight.json")
changed = [name for name, digest in preflight["protected_files"].items() if sha(ROOT / name) != digest]
review = read(ROOT / "docs/audits/CR-hermes-symphony-ingress-offline-20261106/cancel-correction-code-review.json")
review_drift = [name for name, digest in review["reviewed_git_blob_sha256"].items() if sha(ROOT / name) != digest]
head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT).decode().strip()
worktrees = subprocess.check_output(["git", "worktree", "list", "--porcelain"], cwd=ROOT).decode()
check("protected_file_bytes_preserved", not changed)
check("reviewed_sources_preserved", not review_drift)
check("expected_branch_and_head_preserved", head == preflight["branch_head"] and branch == preflight["branch"])
check("other_worktree_ref_unchanged", worktrees.split("\n\n")[1] == preflight["worktrees_before"].split("\n\n")[1])
check("readback_did_not_change_database_bytes", sha(db_path) == db_hash)
save("verification.json", {"execution_id": work.name, "checks": checks,
     "pass": sum(c["status"] == "PASS" for c in checks), "fail": sum(c["status"] == "FAIL" for c in checks),
     "head": head, "branch": branch, "protected_files_verified": len(preflight["protected_files"]),
     "changed_protected_files": changed, "review_source_drift": review_drift,
     "post_exit_os_queries": os_queries, "overall": "HOLD",
     "unknown": ["actual Codex worker stop under controller restart", "remote inference and billing settlement",
                 "observer/whole-machine restart", "power-loss and create-process/storage atomicity",
                 "timeout/crash before effect native-host variants", "serving-process owner/observer admission/fencing integration"],
     "limits": ["one post-effect/pre-ack cut only", "parent query-only observer survives C1->C2",
                "C2 opens fresh handles; receiver=None", "no model or new Codex sessions",
                "no attribution separating acknowledged TerminateJobObject from later kernel kill-on-close"]})
print(json.dumps({"checks": len(checks), "pass": sum(c["status"] == "PASS" for c in checks),
     "fail": sum(c["status"] == "FAIL" for c in checks), "overall": "HOLD", "execution": work.name}))
sys.exit(1 if any(c["status"] == "FAIL" for c in checks) else 0)
