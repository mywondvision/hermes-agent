"""Verify exact reviewed source and prepare non-overwriting branch attachment."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
BRANCH = "codex/hermes-symphony-cancel-correction-20261106"
HEAD = "c419cf7c864ac0ee511dbd9f7091a8bba0b567ee"
TARGET = "fda3bda4bffece8bc3cca7e11be8dd8e72eef91d"
CODE = "b766aecb8524a5c0b78197cda7a144473af4fd07"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8").strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if (OUT / "preflight.json").exists():
    raise RuntimeError("Preflight already recorded; inspect before repeating")
assert git("rev-parse", "HEAD") == HEAD
assert git("rev-parse", BRANCH) == TARGET
subprocess.run(["git", "diff", "--quiet"], cwd=ROOT, check=True)
subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT, check=True)
changed = git("diff", "--name-only", HEAD, TARGET).splitlines()
matched = []
for name in changed:
    path = ROOT / name
    assert path.is_file(), name
    # Git filters account for text EOL; no object or file is written.
    expected = git("rev-parse", TARGET + ":" + name)
    actual = git("hash-object", "--path=" + name, name)
    assert actual == expected, "BRANCH_OVERLAY_CONFLICT:" + name
    matched.append({"path": name, "git_blob": expected, "raw_sha256": sha(path)})
review = json.loads((ROOT / "docs/audits/CR-hermes-symphony-ingress-offline-20261106/cancel-correction-code-review.json").read_text(encoding="utf-8"))
assert review["candidate_commit"] == CODE and review["verdict"] == "PASS"
for name, expected in review["reviewed_git_blob_sha256"].items():
    blob = subprocess.check_output(["git", "show", CODE + ":" + name], cwd=ROOT)
    assert hashlib.sha256(blob).hexdigest() == expected, name
    assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, "REVIEW_SOURCE_DRIFT:" + name
extra = ["tests/tools/tools.zip", "tests/tools/symphony_pm_adapter.py", ".artifacts/live-smoke-20261106/owner.sqlite",
         ".artifacts/live-smoke-20261106/approved-retry-1/artifact-hashes.json"]
frozen_root = ROOT / ".artifacts/frozen-s2"
frozen = json.loads((frozen_root / "snapshot-manifest.json").read_text(encoding="utf-8"))
for row in frozen["files"]:
    assert sha(frozen_root / row["path"]) == row["sha256"]
    extra.append(".artifacts/frozen-s2/" + row["path"])
receipt = {"initial_head": HEAD, "branch": BRANCH, "branch_head": TARGET, "reviewed_code": CODE,
           "tracked_and_staged_clean": True, "overlay_paths_verified": matched,
           "protected_extra_files": {name: sha(ROOT / name) for name in extra},
           "worktrees_before": git("worktree", "list", "--porcelain"),
           "status_before": git("status", "--short", "--untracked-files=normal"),
           "source_changes_authorized": False, "model_or_codex_worker_authorized": False,
           "authorization": "current user: execute the prepared no-model C1/C2 probe only"}
(OUT / "preflight.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"overlay_files_verified": len(matched), "reviewed_blob_hashes_verified": len(review["reviewed_git_blob_sha256"]),
                  "protected_extra_files": len(extra), "branch_attachment_safe_without_checkout": True}, indent=2))
