"""Opt-in Windows CR-8 A3, through the operator adapter in a disposable local clone."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from plugins.symphony_pm.transport.attempt_workspace import Result
from plugins.symphony_pm.transport.codex_exec_check import check_patch
from plugins.symphony_pm.transport.owner_check_profile import CheckCommand, DIFF_CHECK, OwnerCheckProfile
from plugins.symphony_pm.transport.types import digest
from plugins.symphony_pm.transport.windows_ownership import WindowsOwnershipGuard
from tests.plugins.symphony_typed_human_support import human_session
from tests.plugins.test_symphony_pm_codex_exec import _git
from tests.plugins.test_symphony_pm_windows_owner_check import ATTEMPT, PATCH, SOURCE, TEST, _hash, _memory
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("windows")
JOB = "job-8c0dca26e9794d41b9a15ea7534ac1c6"
SOURCE_TREE = "82f781b84aa6cc89a53b0327bcaa0a07e80d45ed"
DATABASE = "15ac2242c9362f99410555838b3f3d8f193fb203cdd011dedf37c12db086e1c0"


def _publish(path, payload):
    temporary = path.with_suffix(".writing")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.rename(path)


class ApplyOnly:
    def __init__(self, settings):
        self.settings = settings

    def workspace_clear(self):
        return not self.settings.owner_check.active.exists()

    def __call__(self, *args):
        raise AssertionError("A3 cannot launch a task")


def _check_capture(owner, spec, run, settings):
    dbpath = owner.journal.path
    with sqlite3.connect(dbpath.as_uri() + "?mode=ro", uri=True) as conn:
        revision, = conn.execute("SELECT revision FROM jobs WHERE job=? AND project=?", (JOB, "hermes-symphony")).fetchone()
        raw, = conn.execute("SELECT body FROM serving_observations WHERE attempt=? AND seq=1", (ATTEMPT,)).fetchone()
    ticket, row = owner.journal.read_launch(owner.token, "hermes-symphony", JOB, revision)
    original = json.loads(raw)
    assert ticket.attempt == ATTEMPT and ticket.plan.source_sha == original["source_sha"] == SOURCE
    assert row["state"] == "STOPPED" and row["stop_proof"] == "OWNER_DEAD" and row["proof_source"] and not row["hold_reason"]
    assert original["kind"] == "CAPTURE" and original["baseline_matches_source"] is True
    assert original["patch"]["sha256"] == PATCH
    changed = tuple(original["patch"]["paths"])
    assert changed == ("plugins/symphony_pm/transport/codex_exec_launcher.py", TEST)
    data = dbpath.parent / "attempts" / digest(ATTEMPT)[:12] / "cr8"
    data.mkdir(parents=True, exist_ok=False)
    saved = data / "saved.patch"
    saved.write_bytes(Path(spec["patch"]).read_bytes())
    assert _hash(saved) == PATCH
    copied = dict(original, patch=dict(original["patch"], path=str(saved)), copied_from_capture_seq=1)
    capture_seq = owner.journal.append_observation(owner.token, ticket, copied)
    _memory()
    check = check_patch(settings, str(settings.owner_check.git), data, Result(saved, PATCH, changed, True), SOURCE, ATTEMPT)
    _publish(run / "sandbox-check.json", check)
    assert check["commands"] == {"ruff": 0, "pytest": 0} and check["reason"] == "CHECK_GIT_TESTS_DEFERRED"
    assert check["deferred"] == [DIFF_CHECK.name] and "15 passed" in (data/"check-pytest.log").read_text("utf-8")
    seq = owner.journal.append_observation(owner.token, ticket, dict(kind="CHECK", check=check,
        capture_ref=dict(attempt=ATTEMPT, seq=capture_seq, sha256=PATCH)))
    return capture_seq, seq


def _native_apply(run, spec):
    root, repo = run / "stand", run / "stand" / "project"
    dbpath = root / "profile" / "plugin-data" / "symphony_pm" / digest("symphony_pm:ingress")[:32] / "journal.sqlite3"
    assert _hash(dbpath) == DATABASE and _hash(Path(spec["patch"])) == PATCH
    assert _git("rev-parse", "HEAD", cwd=repo) == SOURCE
    assert _git("rev-parse", "HEAD^{tree}", cwd=repo) == SOURCE_TREE
    assert _git("for-each-ref", "--format=%(refname)", "refs/heads/symphony", cwd=repo) == ""
    pin = spec["profile"]
    q = OwnerCheckProfile(**{k: Path(pin[k]) for k in ("root", "client", "git", "python", "ruff")},
                          **pin["hashes"])
    assert q.root == run / "mxc" and q.root.resolve() == q.root
    assert not list(q.root.glob("checked-*")) and not q.active.exists()
    assert hashlib.sha256(q.config_bytes()).hexdigest() == q.config_sha256
    pytest_args = ("-I", "-B", "-c", "import os,pytest,sys; sys.path.insert(0,os.getcwd()); "
                   "raise SystemExit(pytest.main(sys.argv[1:]))", "-q", "-o", "addopts=", "--noconftest",
                   "--disable-plugin-autoload", "-p", "no:cacheprovider", TEST)
    def checks(paths):
        return (CheckCommand("ruff", "ruff", ("check", "--no-cache", "--", *paths)),
                CheckCommand("pytest", "python", pytest_args), DIFF_CHECK)
    settings = SimpleNamespace(repository=str(repo), attempts_root=str(root / "attempts"), owner_check=q,
                               checks=checks, check_environment=os.environ, check_timeout=240)
    with human_session(root, restart=True, owner_guard=WindowsOwnershipGuard,
                       project_ref="hermes-symphony", launcher=ApplyOnly(settings)) as channel:
        capture_seq, check_seq = _check_capture(channel.owner, spec, run, settings)
        decision = f"/symphony apply {JOB} {PATCH}"
        _publish(run/"prompt.json", dict(session=spec["session"], command=decision, source=SOURCE, repository=str(repo),
                                        checks=["ruff", "pytest", "git diff --check"], check_seq=check_seq))
        response = run/"owner-input.json"
        wait_until(response.is_file, timeout=600, interval=0.5, what="owner decision in local qualification console")
        reply = json.loads(response.read_bytes())
        assert reply["session"] == spec["session"] and reply["text"] == decision
        result = json.loads(channel.deliver(reply["text"], timeout=600))
        assert result["state"] == "APPLIED" and not result["accepted_failed_git_checks"]
        oid = result["commit"]
        assert result["identity"]["capture_seq"] == capture_seq and result["identity"]["check_seq"] == check_seq
        assert result["identity"]["patch_sha256"] == PATCH
        assert result["git_checks"] == [{"name": DIFF_CHECK.name, "source_sha": SOURCE, "candidate_sha": oid, "exit_code": 0}]
        assert json.loads(channel.deliver(f"/symphony status {JOB} --saved"))["apply"] == result
    export, = q.root.glob("checked-*")
    assert export.resolve() == export and export.parent == q.root and re.fullmatch(r"checked-[0-9a-f]{32}", export.name)
    shutil.rmtree(export)
    assert not export.exists() and not q.active.exists()
    ref = f"refs/heads/symphony/{JOB}"
    assert _git("rev-parse", ref, cwd=repo) == oid and _git("rev-parse", "HEAD", cwd=repo) == SOURCE
    assert _git("show", "-s", "--format=%P|%an|%ae", oid, cwd=repo) == SOURCE + "|Robot|robot@symphony.invalid"
    assert _git("diff", "--check", SOURCE, oid, cwd=repo) == ""
    return dict(state="PASS", ref=ref, source=SOURCE, commit=oid, tree=_git("rev-parse", ref+"^{tree}", cwd=repo),
                patch_sha256=PATCH, capture_seq=capture_seq, check_seq=check_seq, apply=result)


def test_saved_patch_through_owner_command():
    artifacts = os.environ.get("HERMES_E2E_ARTIFACTS")
    permit = Path(artifacts) / "cr8-stand.json" if artifacts else None
    if permit is None or not permit.is_file():
        pytest.skip("requires the one-pass Windows CR-8 stand and the owner's typed decision")
    spec = json.loads(permit.read_bytes())
    run = permit.resolve().parent
    _publish(run/"native-intent.json", dict(session=spec["session"], permit_sha256=_hash(permit), free_bytes=_memory()))
    try:
        result = _native_apply(run, spec)
    except BaseException as exc:
        _publish(run/"native-result.json", dict(state="INCOMPLETE", error=type(exc).__name__))
        raise
    _publish(run/"native-result.json", result)
